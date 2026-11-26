# -*- coding: utf-8 -*-
"""Import an ArcGIS Pro parcel fabric, a parcel layer, or LandXML; export DXF and the tax roll."""

import math
import os
import re

from qgis.core import (
    QgsCircularString,
    QgsCompoundCurve,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsLineString,
    QgsPoint,
    QgsFeatureSink,
    QgsProject,
    QgsTransaction,
    QgsVectorLayer,
)

from . import geomops as G
from . import landxml
from . import schema as S
from . import units as U
from .build import build_parcels, update_record_footprint
from .edits import _new_line
from .fabric import CURRENT, EditSession, quote, type_expr
from .geomio import match_wkb_type, parts_of, polygon_rings

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
    return match_wkb_type(target.wkbType(), g)


def _tell(progress, pct, text=None):
    if progress is None:
        return
    try:
        progress(pct, text)
    except TypeError:
        progress(pct)


def _copy(src, target, xf, extra=None, feedback=None, label="", progress=None):
    pk = set(target.dataProvider().pkAttributeIndexes())
    names = [n for i, n in enumerate(target.fields().names()) if i not in pk and n.lower() not in ("fid", "objectid")]
    src_names = src.fields().names()
    lookup = {}
    for n in names:
        for alt in ALIASES.get(n, [n]):
            if alt in src_names:
                lookup[n] = alt
                break
    batch, n_ok, n_skip, seen = [], 0, 0, 0
    total = src.featureCount()
    name = label or src.name()
    for f in src.getFeatures():
        seen += 1
        if seen % 200 == 0:
            if total and total > 0:
                _tell(progress, min(99.0, 100.0 * seen / float(total)),
                      "Importing %s: %d of %d" % (name, seen, total))
            else:
                _tell(progress, 0, "Importing %s: %d" % (name, seen))
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


def import_pro(fabric, path, tables, feedback=None, progress=None):
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

    jobs = []
    for role, target in (("records", fabric.records), ("points", fabric.points),
                         ("connections", fabric.connections)):
        name = tables.get(role)
        if name:
            jobs.append((name, target, None))
    types = list(fabric.parcel_types)
    for ptype, (poly, lines) in tables.get("types", {}).items():
        if ptype not in types:
            types.append(ptype)
        jobs.append((poly, fabric.parcels, ptype))
        jobs.append((lines, fabric.lines, ptype))
    count = max(len(jobs), 1)
    for index, (name, target, ptype) in enumerate(jobs):
        def tick(pct, text=None, index=index, name=name):
            _tell(progress, (index + (pct or 0) / 100.0) * 100.0 / count, text or ("Importing %s" % name))

        src = open_src(name)
        extra = {"ParcelType": ptype} if ptype else None
        ok, skip = _copy(src, target, xf_for(src), extra, feedback, name, tick)
        if ptype:
            msgs.append("%s -> %s (%s): %d copied%s" % (name, target.name(), ptype, ok,
                                                        ", %d multi-part skipped" % skip if skip else ""))
        else:
            msgs.append("%s: %d copied%s" % (name, ok, ", %d multi-part skipped" % skip if skip else ""))
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


def import_landxml(fabric, path, ptype, record, progress=None):
    """Lines (shared edges once), points and parcels from a LandXML file,
    built under ``record``. Coordinates are taken as the fabric's CRS."""
    parcels = landxml.read(path)
    if not parcels:
        raise RuntimeError("No parcels with geometry in that LandXML file.")
    seen, feats, pts, ptxy = set(), [], [], []
    tol = fabric.tolerance
    total = max(len(parcels), 1)
    for n, (_name, segs, _attrs) in enumerate(parcels):
        if n % 10 == 0:
            _tell(progress, 80.0 * n / total, "Importing LandXML: parcel %d of %d" % (n + 1, total))
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
    _tell(progress, 85, "Saving LandXML lines and points")
    with EditSession([fabric.lines, fabric.points], "Import LandXML") as ed:
        for f in feats:
            ed.add(fabric.lines, f)
        for p in pts:
            ed.add(fabric.points, p)
    _tell(progress, 92, "Building parcels from LandXML")
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
# A polygon layer that was never a fabric
# --------------------------------------------------------------------------
def open_polygon_layer(path):
    """The polygon layer in a shapefile, or the only polygon layer in a GeoPackage."""
    from qgis.core import QgsWkbTypes
    if not path or not os.path.exists(path):
        raise RuntimeError("File not found.")
    if path.lower().endswith(".shp"):
        lyr = QgsVectorLayer(path, os.path.splitext(os.path.basename(path))[0], "ogr")
        if not lyr.isValid() or lyr.geometryType() != QgsWkbTypes.PolygonGeometry:
            raise RuntimeError("That shapefile is not polygons.")
        return lyr
    found = []
    for name in source_layers(path):
        lyr = QgsVectorLayer("%s|layername=%s" % (path, name), name, "ogr")
        if lyr.isValid() and lyr.isSpatial() and lyr.geometryType() == QgsWkbTypes.PolygonGeometry:
            found.append(lyr)
    if not found:
        raise RuntimeError("No polygon layer in that file.")
    if len(found) > 1:
        raise RuntimeError("That file has more than one polygon layer (%s). "
                           "Add the one you want to the map and pick it here."
                           % ", ".join(lyr.name() for lyr in found))
    return found[0]


def _attr_text(feature, field):
    if not field or feature.fields().indexOf(field) < 0:
        return ""
    v = feature[field]
    if v is None:
        return ""
    try:
        if hasattr(v, "isNull") and v.isNull():
            return ""
    except TypeError:
        pass
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v).strip()


def _surface_parts(geom):
    """Polygon parts of a parcel, holes and curves included."""
    out = []
    for part in parts_of(geom):
        body = part.constGet()
        if body is None:
            continue
        if hasattr(body, "exteriorRing"):
            out.append(part)
        elif hasattr(body, "numGeometries"):
            out.extend(_surface_parts(part))
    return out


def _pack_parcel(target, parts):
    """The geometry for these parts.

    A curve stays a curve only when the table can store one. Lots, blocks and
    subdivisions are plain MultiPolygon. SQL Server rejects MultiSurface WKT,
    so those curves are stored as straight segments.
    """
    from qgis.core import QgsMultiPolygon, QgsMultiSurface, QgsWkbTypes
    want = target.wkbType()
    multi = QgsWkbTypes.isMultiType(want)
    if not multi and len(parts) != 1:
        return None
    curved = QgsWkbTypes.isCurvedType(want)
    owned = []
    for part in parts:
        body = part.constGet()
        if body is None:
            continue
        if curved:
            piece = body.toCurveType() if not body.hasCurvedSegments() else body.clone()
        else:
            piece = body.segmentize()
        if piece is not None:
            owned.append(piece)
    if not owned:
        return None
    if not multi:
        if len(owned) != 1:
            return None
        return QgsGeometry(owned[0])
    surface = QgsMultiSurface() if curved else QgsMultiPolygon()
    for piece in owned:
        if not surface.addGeometry(piece):
            return None
    return QgsGeometry(surface)


