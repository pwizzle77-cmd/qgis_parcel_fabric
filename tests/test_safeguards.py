import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "parcel_fabric"))

import pytest

from core import safeguards as G


def test_sqlite_triggers(tmp_path):
    db = sqlite3.connect(tmp_path / "f.gpkg")
    db.execute('CREATE TABLE "Parcels" (id INTEGER PRIMARY KEY, "RetiredByRecord" TEXT)')
    db.execute('INSERT INTO "Parcels" VALUES (1, NULL), (2, NULL)')
    for s in G.statements("ogr", "", ["Parcels"]):
        db.execute(s)
    db.execute('UPDATE "Parcels" SET "RetiredByRecord" = \'{A}\' WHERE id = 1')     # first retire is fine
    with pytest.raises(sqlite3.DatabaseError):
        db.execute('UPDATE "Parcels" SET "RetiredByRecord" = \'{B}\' WHERE id = 1')
    with pytest.raises(sqlite3.DatabaseError):
        db.execute('UPDATE "Parcels" SET "RetiredByRecord" = NULL WHERE id = 1')
    with pytest.raises(sqlite3.DatabaseError):
        db.execute('DELETE FROM "Parcels" WHERE id = 2')


def test_server_sql_mentions_every_table():
    for prov in ("postgres", "mssql"):
        sql = "\n".join(G.statements(prov, "gis", ["Parcels", "Parcel_Lines"]))
        assert "Parcels" in sql and "Parcel_Lines" in sql
        assert "RetiredByRecord" in sql
