# -*- coding: utf-8 -*-
"""Import an ArcGIS Pro parcel fabric or LandXML; export DXF."""


from qgis.core import (
    QgsCircularString,
    QgsCompoundCurve,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsLineString,
    QgsPoint,
    QgsProject,
    QgsVectorLayer,
)

from . import geomops as G
from . import landxml
from . import schema as S
from . import units as U
from .build import build_parcels
from .fabric import CURRENT, EditSession
from .geomio import parts_of

# a few field names that differ between Pro versions / exports
ALIASES = {"RecordedDate": ["RecordedDate", "RecordDate", "Recorded_Date", "DateRecorded"],
           "RecordType": ["RecordType", "Record_Type", "Type"]}


# --------------------------------------------------------------------------
# ArcGIS Pro parcel fabric (File Geodatabase or GeoPackage export)
# --------------------------------------------------------------------------
def source_layers(path):
    """Table names in a .gdb folder or .gpkg."""
    from osgeo import ogr
    ds = ogr.Open(path)
    if ds is None:
        raise RuntimeError("Can't open %s" % path)
    return [ds.GetLayerByIndex(i).GetName() for i in range(ds.GetLayerCount())]


def detect_fabric(names):
    """Guess a Pro fabric's tables: {'records', 'points', 'connections', 'types': {type: (poly, lines)}}."""
    lower = {n.lower(): n for n in names}

    def find(suffix):
        for n in names:
            if n.lower() == suffix.lower() or n.lower().endswith("_" + suffix.lower()):
                return n
        return None
    types = {}
    for n in names:
        if n.lower().endswith("_lines"):
            poly = n[:-6]
            if poly.lower() in lower:
                base = poly.split("_")[-1] if "_" in poly else poly
                types[base] = (lower[poly.lower()], n)
    return {"records": find("Records"), "points": find("Points"), "connections": find("Connections"),
            "types": types}


def _geom_for(target, g, xf):
    g = QgsGeometry(g)
    if xf is not None:
        g.transform(xf)
    a = g.get()
    a.dropZValue()
    a.dropMValue()
    want = target.wkbType()
    from qgis.core import QgsWkbTypes
    if g.isMultipart() and not QgsWkbTypes.isMultiType(want):
        parts = parts_of(g)
        if len(parts) != 1:
            return None
        g = parts[0]
    if QgsWkbTypes.isCurvedType(want) and not g.constGet().hasCurvedSegments():
        g = QgsGeometry(g.constGet().toCurveType())
    if QgsWkbTypes.isMultiType(want):
        g.convertToMultiType()
    return g


def _copy(src, target, xf, extra=None, feedback=None, label=""):
    pk = set(target.dataProvider().pkAttributeIndexes())
    names = [n for i, n in enumerate(target.fields().names()) if i not in pk and n.lower() not in ("fid", "objectid")]
    src_names = src.fields().names()
    lookup = {}
    for n in names:
        for alt in ALIASES.get(n, [n]):
            if alt in src_names:
                lookup[n] = alt
                break
    batch, n_ok, n_skip = [], 0, 0
    for f in src.getFeatures():
        g = _geom_for(target, f.geometry(), xf) if f.hasGeometry() else None
        if f.hasGeometry() and g is None:
            n_skip += 1
            continue
        nf = QgsFeature(target.fields())
        if g is not None:
            nf.setGeometry(g)
        for n, alt in lookup.items():
            nf.setAttribute(n, f[alt])
        for k, v in (extra or {}).items():
            nf.setAttribute(k, v)
        batch.append(nf)
        if len(batch) >= 1000:
            ok, _ = target.dataProvider().addFeatures(batch)
            n_ok += len(batch) if ok else 0
            batch = []
            if feedback:
                feedback("%s: %d" % (label, n_ok))
    if batch:
        ok, _ = target.dataProvider().addFeatures(batch)
        n_ok += len(batch) if ok else 0
    target.reload()
    return n_ok, n_skip


def import_pro(fabric, path, tables, feedback=None):
    """Copy a Pro fabric's tables into this fabric. ``tables`` as detect_fabric gives.
    Field names already match, so values come across as they are. Returns a
    list of messages."""
    msgs = []

    def open_src(name):
        lyr = QgsVectorLayer("%s|layername=%s" % (path, name), name, "ogr")
        if not lyr.isValid():
            raise RuntimeError("Can't open table %s" % name)
        return lyr

    def xf_for(src):
        if not src.isSpatial() or src.crs() == fabric.parcels.crs():
            return None
        return QgsCoordinateTransform(src.crs(), fabric.parcels.crs(), QgsProject.instance())

    for role, target in (("records", fabric.records), ("points", fabric.points),
                         ("connections", fabric.connections)):
        name = tables.get(role)
        if not name:
            continue
        src = open_src(name)
        ok, skip = _copy(src, target, xf_for(src), feedback=feedback, label=name)
        msgs.append("%s: %d copied%s" % (name, ok, ", %d multi-part skipped" % skip if skip else ""))
    types = list(fabric.parcel_types)
    for ptype, (poly, lines) in tables.get("types", {}).items():
        if ptype not in types:
            types.append(ptype)
        for name, target in ((poly, fabric.parcels), (lines, fabric.lines)):
            src = open_src(name)
            ok, skip = _copy(src, target, xf_for(src), {"ParcelType": ptype}, feedback, name)
            msgs.append("%s -> %s (%s): %d copied%s" % (name, target.name(), ptype, ok,
                                                        ", %d multi-part skipped" % skip if skip else ""))
    if types != fabric.parcel_types:
        import json
        fabric.save_settings({"parcel_types": json.dumps(types)})
    return msgs


