# -*- coding: utf-8 -*-
"""Give parcels numbers like 12-4S-16-02942-000.

Section, township and range come from PLSS layer(s) (default: BLM National PLSS REST), set in
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
    QgsRectangle,
    QgsVectorLayer,
)

from . import numbering as N
from . import schema as S
from .fabric import CURRENT


class NumberingNotSetUp(RuntimeError):
    pass


def number_fields(number):
    """ParcelNumber and its parts as field values. A non-strap roll ID is stored as typed."""
    return N.fields_for(number)


_OPEN = {}   # (provider, uri) -> layer with no subset filter


def _source_key(layer):
    reg = QgsProviderRegistry.instance()
    prov = layer.providerType()
    parts = reg.decodeUri(prov, layer.source())
    for k in ("subset", "sql"):
        parts.pop(k, None)
    return prov, reg.encodeUri(prov, parts)


def drop_unfiltered(layer):
    """Forget the cached copy after a commit so the next read sees it."""
    try:
        _OPEN.pop(_source_key(layer), None)
    except Exception:
        return


def unfiltered(layer, refresh=False):
    """A second copy of ``layer`` without its subset filter (sees retired).

    The copy is kept and reused, so a parcel history does not open a new
    database connection for every parcel. ``refresh`` reloads it.
    """
    try:
        key = _source_key(layer)
    except Exception:
        key = None
    lyr = _OPEN.get(key) if key else None
    if lyr is not None:
        try:
            ok = lyr.isValid()
        except RuntimeError:
            ok = False
        if not ok:
            _OPEN.pop(key, None)
            lyr = None
    if lyr is None:
        if not key:
            return layer
        lyr = QgsVectorLayer(key[1], layer.name() + " (all)", key[0])
        if not lyr.isValid():
            return layer
        _OPEN[key] = lyr
    elif refresh:
        lyr.reload()
    return lyr


def used_numbers(fabric):
    """Every parcel number in the fabric: current, retired and unsaved edits."""
    out = set()
    req = QgsFeatureRequest().setSubsetOfAttributes(["ParcelNumber"], fabric.parcels.fields())
    req.setFlags(Qgis.FeatureRequestFlag.NoGeometry)
    for lyr in (unfiltered(fabric.parcels, refresh=True), fabric.parcels):
        for f in lyr.getFeatures(req):
            v = f["ParcelNumber"]
            if v:
                out.add(str(v))
    return out


def plss_layers(settings):
    """The PLSS layers the settings point at: (sections spec, townships spec or None).

    Each spec is (uri, provider, name).  A chosen sections layer wins (an optional
    township layer joins it); otherwise the BLM REST default is used.
    """
    if settings.get("plss_source"):
        sec = (settings["plss_source"], settings.get("plss_provider") or "ogr", "PLSS")
        twp = None
        if settings.get("plss_twp_source"):
            twp = (settings["plss_twp_source"], settings.get("plss_twp_provider") or "ogr", "PLSS townships")
        return sec, twp
    if settings.get("plss_use_default", "1") == "1":
        d = S.PLSS_DEFAULT
        return ((S.rest_uri(d["sections"]["url"]), "arcgisfeatureserver", d["sections"]["name"]),
                (S.rest_uri(d["townships"]["url"]), "arcgisfeatureserver", d["townships"]["name"]))
    return None, None


class _PointLayer:
    """Point-in-polygon lookups on a layer, fetching only features near the point.

    Works for huge online layers (no full download); found polygons are cached.
    """

    def __init__(self, spec, crs, label):
        uri, prov, _name = spec
        self.layer = QgsVectorLayer(uri, label, prov)
        if not self.layer.isValid():
            raise NumberingNotSetUp("The PLSS %s layer can't be opened:\n%s" % (label, uri))
        self.xf = QgsCoordinateTransform(crs, self.layer.crs(), QgsProject.instance())
        self.cache = []

    def at(self, pt):
        g = QgsGeometry.fromPointXY(self.xf.transform(pt))
        for geom, f in self.cache:
            if geom.contains(g):
                return f
        d = 1e-6 if self.layer.crs().isGeographic() else 0.5
        p = g.asPoint()
        req = QgsFeatureRequest().setFilterRect(QgsRectangle(p.x() - d, p.y() - d, p.x() + d, p.y() + d))
        for f in self.layer.getFeatures(req):
            if f.geometry().contains(g):
                self.cache.append((f.geometry(), f))
                return f
        if self.layer.dataProvider() and self.layer.dataProvider().hasErrors():
            raise NumberingNotSetUp("The PLSS service didn't answer (%s). Check the connection or "
                                    "choose a local PLSS layer in Fabric Settings." %
                                    "; ".join(self.layer.dataProvider().errors()[:1]))
        return None


class PlssLookup:
    """Point -> (section, township number, N/S, range).

    Sections come from the sections layer.  Township/range/direction come from the
    township layer when there is one (the usual split PLSS layout), else from fields
    on the sections layer (a single combined layer).
    """

    def __init__(self, fabric):
        s = fabric.settings
        sec, twp = plss_layers(s)
        if sec is None:
            raise NumberingNotSetUp("Choose the PLSS layer(s) in Fabric Settings > Parcel numbers.")
        crs = fabric.parcels.crs()
        if s.get("plss_source"):
            self.fsec = s.get("plss_section_field")
            self.ftwp, self.ftdir, self.frng = (s.get("plss_township_field"),
                                                s.get("plss_township_dir_field") or None,
                                                s.get("plss_range_field"))
            if twp:
                self.ttwp, self.tdir, self.trng = (s.get("plss_twp_township_field"),
                                                   s.get("plss_twp_dir_field") or None,
                                                   s.get("plss_twp_range_field"))
        else:
            d = S.PLSS_DEFAULT
            self.fsec = d["sections"]["section"]
            self.ttwp, self.tdir, self.trng = (d["townships"][k] for k in ("township", "dir", "range"))
        if not self.fsec or (twp and not (self.ttwp and self.trng)) or \
                (not twp and not (self.ftwp and self.frng)):
            raise NumberingNotSetUp("Choose the PLSS section, township and range fields in "
                                    "Fabric Settings > Parcel numbers.")
        self.sections = _PointLayer(sec, crs, "sections")
        self.townships = _PointLayer(twp, crs, "townships") if twp else None

    def at(self, pt: QgsPointXY):
        f = self.sections.at(pt)
        if f is None:
            return None
        sec = N.normalize_section(f[self.fsec])
        if self.townships is None:
            twp, tdir = N.normalize_township(f[self.ftwp], f[self.ftdir] if self.ftdir else None)
            return sec, twp, tdir, N.normalize_range(f[self.frng])
        t = self.townships.at(pt)
        if t is None:
            return None
        twp, tdir = N.normalize_township(t[self.ttwp], t[self.tdir] if self.tdir else None)
        return sec, twp, tdir, N.normalize_range(t[self.trng])


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
    for lyr in (fabric.parcels, unfiltered(fabric.parcels, refresh=True)):
        for f in lyr.getFeatures(QgsFeatureRequest().setFilterExpression(expr)):
            return f
    return None
