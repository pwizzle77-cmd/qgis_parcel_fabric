# -*- coding: utf-8 -*-
"""Parcel lineage: which parcels came from which, by which record."""

import csv
import datetime
import html

from qgis.core import Qgis, QgsFeature, QgsFeatureRequest

from . import schema as S
from .fabric import EditSession, quote
from .parcel_numbers import unfiltered

INFERRED = "Inferred split"     # read from parcel numbers on import, not from a recorded split


def add(fabric, record_gid, operation, parents, children):
    """Write parent -> child rows (every parent to every child)."""
    lyr = fabric.lineage
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    feats = []
    for p in parents:
        for c in children:
            f = QgsFeature(lyr.fields())
            vals = {"GlobalID": S.new_guid(), "RecordID": record_gid, "Operation": operation,
                    "ParentID": p["GlobalID"], "ChildID": c["GlobalID"],
                    "ParentNumber": p["ParcelNumber"] or None, "ChildNumber": c["ParcelNumber"] or None,
                    "ParcelType": c["ParcelType"], "EditDate": now}
            for k, v in vals.items():
                f.setAttribute(k, v)
            feats.append(f)
    if feats:
        with EditSession([lyr], "Lineage") as ed:
            for f in feats:
                ed.add(lyr, f)
    return len(feats)


_CACHE = {}
_INFO_FIELDS = ("GlobalID", "ParcelNumber", "Name", "ParcelType", "RetiredByRecord",
                "CreatedByRecord", "QPF_StartDate")


def prime(fabric, gids, refresh=False):
    """Load attributes for these parcel GlobalIDs in one query per batch."""
    missing = [g for g in dict.fromkeys(gids) if g and (fabric.id, g) not in _CACHE]
    if not missing:
        return
    lyr = unfiltered(fabric.parcels, refresh=refresh)
    fields = lyr.fields()
    has_start = fields.indexOf("QPF_StartDate") >= 0
    idx = [fields.indexOf(n) for n in _INFO_FIELDS if fields.indexOf(n) >= 0]
    found = set()
    for i in range(0, len(missing), 200):
        chunk = missing[i:i + 200]
        req = QgsFeatureRequest().setFilterExpression(
            '"GlobalID" IN (%s)' % ", ".join(quote(g) for g in chunk))
        req.setFlags(Qgis.FeatureRequestFlag.NoGeometry)
        if idx:
            req.setSubsetOfAttributes(idx)
        for f in lyr.getFeatures(req):
            gid = f["GlobalID"]
            found.add(gid)
            info = {"gid": gid, "number": f["ParcelNumber"] or "", "name": f["Name"] or "",
                    "type": f["ParcelType"] or "", "retired": f["RetiredByRecord"] or None,
                    "record": "", "start": f["QPF_StartDate"] if has_start else None}
            rec = fabric.record_by_id(f["CreatedByRecord"]) if f["CreatedByRecord"] else None
            info["record"] = (rec["Name"] if rec is not None else "") or ""
            _CACHE[(fabric.id, gid)] = info
    blank = {"number": "", "name": "", "type": "", "retired": None, "record": "", "start": None}
    for g in missing:
        if g not in found:
            _CACHE[(fabric.id, g)] = dict(blank, gid=g)


def parcel_info(fabric, gid):
    """Number, name, type, retired-by and record name of a parcel (retired too)."""
    key = (fabric.id, gid)
    if key not in _CACHE:
        prime(fabric, [gid])
    return _CACHE[key]


def clear_cache():
    _CACHE.clear()


def walk(fabric, gid, up=True, limit=200):
    """Edges (parent gid, child gid, operation, record gid) reachable from ``gid``,
    and whether ``limit`` cut the search short (there may be more history than shown).

    ``up`` walks to ancestors, otherwise to descendants.
    """
    edges, seen, todo = [], {gid}, [gid]
    truncated = False
    field = "ChildID" if up else "ParentID"
    other = "ParentID" if up else "ChildID"
    flags = Qgis.FeatureRequestFlag.NoGeometry
    while todo:
        if len(edges) >= limit:
            truncated = True
            break
        batch = todo[:80]
        del todo[:80]
        req = QgsFeatureRequest().setFilterExpression(
            '"%s" IN (%s)' % (field, ", ".join(quote(g) for g in batch)))
        req.setFlags(flags)
        for r in fabric.lineage.getFeatures(req):
            if len(edges) >= limit:
                truncated = True
                break
            edges.append((r["ParentID"], r["ChildID"], r["Operation"], r["RecordID"]))
            nxt = r[other]
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
        if truncated:
            break
    return edges, truncated


