# -*- coding: utf-8 -*-
"""Give parcels numbers like 12-4S-16-02942-000.

Section, township and range come from a PLSS sections layer chosen in
Fabric Settings.  New parcels take the next unused ParcelID in their
section-township-range; splits take the next -001, -002... under a parent.
Retired parcels count as "used", so a number is never handed out twice.
"""

from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsFeatureRequest,
    QgsPointXY,
    QgsProject,
    QgsProviderRegistry,
    QgsSpatialIndex,
    QgsVectorLayer,
)

from . import numbering as N
from .fabric import CURRENT


class NumberingNotSetUp(RuntimeError):
    pass


def number_fields(number):
    """ParcelNumber and its parts as field values."""
    p = N.parse_number(number)
    return {"ParcelNumber": number, "Section": p["section"],
            "Township": "%d%s" % (p["township"], p["twp_dir"]), "Range": p["range"],
            "ParcelID": "%05d-%03d" % (p["pid"], p["split"]), "ParcelSeq": p["pid"],
            "SplitNo": p["split"]}


def unfiltered(layer):
    """A second copy of ``layer`` without its subset filter (sees retired)."""
    reg = QgsProviderRegistry.instance()
    prov = layer.providerType()
    parts = reg.decodeUri(prov, layer.source())
    for k in ("subset", "sql"):
        parts.pop(k, None)
    uri = reg.encodeUri(prov, parts)
    lyr = QgsVectorLayer(uri, layer.name() + " (all)", prov)
    return lyr if lyr.isValid() else layer


def used_numbers(fabric):
    """Every parcel number in the fabric: current, retired and unsaved edits."""
    out = set()
    req = QgsFeatureRequest().setSubsetOfAttributes(["ParcelNumber"], fabric.parcels.fields())
    req.setFlags(Qgis.FeatureRequestFlag.NoGeometry)
    for lyr in (unfiltered(fabric.parcels), fabric.parcels):
        for f in lyr.getFeatures(req):
            v = f["ParcelNumber"]
            if v:
                out.add(str(v))
    return out


class PlssLookup:
    """Point -> (section, township number, N/S, range) from the PLSS layer."""

    def __init__(self, fabric):
        s = fabric.settings
        src, prov = s.get("plss_source"), s.get("plss_provider")
        self.fsec = s.get("plss_section_field")
        self.ftwp = s.get("plss_township_field")
        self.ftdir = s.get("plss_township_dir_field") or None
        self.frng = s.get("plss_range_field")
        if not (src and prov and self.fsec and self.ftwp and self.frng):
            raise NumberingNotSetUp("Choose the PLSS sections layer and its fields in "
                                    "Fabric Settings > Parcel numbers.")
        self.layer = QgsVectorLayer(src, "PLSS", prov)
        if not self.layer.isValid():
            raise NumberingNotSetUp("The PLSS sections layer can't be opened:\n%s" % src)
        self.xf = QgsCoordinateTransform(fabric.parcels.crs(), self.layer.crs(), QgsProject.instance())
        self.index = QgsSpatialIndex(self.layer.getFeatures())

    def at(self, pt: QgsPointXY):
        p = self.xf.transform(pt)
        g = QgsGeometry.fromPointXY(p)
        for fid in self.index.intersects(g.boundingBox()):
            f = self.layer.getFeature(fid)
            if f.geometry().contains(g):
                sec = N.normalize_section(f[self.fsec])
                twp, tdir = N.normalize_township(f[self.ftwp], f[self.ftdir] if self.ftdir else None)
                rng = N.normalize_range(f[self.frng])
                return sec, twp, tdir, rng
        return None


def number_new(fabric, features, used=None, plss=None, overwrite=False):
    """Set numbers on new (not split) parcel features in place.

    ``features`` are QgsFeature objects (added or not).  Returns a list of
    (feature, message) for parcels that couldn't be numbered.
    """
    plss = plss or PlssLookup(fabric)
    used = set(used if used is not None else used_numbers(fabric))
    types = set(fabric.number_types)
    problems = []
    for f in features:
        if f["ParcelType"] not in types:
            continue
        if f["ParcelNumber"] and not overwrite:
            continue
        g = f.geometry()
        if g is None or g.isNull():
            continue
        try:
            strr = plss.at(g.pointOnSurface().asPoint())
        except N.NumberError as e:
            problems.append((f, str(e)))
            continue
        if strr is None:
            problems.append((f, "Parcel isn't inside any PLSS section"))
            continue
        pid = N.next_parcel_id(used, *strr)
        num = N.format_number(*strr, pid, 0)
        used.add(num)
        for k, v in number_fields(num).items():
            f.setAttribute(k, v)
    return problems


def number_splits(fabric, parent_number, features, used=None):
    """Give ``features`` split numbers under ``parent_number`` in place.

    They are numbered north-west to south-east.  Returns the numbers.
    """
    used = set(used if used is not None else used_numbers(fabric))

    def pos(f):
        p = f.geometry().pointOnSurface().asPoint()
        return (-round(p.y(), 1), round(p.x(), 1))
    ordered = sorted(features, key=pos)
    nums = N.split_numbers(used, parent_number, len(ordered))
    for f, num in zip(ordered, nums):
        for k, v in number_fields(num).items():
            f.setAttribute(k, v)
        f.setAttribute("ParentNumber", parent_number)
    return nums


def find_parcel(fabric, number, current_only=False):
    """The parcel with this number (current first), or None."""
    expr = '"ParcelNumber" = \'%s\'' % str(number).replace("'", "''")
    if current_only:
        expr += " AND " + CURRENT
    for lyr in (fabric.parcels, unfiltered(fabric.parcels)):
        for f in lyr.getFeatures(QgsFeatureRequest().setFilterExpression(expr)):
            return f
    return None