def _join_parcel(target, parts):
    """One parcel from several features. Polygons that share a side are dissolved."""
    packed = _pack_parcel(target, parts)
    flat = QgsGeometry(packed.constGet().segmentize())
    if flat.isGeosValid() or packed.constGet().hasCurvedSegments():
        return packed
    whole = QgsGeometry.unaryUnion([QgsGeometry(p) for p in parts])
    return _pack_parcel(target, _surface_parts(whole)) if whole.isGeosValid() else packed


def _has_area(geom):
    if geom is None or geom.isEmpty():
        return False
    if abs(geom.area()) > 0:
        return True
    try:
        flat = QgsGeometry(geom.constGet().segmentize())
    except Exception:
        return False
    return abs(flat.area()) > 0


def _same_outline(geom, index, held):
    """True when an existing polygon covers this one and this one covers it."""
    for fid in index.intersects(geom.boundingBox()):
        other = held.get(fid)
        if other is None:
            continue
        try:
            inter = geom.intersection(other)
        except Exception:
            continue
        if inter is None or inter.isEmpty():
            continue
        if inter.area() >= 0.99 * max(geom.area(), other.area()):
            return True
    return False


def _as_qdate(value):
    """A QDate from a date field or a text date, or None."""
    import datetime
    from qgis.PyQt.QtCore import QDate, QDateTime
    if value is None:
        return None
    if isinstance(value, QDate):
        return value if value.isValid() else None
    if isinstance(value, QDateTime):
        day = value.date()
        return day if day.isValid() else None
    if isinstance(value, datetime.datetime):
        return QDate(value.year, value.month, value.day)
    if isinstance(value, datetime.date):
        return QDate(value.year, value.month, value.day)
    try:
        if hasattr(value, "isNull") and value.isNull():
            return None
    except TypeError:
        pass
    text = str(value).strip()
    if not text:
        return None
    for fmt, width in (("yyyy-MM-dd", 10), ("yyyy/MM/dd", 10), ("MM/dd/yyyy", 10),
                       ("M/d/yyyy", 10), ("yyyyMMdd", 8)):
        day = QDate.fromString(text[:width], fmt)
        if day.isValid():
            return day
    return None


def _suffix_int(value):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return number if 0 <= number <= 999 else 0


def _record_feature(fabric, name, when, parcel_id, book="", page="", created_by="", edited_by=""):
    lyr = fabric.records
    feat = QgsFeature(lyr.fields())
    gid = S.new_guid()
    vals = {"GlobalID": gid, "Name": name, "RecordType": "Existing", "ParcelCount": 0,
            "QPF_ScaleFactor": 1.0, "QPF_Rotation": 0.0}
    if when is not None:
        vals["RecordedDate"] = when
    if parcel_id and lyr.fields().indexOf("QPF_ParcelID") >= 0:
        vals["QPF_ParcelID"] = parcel_id[:30]
    if book and lyr.fields().indexOf("Book") >= 0:
        vals["Book"] = str(book)[:20]
    if page and lyr.fields().indexOf("Page") >= 0:
        vals["Page"] = str(page)[:20]
    if created_by and lyr.fields().indexOf("CreatedByUser") >= 0:
        vals["CreatedByUser"] = str(created_by)[:100]
    if edited_by and lyr.fields().indexOf("EditUser") >= 0:
        vals["EditUser"] = str(edited_by)[:100]
    for key, value in vals.items():
        i = lyr.fields().indexOf(key)
        if i >= 0:
            feat.setAttribute(i, value)
    return gid, feat


def _stamp_footprints(fabric, record_gids):
    """Record polygon and parcel count for each of these records. One edit."""
    wanted = [g for g in record_gids if g]
    if not wanted:
        return
    geoms = {g: [] for g in wanted}
    if len(wanted) > 400:
        req = QgsFeatureRequest().setFilterExpression(CURRENT)
    else:
        req = QgsFeatureRequest().setFilterExpression(
            '"CreatedByRecord" IN (%s) AND %s' % (", ".join(quote(g) for g in wanted), CURRENT))
    for feat in fabric.parcels.getFeatures(req):
        if feat["IsSeed"] == 1:
            continue
        bucket = geoms.get(feat["CreatedByRecord"])
        if bucket is None:
            continue
        geom = feat.geometry()
        if geom is None or geom.isNull() or geom.isEmpty():
            continue
        try:
            bucket.append(QgsGeometry(geom.constGet().segmentize()))
        except Exception:
            bucket.append(QgsGeometry(geom))
    ids = {}
    for rec in fabric.records.getFeatures():
        gid = rec["GlobalID"]
        if gid in geoms:
            ids[gid] = rec.id()
    with EditSession([fabric.records], "Imported parcels") as ed:
        for gid, parts in geoms.items():
            fid = ids.get(gid)
            if fid is None:
                continue
            if parts:
                union = QgsGeometry.unaryUnion(parts)
                if union is not None and not union.isEmpty():
                    union.convertToMultiType()
                    ed.set_geometry(fabric.records, fid, union)
            ed.change(fabric.records, fid, "ParcelCount", len(parts))


def _border(a, b, reach):
    """Length of the border two polygons share; 0 when they only meet at a corner."""
    if not a.boundingBox().buffered(reach).intersects(b.boundingBox()) or a.distance(b) > reach:
        return 0.0
    length = a.buffer(reach, 2).intersection(QgsGeometry(b.constGet().boundary())).length()
    return length if length > 10 * reach else 0.0


def _foot(fabric):
    """Map units in a foot, or None where map areas mean nothing (degrees, Web Mercator)."""
    try:
        return fabric.map_units_per_foot
    except ValueError:
        return None


CUT_OUT = 0.4       # a square corner cut is half inside the parent's outline; a neighbour is 0


def _cut_from(child, lower, reach):
    """Key of the polygon ``child`` was cut out of, or None.

    ``lower`` are (key, polygon). A piece cut out of a parcel fills a notch: much of it
    lies inside the outline (convex hull) of what is left. A neighbour, such as the next
    platted lot, only shares a side.
    """
    best, found = CUT_OUT, None
    for key, polygon in lower:
        if polygon.area() <= child.area():
            continue
        for part in parts_of(polygon):      # pieces that lie apart have no outline in common
            hull = part.convexHull()
            if not hull.boundingBox().intersects(child.boundingBox()) or not _border(child, part, reach):
                continue
            inside = child.intersection(hull).area() / child.area()
            if inside >= best:
                best, found = inside, key
    return found


