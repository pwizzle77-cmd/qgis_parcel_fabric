# -*- coding: utf-8 -*-
"""Database maintenance for a fabric's own tables.

A records-driven fabric only grows: nothing is ever deleted, only retired
(see safeguards.py), so every table accumulates history forever. Three
things keep that healthy as a fabric ages:

* indexes on the fields every current-parcel query filters or joins by
  (``GlobalID``, ``RetiredByRecord``, ``CreatedByRecord``...), which a
  fabric does not get out of the box;
* refreshed statistics, so the query planner knows how the table has grown;
* reclaiming the space years of retired rows leave behind.
"""

# role names match schema.ROLE_* (plain strings here, like safeguards.py, so this
# module stays import-able - and so its SQL generation testable - without QGIS)
ROLES = ("parcels", "lines", "points", "connections", "records", "lineage", "lots", "blocks",
        "subdivisions")

# every field a query filters or joins by, role -> [field, ...]
INDEX_FIELDS = {
    "parcels": ["GlobalID", "RetiredByRecord", "CreatedByRecord", "ParcelNumber", "ParcelID"],
    "lines": ["GlobalID", "RetiredByRecord", "CreatedByRecord"],
    "points": ["GlobalID", "RetiredByRecord", "CreatedByRecord"],
    "connections": ["GlobalID", "RetiredByRecord", "CreatedByRecord"],
    "records": ["GlobalID"],
    "lineage": ["GlobalID", "ParentID", "ChildID", "RecordID"],
    "lots": ["GlobalID"],
    "blocks": ["GlobalID"],
    "subdivisions": ["GlobalID"],
}


def _dq(name):
    """A safe double-quoted identifier body."""
    return str(name).replace('"', '""')


def _br(name):
    """A safe [bracketed] identifier body (SQL Server)."""
    return str(name).replace("]", "]]")


def _index_name(table, field):
    return "qpf_ix_%s_%s" % (table, field)


def index_statements(provider, schema, tables):
    """CREATE INDEX for every field in INDEX_FIELDS. ``tables``: role -> table name.

    Harmless to run again later: an index that already exists is left alone.
    """
    out = []
    if provider == "postgres":
        sch = _dq(schema or "public")
        for role, table in tables.items():
            for field in INDEX_FIELDS.get(role, []):
                out.append('CREATE INDEX IF NOT EXISTS "%s" ON "%s"."%s" ("%s")' % (
                    _dq(_index_name(table, field)), sch, _dq(table), _dq(field)))  # nosec B608
    elif provider == "mssql":
        sch = _br(schema or "dbo")
        for role, table in tables.items():
            for field in INDEX_FIELDS.get(role, []):
                name = _br(_index_name(table, field)).replace("'", "''")
                out.append(
                    "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = '%s') "  # nosec B608
                    "CREATE INDEX [%s] ON [%s].[%s] ([%s])"
                    % (name, _br(_index_name(table, field)), sch, _br(table), _br(field)))
    else:
        for role, table in tables.items():
            for field in INDEX_FIELDS.get(role, []):
                out.append('CREATE INDEX IF NOT EXISTS "%s" ON "%s" ("%s")' % (
                    _dq(_index_name(table, field)), _dq(table), _dq(field)))  # nosec B608
    return out


def analyze_statements(provider, schema, tables):
    """Refresh the query planner's statistics for every table."""
    if provider == "mssql":
        sch = _br(schema or "dbo")
        return ["UPDATE STATISTICS [%s].[%s]" % (sch, _br(t)) for t in tables.values()]
    if provider == "postgres":
        sch = _dq(schema or "public")
        return ['ANALYZE "%s"."%s"' % (sch, _dq(t)) for t in tables.values()]
    return ['ANALYZE "%s"' % _dq(t) for t in tables.values()]


def vacuum_statements(provider, schema, tables):
    """Statements that reclaim space. GeoPackage/SpatiaLite: the whole file
    (SQLite's VACUUM is file-wide, not per table - run outside a
    transaction, see ``reclaim_space``). PostgreSQL: a plain VACUUM per
    table (no exclusive lock, unlike VACUUM FULL). SQL Server has no
    equivalent statement; rebuilding every index reclaims the same space.
    """
    if provider == "postgres":
        sch = _dq(schema or "public")
        return ['VACUUM "%s"."%s"' % (sch, _dq(t)) for t in tables.values()]
    if provider == "mssql":
        sch = _br(schema or "dbo")
        return ["ALTER INDEX ALL ON [%s].[%s] REBUILD" % (sch, _br(t)) for t in tables.values()]
    return ["VACUUM"]


def target(fabric):
    """(provider, connection source, schema, {role: table name}) of the
    fabric's own tables - opening a role (Lots, Lineage...) not already in
    the project, the same tables Check Fabric and the rest of the fabric work with.
    """
    from qgis.core import QgsDataSourceUri
    lazy_roles = {"lots", "blocks", "subdivisions", "lineage"}   # opened (created if missing) on first use
    layers = {}
    for role in ROLES:
        lyr = fabric.layers.get(role)
        if lyr is None and role in lazy_roles:
            lyr = getattr(fabric, role)
        if lyr is not None:
            layers[role] = lyr
    if not layers:
        raise RuntimeError("This fabric has no tables to maintain.")
    first = next(iter(layers.values()))
    prov = first.providerType()
    if prov == "ogr":
        path = first.source().split("|")[0]
        tables = {}
        for role, lx in layers.items():
            parts = dict(p.split("=", 1) for p in lx.source().split("|")[1:] if "=" in p)
            tables[role] = parts.get("layername", lx.name())
        return prov, path, "", tables
    uri = QgsDataSourceUri(first.source())
    tables = {role: QgsDataSourceUri(lx.source()).table() for role, lx in layers.items()}
    return prov, first.source(), uri.schema(), tables


def _any_editing(fabric):
    return any(lx.isEditable() for lx in fabric.all_layers())


def _execute(provider, source, sql, autocommit=False):
    if provider == "ogr":
        import sqlite3
        con = sqlite3.connect(source, isolation_level=None if autocommit else "")
        try:
            for s in sql:
                con.execute(s)
            if not autocommit:
                con.commit()
        finally:
            con.close()
        return len(sql)
    from qgis.core import QgsDataSourceUri, QgsProviderRegistry
    md = QgsProviderRegistry.instance().providerMetadata(provider)
    uri = QgsDataSourceUri(source)
    uri.setTable("")
    uri.setGeometryColumn("")
    conn = md.createConnection(uri.uri(False), {})
    for s in sql:
        conn.executeSql(s)
    return len(sql)


def create_indexes(fabric):
    """Add any of the standard indexes this fabric's tables are missing. Returns the count."""
    if _any_editing(fabric):
        raise RuntimeError("Save or discard fabric edits first.")
    provider, source, schema, tables = target(fabric)
    return _execute(provider, source, index_statements(provider, schema, tables))


def update_statistics(fabric):
    """Refresh the query planner's statistics. Returns the statement count."""
    if _any_editing(fabric):
        raise RuntimeError("Save or discard fabric edits first.")
    provider, source, schema, tables = target(fabric)
    return _execute(provider, source, analyze_statements(provider, schema, tables))


def reclaim_space(fabric):
    """Reclaim the space retired (never deleted) rows leave behind. Returns the statement count."""
    if _any_editing(fabric):
        raise RuntimeError("Save or discard fabric edits first.")
    provider, source, schema, tables = target(fabric)
    sql = vacuum_statements(provider, schema, tables)
    return _execute(provider, source, sql, autocommit=(provider == "ogr"))
