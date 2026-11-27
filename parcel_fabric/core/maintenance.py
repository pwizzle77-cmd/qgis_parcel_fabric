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


# Geometry tables a click has to search. Lineage and settings have no geometry.
SPATIAL_ROLES = ("parcels", "lines", "points", "connections", "records", "lots", "blocks",
                 "subdivisions")
# shortcut: the index box is the data extent plus 10 miles, upgrade by dropping qpf_six_* if an insert reports the bounding box does not cover the geometry
_SPATIAL_TRIED = set()


def spatial_index_name(table, column):
    return "qpf_six_%s_%s" % (table, column)


def spatial_index_batch(schema, table, column):
    """One SQL Server batch: a spatial index so a map click does not read every row.

    Skips the work when that index is already there. An empty table is left alone.
    """
    schema = schema or "dbo"
    column = column or "geom"
    name = spatial_index_name(table, column).replace("'", "''")
    sch, tbl, col = _br(schema), _br(table), _br(column)
    obj = "%s.%s" % (schema.replace("'", "''"), table.replace("'", "''"))
    return (
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'%s' "
        "AND object_id = OBJECT_ID(N'%s'))\n"
        "BEGIN\n"
        "  DECLARE @xmin float, @ymin float, @xmax float, @ymax float\n"
        "  SELECT @xmin = MIN(e.STPointN(1).STX), @ymin = MIN(e.STPointN(1).STY),\n"
        "         @xmax = MAX(e.STPointN(3).STX), @ymax = MAX(e.STPointN(3).STY)\n"
        "  FROM (SELECT [%s].STEnvelope() AS e FROM [%s].[%s] WHERE [%s] IS NOT NULL) s\n"
        "  IF @xmin IS NOT NULL\n"
        "  BEGIN\n"
        "    DECLARE @pad float = 52800\n"
        "    SET @xmin = @xmin - @pad\n"
        "    SET @ymin = @ymin - @pad\n"
        "    SET @xmax = @xmax + @pad\n"
        "    SET @ymax = @ymax + @pad\n"
        "    DECLARE @sql nvarchar(max) = N'CREATE SPATIAL INDEX [%s] ON [%s].[%s]([%s]) "
        "WITH (BOUNDING_BOX = (' + LTRIM(STR(@xmin, 20, 3)) + N',' + LTRIM(STR(@ymin, 20, 3)) "
        "+ N',' + LTRIM(STR(@xmax, 20, 3)) + N',' + LTRIM(STR(@ymax, 20, 3)) + N'))'\n"
        "    EXEC sys.sp_executesql @sql\n"
        "  END\n"
        "END"
        % (name, obj, col, sch, tbl, col, name, sch, tbl, col))


def _sql_server_layer(layer):
    """(schema, table, geometry column, connection source) for a native SQL Server layer."""
    if layer is None or layer.providerType() != "mssql":
        return None
    from qgis.core import QgsDataSourceUri
    uri = QgsDataSourceUri(layer.source())
    table = uri.table()
    if not table:
        return None
    return uri.schema() or "dbo", table, uri.geometryColumn() or "geom", layer.source()


def _mssql_connection(source):
    from qgis.core import QgsDataSourceUri, QgsProviderRegistry
    md = QgsProviderRegistry.instance().providerMetadata("mssql")
    uri = QgsDataSourceUri(source)
    uri.setTable("")
    uri.setGeometryColumn("")
    return md.createConnection(uri.uri(False), {})


def ensure_spatial_indexes(fabric, roles=None, once=False):
    """Add a SQL Server spatial index on each named role that does not have one.

    ``once`` remembers a try for this QGIS session, so opening the project does
    not build the same index again. Returns how many indexes were created.
    GeoPackage fabrics already have one and are left alone.
    """
    roles = roles or SPATIAL_ROLES
    made = 0
    for role in roles:
        lyr = fabric.layers.get(role)
        info = _sql_server_layer(lyr)
        if info is None:
            continue
        schema, table, column, source = info
        key = (schema, table, column)
        if once and key in _SPATIAL_TRIED:
            continue
        if once:
            _SPATIAL_TRIED.add(key)
        conn = _mssql_connection(source)
        name = spatial_index_name(table, column).replace("'", "''")
        obj = "%s.%s" % (schema.replace("'", "''"), table.replace("'", "''"))
        found = conn.executeSql(
            "SELECT 1 FROM sys.indexes WHERE name = N'%s' AND object_id = OBJECT_ID(N'%s')"
            % (name, obj))
        if found:
            continue
        from qgis.core import Qgis, QgsMessageLog
        QgsMessageLog.logMessage(
            "Building a spatial index on %s. This reads the table once." % table,
            "Parcel Fabric", Qgis.MessageLevel.Info)
        conn.executeSql(spatial_index_batch(schema, table, column))
        made += 1
    return made


def create_indexes(fabric):
    """Add any of the standard indexes this fabric's tables are missing. Returns the count."""
    if _any_editing(fabric):
        raise RuntimeError("Save or discard fabric edits first.")
    provider, source, schema, tables = target(fabric)
    n = _execute(provider, source, index_statements(provider, schema, tables))
    # The button should try again even if opening the project already tried.
    for role in SPATIAL_ROLES:
        lyr = fabric.layers.get(role)
        info = _sql_server_layer(lyr)
        if info is not None:
            _SPATIAL_TRIED.discard(info[:3])
    n += ensure_spatial_indexes(fabric)
    return n


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
