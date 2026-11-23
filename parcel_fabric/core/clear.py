# -*- coding: utf-8 -*-
"""Clear a record: delete everything it created and bring back what it retired.

The one place features are deleted instead of retired: it removes a mistake, so
it refuses when a later record builds on this one, and it keeps boundary lines
and corners that other records' parcels still use.
"""

import math
from dataclasses import dataclass, field

from qgis.core import QgsFeatureRequest, QgsGeometry, QgsPointXY, QgsRectangle

from . import lineage as L
from . import schema as S
from .build import _dates, _single_curve, build_parcels, update_record_footprint
from .edits import EditError
from .fabric import EditSession, quote
from .parcel_numbers import unfiltered

ROLES = (S.ROLE_PARCELS, S.ROLE_LINES, S.ROLE_CONNECTIONS, S.ROLE_POINTS)
LABELS = {S.ROLE_PARCELS: "parcel(s)", S.ROLE_LINES: "line(s)", S.ROLE_CONNECTIONS: "connection(s)",
          S.ROLE_POINTS: "point(s)"}


@dataclass
class ClearPlan:
    record: str
    layers: dict = field(default_factory=dict)      # role -> layer that also sees retired features
    delete: dict = field(default_factory=dict)      # role -> {fid: feature}
    restore: dict = field(default_factory=dict)     # role -> {fid: feature}
    lineage: list = field(default_factory=list)     # lineage fids
    remainders: list = field(default_factory=list)  # (parcel, area to rebuild it in)
    kept: int = 0

    def counts(self, what):
        return ", ".join("%d %s" % (len(what[r]), LABELS[r]) for r in ROLES if what[r])


def plan_clear(fabric, record):
    """What clearing ``record`` would do. Raises EditError when it can't be done safely."""
    editing = [lx.name() for lx in fabric.edit_layers() if lx.isEditable()]
    if editing:
        raise EditError("Save or discard the unsaved edits on %s first." % ", ".join(editing))
    p = ClearPlan(record)
    later = set()
    mine = QgsFeatureRequest().setFilterExpression(
        '"CreatedByRecord" = %s OR "RetiredByRecord" = %s' % (quote(record), quote(record)))
    for role in ROLES:
        lyr = p.layers[role] = unfiltered(fabric.layers[role])
        p.delete[role], p.restore[role] = {}, {}
        for f in lyr.getFeatures(mine):
            retired = f["RetiredByRecord"] or None
            if f["CreatedByRecord"] != record:
                p.restore[role][f.id()] = f
            elif retired and retired != record:
                later.add(retired)
            else:
                p.delete[role][f.id()] = f

    gone = {f["GlobalID"] for f in p.delete[S.ROLE_PARCELS].values()}
    back = {f["GlobalID"] for f in p.restore[S.ROLE_PARCELS].values()}
    remainders, rows = set(), []
    for row in unfiltered(fabric.lineage).getFeatures():      # past any layer filter
        rows.append(row)
        if row["RecordID"] == record:
            p.lineage.append(row.id())
            if row["ParentID"] not in gone | back:
                remainders.add(row["ParentID"])
        elif row["ParentID"] in gone:
            later.add(row["RecordID"])
    later |= {r["RecordID"] for r in rows if r["ParentID"] in remainders and r["RecordID"] != record
              and r.id() > max(p.lineage)}
    if later:
        raise EditError("Later records changed what this record created: %s. Clear those first."
                        % ", ".join(sorted(L.record_name(fabric, g) or str(g) for g in later)))
    children = {r["ParentID"]: set() for r in rows if r["ParentID"] in remainders}
    for r in rows:
        if r["RecordID"] == record and r["ParentID"] in children:
            children[r["ParentID"]].add(r["ChildID"])
    for gid, kids in children.items():
        for f in p.layers[S.ROLE_PARCELS].getFeatures(
                QgsFeatureRequest().setFilterExpression('"GlobalID" = %s' % quote(gid))):
            parts = [f] + [k for k in p.delete[S.ROLE_PARCELS].values() if k["GlobalID"] in kids]
            p.remainders.append((f, QgsGeometry.unaryUnion(
                [QgsGeometry(k.geometry().constGet().segmentize()).buffer(fabric.tolerance, 4) for k in parts])))

    _keep_shared(fabric, p)
    return p


def _near(p, role, rect):
    """Lines that will be current after the clear."""
    for f in p.layers[role].getFeatures(QgsFeatureRequest().setFilterRect(rect)):
        if f.id() in p.restore[role] or (f.id() not in p.delete[role] and not f["RetiredByRecord"]):
            yield f


