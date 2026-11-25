# -*- coding: utf-8 -*-
"""Plat closure report: every course, misclose and area for a record."""

import datetime
import html
import math

from qgis.core import QgsFeatureRequest, QgsGeometry, QgsPointXY
from qgis.PyQt.QtCore import QUrl

from . import lineage as L
from . import units as U
from .build import _ends, _single_curve
from .cogo import CURVE, Course, loop_misclose
from .fabric import CURRENT, Fabric, quote, type_expr
from .geomio import parts_of
from .parcel_numbers import unfiltered
from .topo import chain_ring


def boundary_chain(fabric, parcel, poly=None):
    """The current boundary lines of one of the parcel's polygons (default: its first) in walking
    order: [(feature, reversed)]."""
    tol = fabric.tolerance
    g = poly or parts_of(parcel.geometry())[0]
    seg = QgsGeometry(g.constGet().segmentize())
    ring = QgsGeometry(seg.constGet().exteriorRing().clone())
    box = seg.boundingBox()
    box.grow(tol * 2)
    req = QgsFeatureRequest().setFilterRect(box).setFilterExpression(
        "%s AND %s" % (CURRENT, type_expr(parcel["ParcelType"])))

    def on(xy):
        return ring.distance(QgsGeometry.fromPointXY(QgsPointXY(*xy))) <= tol

    lines = {}
    for f in fabric.lines.getFeatures(req):
        curve = _single_curve(f.geometry())
        if curve is None:
            continue
        s, e = _ends(curve)
        sg = QgsGeometry(curve.segmentize())
        mid = sg.interpolate(sg.length() / 2).asPoint()
        if on(s) and on(e) and on((mid.x(), mid.y())):
            lines[f.id()] = (f, s, e)
    chain = chain_ring([(fid, s, e) for fid, (f, s, e) in lines.items()], tol)
    if chain is None:
        return None
    return [(lines[fid][0], rev) for fid, rev in chain]


def walked_course(f, rev):
    """The line's record course in walking direction (or None)."""
    c = Fabric.course_from_feature(f)
    if c is None:
        return None
    if not rev:
        return c
    if c.kind == CURVE:
        return Course(CURVE, c.azimuth + math.pi, None, -c.radius, c.arc_length)
    return Course(c.kind, c.azimuth + math.pi, c.distance)


def _course_cells(c):
    if c is None:
        return ["(no COGO)", "", ""]
    if c.kind == CURVE:
        side = "R" if c.radius > 0 else "L"
        curve = "R %.2f %s, Δ %s, chord %s %.2f" % (
            abs(c.radius), side, U.format_angle(math.degrees(c.delta)),
            U.azimuth_to_bearing(c.azimuth), c.chord_length)
        return ["Curve", "arc %.2f" % c.arc_length, curve]
    return [U.azimuth_to_bearing(c.azimuth), "%.2f" % c.distance, ""]


def _ratio_text(ratio):
    return "closed" if math.isinf(ratio) else "1:{:,}".format(int(ratio))


