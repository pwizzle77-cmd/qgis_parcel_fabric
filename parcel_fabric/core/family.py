# -*- coding: utf-8 -*-
"""Parcel families from lineage edges.  Pure Python (no QGIS imports).

An edge is (parent gid, child gid, operation, record gid).
"""


def roots(edges, gid):
    """The oldest ancestors of ``gid`` (``gid`` itself if it has none)."""
    parents = {}
    for p, c, _op, _r in edges:
        parents.setdefault(c, set()).add(p)
    out, seen, todo = set(), {gid}, [gid]
    while todo:
        g = todo.pop()
        ps = parents.get(g)
        if not ps:
            out.add(g)
            continue
        for p in ps:
            if p not in seen:
                seen.add(p)
                todo.append(p)
    return out


def descendants(edges, start):
    """``start`` gids plus everything that came from them."""
    kids = {}
    for p, c, _op, _r in edges:
        kids.setdefault(p, set()).add(c)
    out = set(start)
    todo = list(start)
    while todo:
        g = todo.pop()
        for c in kids.get(g, ()):
            if c not in out:
                out.add(c)
                todo.append(c)
    return out


def generations(edges, members):
    """gid -> generation (0 = an original parcel) within ``members``.

    A parcel with several parents (a merge) is one generation after the
    youngest of them.
    """
    parents = {}
    for p, c, _op, _r in edges:
        if p in members and c in members:
            parents.setdefault(c, set()).add(p)
    gen = {}

    def g(x, stack=()):
        if x in gen:
            return gen[x]
        if x in stack:                 # defensive: a cycle in bad data
            return 0
        ps = parents.get(x)
        val = 0 if not ps else 1 + max(g(p, stack + (x,)) for p in ps)
        gen[x] = val
        return val

    for m in members:
        g(m)
    return gen


def events(edges, members, record_dates, record_names=None):
    """Steps of a family's history, oldest first.

    Returns a list of dicts: date (ISO text or ""), record, record_name,
    operation, parents (gids), children (gids).  One event per record and
    operation.  Undated events sort last.
    """
    record_names = record_names or {}
    by = {}
    for p, c, op, rec in edges:
        if p not in members and c not in members:
            continue
        e = by.setdefault((rec, op), {"date": record_dates.get(rec) or "", "record": rec,
                                      "record_name": record_names.get(rec, ""), "operation": op,
                                      "parents": [], "children": []})
        if p not in e["parents"]:
            e["parents"].append(p)
        if c not in e["children"]:
            e["children"].append(c)
    return sorted(by.values(), key=lambda e: (e["date"] == "", e["date"], e["record_name"]))
