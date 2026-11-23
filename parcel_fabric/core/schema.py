# -*- coding: utf-8 -*-
"""Fabric schema: table names, fields and how to create them.

Field names mirror the ArcGIS Pro parcel fabric so a fabric exported from
Pro (or to Pro) maps one-to-one.  Fields the plugin needs that Pro does not
have start with ``QPF_``; Pro simply ignores them.

VERIFY BEFORE 1.0 (against a fabric exported from Pro):
  * Records date/type field names (RecordedDate, RecordType are placeholders)
  * coded values of COGOType, StatedAreaUnit and AdjustmentConstraint
"""

import json
import os
import re
import uuid

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsField,
    QgsFields,
    QgsProviderRegistry,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QMetaType


def _mt(name):
    """QMetaType value that works on PyQt5 (QGIS 3) and PyQt6 (QGIS 4)."""
    t = getattr(QMetaType, "Type", None)
    if t is not None and hasattr(t, name):
        return getattr(t, name)
    return getattr(QMetaType, name)


STR, INT, DBL, DATE = "QString", "Int", "Double", "QDate"

# --------------------------------------------------------------------------
# Coded values (plugin defaults - see VERIFY note above)
# --------------------------------------------------------------------------
YES_NO = {0: "No", 1: "Yes"}
COGO_TYPE = {1: "Entered", 2: "Computed", 3: "Estimated"}
DIM_SOURCE = ["Record", "Measured", "Computed", "Estimated"]
CURVE_FORM = ["Chord", "Radial", "Tangent"]
AREA_UNIT = {1: "Square feet", 2: "Acres"}
ADJ_CONSTRAINT = {0: "XY free", 1: "XY constrained", 2: "XY weighted"}
RECORD_TYPES = ["Plat", "Deed", "Survey", "Right of way", "Easement",
                "Boundary line adjustment", "Lot split", "Data cleanup"]

GUID_LEN = 38  # {XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}


def new_guid():
    """Esri style GlobalID: braces, upper case."""
    return "{%s}" % str(uuid.uuid4()).upper()


# name, type, length, alias
_COMMON_TAIL = [
    ("CreatedByRecord", STR, GUID_LEN, "Created by record"),
    ("RetiredByRecord", STR, GUID_LEN, "Retired by record"),
]

RECORD_FIELDS = [
    ("GlobalID", STR, GUID_LEN, "Global ID"),
    ("Name", STR, 255, "Name"),
    ("RecordedDate", DATE, 0, "Recorded date"),
    ("RecordType", STR, 50, "Record type"),
    ("Book", STR, 20, "Book"),
    ("Page", STR, 20, "Page"),
    ("ParcelCount", INT, 0, "Parcel count"),
    ("QPF_DocumentURL", STR, 1024, "Document link"),
    ("QPF_BasisOfBearing", STR, 255, "Basis of bearing"),
    ("QPF_ScaleFactor", DBL, 0, "Ground to grid scale"),
    ("QPF_Rotation", DBL, 0, "Ground to grid rotation (deg)"),
    ("QPF_Notes", STR, 1024, "Notes"),
]

POINT_FIELDS = [
    ("GlobalID", STR, GUID_LEN, "Global ID"),
    ("Name", STR, 50, "Name"),
    ("X", DBL, 0, "X"),
    ("Y", DBL, 0, "Y"),
    ("Z", DBL, 0, "Z"),
    ("IsFixed", INT, 0, "Is fixed"),
    ("AdjustmentConstraint", INT, 0, "Adjustment constraint"),
] + _COMMON_TAIL + [
    ("QPF_IsControl", INT, 0, "Is control"),
    ("QPF_StdDevXY", DBL, 0, "Std. deviation XY"),
]

