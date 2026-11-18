# -*- coding: utf-8 -*-
"""Phase 2 record-driven edits: split, divide, merge, copy parallel.

Nothing is deleted.  A line or parcel that changes is RETIRED by the active
record and replaced by new features created by that record, with
ParentLineID / lineage rows pointing back to what they replaced.

Shared boundaries are never duplicated: a parent's lines stay in use by the
neighbours, and only the lines a cut crosses are split in two.
"""

import math
from dataclasses import dataclass, field
from typing import List

from qgis.core import (
    QgsCircularString,
    QgsCompoundCurve,
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsLineString,
    QgsPoint,
    QgsPointXY,
    QgsRectangle,
)

from . import geomops as G
from . import lineage as L
from . import schema as S
from . import units as U
from .build import _dates, _single_curve, build_parcels, update_record_footprint
from .fabric import CURRENT, EditSession, quote, type_expr


class EditError(RuntimeError):
    """A problem the user can fix (shown as a message, not a traceback)."""


@dataclass
class EditResult:
    children: List[str] = field(default_factory=list)      # parcel numbers or names
    lines_split: int = 0
    lines_added: int = 0
    lines_retired: int = 0
    parents_retired: int = 0
    messages: List[str] = field(default_factory=list)
    issues: list = field(default_factory=list)

    def summary(self):
        parts = []
        if self.children:
            parts.append("%d new parcel(s): %s" % (len(self.children), ", ".join(self.children)))
        if self.parents_retired:
            parts.append("%d parent(s) retired" % self.parents_retired)
        if self.lines_split:
            parts.append("%d boundary line(s) split" % self.lines_split)
        if self.lines_added:
            parts.append("%d line(s) added" % self.lines_added)
        if self.lines_retired:
            parts.append("%d line(s) retired" % self.lines_retired)
        return "; ".join(parts + self.messages) or "Nothing changed."


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _xy(p):
    return (p.x(), p.y())


def _seg(geom):
    """Segmented copy of a (possibly curved) geometry."""
    return QgsGeometry(geom.constGet().segmentize())


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parts(curve):
    """('line', [pts]) or ('arc', (s, m, e)) for a fabric line's one curve."""
    c = curve
    if isinstance(c, QgsCompoundCurve):
        if c.nCurves() == 1:
            c = c.curveAt(0)
        else:
            seg = c.segmentize()
            return "line", [_xy(seg.pointN(i)) for i in range(seg.numPoints())], True
    if isinstance(c, QgsCircularString) and c.numPoints() == 3:
        return "arc", tuple(_xy(c.pointN(i)) for i in range(3)), False
    if isinstance(c, QgsLineString):
        return "line", [_xy(c.pointN(i)) for i in range(c.numPoints())], False
    seg = c.segmentize()
    return "line", [_xy(seg.pointN(i)) for i in range(seg.numPoints())], True


def _line_geom(pts):
    cc = QgsCompoundCurve()
    cc.addCurve(QgsLineString([QgsPoint(*p) for p in pts]))
    return QgsGeometry(cc)


def _arc_geom(s, m, e):
    cc = QgsCompoundCurve()
    cc.addCurve(QgsCircularString(QgsPoint(*s), QgsPoint(*m), QgsPoint(*e)))
    return QgsGeometry(cc)


def _copy_attrs(src, lyr):
    f = QgsFeature(lyr.fields())
    for name in lyr.fields().names():
        if name in src.fields().names():
            f.setAttribute(name, src[name])
    return f


def still_current(layer, fid):
    """Re-read a feature from the database (not this session's copy): has
    another editor retired it meanwhile?"""
    req = QgsFeatureRequest().setFilterFid(fid)
    for f in layer.dataProvider().getFeatures(req):
        return not f["RetiredByRecord"]
    return False


def _require_record(record):
    if not record:
        raise EditError("Set an active record first - edits belong to the record that makes them.")


def _set(f, values):
    for k, v in values.items():
        if f.fields().indexOf(k) >= 0:
            f.setAttribute(k, v)