def _link_splits(fabric, ptype, parent_suffix, stems, separator="-", digits=3):
    """Inferred lineage between the -NNN pieces of these stems. Nothing is retired.

    A parcel came from the lower number it was cut out of (_cut_from), else from the
    parent suffix (000). With neither there is nothing to go by, so no link. A parcel
    that already has a parent keeps it. Returns how many links were made.
    """
    import datetime
    from .lineage import INFERRED
    from .numbering import split_parts
    from .parcel_numbers import unfiltered

    if not stems:
        return 0
    groups = {}
    lyr = unfiltered(fabric.parcels)
    req = QgsFeatureRequest().setFilterExpression(type_expr(ptype))
    for feat in lyr.getFeatures(req):
        parts = split_parts(feat["ParcelNumber"], separator, digits)
        if parts is None or parts[0] not in stems:
            continue
        groups.setdefault(parts[0], []).append((parts[1], feat))
    has_parent = {row["ChildID"] for row in fabric.lineage.getFeatures()}
    foot = _foot(fabric)
    pairs = []
    for members in groups.values():
        members.sort(key=lambda m: m[0])
        parent = next((feat for split, feat in members if split == parent_suffix), None)
        shapes = []
        for split, feat in members:
            geom = feat.geometry()
            shapes.append((split, feat, QgsGeometry(geom.constGet().segmentize())))
        for split, feat, shape in shapes:
            if split == parent_suffix or feat["GlobalID"] in has_parent:
                continue
            lower = [(f, g) for s, f, g in shapes if s < split]
            source = (_cut_from(shape, lower, foot) if foot else None) or parent
            if source is not None:
                pairs.append((source, feat))
    if not pairs:
        return 0
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lin = fabric.lineage
    with EditSession([lin, lyr], "Split history") as ed:
        for parent, child in pairs:
            feat = QgsFeature(lin.fields())
            vals = {"GlobalID": S.new_guid(), "RecordID": child["CreatedByRecord"],
                    "Operation": INFERRED, "ParentID": parent["GlobalID"],
                    "ChildID": child["GlobalID"],
                    "ParentNumber": parent["ParcelNumber"] or None,
                    "ChildNumber": child["ParcelNumber"] or None,
                    "ParcelType": child["ParcelType"], "EditDate": now,
                    "EditUser": (_stored_user(child, "EditUser") or _stored_user(child, "CreatedByUser"))[:100] or None}
            for key, value in vals.items():
                i = lin.fields().indexOf(key)
                if i >= 0:
                    feat.setAttribute(i, value)
            ed.add(lin, feat)
            if not child["ParentNumber"]:
                ed.change(lyr, child.id(), "ParentNumber", parent["ParcelNumber"])
    return len(pairs)


def _stored_user(feat, name):
    if feat.fields().indexOf(name) < 0:
        return ""
    return _attr_text(feat, name)


def _curve_ends(geom):
    """Start and end of a line, or None."""
    if geom is None or geom.isEmpty():
        return None
    g = geom.constGet()
    if g is not None and hasattr(g, "startPoint") and hasattr(g, "endPoint"):
        s, e = g.startPoint(), g.endPoint()
        return (s.x(), s.y()), (e.x(), e.y())
    verts = list(QgsGeometry(g.segmentize()).vertices()) if g is not None else []
    if len(verts) < 2:
        return None
    return (verts[0].x(), verts[0].y()), (verts[-1].x(), verts[-1].y())


def _result_ok(result):
    if result is None or isinstance(result, bool):
        return result is None or result
    if isinstance(result, tuple):
        return bool(result[0])
    return bool(result)


class _NearIndex:
    """Corners and line ends within the snap tolerance.

    One spatial query per edge becomes a table scan on SQL Server once the
    line table is large. This reads the tables once, then matches in memory,
    including corners created earlier in the same import.
    """

    def __init__(self, tol):
        self.tol = tol if tol and tol > 0 else 0.0
        self.cell = self.tol if self.tol > 0 else 1e-7
        self.points = {}
        self.lines = {}

    def _bucket(self, x, y):
        c = self.cell
        return int(math.floor(x / c)), int(math.floor(y / c))

    def _close(self, p, q):
        return math.hypot(p[0] - q[0], p[1] - q[1]) <= self.tol

    def add_point(self, x, y, gid):
        self.points.setdefault(self._bucket(x, y), []).append((x, y, gid))

    def find_point(self, x, y):
        ix, iy = self._bucket(x, y)
        best, bd = None, None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for px, py, gid in self.points.get((ix + dx, iy + dy), ()):
                    d = math.hypot(px - x, py - y)
                    if d <= self.tol and (bd is None or d < bd):
                        best, bd = gid, d
        return best

    def add_line(self, a, b):
        rec = (a[0], a[1], b[0], b[1])
        seen = set()
        for p in (a, b):
            key = self._bucket(p[0], p[1])
            if key in seen:
                continue
            seen.add(key)
            self.lines.setdefault(key, []).append(rec)

    def has_line(self, a, b):
        ix, iy = self._bucket(a[0], a[1])
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for ax, ay, bx, by in self.lines.get((ix + dx, iy + dy), ()):
                    start, end = (ax, ay), (bx, by)
                    if ((self._close(start, a) and self._close(end, b))
                            or (self._close(start, b) and self._close(end, a))):
                        return True
        return False

    def load(self, layer, expr, kind, progress=None, n=0):
        req = QgsFeatureRequest().setFilterExpression(expr)
        req.setSubsetOfAttributes(["GlobalID"], layer.fields())
        seen = 0
        for f in layer.getFeatures(req):
            seen += 1
            if progress is not None and seen % 20000 == 0:
                progress(0, n)
            if kind == "point":
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                p = g.asPoint()
                self.add_point(p.x(), p.y(), f["GlobalID"])
            else:
                ends = _curve_ends(f.geometry())
                if ends:
                    self.add_line(ends[0], ends[1])