LINE_FIELDS = [
    ("GlobalID", STR, GUID_LEN, "Global ID"),
    ("ParcelType", STR, 50, "Parcel type"),
    ("Direction", DBL, 0, "Direction"),
    ("Distance", DBL, 0, "Distance"),
    ("Radius", DBL, 0, "Radius"),
    ("ArcLength", DBL, 0, "Arc length"),
    ("Radius2", DBL, 0, "Radius 2"),
    ("COGOType", INT, 0, "COGO type"),
    ("IsCOGOGround", INT, 0, "Is COGO ground"),
    ("Rotation", DBL, 0, "Rotation"),
    ("Scale", DBL, 0, "Scale"),
    ("ParentLineID", STR, GUID_LEN, "Parent line ID"),
] + _COMMON_TAIL + [
    ("QPF_CurveForm", STR, 10, "Curve form"),
    ("QPF_DimSource", STR, 12, "Dimension source"),
]

PARCEL_FIELDS = [
    ("GlobalID", STR, GUID_LEN, "Global ID"),
    ("ParcelType", STR, 50, "Parcel type"),
    ("ParcelNumber", STR, 30, "Parcel number"),
    ("Section", INT, 0, "Section"),
    ("Township", STR, 4, "Township"),
    ("Range", INT, 0, "Range"),
    ("ParcelID", STR, 12, "Parcel ID"),              # 02942-000: the number without S-T-R
    ("ParcelSeq", INT, 0, "Parcel sequence"),        # 2942
    ("SplitNo", INT, 0, "Split"),
    ("ParentNumber", STR, 30, "Parent parcel number"),
    ("Name", STR, 255, "Name"),
    ("StatedArea", DBL, 0, "Stated area"),
    ("StatedAreaUnit", INT, 0, "Stated area unit"),
    ("CalculatedArea", DBL, 0, "Calculated area"),
    ("MiscloseRatio", DBL, 0, "Misclose ratio"),
    ("MiscloseDistance", DBL, 0, "Misclose distance"),
    ("IsSeed", INT, 0, "Is seed"),
] + _COMMON_TAIL + [
    ("QPF_StartDate", DATE, 0, "Start date (from its record)"),
    ("QPF_EndDate", DATE, 0, "End date (from the retiring record)"),
]

LINEAGE_FIELDS = [
    ("GlobalID", STR, GUID_LEN, "Global ID"),
    ("RecordID", STR, GUID_LEN, "Record"),
    ("Operation", STR, 20, "Operation"),
    ("ParentID", STR, GUID_LEN, "Parent parcel"),
    ("ChildID", STR, GUID_LEN, "Child parcel"),
    ("ParentNumber", STR, 30, "Parent number"),
    ("ChildNumber", STR, 30, "Child number"),
    ("ParcelType", STR, 50, "Parcel type"),
    ("EditDate", STR, 20, "Edit date"),
]

SETTINGS_FIELDS = [
    ("Key", STR, 64, "Key"),
    ("Value", STR, 4000, "Value"),
]

# role -> (table suffix, geometry type, fields)
ROLE_RECORDS, ROLE_POINTS, ROLE_CONNECTIONS, ROLE_SETTINGS = (
    "records", "points", "connections", "settings")
ROLE_LINES, ROLE_PARCELS = "lines", "parcels"
ROLE_LINEAGE = "lineage"

# BLM National PLSS CadNSDI (verified 2026-11): layer 1 = townships (TWNSHPNO, TWNSHPDIR,
# RANGENO, RANGEDIR), layer 2 = sections (FRSTDIVNO).  Both Web Mercator; both carry PLSSID.
PLSS_DEFAULT_URL = "https://gis.blm.gov/arcgis/rest/services/Cadastral/BLM_Natl_PLSS_CadNSDI/MapServer"
PLSS_DEFAULT = {
    "sections": {"url": PLSS_DEFAULT_URL + "/2", "name": "PLSS Sections (BLM)", "section": "FRSTDIVNO"},
    "townships": {"url": PLSS_DEFAULT_URL + "/1", "name": "PLSS Townships (BLM)",
                  "township": "TWNSHPNO", "dir": "TWNSHPDIR", "range": "RANGENO"},
}


