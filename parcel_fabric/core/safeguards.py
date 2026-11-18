# -*- coding: utf-8 -*-
"""Database rules for fabrics shared by several editors.

Two rules, enforced by the database so they hold for every client
(QGIS, ArcGIS, scripts):

* once ``RetiredByRecord`` is set it can't be changed or cleared - two
  editors can't both retire (split) the same parcel;
* features can't be deleted - history is retired, never removed.

GeoPackage gets the same rules as SQLite triggers (useful when a GeoPackage
sits on a shared drive).
"""



ROLES = ("parcels", "lines", "points", "connections")    # schema.ROLE_* values
MSG = "Parcel fabric: retired features are history and cannot be changed or deleted"


def statements(provider, schema, tables):
    """SQL to install the rules for ``provider`` ('postgres', 'mssql', 'ogr'/'gpkg')."""
    out = []
    if provider == "postgres":
        sch = schema or "public"
        out.append('''CREATE OR REPLACE FUNCTION "%s".qpf_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION '%s';
  END IF;
  IF OLD."RetiredByRecord" IS NOT NULL AND NEW."RetiredByRecord" IS DISTINCT FROM OLD."RetiredByRecord" THEN
    RAISE EXCEPTION '%s';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql''' % (sch, MSG, MSG))
        for t in tables:
            out.append('DROP TRIGGER IF EXISTS qpf_guard ON "%s"."%s"' % (sch, t))
            out.append('CREATE TRIGGER qpf_guard BEFORE UPDATE OR DELETE ON "%s"."%s" '
                       'FOR EACH ROW EXECUTE FUNCTION "%s".qpf_guard()' % (sch, t, sch))
    elif provider == "mssql":
        sch = schema or "dbo"
        for t in tables:
            trg = "qpf_guard_%s" % t
            out.append("IF OBJECT_ID('[%s].[%s]', 'TR') IS NOT NULL DROP TRIGGER [%s].[%s]" % (sch, trg, sch, trg))
            out.append('''CREATE TRIGGER [%s].[%s] ON [%s].[%s] AFTER UPDATE, DELETE AS
BEGIN
  SET NOCOUNT ON;
  IF NOT EXISTS (SELECT 1 FROM inserted) AND EXISTS (SELECT 1 FROM deleted)
  BEGIN ROLLBACK TRANSACTION; THROW 51000, '%s', 1; END
  IF EXISTS (SELECT 1 FROM deleted d JOIN inserted i ON i.[GlobalID] = d.[GlobalID]
             WHERE d.[RetiredByRecord] IS NOT NULL
               AND (i.[RetiredByRecord] IS NULL OR i.[RetiredByRecord] <> d.[RetiredByRecord]))
  BEGIN ROLLBACK TRANSACTION; THROW 51000, '%s', 1; END
END''' % (sch, trg, sch, t, MSG, MSG))
    elif provider in ("ogr", "gpkg", "spatialite"):
        for t in tables:
            out.append('DROP TRIGGER IF EXISTS "qpf_noretire_%s"' % t)
            out.append('''CREATE TRIGGER "qpf_noretire_%s" BEFORE UPDATE OF "RetiredByRecord" ON "%s"
FOR EACH ROW WHEN OLD."RetiredByRecord" IS NOT NULL
  AND (NEW."RetiredByRecord" IS NULL OR NEW."RetiredByRecord" <> OLD."RetiredByRecord")
BEGIN SELECT RAISE(ABORT, '%s'); END''' % (t, t, MSG))
            out.append('DROP TRIGGER IF EXISTS "qpf_nodelete_%s"' % t)
            out.append('''CREATE TRIGGER "qpf_nodelete_%s" BEFORE DELETE ON "%s"
BEGIN SELECT RAISE(ABORT, '%s'); END''' % (t, t, MSG))
    else:
        raise ValueError("Safeguards aren't available for provider '%s'." % provider)
    return out


def remove_statements(provider, schema, tables):
    out = []
    if provider == "postgres":
        sch = schema or "public"
        out += ['DROP TRIGGER IF EXISTS qpf_guard ON "%s"."%s"' % (sch, t) for t in tables]
    elif provider == "mssql":
        sch = schema or "dbo"
        for t in tables:
            trg = "qpf_guard_%s" % t
            out.append("IF OBJECT_ID('[%s].[%s]', 'TR') IS NOT NULL DROP TRIGGER [%s].[%s]" % (sch, trg, sch, trg))
    else:
        for t in tables:
            out += ['DROP TRIGGER IF EXISTS "qpf_noretire_%s"' % t, 'DROP TRIGGER IF EXISTS "qpf_nodelete_%s"' % t]
    return out


def target(fabric):
    """(provider, connection source, schema, [table names]) of the fabric's layers."""
    from qgis.core import QgsDataSourceUri
    lyrs = [fabric.layers.get(r) for r in ROLES]
    lyrs = [l for l in lyrs if l is not None]
    prov = lyrs[0].providerType()
    if prov == "ogr":
        path = lyrs[0].source().split("|")[0]
        tables = []
        for l in lyrs:
            parts = dict(p.split("=", 1) for p in l.source().split("|")[1:] if "=" in p)
            tables.append(parts.get("layername", l.name()))
        return prov, path, "", tables
    uri = QgsDataSourceUri(lyrs[0].source())
    tables = [QgsDataSourceUri(l.source()).table() for l in lyrs]
    return prov, lyrs[0].source(), uri.schema(), tables


def run(fabric, install=True):
    """Install (or remove) the rules on the fabric's own database. Returns the statement count."""
    provider, source, schema, tables = target(fabric)
    sql = (statements if install else remove_statements)(provider, schema, tables)
    if provider == "ogr":
        import sqlite3
        con = sqlite3.connect(source)
        try:
            for s in sql:
                con.execute(s)
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