class _BulkWriter:
    """Write points and lines a few thousand at a time.

    SQL Server commits every INSERT on its own unless a transaction is open,
    and each commit flushes the log. One transaction per batch avoids that.
    """

    _AT = 2000

    def __init__(self, *layers):
        self.layers = [lyr for lyr in layers if lyr is not None]
        self.pending = {id(lyr): [] for lyr in self.layers}
        self.count = 0
        self.tr = None

    def add(self, layer, feat):
        self.pending[id(layer)].append(feat)
        self.count += 1
        if self.count >= self._AT:
            self.flush()

    def finish(self):
        self.flush()

    def abort(self):
        self.pending = {id(lyr): [] for lyr in self.layers}
        self.count = 0
        tr, self.tr = self.tr, None
        if tr is not None:
            try:
                tr.rollback()
            except Exception:
                pass

    def flush(self):
        batches = []
        for lyr in self.layers:
            feats = self.pending[id(lyr)]
            if feats:
                batches.append((lyr, feats))
        if not batches:
            return
        self._begin()
        try:
            for lyr, feats in batches:
                self._add(lyr, feats)
        except Exception:
            self.abort()
            raise
        self._commit()
        for lyr in self.layers:
            self.pending[id(lyr)] = []
        self.count = 0

    def _add(self, layer, feats):
        prov = layer.dataProvider()
        if layer.providerType() == "mssql":
            flag = getattr(getattr(QgsFeatureSink, "Flag", QgsFeatureSink), "FastInsert", None)
            result = prov.addFeatures(feats, flag) if flag is not None else prov.addFeatures(feats)
        else:
            result = prov.addFeatures(feats)
        if not _result_ok(result):
            raise RuntimeError("Could not write fabric features while deriving lines.")

    def _begin(self):
        if self.tr is not None or not self.layers:
            return
        prov = self.layers[0].providerType()
        if prov not in ("mssql", "postgres"):
            return
        try:
            # create(layers) keeps the database connection and drops the table
            # name, so Points and Parcel_Lines share one transaction.
            tr = QgsTransaction.create(self.layers)
            if tr is None:
                return
            if not _result_ok(tr.begin()):
                try:
                    tr.rollback()
                except Exception:
                    pass
                return
            self.tr = tr
        except Exception:
            self.tr = None

    def _commit(self):
        tr, self.tr = self.tr, None
        if tr is None:
            return
        done = tr.commit()
        if not _result_ok(done):
            try:
                tr.rollback()
            except Exception:
                pass
            raise RuntimeError("Could not commit fabric lines.")


def derive_lines_and_points(fabric, ptype, parcel_gids, progress=None):
    """Create the boundary lines and corner points freshly-imported parcels
    don't otherwise have, sharing a line between adjoining parcels instead
    of duplicating it - what Build and Merge both need and an import alone
    does not create. Returns (lines made, points made).

    shortcut: an imported curved ring (rare - external parcel layers are
    almost always straight-sided already) is written as a straight chord
    between its ends, with no recorded radius or arc; re-digitize that
    boundary with Traverse if it really is a curve.
    """
    if not parcel_gids:
        return 0, 0
    tol = fabric.tolerance
    seen = set()
    gids = [g for g in parcel_gids if g and not (g in seen or seen.add(g))]
    expr = '"GlobalID" IN (%s)' % ", ".join(quote(g) for g in gids)
    parcels = list(fabric.parcels.getFeatures(QgsFeatureRequest().setFilterExpression(expr)))
    n = len(parcels)
    if not n:
        return 0, 0
    if progress is not None:
        progress(0, n)
    index = _NearIndex(tol)
    index.load(fabric.points, CURRENT, "point", progress, n)
    index.load(fabric.lines, "%s AND %s" % (CURRENT, type_expr(ptype)), "line", progress, n)
    lines_made = points_made = 0
    writer = _BulkWriter(fabric.points, fabric.lines)
    try:
        for i, p in enumerate(parcels):
            if progress is not None and (i % 25 == 0 or i + 1 == n):
                progress(i + 1, n)
            record_gid = p["CreatedByRecord"]
            created_by, edited_by = _stored_user(p, "CreatedByUser"), _stored_user(p, "EditUser")
            for ext, holes in polygon_rings(p.geometry()):
                for ring in [ext] + list(holes):
                    for seg in ring:
                        a, b = seg[1], seg[-1]
                        if math.hypot(a[0] - b[0], a[1] - b[1]) <= tol:
                            continue
                        pa = index.find_point(a[0], a[1])
                        if pa is None:
                            pf = fabric.new_point_feature(a[0], a[1], record_gid, created_by, edited_by)
                            pa = pf["GlobalID"]
                            index.add_point(a[0], a[1], pa)
                            writer.add(fabric.points, pf)
                            points_made += 1
                        pb = index.find_point(b[0], b[1])
                        if pb is None:
                            pf = fabric.new_point_feature(b[0], b[1], record_gid, created_by, edited_by)
                            pb = pf["GlobalID"]
                            index.add_point(b[0], b[1], pb)
                            writer.add(fabric.points, pf)
                            points_made += 1
                        if pa == pb or index.has_line(a, b):
                            continue
                        nf = _new_line(fabric, fabric.lines, [a, b], ptype, record_gid,
                                       created_by=created_by, edited_by=edited_by)
                        index.add_line(a, b)
                        writer.add(fabric.lines, nf)
                        lines_made += 1
        writer.finish()
    except Exception:
        writer.abort()
        raise
    finally:
        fabric.points.reload()
        fabric.lines.reload()
    return lines_made, points_made


