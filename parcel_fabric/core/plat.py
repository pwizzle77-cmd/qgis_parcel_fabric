# -*- coding: utf-8 -*-
"""Fill a parcel's Subdivision, Block and Lot from the Lots, Blocks and Subdivisions layers.

Runs after each import, so the layers can come in any order. A value somebody typed
is never replaced.
"""

import re

from qgis.core import QgsFeatureRequest, QgsGeometry, QgsSpatialIndex

from . import schema as S
from .fabric import CURRENT, EditSession

HALF = 0.5          # layers drawn apart rarely match exactly: most of the smaller shape has to be inside


def _text(value):
    """Attribute text; '' for NULL."""
    if value is None or (hasattr(value, "isNull") and value.isNull()):
        return ""
    return str(value).strip()


def _flat(feature):
    geom = feature.geometry()
    return QgsGeometry(geom.constGet().segmentize())


def _overlap(a, b):
    inter = a.intersection(b)
    if inter.isNull():          # an invalid outline
        inter = a.makeValid().intersection(b.makeValid())
    return max(inter.area(), 0.0)


def _natural(text):
    return [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.findall(r"\d+|\D+", text)]


def join_values(values, limit):
    """'1-3,5' from lots 1, 2, 3 and 5. '' when it doesn't fit."""
    names = sorted({v for v in values if v}, key=_natural)
    out, i = [], 0
    while i < len(names):
        j = i
        while (j + 1 < len(names) and names[j].isdigit() and names[j + 1].isdigit()
               and int(names[j + 1]) == int(names[j]) + 1):
            j += 1
        if j - i >= 2:
            out.append("%s-%s" % (names[i], names[j]))
        else:
            out += names[i:j + 1]
        i = j + 1
    text = ",".join(out)
    return text if len(text) <= limit else ""


class _Shapes:
    """The polygons of one layer, found by overlap."""

    def __init__(self, layer):
        self.items, self.index = {}, QgsSpatialIndex()
        for f in layer.getFeatures():
            if f.geometry().isNull() or f.geometry().isEmpty():
                continue
            geom = _flat(f)
            if geom.area() > 0:
                self.items[f.id()] = (f, geom)
                self.index.addFeature(f.id(), geom.boundingBox())

    def over(self, geom):
        """(feature, its polygon, shared area) for each polygon over ``geom``."""
        for fid in self.index.intersects(geom.boundingBox()):
            f, other = self.items[fid]
            shared = _overlap(geom, other)
            if shared > 0:
                yield f, other, shared


def fill(fabric, progress=None):
    """Set Subdivision, Block and Lot on the current parcels.

    Lots go to the parcel with their parcel number, or else to the parcels they lie
    in (or that lie in them). A lot without a block takes the block it lies in.
    Returns {"Subdivision": n, "Block": n, "Lot": n, "too_long": n} parcels changed.
    """
    done = {"Subdivision": 0, "Block": 0, "Lot": 0, "too_long": 0}
    if not any(lyr.featureCount() for lyr in (fabric.lots, fabric.blocks, fabric.subdivisions)):
        return done
    tell = progress or (lambda frac: None)
    parcels = fabric.parcels
    S.add_missing_fields(parcels, S.ROLE_PARCELS)
    lots, blocks, subs = _Shapes(fabric.lots), _Shapes(fabric.blocks), _Shapes(fabric.subdivisions)
    tell(0.1)
    req = QgsFeatureRequest().setFilterExpression('%s AND ("IsSeed" IS NULL OR "IsSeed" = 0)' % CURRENT)
    everyone = [f for f in parcels.getFeatures(req) if not f.geometry().isNull() and not f.geometry().isEmpty()]
    by_number = {}
    for f in everyone:
        number = _text(f["ParcelNumber"])
        if number:
            by_number.setdefault(number, []).append(f.id())
    given, placed = {}, set()           # parcel fid -> lot fids by number; those lots
    for fid, (lot, _g) in lots.items.items():
        for parcel in by_number.get(_text(lot["ParcelNumber"]), ()):
            given.setdefault(parcel, []).append(fid)
            placed.add(fid)
    lot_block = {}

    def block_of(fid):
        if fid not in lot_block:
            lot, geom = lots.items[fid]
            text = _text(lot["Block"])
            if not text:
                found = [(shared, _text(b["Block"])) for b, _g, shared in blocks.over(geom)
                         if shared >= HALF * geom.area()]
                text = max(found)[1] if found else ""
            lot_block[fid] = text
        return lot_block[fid]

    # names the Lots and Blocks layers gave: the Subdivisions layer may correct those
    spare = {_text(f["Subdivision"]) for shapes in (lots, blocks) for f, _g in shapes.items.values()} - {""}
    changes = []
    for n, f in enumerate(everyone):
        if n % 500 == 0:
            tell(0.1 + 0.8 * n / len(everyone))
        geom = _flat(f)
        area = geom.area()
        if area <= 0:
            continue
        mine = {fid: lots.items[fid][1].area() for fid in given.get(f.id(), ())}
        for lot, shape, shared in lots.over(geom):
            if lot.id() not in placed and shared >= HALF * min(area, shape.area()):
                mine[lot.id()] = shared
        under = [(shared, b) for b, _g, shared in blocks.over(geom) if shared >= HALF * area]
        under = max(under, key=lambda m: m[0])[1] if under else None
        platted = [(shared, _text(s["Name"])) for s, _g, shared in subs.over(geom) if shared >= HALF * area]
        platted = max(platted)[1] if platted else ""
        named = {}
        for fid, shared in mine.items():
            name = _text(lots.items[fid][0]["Subdivision"])
            if name:
                named[name] = named.get(name, 0.0) + shared
        sub = platted or (max(named, key=named.get) if named else "") or (_text(under["Subdivision"]) if under else "")
        block = [block_of(fid) for fid in mine]
        if not any(block) and under is not None:
            block = [_text(under["Block"])]
        wanted = {"Subdivision": [sub], "Block": block, "Lot": [_text(lots.items[fid][0]["Lot"]) for fid in mine]}
        for field, values in wanted.items():
            values = [v for v in values if v]
            if not values:
                continue
            have = _text(f[field])
            if have and not (field == "Subdivision" and platted and have in spare):
                continue
            text = values[0][:80] if field == "Subdivision" else join_values(values, 16)
            if not text:
                done["too_long"] += 1
            elif text != have:
                changes.append((f.id(), field, text))
                done[field] += 1
    tell(0.9)
    if changes:
        with EditSession([parcels], "Subdivision, block and lot") as ed:
            for fid, field, text in changes:
                ed.change(parcels, fid, field, text)
    return done
