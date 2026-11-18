# -*- coding: utf-8 -*-
"""Build parcels: closed loops of fabric lines become polygons and points.

Like Pro's Build, it:
  * finds every closed loop of current lines of one parcel type that touches
    the active record's lines (or the given map extent),
  * creates a parcel polygon with TRUE CURVES assembled from the lines
    themselves (not the segmented outline), so parcels and lines coincide,
  * fills a seed polygon (IsSeed = 1) that sits inside the loop, so a name
    typed on the seed carries over,
  * stores MiscloseDistance / MiscloseRatio / CalculatedArea computed from
    the COGO dimensions of the loop,
  * adds a point at every line end that has none, and
  * updates the record's footprint and ParcelCount.
"""

import math
from dataclasses import dataclass, field
from typing import List

from qgis.core import (
    QgsCompoundCurve,
    QgsCurvePolygon,
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointXY,
    QgsRectangle,
)

from . import schema as S
from .cogo import loop_misclose
from .fabric import CURRENT, EditSession, Fabric, type_expr
from .topo import chain_ring


@dataclass
class Issue:
    severity: str          # "error" | "warning" | "info"
    message: str
    x: float = None
    y: float = None
    layer_id: str = None
    fid: int = None


@dataclass
class BuildReport:
    created: int = 0
    new_ids: List[str] = field(default_factory=list)    # GlobalIDs of new parcels
    rebuilt: int = 0
    seeds_used: int = 0
    points_added: int = 0
    issues: List[Issue] = field(default_factory=list)

    def summary(self):
        parts = ["%d new parcel(s)" % self.created]
        if self.rebuilt:
            parts.append("%d rebuilt" % self.rebuilt)
        if self.seeds_used:
            parts.append("%d from seeds" % self.seeds_used)
        if self.points_added:
            parts.append("%d point(s) added" % self.points_added)
        errs = sum(1 for i in self.issues if i.severity == "error")
        warns = sum(1 for i in self.issues if i.severity == "warning")
        if errs or warns:
            parts.append("%d problem(s), %d warning(s)" % (errs, warns))
        return ", ".join(parts)


def _single_curve(geom):
    """The one curve of a line feature (CompoundCurve, LineString...)."""
    g = geom.constGet()
    if g is None:
        return None
    if hasattr(g, "numGeometries"):           # multi-part
        if g.numGeometries() != 1:
            return None
        g = g.geometryN(0)
    return g


def _ends(curve):
    a, b = curve.startPoint(), curve.endPoint()
    return (a.x(), a.y()), (b.x(), b.y())


def _components(curve):
    """Simple curves of a curve (a compound curve is split up)."""
    if isinstance(curve, QgsCompoundCurve):
        return [curve.curveAt(i) for i in range(curve.nCurves())]
    return [curve]


def _ring_from(lines, chain):
    cc = QgsCompoundCurve()
    for fid, rev in chain:
        parts = _components(lines[fid]["curve"])
        if rev:
            parts = [p.reversed() for p in reversed(parts)]
        else:
            parts = [p.clone() for p in parts]
        for p in parts:
            cc.addCurve(p)
    if not cc.isClosed():
        try:
            cc.close()
        except AttributeError:
            pass
    return cc


