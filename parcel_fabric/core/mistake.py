# -*- coding: utf-8 -*-
"""Roll back one record when nothing later depends on it.

Ordinary edits retire features and the database rules can forbid deletes.
A mistake or a test record is different: if no later record used what it
created, remove that work and put back what it retired. The scan on disk
is never touched.
"""

import sqlite3

from . import safeguards as G

ROLES = ("parcels", "lines", "points", "connections")


class MistakeError(RuntimeError):
    pass


def plan(record_id, features, lineage, needed):
    """What to delete and what to make current again.

    ``features``: (role, fid, gid, created, retired, parent line gid).
    ``lineage``: (fid, record gid, parent gid, child gid).
    ``needed``: gids this record created that another surviving feature uses.
    """
    delete = {role: [] for role in ROLES}
    restore = {role: [] for role in ROLES}
    created_lines, created_parcels, created_any = set(), set(), set()
    owners = {}
    for role, fid, gid, created, retired, _parent in features:
        if gid:
            owners[gid] = created
        if created != record_id:
            continue
        if retired not in (None, record_id):
            raise MistakeError("A later record retired something this one created. "
                               "Retire this work instead of removing it.")
        delete[role].append(fid)
        if gid:
            created_any.add(gid)
            if role == "parcels":
                created_parcels.add(gid)
            elif role in ("lines", "connections"):
                created_lines.add(gid)
    for _role, _fid, gid, created, _retired, parent in features:
        if parent and parent in created_lines and created != record_id:
            raise MistakeError("A later line was split from a line this record created. "
                               "Retire this work instead of removing it.")
    for role, fid, gid, created, retired, _parent in features:
        if retired == record_id and created != record_id:
            restore[role].append(fid)
    for _fid, _rec, parent, child in lineage:
        if parent in created_parcels and owners.get(child) != record_id:
            raise MistakeError("A later parcel comes from this record. Retire this work instead of removing it.")
    if needed & created_any:
        raise MistakeError("A line or point this record created is still part of another parcel. "
                           "Retire this work instead of removing it.")
    lineage_ids = [fid for fid, rec, parent, child in lineage
                   if rec == record_id or parent in created_parcels or child in created_parcels]
    return {"delete": delete, "restore": restore, "lineage": lineage_ids}


def has_work(planned):
    return any(planned["delete"].values()) or any(planned["restore"].values()) or bool(planned["lineage"])


def summary(planned):
    labels = (("parcels", "parcel(s)"), ("lines", "line(s)"), ("points", "point(s)"),
              ("connections", "connection(s)"))
    removed = ["%d %s" % (len(planned["delete"][role]), label) for role, label in labels if planned["delete"][role]]
    back = ["%d %s" % (len(planned["restore"][role]), label) for role, label in labels if planned["restore"][role]]
    if not removed and not back:
        return "This record created nothing. You can delete the record itself."
    text = "Removes " + ", ".join(removed) if removed else "Removes nothing it created"
    if back:
        text += " and puts back " + ", ".join(back)
    return text + "."


def _gpkg_functions(con):
    """The functions a GeoPackage's spatial index triggers call. No geometry is written here,
    so the extent ones must never run; failing rolls everything back."""
    def unused(_geom):
        raise ValueError("no geometry should be written")
    con.create_function("ST_IsEmpty", 1, lambda g: 1 if g is None or g[3] >> 4 & 1 else 0)
    for name in ("ST_MinX", "ST_MaxX", "ST_MinY", "ST_MaxY"):
        con.create_function(name, 1, unused)