def closure_report(fabric, record_gid, path):
    rec = fabric.record_by_id(record_gid)
    if rec is None:
        raise ValueError("Record not found")
    expr = '"CreatedByRecord" = %s AND %s AND ("IsSeed" IS NULL OR "IsSeed" = 0)' % (quote(record_gid), CURRENT)
    parcels = sorted(fabric.parcels.getFeatures(QgsFeatureRequest().setFilterExpression(expr)),
                     key=lambda f: str(f["ParcelNumber"] or f["Name"] or f.id()))
    rows_summary, blocks = [], []
    for p in parcels:
        name = str(p["ParcelNumber"] or p["Name"] or "#%d" % p.id())
        polys = parts_of(p.geometry())
        geom_area = QgsGeometry(p.geometry().constGet().segmentize()).area()
        tables, carea, worst, broken = [], 0.0, None, False
        for k, poly in enumerate(polys, 1):
            title = "<h4>Polygon %d of %d</h4>" % (k, len(polys)) if len(polys) > 1 else ""
            chain = boundary_chain(fabric, p, poly)
            if chain is None:
                tables.append(title + "<p class=bad>Its boundary lines don't form one loop - run Check Fabric.</p>")
                broken, carea = True, None
                continue
            courses = [walked_course(f, rev) for f, rev in chain]
            table = []
            for i, ((f, rev), c) in enumerate(zip(chain, courses), 1):
                cells = _course_cells(c)
                table.append("<tr><td>%d</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                    i, cells[0], cells[1], html.escape(cells[2]), html.escape(str(f["QPF_DimSource"] or ""))))
            ms = loop_misclose([(Fabric.course_from_feature(f), rev) for f, rev in chain])
            if ms is None:
                close = "<p class=bad>Some lines have no record dimensions, so misclose can't be computed.</p>"
                carea = None
            else:
                dx, dy, per, loop_area = ms
                mis = math.hypot(dx, dy)
                ratio = math.inf if mis < 1e-6 else per / mis
                worst = ratio if worst is None else min(worst, ratio)
                carea = None if carea is None else carea + loop_area
                close = "<p>Perimeter %.2f ft &middot; misclose %.3f ft %s &middot; precision <b>%s</b></p>" % (
                    per, mis, U.azimuth_to_bearing(math.atan2(dx, dy)) if mis > 1e-6 else "", _ratio_text(ratio))
            tables.append("%s<table><tr><th>#</th><th>Bearing</th><th>Distance</th><th>Curve</th>"
                          "<th>Source</th></tr>%s</table>%s" % (title, "".join(table), close))
        ratio_txt = "" if worst is None or broken else _ratio_text(worst)
        stated = p["StatedArea"]
        stated_txt = ""
        try:
            st = float(stated)
            unit = int(p["StatedAreaUnit"] or 1)
            stated_txt = "%.3f ac" % (st if unit == 2 else st / 43560.0)
        except (TypeError, ValueError):
            pass
        areas = "<p>Area by record dimensions %s &middot; by coordinates %.3f ac (%s sq ft)%s</p>" % (
            "%.3f ac" % (carea / 43560.0) if carea else "-", geom_area / 43560.0, "{:,.0f}".format(geom_area),
            " &middot; stated %s" % stated_txt if stated_txt else "")
        blocks.append("<h3>%s</h3>%s%s" % (html.escape(name), "".join(tables), areas))
        rows_summary.append((name, ratio_txt, "%.3f" % (carea / 43560.0) if carea else "",
                             "%.3f" % (geom_area / 43560.0), stated_txt))
    doc = rec["QPF_DocumentURL"]
    doc_html = ""
    if doc:
        href = str(doc) if "://" in str(doc) else QUrl.fromLocalFile(str(doc)).toString()
        doc_html = ' &middot; <a href="%s">document</a>' % html.escape(href)
    d = rec["RecordedDate"]
    dtxt = d.toString("yyyy-MM-dd") if hasattr(d, "toString") and d else ""
    summary = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % tuple(
        html.escape(str(x)) for x in r) for r in rows_summary)
    page = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Closure report - %s</title>