# --------------------------------------------------------------------------
# Splitting a line at a point
# --------------------------------------------------------------------------
def split_line_features(fabric, line_feat, xy, record):
    """Two new line features replacing ``line_feat`` split at ``xy``.

    Record dimensions are prorated; direction keeps the record basis.
    Returns (feature_a, feature_b) or None when xy is at an end.
    """
    lyr = line_feat.fields()
    curve = _single_curve(line_feat.geometry())
    kind, data, flattened = _parts(curve)
    rec_dir = _num(line_feat["Direction"])
    rec_dist = _num(line_feat["Distance"])
    rec_arc = _num(line_feat["ArcLength"])
    if kind == "arc":
        s, m, e = data
        if G.arc_center(s, m, e) is None:
            kind, data = "line", [s, e]
    if kind == "arc":
        s, m, e = data
        a, b = G.split_arc(s, m, e, xy)
        whole_len = G.arc_length(s, m, e)
        la, lb = G.arc_length(*a), G.arc_length(*b)
        if la < 1e-6 or lb < 1e-6:
            return None
        whole_az = G.chord_azimuth(s, e)
        parts = [(a, la), (b, lb)]
    else:
        res = G.split_polyline(data, xy)
        if res is None:
            return None
        pa, pb, _frac = res
        whole_len = sum(math.hypot(q[0] - p[0], q[1] - p[1]) for p, q in zip(data, data[1:]))
        whole_az = G.chord_azimuth(data[0], data[-1])
        parts = [(pa, sum(math.hypot(q[0] - p[0], q[1] - p[1]) for p, q in zip(pa, pa[1:]))),
                 (pb, sum(math.hypot(q[0] - p[0], q[1] - p[1]) for p, q in zip(pb, pb[1:])))]
    # how far the record direction is turned from the drawn chord
    offset = (math.radians(rec_dir) - whole_az) if rec_dir is not None else 0.0
    out = []
    for part, plen in parts:
        f = QgsFeature(lyr)
        for name in lyr.names():
            f.setAttribute(name, line_feat[name])
        if kind == "arc":
            f.setGeometry(_arc_geom(*part))
            caz = G.chord_azimuth(part[0], part[2])
            arc = G.prorate(rec_arc, whole_len, plen)
            r = _num(line_feat["Radius"])
            chord = 2 * abs(r) * math.sin(arc / (2 * abs(r))) if r else math.hypot(
                part[2][0] - part[0][0], part[2][1] - part[0][1])
            _set(f, {"ArcLength": arc, "Distance": chord})
        else:
            f.setGeometry(_line_geom(part))
            caz = G.chord_azimuth(part[0], part[-1])
            _set(f, {"Distance": G.prorate(rec_dist, whole_len, plen)})
        _set(f, {"GlobalID": S.new_guid(), "Direction": math.degrees((caz + offset) % (2 * math.pi)),
                 "ParentLineID": line_feat["GlobalID"], "CreatedByRecord": record,
                 "RetiredByRecord": None, "COGOType": 2})
        out.append(f)
    return tuple(out)


def node_lines(fabric, ptype, xy, record, ed, res):
    """Split every current ``ptype`` line that passes through ``xy`` mid-line."""
    tol = fabric.tolerance
    lyr = fabric.lines
    pt = QgsGeometry.fromPointXY(QgsPointXY(*xy))
    rect = QgsRectangle(xy[0] - tol, xy[1] - tol, xy[0] + tol, xy[1] + tol)
    req = QgsFeatureRequest().setFilterRect(rect).setFilterExpression(
        "%s AND %s" % (CURRENT, type_expr(ptype)))
    for f in list(lyr.getFeatures(req)):
        if f["RetiredByRecord"]:          # retired earlier in this same edit
            continue
        g = f.geometry()
        if _seg(g).distance(pt) > tol:
            continue
        curve = _single_curve(g)
        if curve is None:
            continue
        s, e = _xy(curve.startPoint()), _xy(curve.endPoint())
        if math.hypot(s[0] - xy[0], s[1] - xy[1]) <= tol or math.hypot(e[0] - xy[0], e[1] - xy[1]) <= tol:
            continue            # already a corner
        pair = split_line_features(fabric, f, xy, record)
        if pair is None:
            continue
        ed.change(lyr, f.id(), "RetiredByRecord", record)
        for nf in pair:
            ed.add(lyr, nf)
        res.lines_split += 1