def _box(xy, tol):
    return QgsRectangle(xy[0] - tol, xy[1] - tol, xy[0] + tol, xy[1] + tol)


def _line_points(f):
    """(segmented geometry, start, middle, end) of a line, or None."""
    curve = _single_curve(f.geometry())
    if curve is None or curve.isEmpty():
        return None
    seg = QgsGeometry(curve.segmentize())
    m = seg.interpolate(seg.length() / 2).asPoint()
    s, e = curve.startPoint(), curve.endPoint()
    return seg, (s.x(), s.y()), (m.x(), m.y()), (e.x(), e.y())


def _keep_shared(fabric, p):
    """Keep lines that bound another record's parcel, and points other lines end on."""
    tol = fabric.tolerance

    def on(geom, pts):
        return all(geom.distance(QgsGeometry.fromPointXY(QgsPointXY(*q))) <= tol for q in pts)

    regrown = {f.id() for f, _area in p.remainders}     # rebuilt afterwards, so they need none of these lines
    lines = p.delete[S.ROLE_LINES]
    shared = []
    for fid, f in lines.items():
        info = _line_points(f)
        if info is None or f["RetiredByRecord"]:
            continue
        pts = info[1:]
        box = _box(pts[1], tol)
        bounds = any(
            g.id() not in regrown and g["ParcelType"] == f["ParcelType"] and g["IsSeed"] != 1
            and on(QgsGeometry(g.geometry().constGet().segmentize().boundary()), pts)
            for g in _near(p, S.ROLE_PARCELS, box) if not g.geometry().isNull())
        if not bounds:
            continue
        others = (_line_points(o) for o in _near(p, S.ROLE_LINES, box) if o["ParcelType"] == f["ParcelType"])
        if not any(o is not None and on(o[0], pts) for o in others):
            shared.append(fid)          # no other line covers it: the neighbour needs this one
    for fid in shared:
        del lines[fid]

    def used(xy):
        for role in (S.ROLE_LINES, S.ROLE_CONNECTIONS):
            for f in _near(p, role, _box(xy, tol)):
                info = _line_points(f)
                if info and any(math.hypot(q[0] - xy[0], q[1] - xy[1]) <= tol for q in (info[1], info[3])):
                    return True
        return False

    points = p.delete[S.ROLE_POINTS]
    corners = []
    for fid, f in points.items():
        q = f.geometry().asPoint()
        if used((q.x(), q.y())):
            corners.append(fid)
    for fid in corners:
        del points[fid]
    p.kept = len(shared) + len(corners)


def apply_clear(fabric, p):
    """Carry out a plan. The record itself stays, empty."""
    with EditSession(list(p.layers.values()), "Clear record") as ed:
        for role, lyr in p.layers.items():
            if p.delete[role] and not lyr.deleteFeatures(list(p.delete[role])):
                raise RuntimeError("Could not delete features from %s" % lyr.name())
            for fid in p.restore[role]:
                ed.change(lyr, fid, "RetiredByRecord", None)
    for role in ROLES:
        fabric.layers[role].reload()
    for f, area in p.remainders:        # a split's remainder grows back to its outline before the split
        inside = QgsGeometry(f.geometry().constGet().segmentize()).pointOnSurface().asPoint()
        rep = build_parcels(fabric, f["ParcelType"], None, area.boundingBox(), number=False,
                            adopt={f.id(): inside}, within=area.buffer(fabric.tolerance, 4))
        if not rep.rebuilt:
            raise RuntimeError("The record was cleared, but parcel %s couldn't be rebuilt to its outline before "
                               "the split. Run Check Fabric." % (f["ParcelNumber"] or f["Name"] or f.id()))
    owners = {f["CreatedByRecord"] for f in list(p.restore[S.ROLE_PARCELS].values()) + [r[0] for r in p.remainders] if f["CreatedByRecord"]}
    rows = unfiltered(fabric.lineage)
    with EditSession([rows, fabric.records], "Clear record") as ed:
        if p.lineage and not rows.deleteFeatures(p.lineage):
            raise RuntimeError("Could not delete lineage rows")
        rec = fabric.record_by_id(p.record)
        if rec is not None:
            ed.set_geometry(fabric.records, rec.id(), QgsGeometry())
        for gid in owners | {p.record}:
            update_record_footprint(fabric, gid, ed)
    L.clear_cache()
    _dates(fabric, list(owners))