def rest_uri(url):
    return "crs='EPSG:3857' url='%s'" % url


DEFAULT_SETTINGS = {
    "version": "2",
    "foot": "us_survey",          # or "international"
    "bearing_style": "quadrant",
    "packed_dms": "0",            # "1": read 45.3015 as 45°30'15"
    "snap_tolerance": "0.01",     # map units
    "misclose_warn_ratio": "10000",
    # documents
    "doc_root": "",
    "doc_folder_pattern": "{type}/Book {book}",
    "doc_name_pattern": "{type}_B{book}_P{page}",
    # parcel numbers
    "plss_provider": "",
    "plss_source": "",
    "plss_section_field": "",
    "plss_township_field": "",
    "plss_township_dir_field": "",
    "plss_range_field": "",
    # optional separate township/range layer (online PLSS is split into layers)
    "plss_twp_provider": "",
    "plss_twp_source": "",
    "plss_twp_township_field": "",
    "plss_twp_dir_field": "",
    "plss_twp_range_field": "",
    # "1": when no layer above is chosen, use the BLM National PLSS REST service
    "plss_use_default": "1",
    "number_on_build": "1",
    "number_types": "",           # JSON list; blank = every parcel type
}


def _fields(spec):
    fs = QgsFields()
    for name, typ, length, alias in spec:
        f = QgsField(name, _mt(typ), len=length)
        f.setAlias(alias)
        fs.append(f)
    return fs


def table_name(prefix, role):
    p = (prefix + "_") if prefix else ""
    names = {ROLE_RECORDS: "Records", ROLE_POINTS: "Points", ROLE_CONNECTIONS: "Connections",
             ROLE_SETTINGS: "QPF_Settings", ROLE_PARCELS: "Parcels", ROLE_LINES: "Parcel_Lines",
             ROLE_LINEAGE: "Parcel_Lineage"}
    return p + names[role]


# SQL Server has no curved multi-polygon type (Oracle is untested), so parcels stay single there
MULTIPART_PROVIDERS = ("ogr", "postgres")


def table_specs(prefix, multipart=True):
    """Every table of a fabric: (role, name, wkb, fields)."""
    W = Qgis.WkbType
    return [
        (ROLE_SETTINGS, table_name(prefix, ROLE_SETTINGS), W.NoGeometry, SETTINGS_FIELDS),
        (ROLE_RECORDS, table_name(prefix, ROLE_RECORDS), W.MultiPolygon, RECORD_FIELDS),
        (ROLE_POINTS, table_name(prefix, ROLE_POINTS), W.Point, POINT_FIELDS),
        (ROLE_CONNECTIONS, table_name(prefix, ROLE_CONNECTIONS), W.CompoundCurve, LINE_FIELDS),
        (ROLE_PARCELS, table_name(prefix, ROLE_PARCELS),
         W.MultiSurface if multipart else W.CurvePolygon, PARCEL_FIELDS),
        (ROLE_LINES, table_name(prefix, ROLE_LINES), W.CompoundCurve, LINE_FIELDS),
        (ROLE_LINEAGE, table_name(prefix, ROLE_LINEAGE), W.NoGeometry, LINEAGE_FIELDS),
    ]


def fields_for(role):
    return {ROLE_SETTINGS: SETTINGS_FIELDS, ROLE_RECORDS: RECORD_FIELDS, ROLE_POINTS: POINT_FIELDS,
            ROLE_CONNECTIONS: LINE_FIELDS, ROLE_PARCELS: PARCEL_FIELDS,
            ROLE_LINES: LINE_FIELDS, ROLE_LINEAGE: LINEAGE_FIELDS}[role]