def import_parcel_layer(fabric, source, ptype, record=None, number_field=None, date_field=None,
                        split_history=True, parent_suffix=0, separator=None, digits=None,
                        book="", page="", progress=None, book_field=None, page_field=None,
                        created_field=None, edited_field=None, parcel_id_field=None):
    """Copy polygons from ``source`` in as current parcels.

    With no ``record``, each parcel id is filed as its own record (name and
    parcel id are that number). ``date_field`` is that record's recorded date.
    A blank number shares one "Existing parcels" record. ``book`` and ``page``
    are written on each new record. ``book_field`` and ``page_field`` read them
    from the source feature instead, and a blank value there falls back to the
    typed book or page. ``created_field`` and ``edited_field`` are the source
    user who created the parcel and the user who last edited it. Pass ``record``
    to file everything under a record that already exists. That path leaves the
    record's book, page, and users alone. The parcel still keeps its own users.

    ``split_history`` adds inferred links from suffixes to the parent (000 unless
    ``parent_suffix`` says otherwise); see _link_splits. ``separator`` and ``digits``
    come from fabric settings when omitted: a hyphen and 3 digits read 12345-002, and
    a hyphen still reads a Florida strap. Every imported parcel is current: nothing is
    retired. CalculatedArea is the map area. Boundary lines and corner points are traced
    from each parcel's polygon afterwards (see derive_lines_and_points), shared between
    adjoining parcels rather than duplicated, so Build, Merge and Split have something to
    work with. A strap fills the section
    fields; any other id is stored as typed. ``parcel_id_field``, when its value
    fits in 12 characters, is stored as ParcelID instead of the id taken from the
    number. A parcel with several polygons is
    one feature, also when the layer has one feature per polygon. GeoPackage and
    PostGIS keep true curves. SQL Server stores them as a straight MultiPolygon.
    A table that still allows only one polygon per parcel keeps each polygon as
    its own current parcel. A number that is all zeros is no number. A GeoPackage
    that still allows one polygon per parcel is switched (a backup is saved).
    shortcut: the same outline is imported once; stack a condo with Stacked Unit
    when the layer has more than one number on one polygon.
    """
    from qgis.core import QgsSpatialIndex, QgsWkbTypes
    from .numbering import NumberError, fields_for, is_placeholder, number_pattern, split_parts
    from .parcel_numbers import used_numbers

    if record is not None and fabric.record_by_id(record) is None:
        raise RuntimeError("That record is not in this fabric.")
    for role in (S.ROLE_PARCELS, S.ROLE_RECORDS, S.ROLE_LINES, S.ROLE_POINTS):
        S.add_missing_fields(fabric.layers[role], role)
    if not ptype:
        raise RuntimeError("Choose a parcel type.")
    rule_sep, rule_digits, _parent = number_pattern(fabric.settings)
    if separator is None:
        separator = rule_sep
    if digits is None:
        digits = rule_digits
    if source is None or not source.isValid():
        raise RuntimeError("Choose a parcel layer.")
    if source.geometryType() != QgsWkbTypes.PolygonGeometry:
        raise RuntimeError("Choose a polygon layer.")
    if not source.crs().isValid():
        raise RuntimeError("The parcel layer has no coordinate system. Set one, then import again.")
    if fabric.parcels.isEditable() or fabric.records.isEditable():
        raise RuntimeError("Save or discard fabric edits before importing parcels.")

    xf = None
    if source.crs() != fabric.parcels.crs():
        xf = QgsCoordinateTransform(source.crs(), fabric.parcels.crs(), QgsProject.instance())
    used = used_numbers(fabric)
    index, held = QgsSpatialIndex(), {}
    expr = '%s AND %s AND ("IsSeed" IS NULL OR "IsSeed" = 0)' % (type_expr(ptype), CURRENT)
    for old in fabric.parcels.getFeatures(QgsFeatureRequest().setFilterExpression(expr)):
        g = old.geometry()
        if g is None or g.isEmpty():
            continue
        held[old.id()] = QgsGeometry(g)
        index.addFeature(old)

    skipped = {"empty": 0, "multipart": 0, "too_long": 0, "duplicate": 0, "same": 0}
    batch, copied, next_id = [], 0, 0
    backup = None
    convert_error = None
    target, fields = fabric.parcels, fabric.parcels.fields()
    pending, made, by_number, stems, all_gids = [], [], {}, set(), []
    blank_gid = None
    foot = _foot(fabric)

    def area(geom):
        """Square feet, like the area Build works out from the dimensions."""
        return geom.area() / foot ** 2 if foot else None

    mine, joined = {}, {}       # number -> [GlobalID, its polygons]; numbers drawn as several features
    overlapping = 0
    separate = 0                 # extra polygons stored as their own current rows

    def allow_multipart():
        """Switch a one-polygon-per-parcel fabric. False when it can't be switched."""
        nonlocal backup, convert_error, target, fields
        if fabric.multipart:
            return True
        if convert_error is None:
            flush()
            src = fabric.parcels.source() or ""
            if fabric.parcels.providerType() == "ogr" and ".gpkg" in src.lower():
                subset = fabric.parcels.subsetString()
                try:
                    backup = fabric.convert_to_multipart()
                    if subset:
                        fabric.parcels.setSubsetString(subset)
                    target, fields = fabric.parcels, fabric.parcels.fields()
                except RuntimeError as e:
                    convert_error = str(e)
            else:
                convert_error = ("This fabric stores one polygon per parcel. "
                                 "A GeoPackage can be switched in Fabric Settings.")
        return fabric.multipart

    def remember(geom):
        nonlocal next_id
        next_id -= 1
        marker = QgsFeature()
        marker.setId(next_id)
        marker.setGeometry(geom)
        held[next_id] = geom
        index.addFeature(marker)

    def flush():
        nonlocal copied, batch
        if pending:
            _write_features(fabric.records, pending)
            pending.clear()
            fabric.records.reload()
        if batch:
            copied += _write_features(target, batch)
            batch = []

    def take_record(number, when, feat, created_by="", edited_by=""):
        nonlocal blank_gid
        if record:
            return record
        this_book = _attr_text(feat, book_field) or book
        this_page = _attr_text(feat, page_field) or page
        if number:
            gid = by_number.get(number)
            if gid:
                return gid
            gid, recfeat = _record_feature(
                fabric, number, when, number, this_book, this_page, created_by, edited_by)
            pending.append(recfeat)
            by_number[number] = gid
            made.append(gid)
            return gid
        if blank_gid is None:
            gid, recfeat = _record_feature(
                fabric, "Existing parcels", when, "", this_book, this_page, created_by, edited_by)
            pending.append(recfeat)
            blank_gid = gid
            made.append(gid)
        return blank_gid

    total = source.featureCount()
    seen = 0
    _tell(progress, 0, "Importing parcels")
    for feat in source.getFeatures():
        seen += 1
        if seen % 50 == 0 or (total and seen == total):
            if total and total > 0:
                _tell(progress, min(90.0, 90.0 * seen / float(total)),
                      "Importing parcels: %d of %d" % (seen, total))
            else:
                _tell(progress, 0, "Importing parcels: %d" % seen)
        raw = feat.geometry()
        if raw is None or raw.isEmpty():
            skipped["empty"] += 1
            continue
        prepared = QgsGeometry(raw)
        if xf is not None:
            prepared.transform(xf)
        body = prepared.get()
        if body is not None:
            body.dropZValue()
            body.dropMValue()
        parts = _surface_parts(prepared)
        # One row per polygon only when this table cannot hold several.
        groups = [[p] for p in parts] if len(parts) > 1 and not allow_multipart() else [parts]
        text = _attr_text(feat, number_field)
        try:
            vals = fields_for("" if is_placeholder(text) else text)
        except NumberError:
            skipped["too_long"] += 1
            continue
        number = vals.get("ParcelNumber")
        if number and number in used and number not in mine:
            skipped["duplicate"] += 1
            continue
        for group in groups:
            geom = _pack_parcel(target, group)
            if not _has_area(geom):
                skipped["empty" if len(group) <= 1 else "multipart"] += 1
                continue
            if _same_outline(geom, index, held):
                skipped["same"] += 1
                continue
            if number in mine:
                # another polygon of a parcel already imported
                flat = QgsGeometry(geom.constGet().segmentize())
                whole = [QgsGeometry(p.constGet().segmentize()) for p in mine[number][1]]
                if any(flat.intersection(p).area() > 0.01 * min(flat.area(), p.area()) for p in whole):
                    overlapping += 1        # not one parcel: kept as its own, for Check Fabric to report
                elif allow_multipart():
                    mine[number][1].extend(group)
                    joined[number] = mine[number]
                    remember(geom)
                    continue
                else:
                    separate += 1
            when = _as_qdate(feat[date_field]) if date_field and feat.fields().indexOf(date_field) >= 0 else None
            created_by = _attr_text(feat, created_field)[:100]
            edited_by = _attr_text(feat, edited_field)[:100]
            nf = QgsFeature(fields)
            nf.setGeometry(geom)
            base = {"GlobalID": S.new_guid(), "ParcelType": ptype, "IsSeed": 0,
                    "StatedAreaUnit": 1,
                    "CreatedByRecord": take_record(number, when, feat, created_by, edited_by),
                    "Name": text or None, "CalculatedArea": area(geom)}
            if created_by:
                base["CreatedByUser"] = created_by
            if edited_by:
                base["EditUser"] = edited_by
            base.update(vals)
            pid = _attr_text(feat, parcel_id_field)
            if pid and len(pid) <= 12:
                base["ParcelID"] = pid
            for key, value in base.items():
                i = fields.indexOf(key)
                if i >= 0:
                    nf.setAttribute(i, value)
            batch.append(nf)
            all_gids.append(base["GlobalID"])
            if number:
                used.add(number)
                slot = mine.get(number)
                if slot is None:
                    mine[number] = [base["GlobalID"], list(group)]
                else:
                    slot[1].extend(group)
                stem = split_parts(number, separator, digits)
                if stem:
                    stems.add(stem[0])
            remember(geom)
            if len(batch) >= 1000:
                flush()
    flush()
    target.reload()
    if joined:
        _tell(progress, 91, "Joining parcels with several polygons")
        shapes = {gid: parts for gid, parts in joined.values()}
        with EditSession([target], "Imported parcels") as ed:
            for gids in (list(shapes)[k:k + 400] for k in range(0, len(shapes), 400)):
                req = QgsFeatureRequest().setFilterExpression(
                    '"GlobalID" IN (%s)' % ", ".join(quote(g) for g in gids))
                for feat in list(target.getFeatures(req)):
                    geom = _join_parcel(target, shapes[feat["GlobalID"]])
                    ed.set_geometry(target, feat.id(), geom)
                    ed.change(target, feat.id(), "CalculatedArea", area(geom))
    linked = 0
    if split_history and stems:
        _tell(progress, 92, "Linking split history")
        linked = _link_splits(fabric, ptype, _suffix_int(parent_suffix), stems, separator, digits)
        target.reload()
    def on_lines(done, count):
        _tell(progress, 93 + 3.0 * done / max(count, 1), "Creating lines: %d/%d" % (done, count))

    lines_made, points_made = derive_lines_and_points(fabric, ptype, all_gids, on_lines)
    record_ids = [] if record else list(made)
    date_ids = []
    if copied and record:
        with EditSession([fabric.records], "Imported parcels") as ed:
            update_record_footprint(fabric, record, ed)
        date_ids = [record]
    elif copied and record_ids:
        _stamp_footprints(fabric, record_ids)
        date_ids = record_ids
    if date_ids:
        _tell(progress, 96, "Refreshing parcel history")
        try:
            from .history import refresh_dates
            refresh_dates(fabric, date_ids)
        except Exception as e:
            from qgis.core import Qgis, QgsMessageLog
            QgsMessageLog.logMessage("History dates not refreshed: %s" % e, "Parcel Fabric",
                                     Qgis.MessageLevel.Warning)
    result = _fill_plat(fabric, {}, progress, 97)
    _tell(progress, 100, "Import finished")
    return {"plat": result["plat"], "copied": copied, "skipped": skipped, "backup": backup, "multipart_note": convert_error,
            "records": len(record_ids), "record_ids": record_ids,
            "history_linked": linked, "joined": len(joined), "overlapping": overlapping,
            "separate": separate, "lines_made": lines_made, "points_made": points_made}