def _new_line(fabric, lyr, pts, ptype, record, dim_source="Computed"):
    f = QgsFeature(lyr.fields())
    f.setGeometry(_line_geom(pts))
    az, dist = U.inverse(pts[0][0], pts[0][1], pts[-1][0], pts[-1][1])
    _set(f, {"GlobalID": S.new_guid(), "ParcelType": ptype, "Direction": U.azimuth_to_esri(az),
             "Distance": dist, "COGOType": 2, "IsCOGOGround": 0, "CreatedByRecord": record,
             "QPF_DimSource": dim_source})
    return f


# --------------------------------------------------------------------------
# Cutting a parcel (Split and Divide)
# --------------------------------------------------------------------------
def clip_cuts(fabric, parcel_geom, cut_geoms):
    """The parts of the cut lines inside a parcel, ends snapped to points.

    Returns a list of point lists (one per straight course).
    """
    tol = fabric.tolerance
    seg = _seg(parcel_geom)
    ring = QgsGeometry(seg.constGet().exteriorRing().clone()) if seg.constGet().numInteriorRings() == 0 \
        else QgsGeometry(seg.constGet().boundary())
    out = []
    for g in cut_geoms:
        inside = g.intersection(seg)
        if inside.isNull() or inside.isEmpty():
            continue
        for part in inside.asGeometryCollection():
            if part.length() <= tol:
                continue
            line = part.asPolyline()
            if not line:
                continue
            pts = [(p.x(), p.y()) for p in line]
            for i in (0, -1):
                q = QgsGeometry.fromPointXY(QgsPointXY(*pts[i]))
                if ring.distance(q) > tol:
                    raise EditError("The cut line must cross the parcel from edge to edge.")
                hit = fabric.find_point(pts[i][0], pts[i][1])
                if hit is not None:
                    pts[i] = _xy(hit.geometry().asPoint())
            out.append(pts)
    if not out:
        raise EditError("The cut line doesn't cross the selected parcel.")
    return out


def _pieces(parcel_geom, cuts):
    """Polygons the parcel would be cut into (for checks and the remainder)."""
    seg = _seg(parcel_geom)
    lines = [QgsGeometry(seg.constGet().boundary())]
    lines += [QgsGeometry.fromPolylineXY([QgsPointXY(*p) for p in c]) for c in cuts]
    faces = QgsGeometry.polygonize([QgsGeometry.unaryUnion(lines)])
    out = []
    for f in faces.asGeometryCollection():
        if seg.contains(f.pointOnSurface()):
            out.append(f)
    return out


def cut_parcel(fabric, parent, cut_geoms, record, operation="Split", remainder_keeps=False,
               dim_source="Computed", remainder_point=None):
    """Cut ``parent`` (a Parcels feature) along ``cut_geoms`` (QgsGeometry lines).

    The parent is retired by ``record`` and the pieces become new parcels -
    or, with ``remainder_keeps``, the largest piece stays the parent (same
    number and record) and only the other pieces are new.  ``remainder_point``
    (QgsGeometry point) picks a different piece to keep.
    """
    _require_record(record)
    if parent["RetiredByRecord"] or not still_current(fabric.parcels, parent.id()):
        raise EditError("That parcel is already retired (another editor may have just retired it).")
    ptype = parent["ParcelType"]
    if ptype not in fabric.parcel_types:
        raise EditError("The parcel has no known parcel type.")
    res = EditResult()
    pgeom = parent.geometry()
    cuts = clip_cuts(fabric, pgeom, cut_geoms)
    pieces = _pieces(pgeom, cuts)
    if len(pieces) < 2:
        raise EditError("The cut doesn't divide the parcel into separate pieces.")

    lines = fabric.lines
    with EditSession([lines, fabric.points, fabric.parcels], operation) as ed:
        for pts in cuts:
            for xy in (pts[0], pts[-1]):
                node_lines(fabric, ptype, xy, record, ed, res)
            for a, b in zip(pts, pts[1:]):           # one fabric line per course
                if math.hypot(b[0] - a[0], b[1] - a[1]) > fabric.tolerance:
                    ed.add(lines, _new_line(fabric, lines, [a, b], ptype, record, dim_source))
                    res.lines_added += 1
        if not remainder_keeps:
            ed.change(fabric.parcels, parent.id(), "RetiredByRecord", record)
            res.parents_retired = 1

    adopt = None
    if remainder_keeps:
        big = next((g for g in pieces if remainder_point is not None and g.contains(remainder_point)),
                   None) or max(pieces, key=lambda g: g.area())
        adopt = {parent.id(): big.pointOnSurface()}
    within = _seg(pgeom).buffer(fabric.tolerance, 4)
    rep = build_parcels(fabric, ptype, record, new_record=record, number=False, adopt=adopt,
                        within=within)
    res.issues = [i for i in rep.issues if i.severity != "info"]
    kids = _by_gid(fabric, rep.new_ids)
    if not kids:
        raise EditError("The new lines were added but no parcels were built - run Check Fabric. "
                        + "; ".join(i.message for i in rep.issues))
    _number_children(fabric, parent, kids, res)
    parent = fabric.parcels.getFeature(parent.id()) if remainder_keeps else parent
    L.add(fabric, record, operation, [parent], _by_gid(fabric, rep.new_ids))
    _dates(fabric, [record])
    return res