<style>body{font-family:Arial,sans-serif;margin:28px;color:#222;max-width:950px}h1{font-size:20px}
h3{font-size:15px;margin:22px 0 6px;border-top:1px solid #ccc;padding-top:12px}
table{border-collapse:collapse;font-size:13px}td,th{border:1px solid #ddd;padding:3px 8px;text-align:left}
.m{color:#666;font-size:12px}.bad{color:#b3261e}</style></head><body>
<h1>Closure report: %s</h1><p class=m>%s &middot; Book %s, Page %s%s &middot; recorded %s%s<br>Basis of bearing: %s
&middot; %d parcel(s) &middot; prepared %s</p>
<table><tr><th>Parcel</th><th>Precision</th><th>Area by record (ac)</th><th>Area by coordinates (ac)</th>
<th>Stated</th></tr>%s</table>%s</body></html>""" % (
        html.escape(str(rec["Name"] or "")), html.escape(str(rec["Name"] or "")),
        html.escape(str(rec["RecordType"] or "")), html.escape(str(rec["Book"] or "-")),
        html.escape(str(rec["Page"] or "-")),
        (" &middot; Parcel %s" % html.escape(str(rec["QPF_ParcelID"])))
        if rec.fields().indexOf("QPF_ParcelID") >= 0 and rec["QPF_ParcelID"] else "",
        dtxt or "-", doc_html,
        html.escape(str(rec["QPF_BasisOfBearing"] or "-")), len(parcels), datetime.date.today().isoformat(),
        summary, "".join(blocks))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return len(parcels)


# --------------------------------------------------------------------------
# Parcel Report: a one-page, counter/citizen handout for a single parcel.
# Not a legal survey document - the closure report and Parcel Lineage tool
# remain the authoritative ones; this just puts the common facts on one page.
# --------------------------------------------------------------------------
def _footprint_svg(parts, size=280, pad=16):
    """An SVG sketch of polygon exterior rings ``[[(x, y), ...], ...]``,
    normalized to fit a ``size``x``size`` box. Pure arithmetic and string
    building - no QGIS types, so this is unit-testable on its own."""
    pts = [p for ring in parts for p in ring]
    if not pts:
        return ""
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    w, h = maxx - minx, maxy - miny
    if w <= 0 or h <= 0:
        return ""
    scale = (size - 2 * pad) / max(w, h)

    def sx(x):
        return pad + (x - minx) * scale

    def sy(y):      # SVG y grows downward; map y grows north, so flip it
        return size - pad - (y - miny) * scale

    polys = []
    for ring in parts:
        if len(ring) < 3:
            continue
        pts_txt = " ".join("%.1f,%.1f" % (sx(x), sy(y)) for x, y in ring)
        polys.append('<polygon points="%s" fill="#dce8f7" stroke="#1f5fa8" stroke-width="1.5"/>' % pts_txt)
    if not polys:
        return ""
    return ('<svg viewBox="0 0 %d %d" width="%d" height="%d" xmlns="http://www.w3.org/2000/svg">%s</svg>'
           % (size, size, size, size, "".join(polys)))


def _ring_coords(poly_geom):
    """[(x, y), ...] of one polygon part's exterior ring, curves flattened."""
    seg = QgsGeometry(poly_geom.constGet().segmentize())
    ring = QgsGeometry(seg.constGet().exteriorRing().clone())
    return [(pt.x(), pt.y()) for pt in ring.asPolyline()]


def _record_line(fabric, record_gid, verb):
    if not record_gid:
        return ""
    rec = fabric.record_by_id(record_gid)
    if rec is None:
        return ""
    d = rec["RecordedDate"]
    dtxt = d.toString("yyyy-MM-dd") if hasattr(d, "toString") and d else ""
    bits = [html.escape(str(rec["RecordType"] or "Record")), html.escape(str(rec["Name"] or ""))]
    if rec["Book"] or rec["Page"]:
        bits.append("Book %s, Page %s" % (html.escape(str(rec["Book"] or "-")),
                                          html.escape(str(rec["Page"] or "-"))))
    if dtxt:
        bits.append("recorded %s" % dtxt)
    return "<p><b>%s by:</b> %s</p>" % (verb, " &middot; ".join(b for b in bits if b))


def _lineage_summary(fabric, gid):
    """One level of lineage only; the Parcel Lineage tool has the full tree."""
    def side(field):
        req = QgsFeatureRequest().setFilterExpression('"%s" = %s' % (field, quote(gid)))
        return list(fabric.lineage.getFeatures(req))

    def rows(recs, other_field):
        out = []
        for r in recs:
            other = r[other_field]
            i = L.parcel_info(fabric, other)
            out.append("<li>%s %s <span class=m>(%s, by %s)</span></li>" % (
                html.escape(i["number"] or "(no number)"), html.escape(i["name"] or ""),
                html.escape(r["Operation"] or ""), html.escape(L.record_name(fabric, r["RecordID"]))))
        return "".join(out)

    parents = rows(side("ChildID"), "ParentID")
    children = rows(side("ParentID"), "ChildID")
    out = ""
    if parents:
        out += "<p><b>Came from:</b></p><ul>%s</ul>" % parents
    if children:
        out += "<p><b>Became:</b></p><ul>%s</ul>" % children
    return out


def parcel_report_html(fabric, parcel_gid, path):
    """A one-page report for one parcel: identification, area, the record
    that created (and, if retired, replaced) it, one level of lineage, and
    a footprint sketch. Returns the parcel's own label."""
    req = QgsFeatureRequest().setFilterExpression('"GlobalID" = %s' % quote(parcel_gid))
    p = next(iter(unfiltered(fabric.parcels).getFeatures(req)), None)
    if p is None:
        raise ValueError("Parcel not found")
    number = str(p["ParcelNumber"] or p["Name"] or parcel_gid)
    bits = [html.escape(str(p["ParcelType"] or ""))]
    if p["Subdivision"]:
        bits.append("Subdivision %s" % html.escape(str(p["Subdivision"])))
    if p["Block"]:
        bits.append("Block %s" % html.escape(str(p["Block"])))
    if p["Lot"]:
        bits.append("Lot %s" % html.escape(str(p["Lot"])))
    subtitle = " &middot; ".join(b for b in bits if b)

    area_bits = []
    try:
        ca = float(p["CalculatedArea"])
        area_bits.append("%.3f ac (%s sq ft) by coordinates" % (ca / 43560.0, "{:,.0f}".format(ca)))
    except (TypeError, ValueError):
        pass
    try:
        st, unit = float(p["StatedArea"]), int(p["StatedAreaUnit"] or 1)
        area_bits.append("%.3f ac stated" % (st if unit == 2 else st / 43560.0))
    except (TypeError, ValueError):
        pass
    try:
        mr = float(p["MiscloseRatio"])
        if mr > 0:
            area_bits.append("precision %s" % _ratio_text(mr))
    except (TypeError, ValueError):
        pass
    area_html = "<p><b>Area:</b> %s</p>" % " &middot; ".join(area_bits) if area_bits else ""

    status_html = ""
    if p["RetiredByRecord"]:
        status_html = ('<p style="color:#a00;font-weight:bold">This parcel is no longer current.</p>'
                       + _record_line(fabric, p["RetiredByRecord"], "Retired"))

    svg = ""
    try:
        rings = [_ring_coords(part) for part in parts_of(p.geometry())]
        svg = _footprint_svg(rings)
    except Exception:
        svg = ""   # the report is still useful without the sketch

    page = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Parcel %s</title>
<style>body{font-family:Arial,sans-serif;margin:28px;color:#222;max-width:720px}
h1{font-size:22px;margin-bottom:2px} .sub{color:#666;margin-top:0}
.m{color:#666;font-size:12px} li{margin:3px 0}
.wrap{display:flex;gap:24px;align-items:flex-start;flex-wrap:wrap}
.col{flex:1;min-width:280px}</style></head><body>
<h1>Parcel %s</h1><p class=sub>%s</p>%s
<div class=wrap><div class=col>%s%s%s</div><div>%s</div></div>
<p class=m>Prepared %s. This is a summary for reference, not a legal survey document - see the
full Parcel Lineage and Closure Report tools for the complete record.</p></body></html>""" % (
        html.escape(number), html.escape(number), subtitle, status_html,
        area_html, _record_line(fabric, p["CreatedByRecord"], "Created"),
        _lineage_summary(fabric, parcel_gid), svg, datetime.date.today().isoformat())
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return number
