import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "parcel_fabric"))

from core import maintenance as M


def test_indexes_are_created_and_safe_to_repeat(tmp_path):
    path = tmp_path / "f.gpkg"
    db = sqlite3.connect(str(path))
    db.execute('CREATE TABLE "Parcels" (id INTEGER PRIMARY KEY, "GlobalID" TEXT, '
              '"RetiredByRecord" TEXT, "CreatedByRecord" TEXT, "ParcelNumber" TEXT, "ParcelID" TEXT)')
    db.execute('CREATE TABLE "Records" (id INTEGER PRIMARY KEY, "GlobalID" TEXT)')
    db.execute('INSERT INTO "Parcels" VALUES (1,\'{A}\',NULL,\'{R1}\',\'100\',\'100-000\'), '
              '(2,\'{B}\',NULL,\'{R1}\',\'101\',\'101-000\')')
    db.commit()
    db.close()

    tables = {"parcels": "Parcels", "records": "Records"}
    sql = M.index_statements("ogr", "", tables)
    assert len(sql) == 6   # 5 parcels fields + 1 records field

    assert M._execute("ogr", str(path), sql) == 6
    db = sqlite3.connect(str(path))
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    for field in ("GlobalID", "RetiredByRecord", "CreatedByRecord", "ParcelNumber", "ParcelID"):
        assert M._index_name("Parcels", field) in names
    assert M._index_name("Records", "GlobalID") in names
    # the data (and queries against the new index) are untouched
    row = db.execute('SELECT "ParcelNumber" FROM "Parcels" WHERE "GlobalID" = ?', ("{B}",)).fetchone()
    assert row[0] == "101"
    db.close()

    # running again doesn't fail or duplicate anything (IF NOT EXISTS)
    assert M._execute("ogr", str(path), M.index_statements("ogr", "", tables)) == 6


def test_analyze_and_vacuum_run_cleanly(tmp_path):
    path = tmp_path / "f.gpkg"
    db = sqlite3.connect(str(path))
    db.execute('CREATE TABLE "Parcels" (id INTEGER PRIMARY KEY, "GlobalID" TEXT)')
    db.execute('INSERT INTO "Parcels" VALUES (1, \'{A}\')')
    db.commit()
    db.close()

    tables = {"parcels": "Parcels"}
    assert M._execute("ogr", str(path), M.analyze_statements("ogr", "", tables)) == 1
    # VACUUM must run outside a transaction - autocommit=True is what reclaim_space uses for ogr
    assert M._execute("ogr", str(path), M.vacuum_statements("ogr", "", tables), autocommit=True) == 1
    assert os.path.isfile(path)
    # the data survived both
    db = sqlite3.connect(str(path))
    assert db.execute('SELECT "GlobalID" FROM "Parcels"').fetchone()[0] == "{A}"
    db.close()


def test_sql_server_spatial_index_is_skipped_when_present():
    sql = M.spatial_index_batch("dbo", "Parcels", "geom")
    assert "CREATE SPATIAL INDEX [qpf_six_Parcels_geom]" in sql
    assert "ON [dbo].[Parcels]([geom])" in sql
    assert "BOUNDING_BOX" in sql
    assert "IF NOT EXISTS" in sql
    assert "52800" in sql
    assert M.spatial_index_name("Parcels", "geom") == "qpf_six_Parcels_geom"


def test_server_sql_mentions_every_table_and_field():
    tables = {"parcels": "Parcels", "lineage": "Parcel_Lineage"}
    for prov in ("postgres", "mssql"):
        idx = "\n".join(M.index_statements(prov, "gis", tables))
        assert "Parcels" in idx and "Parcel_Lineage" in idx
        assert "GlobalID" in idx and "ParentID" in idx and "ChildID" in idx
        an = "\n".join(M.analyze_statements(prov, "gis", tables))
        assert "Parcels" in an and "Parcel_Lineage" in an
        vac = "\n".join(M.vacuum_statements(prov, "gis", tables))
        assert "Parcels" in vac and "Parcel_Lineage" in vac
