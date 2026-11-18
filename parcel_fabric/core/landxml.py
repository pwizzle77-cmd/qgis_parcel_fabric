# -*- coding: utf-8 -*-
"""Read parcels from a LandXML file.  Pure Python (standard library).

LandXML coordinates are written "northing easting"; this returns (x, y).
Each parcel comes back as a name and a list of segments ("line", a, b) or
("arc", start, mid, end), the same shape the legal writer uses.
"""

import math

try:
    from defusedxml import ElementTree as ET
except ImportError:
    import xml.etree.ElementTree as ET  # nosec B405 - DTDs are refused in _parse before parsing


class LandXMLError(ValueError):
    pass


def _parse(text):
    """LandXML never needs a DTD; refusing one blocks entity-expansion and
    external-entity tricks even without defusedxml."""
    head = text[:4096].upper()
    if "<!DOCTYPE" in head or "<!ENTITY" in text.upper():
        raise LandXMLError("This XML file declares a DTD or entities, which LandXML doesn't use - not read.")
    return ET.fromstring(text)  # nosec B314 - DTD/entities refused above


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _ne(text):
    parts = text.replace(",", " ").split()
    if len(parts) < 2:
        raise ValueError("Bad coordinate %r" % text)
    n, e = float(parts[0]), float(parts[1])
    return (e, n)


def _children(el, name):
    return [c for c in el if _local(c.tag) == name]


def _arc_mid(start, center, end, rot):
    """Point halfway along an arc; rot 'cw' or 'ccw'."""
    a0 = math.atan2(start[1] - center[1], start[0] - center[0])
    a1 = math.atan2(end[1] - center[1], end[0] - center[0])
    r = math.hypot(start[0] - center[0], start[1] - center[1])
    if rot == "ccw":
        sweep = (a1 - a0) % (2 * math.pi)
    else:
        sweep = -((a0 - a1) % (2 * math.pi))
    m = a0 + sweep / 2
    return (center[0] + r * math.cos(m), center[1] + r * math.sin(m))


def read(path_or_text):
    """[(parcel name, segments, attributes dict)] from a LandXML file or string."""
    if path_or_text.lstrip().startswith("<"):
        root = _parse(path_or_text)
    else:
        with open(path_or_text, "rb") as fh:
            raw = fh.read()
        root = _parse(raw.decode("utf-8", "replace"))
    # named points (CgPoints) that geometry may refer to with pntRef
    pts = {}
    for el in root.iter():
        if _local(el.tag) == "CgPoint" and el.text and el.text.strip():
            name = el.get("name") or el.get("pntRef")
            if name:
                pts[name] = _ne(el.text)

    def coord(el):
        if el is None:
            return None
        ref = el.get("pntRef")
        if ref and ref in pts and not (el.text and el.text.strip()):
            return pts[ref]
        return _ne(el.text)

    out = []
    for parcel in root.iter():
        if _local(parcel.tag) != "Parcel":
            continue
        geoms = _children(parcel, "CoordGeom")
        if not geoms:
            continue
        segs = []
        for g in geoms[0]:
            kind = _local(g.tag)
            start = coord(next((c for c in g if _local(c.tag) == "Start"), None))
            end = coord(next((c for c in g if _local(c.tag) == "End"), None))
            if start is None or end is None:
                continue
            if kind == "Line":
                segs.append(("line", start, end))
            elif kind == "Curve":
                center = coord(next((c for c in g if _local(c.tag) == "Center"), None))
                rot = (g.get("rot") or "cw").lower()
                segs.append(("arc", start, _arc_mid(start, center, end, rot), end))
            elif kind == "IrregularLine":
                pl = next((c for c in g if _local(c.tag) == "PntList2D"), None)
                if pl is not None and pl.text:
                    nums = [float(v) for v in pl.text.replace(",", " ").split()]
                    chain = [(nums[i + 1], nums[i]) for i in range(0, len(nums) - 1, 2)]
                    segs += [("line", a, b) for a, b in zip(chain, chain[1:])]
                else:
                    segs.append(("line", start, end))
        if segs:
            attrs = {k: v for k, v in parcel.attrib.items()}
            out.append((parcel.get("name") or "Parcel %d" % (len(out) + 1), segs, attrs))
    return out