def gpkg_commit(path, tables, work_sql):
    """Apply ``work_sql`` and leave the no-delete rules in place. One transaction."""
    con = sqlite3.connect(path, isolation_level=None)
    try:
        _gpkg_functions(con)
        con.execute("BEGIN")
        try:
            have = [row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name GLOB 'qpf_*'")]
            for name in have:
                con.execute('DROP TRIGGER IF EXISTS "%s"' % name.replace('"', '""'))
            for sql in work_sql:
                con.execute(sql)
            if have:
                for sql in G.statements("ogr", "", tables):
                    con.execute(sql)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()


def server_batch(provider, schema, tables, work_sql, triggers_on):
    """One statement (PostgreSQL) or one batch (SQL Server) so a failure rolls back."""
    work_sql = list(work_sql)
    if not triggers_on:
        return work_sql
    if provider == "postgres":
        body = "\n".join(G.remove_statements(provider, schema, tables) + work_sql
                         + G.statements(provider, schema, tables))
        return ["DO $qpf$\nBEGIN\n%s\nEND\n$qpf$;" % body]
    if provider == "mssql":
        # shortcut: one batch because CREATE/DISABLE TRIGGER must travel with the deletes;
        # upgrade if a driver splits the batch and a failure leaves the rules off.
        quoted = [G._br(t).replace("'", "''") for t in tables]
        sch = G._br(schema or "dbo").replace("'", "''")
        off, on = [], []
        for table in quoted:
            trigger = "qpf_guard_%s" % table
            off.append("DISABLE TRIGGER [%s].[%s] ON [%s].[%s]" % (sch, trigger, sch, table))
            on.append("ENABLE TRIGGER [%s].[%s] ON [%s].[%s]" % (sch, trigger, sch, table))
        return ["BEGIN TRANSACTION\n%s\nCOMMIT" % ";\n".join(off + work_sql + on)]
    raise MistakeError("Remove mistake isn't available for provider '%s'." % provider)


def preview(fabric, record_id):
    if fabric.record_by_id(record_id) is None:
        raise MistakeError("That record isn't in this fabric.")
    features, lineage, geoms = _collect(fabric)
    remainders = split_remainders(record_id, features, lineage)
    planned = plan(record_id, features, lineage, _needed(record_id, geoms, fabric.tolerance, remainders))
    planned["remainders"] = sorted(remainders)
    return planned


def split_remainders(record_id, features, lineage):
    """Parcels this record split that kept their number: still current, with a smaller outline."""
    gone = {gid for role, _f, gid, created, retired, _p in features
            if role == "parcels" and record_id in (created, retired)}
    known = {gid for role, _f, gid, _c, _r, _p in features if role == "parcels"}
    mine = [row for row in lineage if row[1] == record_id]
    kept = {row[2] for row in mine if row[2] in known} - gone
    if any(row[2] in kept and row[1] != record_id and row[0] > max(m[0] for m in mine) for row in lineage):
        raise MistakeError("A later record split the same parcel again. Remove that one first.")
    return kept


def apply(fabric, record_id, drop_record):
    """Do the rollback. ``drop_record`` also deletes the record row, not its scan."""
    planned = preview(fabric, record_id)
    if not has_work(planned) and not drop_record:
        raise MistakeError("This record didn't create or retire anything. "
                           "Check \"Also delete the record\" to remove an empty record.")
    _require_saved(fabric)
    provider, source, schema, tables = G.target(fabric)
    regrow = _regrow(fabric, record_id, planned)
    owners = _owners(fabric, planned)
    sql = _statements(fabric, provider, schema, planned, drop_record, record_id)
    if provider == "ogr":
        gpkg_commit(source, tables, sql)
    elif provider in ("postgres", "mssql"):
        _server_commit(provider, source, schema, tables, sql)
    else:
        raise MistakeError("Remove mistake works on a GeoPackage, PostgreSQL or SQL Server fabric.")
    for layer in (fabric.parcels, fabric.lines, fabric.points, fabric.connections,
                  fabric.records, fabric.lineage):
        layer.reload()
    from .history import find_history_layer
    hist = find_history_layer(fabric)
    if hist is not None:
        hist.reload()
    _rebuild(fabric, regrow, owners)
    return planned


def _regrow(fabric, record_id, planned):
    """(parcel feature, area it had before the split, points in its polygons) for each remainder."""
    from qgis.core import QgsFeatureRequest, QgsGeometry
    from .fabric import quote
    from .geomio import parts_of
    from .parcel_numbers import unfiltered
    if not planned["remainders"]:
        return []
    lyr = unfiltered(fabric.parcels)
    rows = [r for r in unfiltered(fabric.lineage).getFeatures() if r["RecordID"] == record_id]
    out = []
    for gid in planned["remainders"]:
        gids = [gid] + [r["ChildID"] for r in rows if r["ParentID"] == gid]
        feats = list(lyr.getFeatures(QgsFeatureRequest().setFilterExpression(
            '"GlobalID" IN (%s)' % ", ".join(quote(g) for g in gids))))
        area = QgsGeometry.unaryUnion([QgsGeometry(f.geometry().constGet().segmentize()).buffer(fabric.tolerance, 4)
                                       for f in feats])
        points = [QgsGeometry(p.constGet().segmentize()).pointOnSurface().asPoint()
                  for f in feats for p in parts_of(f.geometry())]
        out.append((next(f for f in feats if f["GlobalID"] == gid), area, points))
    return out


def _owners(fabric, planned):
    """Records whose parcels come back, so their footprints can be brought up to date."""
    from qgis.core import QgsFeatureRequest
    from .parcel_numbers import unfiltered
    req = QgsFeatureRequest().setFilterFids(planned["restore"]["parcels"])
    return {f["CreatedByRecord"] for f in unfiltered(fabric.parcels).getFeatures(req) if f["CreatedByRecord"]}


def _rebuild(fabric, regrow, owners):
    from . import lineage as L
    from .build import _dates, build_parcels, update_record_footprint
    from .fabric import EditSession
    for f, area, points in regrow:
        rep = build_parcels(fabric, f["ParcelType"], None, area.boundingBox(), number=False,
                            adopt={f.id(): points}, within=area.buffer(fabric.tolerance, 4))
        if not rep.rebuilt:
            raise MistakeError("The record's work was removed, but parcel %s couldn't be rebuilt to its outline before "
                               "the split. Run Check Fabric." % (f["ParcelNumber"] or f["Name"] or f.id()))
    owners = owners | {f["CreatedByRecord"] for f, _area, _points in regrow if f["CreatedByRecord"]}
    if owners:
        with EditSession([fabric.records], "Record footprints") as ed:
            for gid in owners:
                update_record_footprint(fabric, gid, ed)
        _dates(fabric, list(owners))
    L.clear_cache()


def _require_saved(fabric):
    busy = [layer.name() for layer in (
        fabric.parcels, fabric.lines, fabric.points, fabric.connections, fabric.records, fabric.lineage)
        if layer.isEditable()]
    if busy:
        raise MistakeError("Save or roll back edits on %s first." % ", ".join(busy))


def _blank(value):
    if value is None or (hasattr(value, "isNull") and value.isNull()):
        return None
    text = str(value).strip()
    return text or None


def _collect(fabric):
    from .parcel_numbers import unfiltered
    features, geoms = [], {}
    layers = (("parcels", fabric.parcels), ("lines", fabric.lines),
              ("points", fabric.points), ("connections", fabric.connections))
    for role, layer in layers:
        for feature in unfiltered(layer).getFeatures():
            gid = _blank(feature["GlobalID"]) if feature.fields().indexOf("GlobalID") >= 0 else None
            created = _blank(feature["CreatedByRecord"]) if feature.fields().indexOf("CreatedByRecord") >= 0 else None
            retired = _blank(feature["RetiredByRecord"]) if feature.fields().indexOf("RetiredByRecord") >= 0 else None
            parent = None
            if role in ("lines", "connections") and feature.fields().indexOf("ParentLineID") >= 0:
                parent = _blank(feature["ParentLineID"])
            features.append((role, feature.id(), gid, created, retired, parent))
            if gid:
                seed = feature.fields().indexOf("IsSeed") >= 0 and feature["IsSeed"] == 1
                ptype = feature["ParcelType"] if feature.fields().indexOf("ParcelType") >= 0 else None
                geoms[gid] = (role, feature.geometry(), created, retired, ptype, seed)
    lineage = []
    for feature in unfiltered(fabric.lineage).getFeatures():
        lineage.append((feature.id(), _blank(feature["RecordID"]), _blank(feature["ParentID"]),
                        _blank(feature["ChildID"])))
    return features, lineage, geoms


def _needed(record_id, geoms, tol, rebuilt=()):
    """Lines and points this record created that a surviving feature still uses.

    ``rebuilt``: parcels that get a new outline afterwards, so they need none of these lines.
    Lines are compared by distance: on a parcel that isn't square to the grid two lines that
    coincide almost never intersect exactly.
    """
    needed = set()
    lines, points, parcels, kept_lines, line_ends, kept_points = [], [], [], [], [], []
    for gid, (role, geom, created, retired, ptype, seed) in geoms.items():
        if geom is None or geom.isNull():
            continue
        stays = created != record_id and retired in (None, record_id)     # current, or brought back
        if role == "lines" and created == record_id and not retired:
            lines.append((gid, geom, ptype))
        elif role == "points" and created == record_id:
            points.append((gid, geom))
        elif role == "points" and stays:
            kept_points.append(geom)
        elif role == "parcels" and created != record_id and not retired and not seed and gid not in rebuilt:
            parcels.append((geom, ptype))
        if role == "lines" and stays:
            kept_lines.append((geom, ptype))
        if role in ("lines", "connections") and created != record_id and not retired:
            line_ends.append(geom)
    for gid, geom, ptype in lines:
        seg, box = _flat(geom), geom.boundingBox()
        box.grow(tol)
        pts = (seg.interpolate(0), seg.interpolate(seg.length() / 2), seg.interpolate(seg.length()))

        def along(other):
            return all(0 <= other.distance(p) <= tol for p in pts)
        bounds = any(kind == ptype and g.boundingBox().intersects(box) and along(_outline(g)) for g, kind in parcels)
        if bounds and not any(kind == ptype and g.boundingBox().intersects(box) and along(_flat(g))
                              for g, kind in kept_lines):
            needed.add(gid)             # no line that stays covers it
    for gid, geom in points:
        point = geom.asPoint()
        xy = (point.x(), point.y())
        if any(_near(xy, kept, tol) for kept in kept_points):
            continue
        if any(_near(xy, end, tol) for line in line_ends for end in _ends(line)):
            needed.add(gid)
    return needed


def _flat(geom):
    from qgis.core import QgsGeometry
    return QgsGeometry(geom.constGet().segmentize())


def _outline(geom):
    from qgis.core import QgsGeometry
    flat = _flat(geom)          # kept alive while its boundary is taken
    return QgsGeometry(flat.constGet().boundary())


def _near(xy, other, tol):
    if other is None or (hasattr(other, "isNull") and other.isNull()):
        return False
    if hasattr(other, "asPoint"):
        other = other.asPoint()
        other = (other.x(), other.y())
    return abs(xy[0] - other[0]) <= tol and abs(xy[1] - other[1]) <= tol


def _ends(geom):
    pts = [(p.x(), p.y()) for p in geom.vertices()]
    if not pts:
        return []
    return [pts[0], pts[-1]]


def _q(provider, name):
    if provider == "mssql":
        return "[%s]" % G._br(name)
    return '"%s"' % G._dq(name)


def _table(provider, schema, layer):
    if provider == "ogr":
        parts = dict(p.split("=", 1) for p in layer.source().split("|")[1:] if "=" in p)
        table = parts.get("layername", layer.name())
        return _q(provider, table)
    from qgis.core import QgsDataSourceUri
    uri = QgsDataSourceUri(layer.source())
    table = _q(provider, uri.table())
    if provider == "postgres" and (uri.schema() or schema):
        return "%s.%s" % (_q(provider, uri.schema() or schema), table)
    if provider == "mssql":
        return "%s.%s" % (_q(provider, uri.schema() or schema or "dbo"), table)
    return table


def _pk(layer):
    indexes = layer.dataProvider().pkAttributeIndexes()
    if len(indexes) != 1:
        raise MistakeError("%s has no single id column, so it can't be cleaned up this way." % layer.name())
    return layer.fields().field(indexes[0]).name()


def _ids(ids):
    return ", ".join(str(int(i)) for i in ids)


def _statements(fabric, provider, schema, planned, drop_record, record_id):
    layers = {"parcels": fabric.parcels, "lines": fabric.lines, "points": fabric.points,
              "connections": fabric.connections, "lineage": fabric.lineage, "records": fabric.records}
    sql = []
    for role in ROLES:
        layer = layers[role]
        table, pk = _table(provider, schema, layer), _q(provider, _pk(layer))
        if planned["delete"][role]:
            sql.append("DELETE FROM %s WHERE %s IN (%s)" % (table, pk, _ids(planned["delete"][role])))
        if planned["restore"][role]:
            sets = "%s = NULL" % _q(provider, "RetiredByRecord")
            if role == "parcels" and layer.fields().indexOf("QPF_EndDate") >= 0:
                sets += ", %s = NULL" % _q(provider, "QPF_EndDate")
            sql.append("UPDATE %s SET %s WHERE %s IN (%s)" % (table, sets, pk, _ids(planned["restore"][role])))
    if planned["lineage"]:
        layer = layers["lineage"]
        sql.append("DELETE FROM %s WHERE %s IN (%s)" % (
            _table(provider, schema, layer), _q(provider, _pk(layer)), _ids(planned["lineage"])))
    record = fabric.record_by_id(record_id)
    layer = layers["records"]
    table, pk = _table(provider, schema, layer), _q(provider, _pk(layer))
    if drop_record:
        sql.append("DELETE FROM %s WHERE %s = %d" % (table, pk, int(record.id())))
    elif planned["delete"]["parcels"] or planned["restore"]["parcels"]:
        sets = "%s = 0" % _q(provider, "ParcelCount")
        column = layer.dataProvider().geometryColumnName()
        if column:
            sets += ", %s = NULL" % _q(provider, column)
        sql.append("UPDATE %s SET %s WHERE %s = %d" % (table, sets, pk, int(record.id())))
    return sql


def _server_commit(provider, source, schema, tables, work_sql):
    from qgis.core import QgsDataSourceUri, QgsProviderRegistry
    md = QgsProviderRegistry.instance().providerMetadata(provider)
    uri = QgsDataSourceUri(source)
    uri.setTable("")
    uri.setGeometryColumn("")
    conn = md.createConnection(uri.uri(False), {})
    if provider == "postgres":
        probe = "SELECT tgname FROM pg_trigger WHERE tgname LIKE 'qpf_%' AND NOT tgisinternal"
    else:
        probe = "SELECT name FROM sys.triggers WHERE name LIKE 'qpf_guard_%'"
    try:
        found = conn.executeSql(probe)
    except Exception as exc:
        raise MistakeError("Couldn't read the database rules: %s" % exc) from None
    try:
        for statement in server_batch(provider, schema, tables, work_sql, bool(found)):
            conn.executeSql(statement)
    except MistakeError:
        raise
    except Exception as exc:
        raise MistakeError("Couldn't remove that record's work: %s" % exc) from None