def sync_parcel_layer(fabric, source, ptype, record, number_field, progress=None,
                      dry_run=False, **import_kwargs):
    """Bring current ``ptype`` parcels in line with ``source`` by parcel number,
    without a full reimport - for running a county's own system and this fabric
    side by side while migrating.

    A number no longer in ``source`` is retired under ``record`` (which it needs,
    if anything is retired). A number not yet in the fabric is imported - see
    import_parcel_layer, which every keyword past ``progress`` is passed straight
    on to. A number in both is left exactly as it is, even if its outline in
    ``source`` has moved, and reported as changed instead: a real split, merge
    or resurvey still has to go through the proper tool, not be overwritten
    silently just because the county's own extract changed.

    ``dry_run`` only counts what would happen (``missing``, ``new`` and ``changed``)
    and changes nothing - for a preview before the real sync.
    """
    from qgis.core import QgsWkbTypes
    from .numbering import NumberError, fields_for, is_placeholder

    if source is None or not source.isValid():
        raise RuntimeError("Choose a parcel layer.")
    if source.geometryType() != QgsWkbTypes.PolygonGeometry:
        raise RuntimeError("Choose a polygon layer.")
    if not source.crs().isValid():
        raise RuntimeError("The parcel layer has no coordinate system. Set one, then import again.")

    xf = None
    if source.crs() != fabric.parcels.crs():
        xf = QgsCoordinateTransform(source.crs(), fabric.parcels.crs(), QgsProject.instance())

    _tell(progress, 0, "Sync: reading the source layer")
    source_parts = {}
    total = source.featureCount()
    seen = 0
    for feat in source.getFeatures():
        seen += 1
        if total and (seen % 200 == 0 or seen == total):
            _tell(progress, min(35.0, 35.0 * seen / float(total)),
                  "Sync: reading the source layer: %d of %d" % (seen, total))
        text = _attr_text(feat, number_field)
        if is_placeholder(text):
            continue
        try:
            number = fields_for(text).get("ParcelNumber")
        except NumberError:
            continue
        if not number:
            continue
        g = QgsGeometry(feat.geometry())
        if g is None or g.isEmpty():
            continue
        if xf is not None:
            g.transform(xf)
        source_parts.setdefault(number, []).append(g)
    source_geom = {n: (g[0] if len(g) == 1 else QgsGeometry.unaryUnion(g)) for n, g in source_parts.items()}

    _tell(progress, 40, "Sync: reading the fabric's current parcels")
    fabric_parts, fabric_fids = {}, {}
    expr = '%s AND %s AND ("IsSeed" IS NULL OR "IsSeed" = 0)' % (type_expr(ptype), CURRENT)
    for f in fabric.parcels.getFeatures(QgsFeatureRequest().setFilterExpression(expr)):
        number = f["ParcelNumber"]
        if not number:
            continue
        g = f.geometry()
        if g is None or g.isEmpty():
            continue
        fabric_fids.setdefault(number, []).append(f.id())
        fabric_parts.setdefault(number, []).append(QgsGeometry(g))
    fabric_geom = {n: (g[0] if len(g) == 1 else QgsGeometry.unaryUnion(g)) for n, g in fabric_parts.items()}

    missing = sorted(set(fabric_geom) - set(source_geom))
    changed = []
    for number in sorted(set(fabric_geom) & set(source_geom)):
        old, new = fabric_geom[number], source_geom[number]
        inter = old.intersection(new)
        same = (not inter.isEmpty()) and inter.area() >= 0.99 * max(old.area(), new.area())
        if not same:
            changed.append({"number": number, "old_area": old.area(), "new_area": new.area()})

    new = sorted(set(source_geom) - set(fabric_geom))
    if dry_run:
        _tell(progress, 100, "Sync preview finished")
        return {"missing": missing, "new": new, "changed": changed}

    retired = 0
    if missing:
        if not record:
            raise RuntimeError(
                "%d parcel(s) are no longer in the source layer and would be retired, which needs "
                "an active record (New Record..., or pick one in the Records panel)." % len(missing))
        _tell(progress, 45, "Sync: retiring %d parcel(s) no longer in the source" % len(missing))
        with EditSession([fabric.parcels], "Sync parcels") as ed:
            for number in missing:
                for fid in fabric_fids[number]:
                    ed.change(fabric.parcels, fid, "RetiredByRecord", record)
                    retired += 1

    def on_import(pct, text=None):
        _tell(progress, 50 + 0.5 * pct, text)

    added = import_parcel_layer(fabric, source, ptype, record, number_field, progress=on_import, **import_kwargs)
    _tell(progress, 100, "Sync finished")
    return dict(added, retired=retired, missing=missing, new=new, changed=changed)