def build_parcels(fabric: Fabric, ptype, record_gid=None, extent: QgsRectangle = None,
                  new_record=None, number=True, adopt=None, within=None):
    """Build parcels of one type.

    ``new_record`` stamps new parcels (default: the build's record).
    ``number`` False skips automatic numbering (splits number themselves).
    ``adopt`` maps a parcel fid to a point: the loop containing that point
    takes over the parcel (a split remainder that stays the parent).
    ``within`` (QgsGeometry) limits the build to loops inside it.
    """
    rep = BuildReport()
    adopt = dict(adopt or {})
    adopt_ids = set(adopt)
    line_lyr = fabric.lines
    parcel_lyr = fabric.parcels
    tol = fabric.tolerance
    current = "%s AND %s" % (CURRENT, type_expr(ptype))

    # ---- scope: which lines does this build look at -----------------------
    if record_gid:
        req = QgsFeatureRequest().setFilterExpression(
            '"CreatedByRecord" = \'%s\' AND %s' % (record_gid.replace("'", "''"), current))
    elif extent is not None:
        req = QgsFeatureRequest().setFilterRect(extent).setFilterExpression(current)
    else:
        raise ValueError("Build needs an active record or a map extent")
    scope_ids = set()
    bbox = None
    for f in line_lyr.getFeatures(req):
        scope_ids.add(f.id())
        b = f.geometry().boundingBox()
        bbox = b if bbox is None else _union_rect(bbox, b)
    if not scope_ids:
        rep.issues.append(Issue("info", "No current %s lines in this %s to build from."
                                % (ptype, "record" if record_gid else "map view")))
        return rep
    bbox.grow(max(tol * 10, bbox.width() * 0.05, bbox.height() * 0.05))

    # ---- candidate lines (neighbours can close a loop) ---------------------
    lines = {}
    req = QgsFeatureRequest().setFilterRect(bbox).setFilterExpression(current)
    for f in line_lyr.getFeatures(req):
        curve = _single_curve(f.geometry())
        if curve is None or curve.isEmpty():
            rep.issues.append(Issue("warning", "Line has empty or multi-part geometry",
                                    layer_id=line_lyr.id(), fid=f.id()))
            continue
        seg = QgsGeometry(curve.segmentize())
        s, e = _ends(curve)
        lines[f.id()] = {"f": f, "curve": curve, "seg": seg, "start": s, "end": e,
                         "len": seg.length()}

    if not lines:
        return rep

    # ---- faces --------------------------------------------------------------
    noded = QgsGeometry.unaryUnion([d["seg"] for d in lines.values()])
    faces = QgsGeometry.polygonize([noded])
    if faces.isNull():
        rep.issues.append(Issue("warning", "No closed loops found among the %s lines." % ptype))
        return rep

    parcel_fields = parcel_lyr.fields()
    new_feats, updates, points_needed = [], [], []

    for face in faces.asGeometryCollection():
        if face.area() <= tol * tol:
            continue
        fpoly = face.constGet()
        ring = QgsGeometry(fpoly.exteriorRing().clone())
        fbox = face.boundingBox()
        fbox.grow(tol * 2)
        bnd = []
        for fid, d in lines.items():
            if not d["seg"].boundingBox().intersects(fbox):
                continue
            if _on(ring, d["start"], tol) and _on(ring, d["end"], tol):
                mid = d["seg"].interpolate(d["len"] / 2.0).asPoint()
                if _on(ring, (mid.x(), mid.y()), tol):
                    bnd.append(fid)
        if not any(fid in scope_ids for fid in bnd):
            continue                       # loop not touched by this build
        pos = face.pointOnSurface().asPoint()
        if within is not None and not within.contains(QgsGeometry.fromPointXY(pos)):
            continue
        chain = chain_ring([(fid, lines[fid]["start"], lines[fid]["end"]) for fid in bnd], tol)
        ring_len = ring.length()
        used_len = sum(lines[fid]["len"] for fid in bnd)
        if chain is None or abs(ring_len - used_len) > max(tol * len(bnd) * 2, ring_len * 1e-6):
            rep.issues.append(Issue(
                "error", "Lines around this area don't form one clean loop - a line may "
                "cross another without a shared corner point.", pos.x(), pos.y()))
            continue

        poly = QgsCurvePolygon()
        poly.setExteriorRing(_ring_from(lines, chain))
        for i in range(fpoly.numInteriorRings()):
            poly.addInteriorRing(fpoly.interiorRing(i).clone())
        geom = QgsGeometry(poly)

        ms = loop_misclose([(Fabric.course_from_feature(lines[fid]["f"]), rev) for fid, rev in chain])
        if ms is None:
            mdist = mratio = carea = None
            rep.issues.append(Issue("warning", "Parcel built, but some of its lines have no COGO "
                                    "dimensions, so misclose and calculated area are blank.",
                                    pos.x(), pos.y()))
        else:
            dx, dy, per, carea = ms
            mdist = math.hypot(dx, dy)
            mratio = (per / mdist) if mdist > 1e-9 else None
            if mratio is not None and mratio < fabric.warn_ratio:
                rep.issues.append(Issue("warning", "Misclose 1:%d (%.3f) is worse than 1:%d."
                                        % (mratio, mdist, fabric.warn_ratio), pos.x(), pos.y()))
        vals = {"MiscloseDistance": mdist, "MiscloseRatio": mratio, "CalculatedArea": carea}

        # ---- match an existing parcel or seed --------------------------------
        target, kind = _match(parcel_lyr, face, pos, tol, current)
        if kind == "overlap" and target.id() in adopt and face.contains(adopt[target.id()]):
            kind = "same"
            adopt.pop(target.id())         # only one loop takes the parent's place
        if target is not None and kind in ("seed", "same"):
            vals["IsSeed"] = 0
            if not target["CreatedByRecord"] and record_gid:
                vals["CreatedByRecord"] = record_gid
            updates.append((target.id(), geom, vals))
            if kind == "seed":
                rep.seeds_used += 1
            else:
                rep.rebuilt += 1
        else:
            if kind == "overlap" and target.id() not in adopt_ids:
                rep.issues.append(Issue(
                    "warning", "New parcel overlaps current parcel '%s' from another record. "
                    "Retire the parent once the split is done." % (target["Name"] or target.id()),
                    pos.x(), pos.y(), parcel_lyr.id(), target.id()))
            nf = QgsFeature(parcel_fields)
            nf.setGeometry(geom)
            base = {"GlobalID": S.new_guid(), "ParcelType": ptype, "IsSeed": 0, "StatedAreaUnit": 1,
                    "CreatedByRecord": new_record or record_gid or _record_of(lines, chain)}
            base.update(vals)
            for k, v in base.items():
                i = parcel_fields.indexOf(k)
                if i >= 0:
                    nf.setAttribute(i, v)
            new_feats.append(nf)
            rep.new_ids.append(nf["GlobalID"])
            rep.created += 1
        for fid, _rev in chain:
            points_needed += [lines[fid]["start"], lines[fid]["end"]]

    # ---- parcel numbers for brand-new parcels ---------------------------------
    if number and new_feats and fabric.settings.get("number_on_build", "1") == "1" \
            and ptype in fabric.number_types:
        from .parcel_numbers import NumberingNotSetUp, number_new
        try:
            for f, why in number_new(fabric, new_feats):
                p = f.geometry().pointOnSurface().asPoint()
                rep.issues.append(Issue("warning", "No parcel number: %s" % why, p.x(), p.y()))
        except NumberingNotSetUp as e:
            rep.issues.append(Issue("info", "Parcels weren't numbered. %s" % e))

    # ---- points -------------------------------------------------------------
    new_pts, seen = [], []
    for xy in points_needed:
        if any(abs(s[0] - xy[0]) <= tol and abs(s[1] - xy[1]) <= tol for s in seen):
            continue
        seen.append(xy)
        if fabric.find_point(xy[0], xy[1]) is None:
            new_pts.append(fabric.new_point_feature(xy[0], xy[1], record_gid))
    rep.points_added = len(new_pts)

    # ---- write everything as one edit ----------------------------------------
    if not (new_feats or updates or new_pts):
        return rep
    with EditSession([parcel_lyr, fabric.points, fabric.records], "Build parcels") as ed:
        for fid, geom, vals in updates:
            ed.set_geometry(parcel_lyr, fid, geom)
            for k, v in vals.items():
                ed.change(parcel_lyr, fid, k, v)
        for nf in new_feats:
            ed.add(parcel_lyr, nf)
        for p in new_pts:
            ed.add(fabric.points, p)
        if record_gid:
            update_record_footprint(fabric, record_gid, ed)
    _dates(fabric, [record_gid, new_record])
    return rep


