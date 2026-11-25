# -*- coding: utf-8 -*-
"""Build parcels: closed loops of fabric lines become polygons and points.

Like Pro's Build, it:
  * finds every closed loop of current lines of one parcel type that touches
    the active record's lines (or the given map extent),
  * keeps a multi-polygon parcel together: rebuilding one of its loops
    rebuilds the parcel,
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
    QgsMultiSurface,
    QgsPointXY,
    QgsPolygon,
    QgsRectangle,
    QgsVertexId,
)

from . import schema as S
from .cogo import loop_misclose
from .fabric import CURRENT, EditSession, Fabric, type_expr
from .geomio import parts_of
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
    rebuilt_ids: List[str] = field(default_factory=list)
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


@dataclass
class Loop:
    """One closed loop: a parcel, or one polygon of a multi-polygon parcel."""
    face: QgsGeometry       # straight-sided copy
    poly: QgsCurvePolygon
    pos: object
    chain: list
    misclose: tuple         # (distance, ratio), None without COGO
    area: float             # by COGO


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
    pieces = []
    for fid, rev in chain:
        parts = _components(lines[fid]["curve"])
        if rev:
            parts = [p.reversed() for p in reversed(parts)]
        else:
            parts = [p.clone() for p in parts]
        pieces += parts
    # lines meet within the tolerance, not always exactly; a ring has to be exact to be saved
    for prev, p in zip([pieces[-1]] + pieces, pieces):
        p.moveVertex(QgsVertexId(0, 0, 0), prev.endPoint())
    for p in pieces:
        cc.addCurve(p)
    if not cc.isClosed():
        try:
            cc.close()
        except AttributeError:
            pass
    return cc


def build_parcels(fabric: Fabric, ptype, record_gid=None, extent: QgsRectangle = None,
                  new_record=None, number=True, adopt=None, within=None, session=None,
                  groups=None, outline=None):
    """Build parcels of one type.

    ``new_record`` stamps new parcels (default: the build's record).
    ``number`` False skips automatic numbering (splits number themselves).
    ``adopt`` maps a parcel fid to a point or points: the loops containing
    them take over the parcel (a split remainder that stays the parent).
    ``groups`` are lists of points: the loops containing one list's points
    become one new multi-polygon parcel.
    ``within`` (QgsGeometry) limits the build to loops inside it.
    ``outline`` (a polygon) adds the lines on it to the build, whoever created
    them: a parcel can be closed by its neighbours' lines.
    """
    rep = BuildReport()
    adopt = {fid: list(p) if isinstance(p, (list, tuple)) else [p] for fid, p in (adopt or {}).items()}
    adopt_ids = set(adopt)
    groups = list(groups or [])
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
    if outline is not None:
        edge = QgsGeometry(outline.constGet().boundary())
        for f in line_lyr.getFeatures(QgsFeatureRequest().setFilterRect(
                outline.boundingBox().buffered(tol)).setFilterExpression(current)):
            curve = _single_curve(f.geometry())
            if curve is None or f.id() in scope_ids:
                continue
            seg = QgsGeometry(curve.segmentize())
            mid = seg.interpolate(seg.length() / 2.0).asPoint()
            if all(_on(edge, xy, tol) for xy in _ends(curve) + ((mid.x(), mid.y()),)):
                scope_ids.add(f.id())
                b = f.geometry().boundingBox()
                bbox = b if bbox is None else _union_rect(bbox, b)
    if not scope_ids:
        rep.issues.append(Issue("info", "No current %s lines in this %s to build from."
                                % (ptype, "record" if record_gid else "map view")))
        return rep
    bbox.grow(max(tol * 10, bbox.width() * 0.05, bbox.height() * 0.05))
    boxes = [bbox]
    if within is not None:
        boxes += [p.boundingBox().buffered(tol * 10) for p in parts_of(within)]  # may be far from the new lines
    multi = {}          # multi-polygon parcels near the build: fid -> their polygons
    if fabric.multipart:
        for f in parcel_lyr.getFeatures(QgsFeatureRequest().setFilterRect(bbox).setFilterExpression(current)):
            polys = [] if f.geometry().isNull() or f.id() in adopt_ids else parts_of(f.geometry())
            if len(polys) > 1 and not f["RetiredByRecord"]:
                multi[f.id()] = polys
                boxes += [p.boundingBox().buffered(tol * 10) for p in polys]

    # ---- candidate lines (neighbours can close a loop) ---------------------
    candidates = {}
    for box in boxes:
        for f in line_lyr.getFeatures(QgsFeatureRequest().setFilterRect(box).setFilterExpression(current)):
            candidates[f.id()] = f
    lines = {}
    for f in candidates.values():
        if within is not None and not within.intersects(QgsGeometry(f.geometry().constGet().segmentize())):
            continue
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
    _snap_ends(lines, tol)
    noded = QgsGeometry.unaryUnion([d["seg"] for d in lines.values()])
    faces = QgsGeometry.polygonize([noded])
    if faces.isNull():
        rep.issues.append(Issue("warning", "No closed loops found among the %s lines." % ptype))
        return rep

    parcel_fields = parcel_lyr.fields()
    new_feats, updates, points_needed = [], [], []
    found, spare = [], {}       # loops this build makes; untouched loops of multi-polygon parcels

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
        owner = None
        if within is None and not any(fid in scope_ids for fid in bnd):
            owner = next((fid for fid, polys in multi.items() if any(_same(face, p, tol) for p in polys)), None)
            if owner is None:
                continue                   # loop not touched by this build
        pos = face.pointOnSurface().asPoint()
        if within is not None and not within.contains(QgsGeometry.fromPointXY(pos)):
            continue
        chain = chain_ring([(fid, lines[fid]["start"], lines[fid]["end"]) for fid in bnd], tol)
        ring_len = ring.length()
        used_len = sum(lines[fid]["len"] for fid in bnd)
        if chain is None or abs(ring_len - used_len) > max(tol * len(bnd) * 2, ring_len * 1e-6):
            if owner is not None:
                continue                   # that polygon stays as it is
            rep.issues.append(Issue(
                "error", "Lines around this area don't form one clean loop - a line may "
                "cross another without a shared corner point.", pos.x(), pos.y()))
            continue

        poly = QgsCurvePolygon()
        poly.setExteriorRing(_ring_from(lines, chain))
        for i in range(fpoly.numInteriorRings()):
            poly.addInteriorRing(fpoly.interiorRing(i).clone())

        ms = loop_misclose([(Fabric.course_from_feature(lines[fid]["f"]), rev) for fid, rev in chain])
        misclose = carea = None
        if ms is None:
            if owner is None:
                rep.issues.append(Issue("warning", "Parcel built, but some of its lines have no COGO "
                                        "dimensions, so misclose and calculated area are blank.",
                                        pos.x(), pos.y()))
        else:
            dx, dy, per, carea = ms
            whole = QgsGeometry(QgsPolygon(fpoly.exteriorRing().clone())).area()
            carea *= face.area() / whole      # holes aren't part of the parcel
            mdist = math.hypot(dx, dy)
            mratio = (per / mdist) if mdist > 1e-9 else None
            misclose = (mdist, mratio)
            if owner is None and mratio is not None and mratio < fabric.warn_ratio:
                rep.issues.append(Issue("warning", "Misclose 1:%d (%.3f) is worse than 1:%d."
                                        % (mratio, mdist, fabric.warn_ratio), pos.x(), pos.y()))
        loop = Loop(face, poly, pos, chain, misclose, carea)
        if owner is not None:
            spare.setdefault(owner, []).append(loop)
        else:
            found.append(loop)

    # ---- which parcel does each loop belong to --------------------------------
    old, new = {}, {}           # parcel fid -> [feature, kind, loops]; key -> loops
    for k, loop in enumerate(found):
        target = kind = None
        for fid, pts in adopt.items():
            rest = [p for p in pts if not loop.face.contains(p)]
            if len(rest) < len(pts):
                adopt[fid] = rest          # only the loops asked for take the parent's place
                target, kind = parcel_lyr.getFeature(fid), "same"
                break
        g = next((i for i, pts in enumerate(groups) if any(loop.face.contains(p) for p in pts)), None)
        if target is None and g is not None:
            new.setdefault(("group", g), []).append(loop)
            continue
        if target is None:
            target, kind = _match(parcel_lyr, loop.face, loop.pos, tol, current)
            if kind == "same" and target.id() in adopt_ids:
                kind = "overlap"           # a polygon leaving the parcel that keeps the others
        if target is not None and kind in ("seed", "same"):
            old.setdefault(target.id(), [target, kind, []])[2].append(loop)
            continue
        if kind == "overlap" and target.id() not in adopt_ids:
            rep.issues.append(Issue(
                "warning", "New parcel overlaps current parcel '%s' from another record. "
                "Retire the parent once the split is done." % (target["Name"] or target.id()),
                loop.pos.x(), loop.pos.y(), parcel_lyr.id(), target.id()))
        new[("loop", k)] = [loop]

    for fid, (target, kind, loops) in old.items():
        polys, kept = [lp.poly for lp in loops], []
        if fid in multi:
            loops = loops + spare.get(fid, [])
            kept = [p for p in multi[fid] if not any(_same(lp.face, p, tol) for lp in loops)]
            polys = [lp.poly for lp in loops] + [p.constGet() for p in kept]
            if kept:
                rep.issues.append(Issue("warning", "%d polygon(s) of parcel '%s' don't close, so they stay as they "
                                        "were and its misclose and area are blank." % (
                                            len(kept), target["ParcelNumber"] or target["Name"] or fid),
                                        loops[0].pos.x(), loops[0].pos.y(), parcel_lyr.id(), fid))
        vals = _cogo_values(loops, not kept)
        vals["IsSeed"] = 0
        if not target["CreatedByRecord"] and record_gid:
            vals["CreatedByRecord"] = record_gid
        updates.append((fid, _parcel_geometry(fabric, polys), vals))
        if kind == "seed":
            rep.seeds_used += 1
        else:
            rep.rebuilt += 1
        rep.rebuilt_ids.append(target["GlobalID"])
    for loops in new.values():
        nf = QgsFeature(parcel_fields)
        nf.setGeometry(_parcel_geometry(fabric, [lp.poly for lp in loops]))
        base = {"GlobalID": S.new_guid(), "ParcelType": ptype, "IsSeed": 0, "StatedAreaUnit": 1,
                "CreatedByRecord": new_record or record_gid or _record_of(lines, loops[0].chain)}
        base.update(_cogo_values(loops))
        for k, v in base.items():
            i = parcel_fields.indexOf(k)
            if i >= 0:
                nf.setAttribute(i, v)
        new_feats.append(nf)
        rep.new_ids.append(nf["GlobalID"])
        rep.created += 1
    for loop in found:
        for fid, _rev in loop.chain:
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

    def write(ed):
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

    if session is not None:
        # Caller saves or discards. History dates commit the parcel layer, so they wait.
        write(session)
        return rep
    with EditSession([parcel_lyr, fabric.points, fabric.records], "Build parcels") as ed:
        write(ed)
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
    """Record polygon = union of the parcels it created; ParcelCount.

    Also fills the record's own Parcel ID from the parcel it created, when
    the record was filed before a parcel id existed (a new plat or deed)
    and it still has none: only when every parcel it created shares one id,
    so a multi-lot plat is left for the roll ID to be typed on each lot.
    """
    rec = fabric.record_by_id(record_gid)
    if rec is None:
        return
    geoms, count, ids = [], 0, []
    expr = '"CreatedByRecord" = \'%s\' AND %s' % (record_gid.replace("'", "''"), CURRENT)
    for f in fabric.parcels.getFeatures(QgsFeatureRequest().setFilterExpression(expr)):
        if f["IsSeed"] == 1:
            continue
        g = f.geometry()
        if not g.isNull():
            geoms.append(QgsGeometry(g.constGet().segmentize()))
        count += 1
        pid = f["ParcelID"] or f["ParcelNumber"]
        if pid:
            ids.append(str(pid))
    if geoms:
        u = QgsGeometry.unaryUnion(geoms)
        u.convertToMultiType()
        ed.set_geometry(fabric.records, rec.id(), u)
    ed.change(fabric.records, rec.id(), "ParcelCount", count)
    if rec.fields().indexOf("QPF_ParcelID") >= 0 and not rec["QPF_ParcelID"] and len(set(ids)) == 1:
        ed.change(fabric.records, rec.id(), "QPF_ParcelID", ids[0][:30])


def _snap_ends(lines, tol):
    """Make line ends that are within ``tol`` of each other share one coordinate.

    Real legal descriptions misclose by a few thousandths of a foot; polygonize
    needs exactly coincident vertices, so without this a nearly-closed loop
    produces no parcel at all.
    """
    reps = []

    def rep_for(xy):
        for r in reps:
            if abs(r[0] - xy[0]) <= tol and abs(r[1] - xy[1]) <= tol:
                return r
        reps.append(xy)
        return xy

    for d in lines.values():
        ls = d["seg"].constGet().clone()
        n = ls.numPoints()
        if n < 2:
            continue
        for i, key in ((0, "start"), (n - 1, "end")):
            x, y = rep_for((ls.xAt(i), ls.yAt(i)))
            if (x, y) != (ls.xAt(i), ls.yAt(i)):
                ls.setXAt(i, x)
                ls.setYAt(i, y)
        d["seg"] = QgsGeometry(ls)


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


def _same(face, poly, tol):
    """Is this loop that polygon (a new footprint within 0.1%)?"""
    seg = QgsGeometry(poly.constGet().segmentize())
    fa = face.area()
    return fa > 0 and abs(seg.area() - fa) <= max(fa * 0.001, tol) and seg.intersects(face) \
        and seg.intersection(face).area() >= fa * 0.999


def _cogo_values(loops, complete=True):
    """Misclose of the worst loop and the total area by COGO (blank when any loop has none)."""
    if not complete or any(lp.misclose is None for lp in loops):
        return {"MiscloseDistance": None, "MiscloseRatio": None, "CalculatedArea": None}
    worst = min((lp.misclose for lp in loops), key=lambda m: math.inf if m[1] is None else m[1])
    return {"MiscloseDistance": worst[0], "MiscloseRatio": worst[1], "CalculatedArea": sum(lp.area for lp in loops)}


def _parcel_geometry(fabric, polys):
    """A Parcels geometry from curve polygons."""
    if not fabric.multipart:
        if len(polys) != 1:
            raise ValueError("This fabric stores one polygon per parcel. Turn on multi-polygon parcels in "
                             "Fabric Settings.")
        return QgsGeometry(polys[0].clone())
    out = QgsMultiSurface()
    for p in polys:
        out.addGeometry(p.clone())
    return QgsGeometry(out)


def _match(parcel_lyr, face, pos, tol, current):
    """Find the parcel this loop should fill.

    Returns (feature, kind): kind is "seed" (a seed inside the loop),
    "same" (a current parcel with that footprint, or that polygon - rebuild it),
    "overlap" (a different current parcel covers the loop) or None.
    """
    req = QgsFeatureRequest().setFilterRect(face.boundingBox()).setFilterExpression(current)
    seed = same = overlap = None
    fa = face.area()
    stack_i = parcel_lyr.fields().indexOf("QPF_Stack")
    for f in parcel_lyr.getFeatures(req):
        g = f.geometry()
        if g.isNull():
            continue
        # A condo or common-area copy sits on the land parcel. Rebuilding the land
        # parcel must not adopt that copy or warn that it overlaps.
        if stack_i >= 0 and f[stack_i]:
            continue
        if f["IsSeed"] == 1:
            if face.contains(g.pointOnSurface()):
                seed = seed or f
            continue
        seg = QgsGeometry(g.constGet().segmentize())
        if not seg.intersects(face):
            continue
        if any(_same(face, p, tol) for p in parts_of(g)):
            same = same or f
        elif seg.intersection(face).area() > fa * 0.5:
            overlap = overlap or f
    if same is not None:
        return same, "same"
    if seed is not None:
        return seed, "seed"
    if overlap is not None:
        return overlap, "overlap"
    return None, None