def _by_gid(fabric, gids):
    if not gids:
        return []
    expr = '"GlobalID" IN (%s)' % ", ".join(quote(g) for g in gids)
    return list(fabric.parcels.getFeatures(QgsFeatureRequest().setFilterExpression(expr)))


def _number_children(fabric, parent, kids, res):
    """Splits under the parent's number; new numbers if the parent has none."""
    from . import parcel_numbers as PN
    ptype = parent["ParcelType"]
    if ptype not in fabric.number_types:
        res.children = [k["Name"] or "#%d" % k.id() for k in kids]
        return
    try:
        if parent["ParcelNumber"]:
            PN.number_splits(fabric, str(parent["ParcelNumber"]), kids)
        elif fabric.settings.get("number_on_build", "1") == "1":
            for f, why in PN.number_new(fabric, kids):
                res.messages.append("no number for one piece: %s" % why)
    except (PN.NumberingNotSetUp, ValueError) as e:
        res.messages.append("pieces not numbered: %s" % e)
    fields = ["ParcelNumber", "Section", "Township", "Range", "ParcelID", "ParcelSeq", "SplitNo", "ParentNumber"]
    with EditSession([fabric.parcels], "Number pieces") as ed:
        for k in kids:
            for name in fields:
                ed.change(fabric.parcels, k.id(), name, k[name])
    res.children = [k["ParcelNumber"] or k["Name"] or "#%d" % k.id() for k in kids]