def _dates(fabric, records):
    """Keep history dates current; never let this break an edit."""
    try:
        from .history import refresh_dates
        refresh_dates(fabric, [r for r in records if r])
    except Exception as e:      # history dates are a convenience; report, don't fail the edit
        from qgis.core import Qgis, QgsMessageLog
        QgsMessageLog.logMessage("History dates not refreshed: %s" % e, "Parcel Fabric", Qgis.MessageLevel.Warning)


def update_record_footprint(fabric, record_gid, ed):
    """Record polygon = union of the parcels it created; ParcelCount."""
    rec = fabric.record_by_id(record_gid)
    if rec is None:
        return
    geoms, count = [], 0
    expr = '"CreatedByRecord" = \'%s\' AND %s' % (record_gid.replace("'", "''"), CURRENT)
    for f in fabric.parcels.getFeatures(QgsFeatureRequest().setFilterExpression(expr)):
        if f["IsSeed"] == 1:
            continue
        g = f.geometry()
        if g.isNull():
            continue
        geoms.append(QgsGeometry(g.constGet().segmentize()))
        count += 1
    if geoms:
        u = QgsGeometry.unaryUnion(geoms)
        u.convertToMultiType()
        ed.set_geometry(fabric.records, rec.id(), u)
    ed.change(fabric.records, rec.id(), "ParcelCount", count)


def _on(ring_geom, xy, tol):
    return ring_geom.distance(QgsGeometry.fromPointXY(QgsPointXY(xy[0], xy[1]))) <= tol


def _union_rect(a, b):
    r = QgsRectangle(a)
    r.combineExtentWith(b)
    return r


def _record_of(lines, chain):
    """Most common CreatedByRecord of a loop's lines (extent builds)."""
    counts = {}
    for fid, _ in chain:
        r = lines[fid]["f"]["CreatedByRecord"]
        if r:
            counts[r] = counts.get(r, 0) + 1
    return max(counts, key=counts.get) if counts else None


def _match(parcel_lyr, face, pos, tol, current):
    """Find the parcel this loop should fill.

    Returns (feature, kind): kind is "seed" (a seed inside the loop),
    "same" (a current parcel with the same footprint - rebuild it),
    "overlap" (a different current parcel covers the loop) or None.
    """
    req = QgsFeatureRequest().setFilterRect(face.boundingBox()).setFilterExpression(current)
    seed = same = overlap = None
    fa = face.area()
    for f in parcel_lyr.getFeatures(req):
        g = f.geometry()
        if g.isNull():
            continue
        if f["IsSeed"] == 1:
            if face.contains(g.pointOnSurface()):
                seed = seed or f
            continue
        seg = QgsGeometry(g.constGet().segmentize())
        if not seg.intersects(face):
            continue
        inter = seg.intersection(face).area()
        if fa > 0 and abs(seg.area() - fa) <= max(fa * 0.001, tol) and inter >= fa * 0.999:
            same = same or f
        elif inter > fa * 0.5:
            overlap = overlap or f
    if same is not None:
        return same, "same"
    if seed is not None:
        return seed, "seed"
    if overlap is not None:
        return overlap, "overlap"
    return None, None
