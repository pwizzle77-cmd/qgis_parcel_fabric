# -*- coding: utf-8 -*-
"""Plat closure report: every course, misclose and area for a record."""

import datetime
import html
import math

from qgis.core import QgsFeatureRequest, QgsGeometry, QgsPointXY
from qgis.PyQt.QtCore import QUrl

from . import units as U
from .build import _ends, _single_curve
from .cogo import CURVE, Course, loop_misclose
from .fabric import CURRENT, Fabric, quote, type_expr
from .topo import chain_ring


def boundary_chain(fabric, parcel):
    """The parcel's current boundary lines in walking order: [(feature, reversed)]."""
    tol = fabric.tolerance
    g = parcel.geometry()
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
        curve = "R %.2f %s, Δ %s, chord %s %.2f" % (abs(c.radius), side, U.format_angle(math.degrees(c.delta)),
                                                   U.azimuth_to_bearing(c.azimuth), c.chord_length)
        return ["Curve", "arc %.2f" % c.arc_length, curve]
    return [U.azimuth_to_bearing(c.azimuth), "%.2f" % c.distance, ""]


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
        chain = boundary_chain(fabric, p)
        geom_area = QgsGeometry(p.geometry().constGet().segmentize()).area()
        if chain is None:
            blocks.append("<h3>%s</h3><p class=bad>Its boundary lines don't form one loop - run Check Fabric.</p>"
                          % html.escape(name))
            rows_summary.append((name, "", "", "%.3f" % (geom_area / 43560.0), ""))
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
            ratio_txt, carea = "", None
        else:
            dx, dy, per, carea = ms
            mis = math.hypot(dx, dy)
            ratio_txt = "closed" if mis < 1e-6 else "1:{:,}".format(int(per / mis))
            close = "<p>Perimeter %.2f ft &middot; misclose %.3f ft %s &middot; precision <b>%s</b></p>" % (
                per, mis, U.azimuth_to_bearing(math.atan2(dx, dy)) if mis > 1e-6 else "", ratio_txt)
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
        blocks.append("<h3>%s</h3><table><tr><th>#</th><th>Bearing</th><th>Distance</th><th>Curve</th>"
                      "<th>Source</th></tr>%s</table>%s%s" % (html.escape(name), "".join(table), close, areas))
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
<h1>Closure report: %s</h1><p class=m>%s &middot; Book %s, Page %s &middot; recorded %s%s<br>Basis of bearing: %s
&middot; %d parcel(s) &middot; prepared %s</p>
<table><tr><th>Parcel</th><th>Precision</th><th>Area by record (ac)</th><th>Area by coordinates (ac)</th>
<th>Stated</th></tr>%s</table>%s</body></html>""" % (
        html.escape(str(rec["Name"] or "")), html.escape(str(rec["Name"] or "")),
        html.escape(str(rec["RecordType"] or "")), html.escape(str(rec["Book"] or "-")),
        html.escape(str(rec["Page"] or "-")), dtxt or "-", doc_html,
        html.escape(str(rec["QPF_BasisOfBearing"] or "-")), len(parcels), datetime.date.today().isoformat(),
        summary, "".join(blocks))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return len(parcels)
