# -*- coding: utf-8 -*-
"""Compare COGO-built parcels with an existing parcel layer.

For each parcel: the existing polygon that overlaps it most, the area
difference, the largest offset between the outlines (Hausdorff distance),
how far the whole parcel is shifted, and how much of it overlaps.
"""

import math
from dataclasses import dataclass, field

from qgis.core import (
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsGeometry,
    QgsProject,
    QgsVectorLayer,
)

from . import units as U
from .schema import _mt


@dataclass
class Match:
    label: str
    geom: QgsGeometry                 # fabric parcel, in the fabric CRS
    ref_label: str = ""
    ref_geom: QgsGeometry = None      # existing polygon, in the fabric CRS
    area: float = 0.0
    ref_area: float = 0.0
    hausdorff: float = None
    shift: float = None
    shift_az: float = None
    overlap: float = None             # share of the fabric parcel covered by the existing one
    extra: dict = field(default_factory=dict)

    @property
    def diff_pct(self):
        if not self.ref_area:
            return None
        return (self.area - self.ref_area) / self.ref_area * 100.0


def _seg(g):
    return QgsGeometry(g.constGet().segmentize()) if g.constGet().hasCurvedSegments() else QgsGeometry(g)


def measure(geom, ref):
    """Metrics between two polygons in the same projected CRS."""
    a, b = _seg(geom), _seg(ref)
    m = {"area": a.area(), "ref_area": b.area()}
    m["hausdorff"] = a.hausdorffDistance(b)
    ca, cb = a.centroid().asPoint(), b.centroid().asPoint()
    m["shift"] = math.hypot(cb.x() - ca.x(), cb.y() - ca.y())
    m["shift_az"], _ = U.inverse(ca.x(), ca.y(), cb.x(), cb.y())
    inter = a.intersection(b)
    m["overlap"] = (inter.area() / a.area()) if a.area() > 0 and not inter.isEmpty() else 0.0
    return m


def compare(items, ref_layer, crs, label_field=None, match_field=None):
    """``items``: (label, geometry in ``crs``, match value or None).

    Each is matched to the existing polygon with the same ``match_field``
    value (when given) or else the one overlapping it most.
    """
    to_ref = QgsCoordinateTransform(crs, ref_layer.crs(), QgsProject.instance())
    from_ref = QgsCoordinateTransform(ref_layer.crs(), crs, QgsProject.instance())
    out = []
    for label, geom, key in items:
        m = Match(label, geom, area=_seg(geom).area())
        best, best_area = None, 0.0
        if match_field and key not in (None, ""):
            req = QgsFeatureRequest().setFilterExpression('"%s" = %s' % (
                match_field.replace('"', '""'), "'%s'" % str(key).replace("'", "''")))
            for f in ref_layer.getFeatures(req):
                g = QgsGeometry(f.geometry())
                g.transform(from_ref)
                best = (f, g)
                break
        if best is None:
            box = to_ref.transformBoundingBox(geom.boundingBox())
            seg = _seg(geom)
            for f in ref_layer.getFeatures(QgsFeatureRequest().setFilterRect(box)):
                g = QgsGeometry(f.geometry())
                g.transform(from_ref)
                if not g.intersects(seg):
                    continue
                ov = g.intersection(seg).area()
                if ov > best_area:
                    best, best_area = (f, g), ov
        if best is not None:
            f, g = best
            m.ref_geom = g
            m.ref_label = str(f[label_field]) if label_field and f[label_field] is not None else "#%d" % f.id()
            mm = measure(geom, g)
            m.ref_area, m.hausdorff, m.shift, m.shift_az, m.overlap = (
                mm["ref_area"], mm["hausdorff"], mm["shift"], mm["shift_az"], mm["overlap"])
        out.append(m)
    return out


def difference_layer(matches, crs, name="Fabric vs existing - differences"):
    """A scratch layer of the slivers: where the fabric parcel and the
    existing polygon disagree."""
    lyr = QgsVectorLayer("MultiPolygon?crs=%s" % crs.authid(), name, "memory")
    pr = lyr.dataProvider()
    pr.addAttributes([QgsField("parcel", _mt("QString")), QgsField("existing", _mt("QString")),
                      QgsField("kind", _mt("QString")), QgsField("sq_ft", _mt("Double"))])
    lyr.updateFields()
    feats = []
    for m in matches:
        if m.ref_geom is None:
            continue
        a, b = _seg(m.geom), _seg(m.ref_geom)
        for kind, g in (("fabric only", a.difference(b)), ("existing only", b.difference(a))):
            if g.isEmpty() or g.area() < 0.01:
                continue
            g.convertToMultiType()
            f = QgsFeature(lyr.fields())
            f.setGeometry(g)
            f.setAttributes([m.label, m.ref_label, kind, g.area()])
            feats.append(f)
    pr.addFeatures(feats)
    lyr.updateExtents()
    _style(lyr)
    return lyr


def _style(lyr):
    from qgis.core import QgsCategorizedSymbolRenderer, QgsFillSymbol, QgsRendererCategory
    cats = []
    for kind, color in (("fabric only", "200,40,40"), ("existing only", "30,90,200")):
        sym = QgsFillSymbol.createSimple({"color": color + ",120", "outline_color": color,
                                          "outline_width": "0.2"})
        cats.append(QgsRendererCategory(kind, sym, kind))
    lyr.setRenderer(QgsCategorizedSymbolRenderer("kind", cats))