def divide_cuts(parcel_geom, azimuth, mode="equal_area", count=2, area=None, flip=False):
    """Cut lines that divide a parcel with lines parallel to ``azimuth``.

    ``mode``: "equal_area" (``count`` parts), "equal_width" (``count`` parts),
    "area" (pieces of ``area`` sq ft each, the rest is the remainder) or
    "width" (lots ``area`` feet wide, measured across the cut lines).
    ``flip`` measures from the other side.  Returns QgsGeometry lines.
    """
    seg = _seg(parcel_geom)
    c = seg.centroid().asPoint()
    deg = math.degrees(azimuth)
    rot = QgsGeometry(seg)
    rot.rotate(-deg, c)           # cut direction now points north
    if flip:
        rot.rotate(180, c)
    box = rot.boundingBox()
    x0, x1, y0, y1 = box.xMinimum(), box.xMaximum(), box.yMinimum() - 10, box.yMaximum() + 10
    total = rot.area()

    def left_area(x):
        if x <= x0:
            return 0.0
        if x >= x1:
            return total
        return rot.intersection(QgsGeometry.fromRect(QgsRectangle(x0 - 1, y0, x, y1))).area()

    def solve(target):
        lo, hi = x0, x1
        for _ in range(80):
            mid = (lo + hi) / 2
            if left_area(mid) < target:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    if mode == "equal_width":
        xs = [x0 + (x1 - x0) * k / count for k in range(1, count)]
    elif mode == "width":
        # lots of a set frontage ``area`` (here: the width in map units) from the start side
        if not area or area <= 0:
            raise EditError("Enter the lot width.")
        n = int((x1 - x0) // area)
        if abs((x1 - x0) - n * area) < 1e-6:
            n -= 1
        if n < 1:
            raise EditError("The block is narrower than one lot.")
        xs = [x0 + area * k for k in range(1, n + 1)]
    elif mode == "area":
        if not area or area <= 0:
            raise EditError("Enter the area of each piece.")
        n = int(total // area)
        if n < 1:
            raise EditError("The parcel is smaller than one piece of that area.")
        if abs(total - n * area) < 1e-6 * total:
            n -= 1                 # last piece would be exactly the remainder
        xs = [solve(area * k) for k in range(1, n + 1)]
    else:
        if count < 2:
            raise EditError("Divide into at least 2 parts.")
        xs = [solve(total * k / count) for k in range(1, count)]
    out = []
    for x in xs:
        g = QgsGeometry.fromPolylineXY([QgsPointXY(x, y0), QgsPointXY(x, y1)])
        if flip:
            g.rotate(-180, c)
        g.rotate(deg, c)
        out.append(g)
    return out


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------
def merge_parcels(fabric, parents, record, keep_number=None):
    """Merge adjacent current parcels of one type into one new parcel.

    ``keep_number``: a parent's number for the merged parcel, or None for a
    new number (if numbering is set up).
    """
    _require_record(record)
    if len(parents) < 2:
        raise EditError("Select at least two parcels to merge.")
    types = {p["ParcelType"] for p in parents}
    if len(types) != 1:
        raise EditError("Only parcels of one parcel type can be merged.")
    if any(p["RetiredByRecord"] or not still_current(fabric.parcels, p.id()) for p in parents):
        raise EditError("A selected parcel is already retired (another editor may have just retired it).")
    ptype = types.pop()
    tol = fabric.tolerance
    union = QgsGeometry.unaryUnion([_seg(p.geometry()).buffer(tol / 2, 2) for p in parents]).buffer(-tol / 2, 2)
    if union.isMultipart() and len(union.asGeometryCollection()) > 1:
        raise EditError("The selected parcels don't touch - they can't become one parcel.")
    boundary = QgsGeometry(union.constGet().boundary())
    res = EditResult()
    lines = fabric.lines
    req = QgsFeatureRequest().setFilterRect(union.boundingBox()).setFilterExpression(
        "%s AND %s" % (CURRENT, type_expr(ptype)))
    with EditSession([lines, fabric.parcels, fabric.points], "Merge") as ed:
        for f in list(lines.getFeatures(req)):
            seg = _seg(f.geometry())
            mid = seg.interpolate(seg.length() / 2)
            if union.contains(mid) and boundary.distance(mid) > tol:
                ed.change(lines, f.id(), "RetiredByRecord", record)
                res.lines_retired += 1
        for p in parents:
            ed.change(fabric.parcels, p.id(), "RetiredByRecord", record)
            res.parents_retired += 1
        retire_orphan_points(fabric, union, record, ed, ptype)
    rep = build_parcels(fabric, ptype, None, union.boundingBox().buffered(tol * 10), new_record=record,
                        number=False, within=union.buffer(tol, 4))
    res.issues = [i for i in rep.issues if i.severity != "info"]
    kids = _by_gid(fabric, rep.new_ids)
    if len(kids) != 1:
        raise EditError("Merge retired the inner lines but built %d parcels instead of 1. "
                        "Run Check Fabric." % len(kids))
    kid = kids[0]
    from . import parcel_numbers as PN
    values = {}
    if keep_number:
        values = PN.number_fields(keep_number)
        donor = next((p for p in parents if p["ParcelNumber"] == keep_number), None)
        if donor is not None and donor["Name"]:
            values["Name"] = donor["Name"]
    elif ptype in fabric.number_types:
        try:
            PN.number_new(fabric, [kid])
            values = {k: kid[k] for k in ("ParcelNumber", "Section", "Township", "Range", "ParcelID", "ParcelSeq", "SplitNo")}
        except PN.NumberingNotSetUp as e:
            res.messages.append("merged parcel not numbered: %s" % e)
    if values:
        with EditSession([fabric.parcels], "Number merged parcel") as ed:
            for k, v in values.items():
                ed.change(fabric.parcels, kid.id(), k, v)
    with EditSession([fabric.records], "Record footprint") as ed:
        update_record_footprint(fabric, record, ed)
    kid = fabric.parcels.getFeature(kid.id())
    res.children = [kid["ParcelNumber"] or kid["Name"] or "#%d" % kid.id()]
    L.add(fabric, record, "Merge", parents, [kid])
    _dates(fabric, [record])
    return res


def retire_orphan_points(fabric, area, record, ed, ptype=None):
    """Retire current points in ``area`` that no current line ends on."""
    tol = fabric.tolerance
    box = area.boundingBox().buffered(tol)
    ends = []
    for lyr in (fabric.lines, fabric.connections):
        for f in lyr.getFeatures(QgsFeatureRequest().setFilterRect(box).setFilterExpression(CURRENT)):
            if f["RetiredByRecord"]:
                continue
            curve = _single_curve(f.geometry())
            if curve is not None:
                ends += [_xy(curve.startPoint()), _xy(curve.endPoint())]
    n = 0
    for p in fabric.points.getFeatures(QgsFeatureRequest().setFilterRect(box).setFilterExpression(CURRENT)):
        q = _xy(p.geometry().asPoint())
        if not area.buffer(tol, 2).contains(p.geometry()):
            continue
        if not any(abs(q[0] - e[0]) <= tol and abs(q[1] - e[1]) <= tol for e in ends):
            ed.change(fabric.points, p.id(), "RetiredByRecord", record)
            n += 1
    return n


# --------------------------------------------------------------------------
# Copy Parallel
# --------------------------------------------------------------------------
def copy_parallel(fabric, line_feats, distance, side, target, record, dim_source="Computed"):
    """Copies of lines offset by ``distance`` to the ``side`` ("right"/"left")
    of each line's direction, written to ``target`` (parcel type or
    "Connections").  Returns an EditResult."""
    _require_record(record)
    if distance <= 0:
        raise EditError("Enter an offset distance.")
    sign = 1.0 if side == "right" else -1.0
    lyr = fabric.line_layer(target)
    ptype = None if target == "Connections" else target
    res = EditResult()
    new_lines, new_pts, seen = [], [], []
    for f in line_feats:
        curve = _single_curve(f.geometry())
        if curve is None:
            continue
        kind, data, _flat = _parts(curve)
        nf = _copy_attrs(f, lyr)
        rec_dir = _num(f["Direction"])
        if kind == "arc" and G.arc_center(*data) is not None:
            s, m, e = G.offset_arc(*data, sign * distance)
            nf.setGeometry(_arc_geom(s, m, e))
            _c, r, _a0, sw = G.arc_sweep(s, m, e)
            whole = G.chord_azimuth(data[0], data[2])
            caz = G.chord_azimuth(s, e)
            old_r = _num(f["Radius"]) or 0.0
            _set(nf, {"Radius": math.copysign(r, old_r if old_r else -sw),
                      "ArcLength": r * abs(sw), "Distance": math.hypot(e[0] - s[0], e[1] - s[1])})
            ends = (s, e)
        else:
            pts = data if kind == "line" else [data[0], data[2]]
            if len(pts) != 2:
                raise EditError("Copy Parallel works on single-course lines; split multi-vertex lines first.")
            a, b = G.offset_segment(pts[0], pts[1], sign * distance)
            nf.setGeometry(_line_geom([a, b]))
            whole = caz = G.chord_azimuth(pts[0], pts[1])
            _set(nf, {"Distance": math.hypot(b[0] - a[0], b[1] - a[1])})
            ends = (a, b)
        offset = (math.radians(rec_dir) - whole) if rec_dir is not None else 0.0
        _set(nf, {"GlobalID": S.new_guid(), "ParcelType": ptype, "CreatedByRecord": record,
                  "RetiredByRecord": None, "ParentLineID": None, "COGOType": 2,
                  "QPF_DimSource": dim_source,
                  "Direction": math.degrees((caz + offset) % (2 * math.pi))})
        new_lines.append(nf)
        for xy in ends:
            if fabric.find_point(*xy) is None and not any(
                    abs(xy[0] - q[0]) <= fabric.tolerance and abs(xy[1] - q[1]) <= fabric.tolerance for q in seen):
                seen.append(xy)
                new_pts.append(fabric.new_point_feature(xy[0], xy[1], record))
    if not new_lines:
        raise EditError("Select one or more fabric lines to copy.")
    with EditSession([lyr, fabric.points], "Copy Parallel") as ed:
        for nf in new_lines:
            ed.add(lyr, nf)
        for p in new_pts:
            ed.add(fabric.points, p)
    res.lines_added = len(new_lines)
    return res
