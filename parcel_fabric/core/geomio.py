# -*- coding: utf-8 -*-
"""QGIS geometry <-> plain segments used by the legal writer and reports."""

from qgis.core import (
    Qgis,
    QgsCircularString,
    QgsCompoundCurve,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsLineString,
    QgsProject,
)


def _xy(p):
    return (p.x(), p.y())


def curve_segments(curve):
    """('line', a, b) / ('arc', s, m, e) segments of any QGIS curve."""
    out = []
    if isinstance(curve, QgsCompoundCurve):
        for i in range(curve.nCurves()):
            out += curve_segments(curve.curveAt(i))
    elif isinstance(curve, QgsCircularString):
        n = curve.numPoints()
        for i in range(0, n - 2, 2):
            out.append(("arc", _xy(curve.pointN(i)), _xy(curve.pointN(i + 1)), _xy(curve.pointN(i + 2))))
    elif isinstance(curve, QgsLineString):
        pts = [_xy(curve.pointN(i)) for i in range(curve.numPoints())]
        for a, b in zip(pts, pts[1:]):
            if abs(a[0] - b[0]) > 1e-9 or abs(a[1] - b[1]) > 1e-9:
                out.append(("line", a, b))
    else:
        seg = curve.segmentize()
        return curve_segments(seg)
    return out


def parts_of(geom):
    """The polygons of a parcel, curves kept."""
    g = geom.constGet()
    if hasattr(g, "numGeometries"):
        return [QgsGeometry(g.geometryN(i).clone()) for i in range(g.numGeometries())]
    return [QgsGeometry(geom)]


def polygon_rings(geom):
    """[(exterior segments, [hole segments...]), ...] for every part."""
    g = geom.constGet()
    parts = []
    if hasattr(g, "numGeometries"):
        polys = [g.geometryN(i) for i in range(g.numGeometries())]
    else:
        polys = [g]
    for p in polys:
        ext = curve_segments(p.exteriorRing())
        holes = [curve_segments(p.interiorRing(i)) for i in range(p.numInteriorRings())]
        parts.append((ext, holes))
    return parts


def to_crs(geom, src_crs, dst_crs):
    g = QgsGeometry(geom)
    if src_crs != dst_crs:
        g.transform(QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance()))
    return g


def is_mercator(crs):
    """Mercator / Web Mercator stretches distances (about 15% in north Florida)."""
    return crs.projectionAcronym() in ("merc", "webmerc")


def unit_word(crs):
    """'feet' or 'meters' for a projected CRS, None for ones that can't measure land."""
    if crs.isGeographic() or is_mercator(crs):
        return None
    u = crs.mapUnits()
    feet = {Qgis.DistanceUnit.Feet, Qgis.DistanceUnit.FeetUSSurvey}
    for name in ("FeetBritish1865", "FeetBritish1936", "FeetBritishBenoit1895A", "FeetBritishBenoit1895B",
                 "FeetBritishSears1922Truncated", "FeetBritishSears1922", "FeetClarkes", "FeetGoldCoast",
                 "FeetIndian", "FeetIndian1937", "FeetIndian1962", "FeetIndian1975"):
        v = getattr(Qgis.DistanceUnit, name, None)
        if v is not None:
            feet.add(v)
    if u in feet:
        return "feet"
    if u == Qgis.DistanceUnit.Meters:
        return "meters"
    return None
