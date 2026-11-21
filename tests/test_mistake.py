"""Rollback of a mistaken record: dependency rules, and GeoPackage rules stay on."""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "parcel_fabric"))

import pytest

from core import mistake as M
from core import safeguards as G


def test_split_undo_deletes_children_and_restores_parent():
    features = [
        ("parcels", 1, "P", "old", "R", None),
        ("parcels", 2, "C1", "R", None, None),
        ("parcels", 3, "C2", "R", None, None),
        ("lines", 4, "Lold", "old", "R", None),
        ("lines", 5, "L1", "R", None, "Lold"),
        ("lines", 6, "L2", "R", None, "Lold"),
        ("points", 7, "PT", "R", None, None),
    ]
    lineage = [(10, "R", "P", "C1"), (11, "R", "P", "C2")]
    planned = M.plan("R", features, lineage, set())
    assert planned["delete"]["parcels"] == [2, 3]
    assert planned["restore"]["parcels"] == [1]
    assert planned["delete"]["lines"] == [5, 6]
    assert planned["restore"]["lines"] == [4]
    assert planned["delete"]["points"] == [7]
    assert planned["lineage"] == [10, 11]


def test_refuses_when_later_work_used_the_record():
    with pytest.raises(M.MistakeError):
        M.plan("R", [("parcels", 1, "P", "R", "other", None)], [], set())
    with pytest.raises(M.MistakeError):
        M.plan("R", [("parcels", 1, "P", "R", None, None), ("parcels", 2, "C", "other", None, None)],
               [(1, "other", "P", "C")], set())
    with pytest.raises(M.MistakeError):
        M.plan("R", [("lines", 1, "L", "R", None, None), ("lines", 2, "L2", "other", None, "L")], [], set())
    with pytest.raises(M.MistakeError):
        M.plan("R", [("lines", 1, "L", "R", None, None)], [], {"L"})


def test_same_record_retire_is_deleted_not_restored():
    planned = M.plan("R", [("parcels", 1, "P", "R", "R", None)], [], set())
    assert planned["delete"]["parcels"] == [1]
    assert planned["restore"]["parcels"] == []


def test_gpkg_commit_bypasses_rules_and_puts_them_back(tmp_path):
    path = tmp_path / "f.gpkg"
    setup = sqlite3.connect(path)
    setup.execute('CREATE TABLE "Parcels" (fid INTEGER PRIMARY KEY, "RetiredByRecord" TEXT)')
    setup.execute("INSERT INTO \"Parcels\" VALUES (1, NULL), (2, '{A}')")
    for statement in G.statements("ogr", "", ["Parcels"]):
        setup.execute(statement)
    setup.commit()
    setup.close()

    with pytest.raises(sqlite3.Error):
        M.gpkg_commit(str(path), ["Parcels"], ['DELETE FROM "Missing"'])
    check = sqlite3.connect(path)
    assert list(check.execute('SELECT fid FROM "Parcels" ORDER BY fid')) == [(1,), (2,)]
    with pytest.raises(sqlite3.DatabaseError):
        check.execute('DELETE FROM "Parcels" WHERE fid = 1')
    check.close()

    M.gpkg_commit(str(path), ["Parcels"], [
        'DELETE FROM "Parcels" WHERE "fid" = 1',
        'UPDATE "Parcels" SET "RetiredByRecord" = NULL WHERE "fid" = 2',
    ])
    check = sqlite3.connect(path)
    assert list(check.execute('SELECT fid, "RetiredByRecord" FROM "Parcels"')) == [(2, None)]
    with pytest.raises(sqlite3.DatabaseError):
        check.execute('DELETE FROM "Parcels" WHERE fid = 2')


def test_server_undo_is_one_batch():
    delete = 'DELETE FROM "Parcels" WHERE "fid" IN (1)'
    pg = M.server_batch("postgres", "gis", ["Parcels"], [delete], True)
    assert len(pg) == 1 and pg[0].startswith("DO $qpf$") and "DELETE FROM" in pg[0] and "CREATE TRIGGER" in pg[0]
    ms = M.server_batch("mssql", "dbo", ["Parcels"], ["DELETE FROM [dbo].[Parcels] WHERE [fid] IN (1)"], True)
    assert len(ms) == 1 and ms[0].startswith("BEGIN TRANSACTION")
    assert "DISABLE TRIGGER" in ms[0] and "ENABLE TRIGGER" in ms[0] and "COMMIT" in ms[0]
    assert M.server_batch("postgres", "gis", ["Parcels"], [delete], False) == [delete]