# --------------------------------------------------------------------------
# LandXML
# --------------------------------------------------------------------------
def _line_feature(fabric, seg, ptype, record):
    lyr = fabric.lines
    f = QgsFeature(lyr.fields())
    cc = QgsCompoundCurve()
    vals = {"GlobalID": S.new_guid(), "ParcelType": ptype, "COGOType": 1, "IsCOGOGround": 0,
            "CreatedByRecord": record, "QPF_DimSource": "Record"}
    if seg[0] == "arc" and G.arc_center(*seg[1:]) is not None:
        s, m, e = seg[1:]
        cc.addCurve(QgsCircularString(QgsPoint(*s), QgsPoint(*m), QgsPoint(*e)))
        _c, r, _a0, sw = G.arc_sweep(s, m, e)
        az, chord = U.inverse(s[0], s[1], e[0], e[1])
        vals.update({"Direction": U.azimuth_to_esri(az), "Distance": chord,
                     "Radius": r if sw < 0 else -r, "ArcLength": abs(sw) * r, "QPF_CurveForm": "Chord"})
    else:
        a, b = seg[1], seg[-1]
        cc.addCurve(QgsLineString([QgsPoint(*a), QgsPoint(*b)]))
        az, d = U.inverse(a[0], a[1], b[0], b[1])
        vals.update({"Direction": U.azimuth_to_esri(az), "Distance": d})
    f.setGeometry(QgsGeometry(cc))
    for k, v in vals.items():
        f.setAttribute(k, v)
    return f


def _seg_key(seg, nd=3):
    a, b = seg[1], seg[-1]
    ends = tuple(sorted([(round(a[0], nd), round(a[1], nd)), (round(b[0], nd), round(b[1], nd))]))
    return (seg[0],) + ends


def import_landxml(fabric, path, ptype, record):
    """Lines (shared edges once), points and parcels from a LandXML file,
    built under ``record``. Coordinates are taken as the fabric's CRS."""
    parcels = landxml.read(path)
    if not parcels:
        raise RuntimeError("No parcels with geometry in that LandXML file.")
    seen, feats, pts, ptxy = set(), [], [], []
    tol = fabric.tolerance
    for _name, segs, _attrs in parcels:
        for seg in segs:
            k = _seg_key(seg)
            if k in seen:
                continue
            seen.add(k)
            feats.append(_line_feature(fabric, seg, ptype, record))
            for xy in (seg[1], seg[-1]):
                if any(abs(xy[0] - q[0]) <= tol and abs(xy[1] - q[1]) <= tol for q in ptxy):
                    continue
                ptxy.append(xy)
                if fabric.find_point(*xy) is None:
                    pts.append(fabric.new_point_feature(xy[0], xy[1], record))
    with EditSession([fabric.lines, fabric.points], "Import LandXML") as ed:
        for f in feats:
            ed.add(fabric.lines, f)
        for p in pts:
            ed.add(fabric.points, p)
    rep = build_parcels(fabric, ptype, record)
    # carry LandXML names onto the parcels that were built
    named = 0
    expr = '"CreatedByRecord" = \'%s\' AND %s' % (record.replace("'", "''"), CURRENT)
    built = list(fabric.parcels.getFeatures(QgsFeatureRequest().setFilterExpression(expr)))
    with EditSession([fabric.parcels], "LandXML names") as ed:
        for name, segs, attrs in parcels:
            ring = [QgsPoint(*s[1]) for s in segs] + [QgsPoint(*segs[0][1])]
            from qgis.core import QgsPolygon
            poly = QgsGeometry(QgsPolygon(QgsLineString(ring)))
            for b in built:
                if not b["Name"] and poly.contains(b.geometry().pointOnSurface()):
                    ed.change(fabric.parcels, b.id(), "Name", name)
                    try:
                        if attrs.get("area"):
                            ed.change(fabric.parcels, b.id(), "StatedArea", float(attrs["area"]))
                            ed.change(fabric.parcels, b.id(), "StatedAreaUnit", 1)
                    except ValueError:
                        pass
                    named += 1
                    break
    return len(feats), len(pts), rep, named


# --------------------------------------------------------------------------
# DXF
# --------------------------------------------------------------------------
def export_dxf(fabric, path, extent=None, current_only=True):
    """Parcel lines, connections, points and parcels (labels) to a DXF file."""
    from qgis.core import QgsDxfExport, QgsMapSettings
    from qgis.PyQt.QtCore import QFile
    layers = [fabric.lines, fabric.connections, fabric.points, fabric.parcels]
    ms = QgsMapSettings()
    ms.setLayers(layers)
    ms.setDestinationCrs(fabric.parcels.crs())
    box = None
    for lyr in layers:
        e = lyr.extent()
        if e.isEmpty():
            continue
        box = e if box is None else (box.combineExtentWith(e) or box)
    if extent is not None:
        box = extent
    if box is not None:
        ms.setExtent(box)
    dxf = QgsDxfExport()
    dxf.setMapSettings(ms)
    dxf.addLayers([QgsDxfExport.DxfLayer(lyr) for lyr in layers])
    dxf.setDestinationCrs(fabric.parcels.crs())
    if box is not None:
        dxf.setExtent(box)
    f = QFile(path)
    res = dxf.writeToFile(f, "UTF-8")
    ok = getattr(QgsDxfExport.ExportResult, "Success", 0)
    if res != ok:
        raise RuntimeError("DXF export failed (%s)" % res)
    return path