def _write_features(layer, batch):
    ok, _added = layer.dataProvider().addFeatures(batch)
    if not ok:
        raise RuntimeError("Could not write features to %s." % layer.name())
    return len(batch)


def _cut(text, layer, field):
    """``text`` as far as the field holds it (fabrics made before 0.10.5 have shorter ones)."""
    length = layer.fields().field(field).length()
    return (text or "")[:length] if length > 0 else text or ""


def _copy_outlines(target, source, row_for, progress=None, label="features"):
    """Copy polygon features onto ``target``. ``row_for`` returns the attributes.

    An outline already on the target is skipped. A database that stores one
    polygon per feature skips a shape made of several polygons.
    shortcut: these layers are not fabric parcels, so Remove mistake leaves them.
    """
    from qgis.core import QgsSpatialIndex, QgsWkbTypes
    if source is None or not source.isValid():
        raise RuntimeError("Choose a polygon layer.")
    if source.geometryType() != QgsWkbTypes.PolygonGeometry:
        raise RuntimeError("Choose a polygon layer.")
    if not source.crs().isValid():
        raise RuntimeError("The layer has no coordinate system. Set one, then import again.")
    if target.isEditable():
        raise RuntimeError("Save or discard edits on %s first." % target.name())
    xf = None
    if source.crs() != target.crs():
        xf = QgsCoordinateTransform(source.crs(), target.crs(), QgsProject.instance())
    index, held = QgsSpatialIndex(), {}
    for old in target.getFeatures():
        geom = old.geometry()
        if geom is None or geom.isEmpty():
            continue
        held[old.id()] = QgsGeometry(geom)
        index.addFeature(old)
    skipped = {"empty": 0, "multipart": 0, "same": 0}
    batch, copied, next_id = [], 0, 0
    fields = target.fields()

    def remember(geom):
        nonlocal next_id
        next_id -= 1
        marker = QgsFeature()
        marker.setId(next_id)
        marker.setGeometry(geom)
        held[next_id] = geom
        index.addFeature(marker)

    total = source.featureCount()
    seen = 0
    _tell(progress, 0, "Importing %s" % label)
    for feat in source.getFeatures():
        seen += 1
        if seen % 50 == 0 or (total and seen == total):
            if total and total > 0:
                _tell(progress, min(99.0, 100.0 * seen / float(total)),
                      "Importing %s: %d of %d" % (label, seen, total))
            else:
                _tell(progress, 0, "Importing %s: %d" % (label, seen))
        raw = feat.geometry()
        if raw is None or raw.isEmpty():
            skipped["empty"] += 1
            continue
        prepared = QgsGeometry(raw)
        if xf is not None:
            prepared.transform(xf)
        body = prepared.get()
        if body is not None:
            body.dropZValue()
            body.dropMValue()
        parts = _surface_parts(prepared)
        if not QgsWkbTypes.isMultiType(target.wkbType()) and len(parts) > 1:
            # One polygon per row. Each part is its own row, with the same attributes.
            part_groups = [[p] for p in parts]
        else:
            part_groups = [parts]
        row_values = None
        for group in part_groups:
            geom = _pack_parcel(target, group)
            if not _has_area(geom):
                skipped["empty" if len(group) <= 1 else "multipart"] += 1
                continue
            if _same_outline(geom, index, held):
                skipped["same"] += 1
                continue
            if row_values is None:
                row_values = row_for(feat)
            nf = QgsFeature(fields)
            nf.setGeometry(geom)
            for key, value in row_values.items():
                i = fields.indexOf(key)
                if i >= 0:
                    nf.setAttribute(i, value)
            batch.append(nf)
            remember(geom)
            if len(batch) >= 1000:
                copied += _write_features(target, batch)
                batch = []
    if batch:
        copied += _write_features(target, batch)
    target.reload()
    return {"copied": copied, "skipped": skipped}


def _fill_plat(fabric, result, progress, start):
    """Subdivision, block and lot onto the parcels, reported with the import."""
    from .plat import fill
    text = "Filling subdivision, block and lot on parcels"
    result["plat"] = fill(fabric, lambda frac: _tell(progress, start + (99 - start) * frac, text))
    return result


def _plat_import(fabric, target, source, row, progress, label):
    if fabric.parcels.isEditable():
        raise RuntimeError("Save or discard edits on Parcels first. This import fills their lot, block and subdivision.")

    def first(pct, text=None):
        _tell(progress, pct * 0.6, text)
    result = _fill_plat(fabric, _copy_outlines(target, source, row, first, label), progress, 60)
    _tell(progress, 100, "Import finished")
    return result


def import_lots(fabric, source, lot_field=None, subdivision_field=None, block_field=None,
                parcel_field=None, progress=None):
    """Copy a county lot layer into Lots, and the lot numbers onto the parcels (see plat.fill).

    shortcut: a lot drawn with Subdivide Block stays in Parcels. This table is
    only for a lot layer that already exists.
    """
    def row(feat):
        return {"GlobalID": S.new_guid(),
                "Lot": _cut(_attr_text(feat, lot_field), fabric.lots, "Lot"),
                "Block": _cut(_attr_text(feat, block_field), fabric.lots, "Block"),
                "Subdivision": _cut(_attr_text(feat, subdivision_field), fabric.lots, "Subdivision"),
                "ParcelNumber": _cut(_attr_text(feat, parcel_field), fabric.lots, "ParcelNumber")}
    return _plat_import(fabric, fabric.lots, source, row, progress, "lots")


def import_subdivisions(fabric, source, name_field=None, progress=None):
    """Copy subdivision polygons into Subdivisions."""
    def row(feat):
        return {"GlobalID": S.new_guid(), "Name": _cut(_attr_text(feat, name_field), fabric.subdivisions, "Name")}
    return _plat_import(fabric, fabric.subdivisions, source, row, progress, "subdivisions")


