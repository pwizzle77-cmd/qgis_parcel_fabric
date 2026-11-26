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
from .geomio import parts_of


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
    keys = lyr.primaryKeyAttributes()      # a copied fid collides with the original when saved
    for i, name in enumerate(lyr.fields().names()):
        if i not in keys and name in src.fields().names():
            f.setAttribute(i, src[name])
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


def _require_current(fabric, parent):
    if parent["RetiredByRecord"] or not still_current(fabric.parcels, parent.id()):
        raise EditError("That parcel is already retired (another editor may have just retired it).")
    if parent["ParcelType"] not in fabric.parcel_types:
        raise EditError("The parcel has no known parcel type.")


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
        f = _copy_attrs(line_feat, fabric.lines)
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
        curve = _single_curve(g)
        if curve is None:
            continue
        # a point exactly on a true arc is off its segmented copy, and the other way round
        if min(_seg(g).distance(pt), math.sqrt(curve.closestSegment(QgsPoint(*xy))[0])) > tol:
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


def _new_line(fabric, lyr, pts, ptype, record, dim_source="Computed", created_by="", edited_by=""):
    f = QgsFeature(lyr.fields())
    f.setGeometry(_line_geom(pts))
    az, dist = U.inverse(pts[0][0], pts[0][1], pts[-1][0], pts[-1][1])
    vals = {"GlobalID": S.new_guid(), "ParcelType": ptype, "Direction": U.azimuth_to_esri(az),
            "Distance": dist, "COGOType": 2, "IsCOGOGround": 0, "CreatedByRecord": record,
            "QPF_DimSource": dim_source}
    if created_by:
        vals["CreatedByUser"] = str(created_by)[:100]
    if edited_by:
        vals["EditUser"] = str(edited_by)[:100]
    _set(f, vals)
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
    ring = QgsGeometry(seg.constGet().boundary())
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
    number and record, with any polygon the cut doesn't cross) and only the
    other pieces are new.  ``remainder_point``
    (QgsGeometry point) picks a different piece to keep.
    """
    _require_record(record)
    _require_current(fabric, parent)
    ptype = parent["ParcelType"]
    pgeom = parent.geometry()
    cuts = clip_cuts(fabric, pgeom, cut_geoms)
    pieces = _pieces(pgeom, cuts)
    polys = [_seg(p) for p in parts_of(pgeom)]
    if len(pieces) <= len(polys):
        raise EditError("The cut doesn't divide the parcel into separate pieces.")
    keep = None
    if remainder_keeps:
        whole = [p for p in polys if not any(
            QgsGeometry.fromPolylineXY([QgsPointXY(*q) for q in c]).intersection(p).length() > fabric.tolerance
            for c in cuts)]                 # polygons the cut doesn't cross
        cut_up = [g for g in pieces if not any(p.contains(g.pointOnSurface()) for p in whole)]
        best = next((g for g in cut_up if remainder_point is not None and g.contains(remainder_point)),
                    None) or max(cut_up, key=lambda g: g.area())
        keep = [g.pointOnSurface() for g in [best] + whole]
    new_lines = [_new_line(fabric, fabric.lines, [a, b], ptype, record, dim_source)    # one line per course
                 for pts in cuts for a, b in zip(pts, pts[1:])
                 if math.hypot(b[0] - a[0], b[1] - a[1]) > fabric.tolerance]
    nodes = [xy for pts in cuts for xy in (pts[0], pts[-1])]
    return replace_parcel(fabric, parent, nodes, new_lines, record, operation, keep)


def replace_parcel(fabric, parent, nodes, new_lines, record, operation, keep=None, finish_lines=None,
                   groups=None):
    """Add ``new_lines`` inside ``parent`` and build the pieces, as one edit.

    Boundary lines are split at ``nodes``.  The pieces at the ``keep`` points stay the
    parent; without any the parent is retired.  The pieces at each of the ``groups``'
    points become one new parcel.  ``finish_lines(ed)`` may adjust lines before the
    parcels are built.
    """
    res = EditResult()
    ptype = parent["ParcelType"]
    lines = fabric.lines
    # if anything fails, the retired parent and split lines come back too (parcels are saved first)
    with EditSession([fabric.parcels, lines, fabric.points, fabric.records, fabric.lineage], operation) as ed:
        for xy in nodes:
            node_lines(fabric, ptype, xy, record, ed, res)
        for nf in new_lines:
            ed.add(lines, nf)
        res.lines_added = len(new_lines)
        if finish_lines is not None:
            finish_lines(ed)
        adopt = None
        if keep is None:
            ed.change(fabric.parcels, parent.id(), "RetiredByRecord", record)
            res.parents_retired = 1
        else:
            adopt = {parent.id(): keep}
        within = _seg(parent.geometry()).buffer(fabric.tolerance, 4)
        rep = build_parcels(fabric, ptype, record, new_record=record, number=False, adopt=adopt,
                            within=within, groups=groups) if new_lines else build_parcels(
            fabric, ptype, None, within.boundingBox(), new_record=record, number=False, adopt=adopt,
            within=within, groups=groups)
        res.issues = [i for i in rep.issues if i.severity != "info"]
        kids = _by_gid(fabric, rep.new_ids)
        if not kids or any(i.severity == "error" for i in rep.issues):
            raise EditError("Nothing was saved, because the pieces couldn't be built. "
                            + "; ".join(i.message for i in rep.issues))
        _number_children(fabric, parent, kids, res)
        if keep is not None:
            parent = fabric.parcels.getFeature(parent.id())
        L.add(fabric, record, operation, [parent], _by_gid(fabric, rep.new_ids))
        update_record_footprint(fabric, record, ed)
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


def stamp_lots(fabric, parent_gid, subdivision, block, start_lot, front):
    """Write subdivision, block, lot, width and depth on a Subdivide's children.

    Lot numbers follow the same northwest-to-southeast order as the preview.
    Width and depth are the rotated box the preview already measures (frontage
    east-west after rotating by -frontage + 90).
    """
    if not parent_gid:
        return 0
    if fabric.parcels.fields().indexOf("Lot") < 0:
        S.add_missing_fields(fabric.parcels, S.ROLE_PARCELS)
    expr = '"ParentID" = %s AND "Operation" = %s' % (quote(parent_gid), quote("Subdivide"))
    rows = list(fabric.lineage.getFeatures(QgsFeatureRequest().setFilterExpression(expr)))
    kids = _by_gid(fabric, [r["ChildID"] for r in rows if r["ChildID"]])
    kids = [k for k in kids if k.isValid() and not k["RetiredByRecord"] and not k.geometry().isNull()]

    def order(f):
        p = f.geometry().centroid().asPoint()
        return (-round(p.y(), 1), round(p.x(), 1))

    kids.sort(key=order)
    if not kids:
        return 0
    with EditSession([fabric.parcels], "Lot numbers") as ed:
        for i, k in enumerate(kids):
            g = QgsGeometry(k.geometry())
            g.rotate(-math.degrees(front) + 90, g.centroid().asPoint())
            box = g.boundingBox()
            ed.change(fabric.parcels, k.id(), "Subdivision", subdivision or None)
            ed.change(fabric.parcels, k.id(), "Block", block or None)
            ed.change(fabric.parcels, k.id(), "Lot", str(int(start_lot) + i))
            ed.change(fabric.parcels, k.id(), "LotWidth", box.width())
            ed.change(fabric.parcels, k.id(), "LotDepth", box.height())
    return len(kids)


def add_stacked_unit(fabric, parent, number, kind, record):
    """Another current parcel on the parent's polygon. No new boundary lines.

    Build will not adopt this copy when the land parcel is rebuilt. Building the
    same loop again does not create a second land parcel out of the copy.
    """
    _require_record(record)
    _require_current(fabric, parent)
    kind = (kind or "").strip().lower()
    if kind not in ("condo", "common"):
        raise EditError("Choose condo or common area.")
    if parent["IsSeed"] == 1:
        raise EditError("Build the parcel before stacking a unit on it.")
    if parent.fields().indexOf("QPF_Stack") >= 0 and parent["QPF_Stack"]:
        raise EditError("Stack the unit on the land parcel, not on another unit.")
    from . import numbering as N
    from . import parcel_numbers as PN
    try:
        fields = N.fields_for(number)
    except N.NumberError as e:
        raise EditError(str(e))
    if not fields["ParcelNumber"]:
        raise EditError("Enter the unit's parcel number.")
    if fabric.parcels.fields().indexOf("QPF_Stack") < 0:
        S.add_missing_fields(fabric.parcels, S.ROLE_PARCELS)
    if fields["ParcelNumber"] in PN.used_numbers(fabric):
        raise EditError("Parcel number %s is already used." % fields["ParcelNumber"])
    lyr = fabric.parcels
    feat = _copy_attrs(parent, lyr)
    feat.setGeometry(QgsGeometry(parent.geometry()))
    vals = dict(fields)
    vals.update({"QPF_Stack": kind, "ParentNumber": parent["ParcelNumber"] or None,
                 "CreatedByRecord": record, "RetiredByRecord": None, "GlobalID": S.new_guid(),
                 "IsSeed": 0, "Name": fields["ParcelNumber"]})
    _set(feat, vals)
    with EditSession([lyr], "Stacked unit") as ed:
        ed.add(lyr, feat)
    L.add(fabric, record, "Stack", [parent], [feat])
    res = EditResult(children=[fields["ParcelNumber"]])
    res.messages.append("Stacked %s on the same polygon; no new lines." % kind)
    return res


def divide_cuts(parcel_geom, azimuth, mode="equal_area", count=2, area=None, flip=False):
    """Cut lines that divide a parcel with lines parallel to ``azimuth``.

    ``mode``: "equal_area" (``count`` parts), "equal_width" (``count`` parts),
    "area" (pieces of ``area`` sq ft each, the rest is the remainder) or
    "width" (lots ``area`` feet wide, measured across the cut lines).
    ``flip`` measures from the other side.  Returns QgsGeometry lines.
    """
    if len(parts_of(parcel_geom)) > 1:
        raise EditError("This parcel has several polygons. Separate it first, then divide the one you want.")
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
def merge_parcels(fabric, parents, record, keep_number=None, apart=False):
    """Merge current parcels of one type into one new parcel.

    ``keep_number``: a parent's number for the merged parcel, or None for a
    new number (if numbering is set up).  ``apart`` allows parcels that don't
    touch: they become one multi-polygon parcel.
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
    bad = [p["ParcelNumber"] or p["Name"] or "#%d" % p.id()
          for p in parents if not _seg(p.geometry()).isGeosValid()]
    if bad:
        raise EditError("%s has an invalid shape (self-intersecting or similar) - run Check Fabric, "
                        "or fix it before merging." % ", ".join(bad))
    union = QgsGeometry.unaryUnion([_seg(p.geometry()).buffer(tol / 2, 2) for p in parents]).buffer(-tol / 2, 2)
    polys = union.asGeometryCollection() if union.isMultipart() else [union]
    gap = 0.0
    if len(polys) > 1:
        gap = min(polys[i].distance(polys[j])
                 for i in range(len(polys)) for j in range(i + 1, len(polys)))
    if gap > tol:
        if not apart:
            raise EditError(
                "The selected parcels don't all touch (the closest two are %.3f map units apart - "
                "the tolerance is %.3f). If they should be contiguous, close that gap first (Align "
                "Edge, or Check Fabric to see exactly where). Tick \"Join parcels that don't touch\" "
                "instead to make one multi-polygon parcel out of genuinely separate pieces."
                % (gap, tol))
        if not fabric.multipart:
            raise EditError("This fabric stores one polygon per parcel. Turn on multi-polygon parcels in "
                            "Fabric Settings first.")
    boundary = QgsGeometry(union.constGet().boundary())
    res = EditResult()
    lines = fabric.lines
    req = QgsFeatureRequest().setFilterRect(union.boundingBox()).setFilterExpression(
        "%s AND %s" % (CURRENT, type_expr(ptype)))
    from . import parcel_numbers as PN
    # one edit: if the merged parcel can't be built, the parents and inner lines come back
    with EditSession([fabric.parcels, lines, fabric.points, fabric.records, fabric.lineage], "Merge") as ed:
        covered = []
        for f in list(lines.getFeatures(req)):
            if f["RetiredByRecord"]:
                continue
            seg = _seg(f.geometry())
            if seg.isEmpty() or seg.length() <= 0:
                continue
            mid = seg.interpolate(seg.length() / 2)
            if union.contains(mid) and boundary.distance(mid) > tol:
                ed.change(lines, f.id(), "RetiredByRecord", record)
                res.lines_retired += 1
            else:
                covered.append((f.id(), seg))
        # The inner parcel's own boundary can lie along the outer line without sharing
        # a corner. Build cannot close that loop, so drop a line the others already cover.
        band = boundary.buffer(tol, 2)
        again = True
        while again:
            again = False
            for i, (fid, seg) in enumerate(covered):
                if not band.contains(seg):
                    continue
                others = [s for j, (_fid, s) in enumerate(covered) if j != i]
                if others and QgsGeometry.unaryUnion(others).buffer(tol, 2).contains(seg):
                    ed.change(lines, fid, "RetiredByRecord", record)
                    res.lines_retired += 1
                    del covered[i]
                    again = True
                    break
        for p in parents:
            ed.change(fabric.parcels, p.id(), "RetiredByRecord", record)
            res.parents_retired += 1
        retire_orphan_points(fabric, union, record, ed, ptype)
        rep = build_parcels(fabric, ptype, None, union.boundingBox().buffered(tol * 10), new_record=record,
                            number=False, within=union.buffer(tol, 4),
                            groups=[[p.pointOnSurface() for p in polys]])
        res.issues = [i for i in rep.issues if i.severity != "info"]
        kids = _by_gid(fabric, rep.new_ids)
        if len(kids) != 1 or len(parts_of(kids[0].geometry())) != len(polys):
            raise EditError("Nothing was saved, because the merged parcel couldn't be built. "
                            + "; ".join(i.message for i in rep.issues))
        kid = kids[0]
        values = {}
        if keep_number:
            values = PN.number_fields(keep_number)
            donor = next((p for p in parents if p["ParcelNumber"] == keep_number), None)
            if donor is not None and donor["Name"]:
                values["Name"] = donor["Name"]
        elif ptype in fabric.number_types:
            try:
                PN.number_new(fabric, [kid])
                values = {k: kid[k] for k in ("ParcelNumber", "Section", "Township", "Range", "ParcelID",
                                              "ParcelSeq", "SplitNo")}
            except PN.NumberingNotSetUp as e:
                res.messages.append("merged parcel not numbered: %s" % e)
        for k, v in values.items():
            ed.change(fabric.parcels, kid.id(), k, v)
        update_record_footprint(fabric, record, ed)
        kid = fabric.parcels.getFeature(kid.id())
        res.children = [kid["ParcelNumber"] or kid["Name"] or "%d polygon(s)" % len(polys)]
        L.add(fabric, record, "Merge", parents, [kid])
    _dates(fabric, [record])
    return res


def separate_parcel(fabric, parent, record, remainder_keeps=True):
    """Make each polygon of a multi-polygon parcel its own parcel (the largest may keep the number)."""
    _require_record(record)
    _require_current(fabric, parent)
    polys = sorted(parts_of(parent.geometry()), key=lambda p: -_seg(p).area())
    if len(polys) < 2:
        raise EditError("That parcel is a single polygon.")
    return replace_parcel(fabric, parent, [], [], record, "Separate",
                          [_seg(polys[0]).pointOnSurface()] if remainder_keeps else None)


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