def label(fabric, gid):
    i = parcel_info(fabric, gid)
    main = i["number"] or i["name"] or gid[:9]
    return "%s%s" % (main, " (retired)" if i["retired"] else "")


def record_name(fabric, gid):
    rec = fabric.record_by_id(gid) if gid else None
    return (rec["Name"] if rec is not None else "") or ""


def export_csv(fabric, edges, path, truncated=False):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if truncated:
            w.writerow(["# INCOMPLETE: this parcel's history is larger than shown here; "
                        "not every ancestor or descendant is listed."])
        w.writerow(["ParentNumber", "ChildNumber", "Operation", "Record", "ParentID", "ChildID"])
        for p, c, op, rec in edges:
            w.writerow([parcel_info(fabric, p)["number"], parcel_info(fabric, c)["number"], op,
                        record_name(fabric, rec), p, c])


def export_dot(fabric, edges, path, focus=None, truncated=False):
    """GraphViz DOT file (open with GraphViz, or paste into an online viewer)."""
    nodes = {g for e in edges for g in e[:2]}
    if focus:
        nodes.add(focus)
    lines = ["digraph lineage {", '  rankdir=TB; node [shape=box, fontname="Arial"];']
    if truncated:
        lines.append('  label="INCOMPLETE: more history exists than is shown here"; '
                     'labelloc=t; fontcolor=red;')
    for g in sorted(nodes):
        i = parcel_info(fabric, g)
        style = ', style="filled", fillcolor="#fff2a8"' if g == focus else \
            (', style="dashed"' if i["retired"] else "")
        text = "\\n".join(t for t in (i["number"], i["name"], i["record"]) if t) or g
        lines.append('  "%s" [label="%s"%s];' % (g, text.replace('"', "'"), style))
    for p, c, op, rec in edges:
        lines.append('  "%s" -> "%s" [label="%s"];' % (p, c, (op or "").replace('"', "'")))
    lines.append("}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def export_html(fabric, gid, up_edges, down_edges, path, truncated=False):
    """A printable page: ancestors and descendants as indented lists."""
    def tree(root, edges, up):
        kids = {}
        for p, c, op, rec in edges:
            a, b = (c, p) if up else (p, c)
            kids.setdefault(a, []).append((b, op, rec))

        def item(g, depth, seen):
            out = []
            for b, op, rec in kids.get(g, []):
                if b in seen:
                    continue
                i = parcel_info(fabric, b)
                out.append("<li><b>%s</b> %s <span class=m>%s by %s</span>%s</li>" % (
                    html.escape(i["number"] or "(no number)"), html.escape(i["name"] or ""),
                    html.escape(op or ""), html.escape(record_name(fabric, rec)),
                    ("<ul>%s</ul>" % "".join(item(b, depth + 1, seen | {b}))) if kids.get(b) else ""))
            return out
        body = "".join(item(root, 0, {root}))
        return "<ul>%s</ul>" % body if body else "<p class=m>None recorded.</p>"
    i = parcel_info(fabric, gid)
    warn = ('<p style="color:#a00;font-weight:bold;border:1px solid #a00;padding:6px 10px">'
           "INCOMPLETE: this parcel's history is larger than fits here; not every ancestor "
           "or descendant is listed.</p>") if truncated else ""
    page = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Lineage %s</title>
<style>body{font-family:Arial,sans-serif;margin:32px;color:#222} h1{font-size:20px}
h2{font-size:15px;margin-top:24px} li{margin:4px 0} .m{color:#666;font-size:12px}</style></head><body>
<h1>Parcel lineage: %s</h1><p class=m>%s &middot; %s &middot; created by %s &middot; printed %s</p>%s
<h2>Came from (parents)</h2>%s<h2>Became (children)</h2>%s</body></html>""" % (
        html.escape(i["number"]), html.escape(i["number"] or gid), html.escape(i["name"]),
        html.escape(i["type"]), html.escape(i["record"]),
        datetime.date.today().isoformat(), warn, tree(gid, up_edges, True), tree(gid, down_edges, False))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)