def ensure_table(conn, schema, prefix, role, crs):
    """Create a table a newer plugin version added, if it's missing."""
    for r, name, wkb, fspec in table_specs(prefix):
        if r == role:
            if not conn.tableExists(schema, name):
                opts = {"geometryColumn": "geom"} if wkb != Qgis.WkbType.NoGeometry else {}
                conn.createVectorTable(schema, name, _fields(fspec), wkb, crs, False, opts)
            return name
    raise ValueError(role)


def add_missing_fields(lyr, role):
    """Add fields a newer plugin version expects (keeps old fabrics working)."""
    have = set(lyr.fields().names())
    missing = [f for f in _fields(fields_for(role)) if f.name() not in have]
    if not missing:
        return []
    if not lyr.dataProvider().addAttributes(missing):
        raise RuntimeError("Could not add fields %s to %s"
                           % (", ".join(f.name() for f in missing), lyr.name()))
    lyr.updateFields()
    return [f.name() for f in missing]


def parcel_id_text(number):
    """"12-4S-16-02942-000" -> "02942-000" (the last two parts)."""
    parts = str(number or "").split("-")
    return "-".join(parts[-2:]) if len(parts) >= 5 else None


def migrate_parcel_id(lyr):
    """0.8.0 and earlier stored ParcelID as the bare number (2942). It is now
    text "02942-000"; the bare number moves to ParcelSeq. Also fills any
    ParcelID left empty. Returns the number of parcels updated."""
    from qgis.core import QgsFeatureRequest
    prov = lyr.dataProvider()
    idx = lyr.fields().indexOf("ParcelID")
    if idx >= 0 and lyr.fields().at(idx).isNumeric():
        if lyr.fields().indexOf("ParcelSeq") < 0:
            if not prov.addAttributes([QgsField("ParcelSeq", _mt(INT))]):
                raise RuntimeError("Could not add ParcelSeq to %s" % lyr.name())
            lyr.updateFields()
        seq = lyr.fields().indexOf("ParcelSeq")
        prov.changeAttributeValues({f.id(): {seq: f["ParcelID"]} for f in lyr.getFeatures(
            QgsFeatureRequest().setSubsetOfAttributes(["ParcelID"], lyr.fields())
            .setFlags(Qgis.FeatureRequestFlag.NoGeometry)) if f["ParcelID"]})
        if not prov.deleteAttributes([idx]):
            raise RuntimeError("Could not convert ParcelID to text in %s - drop that field in the "
                               "database and reopen the fabric." % lyr.name())
        lyr.updateFields()
        if not prov.addAttributes([QgsField("ParcelID", _mt(STR), len=12)]):
            raise RuntimeError("Could not add ParcelID to %s" % lyr.name())
        lyr.updateFields()
    idx = lyr.fields().indexOf("ParcelID")
    if idx < 0:
        return 0
    req = QgsFeatureRequest().setSubsetOfAttributes(["ParcelNumber", "ParcelID"], lyr.fields())
    req.setFlags(Qgis.FeatureRequestFlag.NoGeometry)
    changes = {}
    for f in lyr.getFeatures(req):
        want = parcel_id_text(f["ParcelNumber"])
        if want and (f["ParcelID"] or None) != want:
            changes[f.id()] = {idx: want}
    if changes:
        prov.changeAttributeValues(changes)
    return len(changes)