def import_blocks(fabric, source, block_field=None, subdivision_field=None, progress=None):
    """Copy subdivision block polygons into Blocks. Tax parcels stay in Parcels.

    shortcut: a lot drawn with Subdivide Block stays in Parcels. This table is
    only the block outline.
    """
    def row(feat):
        return {"GlobalID": S.new_guid(),
                "Block": _cut(_attr_text(feat, block_field), fabric.blocks, "Block"),
                "Subdivision": _cut(_attr_text(feat, subdivision_field), fabric.blocks, "Subdivision")}
    return _plat_import(fabric, fabric.blocks, source, row, progress, "blocks")


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


# --------------------------------------------------------------------------
# Florida DOR tax-roll shapefile and a CAMA change list
# --------------------------------------------------------------------------
def _county_token(name):
    token = re.sub(r"[^A-Za-z0-9]", "", name or "")
    return token or "County"


def roll_names(county, when):
    """F_{county}_{mmddyyyy}_parcels.shp and the matching change-list CSV."""
    stamp = when.strftime("%m%d%Y") if hasattr(when, "strftime") else str(when)
    base = "F_%s_%s" % (_county_token(county), stamp)
    return base + "_parcels.shp", base + "_changes.csv"


def _acres(geom, crs):
    if geom is None or geom.isNull() or geom.isEmpty():
        return None
    from .geomio import unit_word
    div = 4046.8564224 if unit_word(crs) == "meters" else 43560.0
    return round(geom.area() / div, 3)


def _date_text(value):
    if value is None:
        return ""
    if hasattr(value, "isNull") and value.isNull():
        return ""
    if hasattr(value, "toString"):
        return value.toString("yyyy-MM-dd")
    return str(value)[:10]


def _field_text(feat, name, length):
    if feat.fields().indexOf(name) < 0:
        return ""
    value = feat[name]
    return "" if value is None else str(value)[:length]


def _parcel_by_gid(layer, gid):
    if not gid:
        return None
    from .fabric import quote
    req = QgsFeatureRequest().setFilterExpression('"GlobalID" = %s' % quote(gid))
    for feat in layer.getFeatures(req):
        return feat
    return None


def change_rows(fabric, since):
    """Lineage since ``since`` (yyyy-mm-dd). Undated rows, and splits only inferred on import, are left out."""
    from .lineage import INFERRED
    from .parcel_numbers import unfiltered
    since = since or ""
    parcels = unfiltered(fabric.parcels)
    crs = fabric.parcels.crs()
    rows = []
    for lin in fabric.lineage.getFeatures():
        if lin["Operation"] == INFERRED:
            continue
        rec = fabric.record_by_id(lin["RecordID"]) if lin["RecordID"] else None
        recorded = _date_text(rec["RecordedDate"]) if rec is not None else ""
        edited = (lin["EditDate"] or "")[:10]
        date = recorded or edited
        if since and (not date or date < since):
            continue
        child = _parcel_by_gid(parcels, lin["ChildID"])
        acres = _acres(child.geometry(), crs) if child is not None else None
        rows.append({
            "date": date,
            "operation": lin["Operation"] or "",
            "parent_number": lin["ParentNumber"] or "",
            "child_number": lin["ChildNumber"] or "",
            "book": (rec["Book"] if rec is not None else "") or "",
            "page": (rec["Page"] if rec is not None else "") or "",
            "acres": "" if acres is None else acres,
        })
    rows.sort(key=lambda r: (r["date"], r["parent_number"], r["child_number"], r["operation"]))
    return rows


def _write_shp(layer, path):
    """Shapefiles cannot store true curves; the caller segments them first."""
    from qgis.core import QgsCoordinateTransformContext, QgsVectorFileWriter
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "ESRI Shapefile"
    opts.fileEncoding = "UTF-8"
    # Deleting the .shp first makes this QGIS treat the path as a directory and refuse it.
    if os.path.exists(path):
        opts.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile
    result = QgsVectorFileWriter.writeAsVectorFormatV3(layer, path, QgsCoordinateTransformContext(), opts)
    if isinstance(result, tuple):
        err = result[0]
        detail = result[1] if len(result) > 1 else ""
    else:
        err, detail = result, ""
    if err != QgsVectorFileWriter.NoError:
        raise RuntimeError("Shapefile export failed: %s" % (detail or err))


def export_tax_roll(fabric, folder, county, when, since):
    """Current parcels as F_{county}_{mmddyyyy}_parcels.shp, plus a change-list CSV.

    PARCELNO, ACRES, TYPE, SUBDIV, BLOCK and LOT. Curves become straight segments.
    ``since`` is yyyy-mm-dd. Returns (shp path, csv path, parcel count, change count).
    """
    import csv
    from qgis.core import QgsFeature, QgsField, QgsVectorLayer, QgsWkbTypes
    from .parcel_numbers import unfiltered

    if not os.path.isdir(folder):
        raise RuntimeError("That folder does not exist.")
    shp_name, csv_name = roll_names(county or fabric.name, when)
    shp_path = os.path.join(folder, shp_name)
    csv_path = os.path.join(folder, csv_name)
    crs = fabric.parcels.crs()
    mem = QgsVectorLayer("MultiPolygon", "tax_roll", "memory")
    mem.setCrs(crs)
    mem.dataProvider().addAttributes([
        QgsField("PARCELNO", S._mt(S.STR), len=30),
        QgsField("ACRES", S._mt(S.DBL)),
        QgsField("TYPE", S._mt(S.STR), len=50),
        QgsField("SUBDIV", S._mt(S.STR), len=80),
        QgsField("BLOCK", S._mt(S.STR), len=64),
        QgsField("LOT", S._mt(S.STR), len=64),
    ])
    mem.updateFields()
    feats = []
    req = QgsFeatureRequest().setFilterExpression(CURRENT)
    for src in unfiltered(fabric.parcels).getFeatures(req):
        if src["IsSeed"] == 1:
            continue
        geom = src.geometry()
        if geom.isNull() or geom.isEmpty():
            continue
        raw = geom.constGet()
        if raw is not None and raw.hasCurvedSegments():
            geom = QgsGeometry(raw.segmentize())
        if not QgsWkbTypes.isMultiType(geom.wkbType()):
            geom.convertToMultiType()
        feat = QgsFeature(mem.fields())
        feat.setGeometry(geom)
        acres = _acres(geom, crs)
        feat.setAttributes([
            _field_text(src, "ParcelNumber", 30), acres if acres is not None else None,
            _field_text(src, "ParcelType", 50), _field_text(src, "Subdivision", 80),
            _field_text(src, "Block", 64), _field_text(src, "Lot", 64),
        ])
        feats.append(feat)
    if feats and not mem.dataProvider().addFeatures(feats):
        raise RuntimeError("Could not build the tax-roll layer.")
    mem.updateExtents()
    _write_shp(mem, shp_path)
    rows = change_rows(fabric, since)
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, ["date", "operation", "parent_number", "child_number",
                                     "book", "page", "acres"])
        writer.writeheader()
        writer.writerows(rows)
    return shp_path, csv_path, len(feats), len(rows)
