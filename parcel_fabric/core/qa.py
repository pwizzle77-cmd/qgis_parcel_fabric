# -*- coding: utf-8 -*-
"""Check Fabric: data quality checks on current features."""

from qgis.core import QgsFeatureRequest

from .build import Issue, _ends, _single_curve
from .fabric import CURRENT, Fabric, type_expr
from .topo import dangles

CHECKS = [
    ("dangles", "Line ends that touch no other line (open loops)"),
    ("no_cogo", "Lines without COGO dimensions"),
    ("no_point", "Line ends without a fabric point"),
    ("misclose", "Parcels with a misclose worse than the fabric limit"),
    ("stated_area", "Parcels whose calculated area differs from the stated area by over 1%"),
    ("unbuilt_seeds", "Seeds that were never built into a parcel"),
    ("no_record", "Features that don't belong to any record"),
    ("no_type", "Parcels or parcel lines with no (or an unknown) parcel type"),
    ("numbers", "Parcels without a number, or with a number used twice"),
]


def run_checks(fabric: Fabric, ptypes=None, extent=None, checks=None, feedback=None):
    checks = set(checks or [c[0] for c in CHECKS])
    ptypes = ptypes or fabric.parcel_types
    issues = []
    tol = fabric.tolerance

    def req(extra=None):
        r = QgsFeatureRequest().setFilterExpression(CURRENT + (" AND " + extra if extra else ""))
        if extent is not None:
            r.setFilterRect(extent)
        return r

    # every current fabric point, bucketed on a grid, so "has a point?" is fast
    cell = max(tol, 1e-9)
    point_keys = set()
    if "no_point" in checks:
        for p in fabric.points.getFeatures(req()):
            g = p.geometry()
            if g.isNull():
                continue
            q = g.asPoint()
            point_keys.add((round(q.x() / cell), round(q.y() / cell)))

    def has_point(x, y):
        kx, ky = round(x / cell), round(y / cell)
        return any((kx + dx, ky + dy) in point_keys for dx in (-1, 0, 1) for dy in (-1, 0, 1))

    if "no_type" in checks:
        known = set(fabric.parcel_types)
        for lyr in (fabric.lines, fabric.parcels):
            for f in lyr.getFeatures(req()):
                if f["ParcelType"] not in known:
                    g = f.geometry()
                    p = g.pointOnSurface().asPoint() if not g.isNull() else None
                    issues.append(Issue("error", "%s feature has parcel type %r - Build skips it"
                                        % (lyr.name(), f["ParcelType"] or None),
                                        p.x() if p else None, p.y() if p else None, lyr.id(), f.id()))

    line_layers = [(t, fabric.lines, type_expr(t)) for t in ptypes] + \
        [("Connections", fabric.connections, None)]
    for t, lyr, texpr in line_layers:
        if feedback:
            feedback("Checking %s lines" % t)
        ends = []
        for f in lyr.getFeatures(req(texpr)):
            curve = _single_curve(f.geometry())
            if curve is None or curve.isEmpty():
                issues.append(Issue("error", "Empty or multi-part line", layer_id=lyr.id(), fid=f.id()))
                continue
            s, e = _ends(curve)
            ends += [(f.id(), s), (f.id(), e)]
            if "no_cogo" in checks and Fabric.course_from_feature(f) is None:
                issues.append(Issue("warning", "%s line has no COGO dimensions" % t,
                                    s[0], s[1], lyr.id(), f.id()))
            if "no_record" in checks and not f["CreatedByRecord"]:
                issues.append(Issue("info", "%s line has no record" % t, s[0], s[1], lyr.id(), f.id()))
        if "dangles" in checks and t != "Connections":
            for fid, (x, y) in dangles(ends, tol):
                issues.append(Issue("error", "Dangling %s line end (loop not closed)" % t,
                                    x, y, lyr.id(), fid))
        if "no_point" in checks:
            done = set()
            for fid, (x, y) in ends:
                key = (round(x / cell), round(y / cell))
                if key in done:
                    continue
                done.add(key)
                if not has_point(x, y):
                    issues.append(Issue("warning", "Line end has no fabric point", x, y, lyr.id(), fid))

    numbered = set(fabric.number_types)
    seen_numbers = {}
    lyr = fabric.parcels
    for t in ptypes:
        if feedback:
            feedback("Checking %s parcels" % t)
        for f in lyr.getFeatures(req(type_expr(t))):
            g = f.geometry()
            if g.isNull():
                continue
            p = g.pointOnSurface().asPoint()
            name = f["ParcelNumber"] or f["Name"] or "#%d" % f.id()
            if f["IsSeed"] == 1:
                if "unbuilt_seeds" in checks:
                    issues.append(Issue("info", "Seed %s has not been built" % name,
                                        p.x(), p.y(), lyr.id(), f.id()))
                continue
            ratio = _num(f["MiscloseRatio"])
            dist = _num(f["MiscloseDistance"])
            if "misclose" in checks and dist and ratio and ratio < fabric.warn_ratio:
                issues.append(Issue("warning", "%s %s misclose 1:%d (%.3f)" % (t, name, ratio, dist),
                                    p.x(), p.y(), lyr.id(), f.id()))
            stated = _num(f["StatedArea"])
            calc = _num(f["CalculatedArea"])
            if "stated_area" in checks and stated and calc:
                unit = _num(f["StatedAreaUnit"]) or 1
                stated_sqft = stated * (43560.0 if unit == 2 else 1.0)
                diff = abs(calc - stated_sqft) / stated_sqft
                if diff > 0.01:
                    issues.append(Issue("warning", "%s %s calculated area is %.1f%% off the stated area"
                                        % (t, name, diff * 100), p.x(), p.y(), lyr.id(), f.id()))
            if "no_record" in checks and not f["CreatedByRecord"]:
                issues.append(Issue("info", "%s %s has no record" % (t, name),
                                    p.x(), p.y(), lyr.id(), f.id()))
            if "numbers" in checks and t in numbered:
                num = f["ParcelNumber"]
                if not num:
                    issues.append(Issue("warning", "%s %s has no parcel number" % (t, name),
                                        p.x(), p.y(), lyr.id(), f.id()))
                elif num in seen_numbers:
                    issues.append(Issue("error", "Parcel number %s is used twice" % num,
                                        p.x(), p.y(), lyr.id(), f.id()))
                else:
                    seen_numbers[num] = f.id()
    return issues


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