def convert_gpkg_parcels(path, table):
    """Change a GeoPackage fabric's single-polygon parcels to multi-polygon.

    The file is copied first; the change itself is one transaction.  Returns the backup's path.
    """
    import datetime
    import sqlite3
    from osgeo import gdal, ogr
    backup = "%s.before-multipart-%s.gpkg" % (
        os.path.splitext(path)[0], datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    if os.path.exists(backup):
        raise RuntimeError("%s already exists" % backup)
    src = sqlite3.connect(path)
    dst = sqlite3.connect(backup)
    try:
        src.backup(dst)             # also right when part of the data is still in the write-ahead log
    finally:
        dst.close()
        src.close()

    def totals(ds):
        n, area = 0, 0.0
        for f in ds.GetLayerByName(table):
            g = f.GetGeometryRef()
            n += 1
            area += g.GetArea() if g is not None else 0.0
        return n, area

    gdal.PushErrorHandler("CPLQuietErrorHandler")       # it warns while type and geometry differ
    ds = None
    try:
        ds = gdal.OpenEx(path, gdal.OF_VECTOR | gdal.OF_UPDATE)
        if ds is None:
            raise RuntimeError("Could not open %s for editing" % path)
        before = totals(ds)
        if ds.StartTransaction() != 0:
            raise RuntimeError("Could not start a transaction")
        try:
            lyr = ds.GetLayerByName(table)
            lyr.ResetReading()
            for fid in [f.GetFID() for f in lyr]:
                f = lyr.GetFeature(fid)
                g = f.GetGeometryRef()
                if g is None or g.GetGeometryName() in ("MULTISURFACE", "MULTIPOLYGON"):
                    continue
                multi = ogr.Geometry(ogr.wkbMultiSurface)
                multi.AssignSpatialReference(g.GetSpatialReference())
                if multi.AddGeometry(g) != 0:
                    raise RuntimeError("Parcel #%d can't be converted" % fid)
                f.SetGeometry(multi)
                if lyr.SetFeature(f) != 0:
                    raise RuntimeError("Parcel #%d could not be saved: %s" % (fid, gdal.GetLastErrorMsg()))
            ds.ExecuteSQL("UPDATE gpkg_geometry_columns SET geometry_type_name = 'MULTISURFACE' "
                          "WHERE lower(table_name) = lower('%s')" % table.replace("'", "''"))
            # the declared column type has to agree; SQLite has no ALTER for it
            def one(sql):
                rs = ds.ExecuteSQL(sql)
                value = rs.GetNextFeature().GetField(0)
                ds.ReleaseResultSet(rs)
                return value
            where = "WHERE type = 'table' AND lower(name) = lower('%s')" % table.replace("'", "''")
            ddl = one("SELECT sql FROM sqlite_master " + where)
            new_ddl, n = re.subn(r"\bCURVEPOLYGON\b", "MULTISURFACE", ddl, flags=re.I)
            if n != 1:
                raise RuntimeError("The Parcels table isn't laid out as expected")
            version = int(one("PRAGMA schema_version"))
            ds.ExecuteSQL("PRAGMA writable_schema = 1")
            ds.ExecuteSQL("UPDATE sqlite_master SET sql = '%s' %s" % (new_ddl.replace("'", "''"), where))
            ds.ExecuteSQL("PRAGMA schema_version = %d" % (version + 1))
            ds.ExecuteSQL("PRAGMA writable_schema = 0")
            if one("SELECT sql FROM sqlite_master " + where) != new_ddl:
                raise RuntimeError("The Parcels table could not be changed")
            after = totals(ds)
            if after[0] != before[0] or abs(after[1] - before[1]) > 1e-6 * max(1.0, before[1]):
                raise RuntimeError("The converted parcels don't match the originals")
            if ds.CommitTransaction() != 0:
                raise RuntimeError("Could not save: %s" % gdal.GetLastErrorMsg())
        except Exception:
            ds.RollbackTransaction()
            raise
    finally:
        ds = lyr = f = g = None     # close the file
        gdal.PopErrorHandler()
    return backup


# --------------------------------------------------------------------------
# Connections (GeoPackage or any QGIS database connection)
# --------------------------------------------------------------------------
DB_PROVIDERS = [("postgres", "PostgreSQL / PostGIS"), ("mssql", "SQL Server"),
                ("oracle", "Oracle"), ("ogr", "GeoPackage (saved connection)")]


def saved_connections():
    """[(provider, name)] of database connections saved in QGIS."""
    out = []
    reg = QgsProviderRegistry.instance()
    for key, _label in DB_PROVIDERS:
        md = reg.providerMetadata(key)
        if md is None:
            continue
        try:
            for name in sorted(md.connections(False).keys()):
                out.append((key, name))
        except Exception:  # provider without connection API
            continue
    return out


def open_connection(provider, name_or_path):
    """Return a QgsAbstractDatabaseProviderConnection."""
    md = QgsProviderRegistry.instance().providerMetadata(provider)
    if md is None:
        raise RuntimeError("QGIS has no '%s' data provider" % provider)
    if provider == "ogr" and name_or_path.lower().endswith(".gpkg"):
        if not os.path.exists(name_or_path):
            _create_empty_gpkg(name_or_path)
        return md.createConnection(name_or_path, {})
    return md.createConnection(name_or_path)


def _create_empty_gpkg(path):
    from osgeo import ogr
    drv = ogr.GetDriverByName("GPKG")
    ds = drv.CreateDataSource(path)
    if ds is None:
        raise RuntimeError("Could not create %s" % path)
    ds = None


def layer_provider(provider):
    return "ogr" if provider == "ogr" else provider


def create_fabric(provider, name_or_path, schema, prefix, parcel_types, crs_authid,
                  settings=None, feedback=None):
    """Create every table of a new fabric. Returns the connection.

    ``schema`` is ignored for GeoPackage.  Raises if any table already
    exists, so an existing fabric is never overwritten.
    """
    conn = open_connection(provider, name_or_path)
    if provider == "ogr":
        schema = ""
    crs = QgsCoordinateReferenceSystem(crs_authid)
    if not crs.isValid():
        raise RuntimeError("Unknown coordinate system %s" % crs_authid)
    specs = table_specs(prefix, provider in MULTIPART_PROVIDERS)
    for _role, name, _w, _f in specs:
        if conn.tableExists(schema, name):
            raise RuntimeError("Table %s already exists - pick another prefix" % name)
    for _role, name, wkb, fspec in specs:
        if feedback:
            feedback(name)
        opts = {"geometryColumn": "geom"} if wkb != Qgis.WkbType.NoGeometry else {}
        conn.createVectorTable(schema, name, _fields(fspec), wkb, crs, False, opts)

    s = dict(DEFAULT_SETTINGS)
    s.update(settings or {})
    s["prefix"] = prefix
    s["parcel_types"] = json.dumps(list(parcel_types))
    s["crs"] = crs_authid
    s["fabric_id"] = new_guid()
    lyr = load_table(conn, provider, schema, table_name(prefix, ROLE_SETTINGS), "settings")
    write_settings(lyr, s)
    return conn


def load_table(conn, provider, schema, table, display_name):
    uri = conn.tableUri(schema, table)
    lyr = QgsVectorLayer(uri, display_name, layer_provider(provider))
    if not lyr.isValid():
        raise RuntimeError("Could not open table %s" % table)
    return lyr


def read_settings(lyr):
    out = {}
    for f in lyr.getFeatures():
        out[str(f["Key"])] = "" if f["Value"] is None else str(f["Value"])
    return out


def write_settings(lyr, values):
    from qgis.core import QgsFeature
    existing = {}
    for f in lyr.getFeatures():
        existing[str(f["Key"])] = f.id()
    kidx = lyr.fields().indexOf("Key")
    vidx = lyr.fields().indexOf("Value")
    lyr.startEditing()
    for k, v in values.items():
        if k in existing:
            lyr.changeAttributeValue(existing[k], vidx, str(v))
        else:
            f = QgsFeature(lyr.fields())
            f.setAttribute(kidx, k)
            f.setAttribute(vidx, str(v))
            lyr.addFeature(f)
    if not lyr.commitChanges():
        err = "; ".join(lyr.commitErrors())
        lyr.rollBack()
        raise RuntimeError("Could not save fabric settings: " + err)
