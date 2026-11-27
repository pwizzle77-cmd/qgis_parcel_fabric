# -*- coding: utf-8 -*-
"""A parcel fabric opened in the current QGIS project."""

import json
import math
from copy import copy

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCircularString,
    QgsCompoundCurve,
    QgsDefaultValue,
    QgsEditorWidgetSetup,
    QgsExpressionContextUtils,
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsLineString,
    QgsPoint,
    QgsPointXY,
    QgsCoordinateReferenceSystem,
    QgsProject,
    QgsProviderRegistry,
    QgsRectangle,
    QgsTransaction,
    QgsUnitTypes,
    QgsVectorLayer,
    QgsWkbTypes,
)


def _result_ok(result):
    if result is None or isinstance(result, bool):
        return result is None or result
    if isinstance(result, tuple):
        return bool(result[0])
    return bool(result)

from . import schema as S
from . import units as U
from .geomio import is_mercator
from .cogo import Course, CURVE, LINE, DIR_CHORD, DIR_RADIAL, DIR_TANGENT, DIR_TANGENT_PREV

PROP_FABRIC = "qpf/fabric_id"
PROP_ROLE = "qpf/role"
PROP_PLSS = "qpf/plss"
ACTIVE_RECORD_VAR = "qpf_active_record"
CURRENT = '"RetiredByRecord" IS NULL'
CONNECTIONS = "Connections"
_CURVE_FORM = {DIR_CHORD: "Chord", DIR_RADIAL: "Radial", DIR_TANGENT: "Tangent",
               DIR_TANGENT_PREV: "Tangent"}

LAYER_LABELS = {S.ROLE_RECORDS: "Records", S.ROLE_POINTS: "Points",
                S.ROLE_CONNECTIONS: "Connections", S.ROLE_PARCELS: "Parcels",
                S.ROLE_LINES: "Parcel Lines", S.ROLE_SETTINGS: "Fabric settings",
                S.ROLE_LINEAGE: "Parcel Lineage", S.ROLE_LOTS: "Lots",
                S.ROLE_BLOCKS: "Blocks", S.ROLE_SUBDIVISIONS: "Subdivisions"}

# record dialog key -> Records field
RECORD_MAP = {"name": "Name", "rtype": "RecordType", "date": "RecordedDate", "book": "Book",
              "page": "Page", "parcel_id": "QPF_ParcelID", "doc_url": "QPF_DocumentURL",
              "doc_hash": "QPF_DocumentHash", "basis": "QPF_BasisOfBearing", "notes": "QPF_Notes",
              "scale": "QPF_ScaleFactor", "rotation": "QPF_Rotation"}


def quote(v):
    return "'%s'" % str(v).replace("'", "''")


def type_expr(ptype):
    return '"ParcelType" = %s' % quote(ptype)


def _stamp_schema_version(settings_layer, settings):
    """Record which plugin version created/last opened this fabric.

    Diagnostics only - never blocks opening the fabric, and a plugin older
    than the one that last touched it is only logged, not refused, since
    new fields are always additive (see ``schema.add_missing_fields``).
    """
    from qgis.core import Qgis, QgsMessageLog
    try:
        current = S.plugin_version()
        to_write = {}
        if not settings.get("schema_created_version"):
            to_write["schema_created_version"] = current
        if settings.get("schema_updated_version") != current:
            to_write["schema_updated_version"] = current
        if to_write:
            S.write_settings(settings_layer, to_write)
            settings.update(to_write)
        last = settings.get("schema_updated_version") or ""
        if last and S.version_is_older(current, last):
            QgsMessageLog.logMessage(
                "This fabric was last opened by Parcel Fabric %s; this is %s. Fields that "
                "version added may not be recognised here." % (last, current),
                "Parcel Fabric", Qgis.MessageLevel.Warning)
    except Exception as e:
        from qgis.core import Qgis, QgsMessageLog
        QgsMessageLog.logMessage("Could not record the fabric's schema version: %s" % e,
                                 "Parcel Fabric", Qgis.MessageLevel.Warning)


class Fabric:
    """Holds the layers of one fabric and the editing helpers."""

    def __init__(self, fabric_id, settings, layers):
        self.id = fabric_id
        self.settings = settings
        self.layers = layers          # role -> layer

    # ------------------------------------------------------------------
    # Opening
    # ------------------------------------------------------------------
    @classmethod
    def open(cls, provider, source, schema="", prefix=""):
        """Open a fabric from a GeoPackage path or a saved connection."""
        conn = S.open_connection(provider, source)
        if provider == "ogr":
            schema = ""
        st_name = S.table_name(prefix, S.ROLE_SETTINGS)
        if not conn.tableExists(schema, st_name):
            raise RuntimeError("No parcel fabric found (missing table %s)" % st_name)
        if not conn.tableExists(schema, S.table_name(prefix, S.ROLE_PARCELS)):
            raise RuntimeError("This fabric was made with Parcel Fabric 0.1.0, which kept one "
                               "table per parcel type. Create a new fabric with this version.")
        st = S.load_table(conn, provider, schema, st_name, LAYER_LABELS[S.ROLE_SETTINGS])
        settings = S.read_settings(st)
        fid = settings.get("fabric_id") or S.new_guid()
        settings.setdefault("name", prefix or "Parcel Fabric")
        for k, v in S.DEFAULT_SETTINGS.items():
            settings.setdefault(k, v)
        _stamp_schema_version(st, settings)
        st.setCustomProperty(PROP_FABRIC, fid)
        st.setCustomProperty(PROP_ROLE, S.ROLE_SETTINGS)
        layers = {S.ROLE_SETTINGS: st}
        crs = st.crs() if st.crs().isValid() else QgsCoordinateReferenceSystem(settings.get("crs", ""))
        multipart = provider in S.MULTIPART_PROVIDERS
        if provider == "mssql":
            S.mssql_parcels_multipolygon(conn, schema, S.table_name(prefix, S.ROLE_PARCELS))
        for role in (S.ROLE_LINEAGE, S.ROLE_LOTS, S.ROLE_BLOCKS, S.ROLE_SUBDIVISIONS):
            S.ensure_table(conn, schema, prefix, role, crs, multipart)
        for role in (S.ROLE_RECORDS, S.ROLE_POINTS, S.ROLE_CONNECTIONS, S.ROLE_PARCELS, S.ROLE_LINES,
                     S.ROLE_LINEAGE, S.ROLE_LOTS, S.ROLE_BLOCKS, S.ROLE_SUBDIVISIONS):
            lyr = S.load_table(conn, provider, schema, S.table_name(prefix, role), LAYER_LABELS[role])
            if role == S.ROLE_PARCELS:
                S.migrate_parcel_id(lyr)
            S.add_missing_fields(lyr, role)
            lyr.setCustomProperty(PROP_FABRIC, fid)
            lyr.setCustomProperty(PROP_ROLE, role)
            layers[role] = lyr
        f = cls(fid, settings, layers)
        f._configure_layers()
        return f

    @classmethod
    def from_project(cls, project=None):
        """Rebuild every fabric whose layers are already in the project."""
        project = project or QgsProject.instance()
        found = {}
        for lyr in project.mapLayers().values():
            if not isinstance(lyr, QgsVectorLayer):
                continue
            fid = lyr.customProperty(PROP_FABRIC)
            if not fid or lyr.customProperty(PROP_ROLE) == "history":
                continue        # the history layer is a view, not a fabric table
            found.setdefault(fid, {})[lyr.customProperty(PROP_ROLE)] = lyr
        out = []
        need = {S.ROLE_SETTINGS, S.ROLE_RECORDS, S.ROLE_POINTS, S.ROLE_CONNECTIONS,
                S.ROLE_PARCELS, S.ROLE_LINES}
        for fid, layers in found.items():
            if not need <= set(layers):
                continue
            lin = layers.get(S.ROLE_LINEAGE)
            if lin is not None and lin.subsetString() == CURRENT and not lin.isEditable():
                lin.setSubsetString("")     # projects saved before 0.8.8 hid every lineage row
            settings = S.read_settings(layers[S.ROLE_SETTINGS])
            for k, v in S.DEFAULT_SETTINGS.items():
                settings.setdefault(k, v)
            fab = cls(fid, settings, layers)
            fab._sql_server_multipolygon()
            for role in (S.ROLE_LOTS, S.ROLE_BLOCKS, S.ROLE_SUBDIVISIONS):
                if layers.get(role) is None:
                    try:
                        fab._open_role(role)
                    except Exception as e:
                        from qgis.core import Qgis, QgsMessageLog
                        QgsMessageLog.logMessage("Could not open %s: %s" % (role, e),
                                                 "Parcel Fabric", Qgis.MessageLevel.Warning)
            out.append(fab)
        return out

    # ------------------------------------------------------------------
    @property
    def name(self):
        return self.settings.get("name") or "Parcel Fabric"

    @property
    def parcel_types(self):
        try:
            return list(json.loads(self.settings.get("parcel_types") or "[]"))
        except ValueError:
            return []

    @property
    def number_types(self):
        """Parcel types that get parcel numbers."""
        try:
            t = json.loads(self.settings.get("number_types") or "[]")
        except ValueError:
            t = []
        return t or self.parcel_types

    @property
    def records(self):
        return self.layers[S.ROLE_RECORDS]

    @property
    def points(self):
        return self.layers[S.ROLE_POINTS]

    @property
    def connections(self):
        return self.layers[S.ROLE_CONNECTIONS]

    @property
    def parcels(self):
        return self.layers[S.ROLE_PARCELS]

    @property
    def lots(self):
        return self._open_role(S.ROLE_LOTS)

    @property
    def blocks(self):
        return self._open_role(S.ROLE_BLOCKS)

    @property
    def subdivisions(self):
        return self._open_role(S.ROLE_SUBDIVISIONS)

    @property
    def lines(self):
        return self.layers[S.ROLE_LINES]

    @property
    def multipart(self):
        """Can one parcel have several polygons?"""
        return QgsWkbTypes.isMultiType(self.parcels.wkbType())

    def _sql_server_multipolygon(self):
        """Open an existing SQL Server parcel table as MultiPolygon.

        A project layer keeps the type it was saved with, so changing
        geometry_columns is not enough until the layer is reopened.
        """
        lyr = self.layers.get(S.ROLE_PARCELS)
        if lyr is None or lyr.providerType() != "mssql" or not lyr.isValid():
            return
        if QgsWkbTypes.flatType(lyr.wkbType()) == Qgis.WkbType.MultiPolygon:
            return
        if lyr.isEditable():
            from qgis.core import QgsMessageLog
            QgsMessageLog.logMessage(
                "Save parcel edits and reopen the project so several polygons can share one parcel.",
                "Parcel Fabric", Qgis.MessageLevel.Warning)
            return
        reg = QgsProviderRegistry.instance()
        parts = reg.decodeUri("mssql", lyr.source())
        schema = parts.get("schema") or "dbo"
        table = parts.get("table") or S.table_name(self.settings.get("prefix", ""), S.ROLE_PARCELS)
        try:
            conn = reg.providerMetadata("mssql").createConnection(lyr.source(), {})
        except Exception as e:
            from qgis.core import QgsMessageLog
            QgsMessageLog.logMessage("Could not open the SQL Server fabric: %s" % e,
                                     "Parcel Fabric", Qgis.MessageLevel.Warning)
            return
        if not S.mssql_parcels_multipolygon(conn, schema, table):
            return
        from qgis.core import QgsDataSourceUri
        uri = QgsDataSourceUri(lyr.source())
        uri.setWkbType(Qgis.WkbType.MultiPolygon)
        source, name, subset = lyr.source(), lyr.name(), lyr.subsetString()
        try:
            lyr.setDataSource(uri.uri(False), name, "mssql")
        except Exception as e:
            from qgis.core import QgsMessageLog
            QgsMessageLog.logMessage("Could not reopen parcels as MultiPolygon: %s" % e,
                                     "Parcel Fabric", Qgis.MessageLevel.Warning)
            return
        if not lyr.isValid():
            lyr.setDataSource(source, name, "mssql")
            from qgis.core import QgsMessageLog
            QgsMessageLog.logMessage(
                "Parcels stayed one polygon each. The MultiPolygon reload did not open.",
                "Parcel Fabric", Qgis.MessageLevel.Warning)
        if subset:
            lyr.setSubsetString(subset)

    def convert_to_multipart(self):
        """Let parcels have several polygons. Returns the path of the backup."""
        if self.multipart:
            raise RuntimeError("This fabric already allows multi-polygon parcels.")
        if self.parcels.providerType() != "ogr" or ".gpkg" not in self.parcels.source().lower():
            raise RuntimeError("Only GeoPackage fabrics can be converted.")
        busy = [lx.name() for lx in self.edit_layers() if lx.isEditable()]
        if busy:
            raise RuntimeError("Save or discard the edits on %s first." % ", ".join(busy))
        parts = QgsProviderRegistry.instance().decodeUri("ogr", self.parcels.source())
        table = parts.get("layerName") or S.table_name(self.settings.get("prefix", ""), S.ROLE_PARCELS)
        backup = S.convert_gpkg_parcels(parts["path"], table)
        views = [lx for lx in QgsProject.instance().mapLayers().values()
                 if lx.customProperty(PROP_FABRIC) == self.id and lx.customProperty(PROP_ROLE) == "history"]
        for lx in [self.parcels] + views:       # reopen: a layer keeps the geometry type it was opened with
            lx.setDataSource(lx.source(), lx.name(), "ogr")
        self._configure_layers()
        return backup

    @property
    def lineage(self):
        """Parcel_Lineage table; opened next to Parcels when the project lacks it."""
        return self._open_role(S.ROLE_LINEAGE)

    def _open_role(self, role):
        """Open a table a newer plugin added, and show it in this fabric's group."""
        lyr = self.layers.get(role)
        if lyr is not None:
            return lyr
        lyr = _sibling_table(self.parcels, role, self)
        lyr.setCustomProperty(PROP_FABRIC, self.id)
        lyr.setCustomProperty(PROP_ROLE, role)
        self.layers[role] = lyr
        project = QgsProject.instance()
        if project.mapLayer(lyr.id()) is None:
            project.addMapLayer(lyr, False)
            group = project.layerTreeRoot().findGroup(self.name)
            if group is not None:
                node = group.addLayer(lyr)
                if role == S.ROLE_LINEAGE:
                    node.setItemVisibilityChecked(False)
                elif role in (S.ROLE_LOTS, S.ROLE_BLOCKS, S.ROLE_SUBDIVISIONS):
                    from . import styles
                    styles.apply(lyr, self.parcel_types)
        return lyr

    @property
    def foot_m(self):
        return U.FOOT_DEFINITIONS.get(self.settings.get("foot", "us_survey"), U.US_SURVEY_FOOT_M)

    @property
    def map_units_per_foot(self):
        crs = self.points.crs()
        if not crs.isValid() or crs.isGeographic() or crs.mapUnits() == Qgis.DistanceUnit.Unknown:
            raise ValueError("Traverse needs a projected fabric coordinate system with linear units, "
                             "such as State Plane. Courses are entered in feet.")
        if is_mercator(crs):
            raise ValueError("This fabric is in %s (Web Mercator), which stretches and turns every course. "
                             "Make a new fabric in your State Plane zone, such as EPSG:2238." % crs.authid())
        return self.foot_m / QgsUnitTypes.fromUnitToUnitFactor(crs.mapUnits(), Qgis.DistanceUnit.Meters)

    def traverse_map_legs(self, legs):
        """Convert foot offsets to map units, anchored at the POB in fabric coordinates."""
        factor = self.map_units_per_foot
        if not legs or abs(factor - 1.0) < 1e-12:
            return legs
        x0, y0 = legs[0].start

        def xy(point):
            if point is None:
                return None
            return x0 + (point[0] - x0) * factor, y0 + (point[1] - y0) * factor

        mapped = []
        for leg in legs:
            item = copy(leg)  # Keep the record azimuth carried by compass-adjusted legs.
            item.start, item.end, item.mid, item.center = xy(leg.start), xy(leg.end), xy(leg.mid), xy(leg.center)
            mapped.append(item)
        return mapped

    @property
    def tolerance(self):
        try:
            return float(self.settings.get("snap_tolerance", "0.01"))
        except ValueError:
            return 0.01

    @property
    def packed_dms(self):
        return self.settings.get("packed_dms", "0") == "1"

    @property
    def warn_ratio(self):
        try:
            return float(self.settings.get("misclose_warn_ratio", "10000"))
        except ValueError:
            return 10000.0

    def line_layer(self, target):
        """``target`` is a parcel type name or ``"Connections"``."""
        return self.connections if target == CONNECTIONS else self.lines

    def all_layers(self):
        return [lx for lx in self.layers.values() if lx is not None]

    def edit_layers(self):
        return [lx for lx in self.all_layers() if lx.customProperty(PROP_ROLE) != S.ROLE_SETTINGS]

    def save_settings(self, values):
        S.write_settings(self.layers[S.ROLE_SETTINGS], values)
        self.settings.update({k: str(v) for k, v in values.items()})
        self._configure_layers()
        self.ensure_plss_layers()

    # ------------------------------------------------------------------
    # Adding to the project
    # ------------------------------------------------------------------
    def add_to_project(self, project=None):
        from . import styles
        project = project or QgsProject.instance()
        root = project.layerTreeRoot()
        group = root.insertGroup(0, self.name)
        for lyr in (self.points, self.lines, self.parcels, self.layers.get(S.ROLE_LOTS),
                    self.layers.get(S.ROLE_BLOCKS), self.layers.get(S.ROLE_SUBDIVISIONS),
                    self.connections, self.records):
            if lyr is None:
                continue
            project.addMapLayer(lyr, False)
            group.addLayer(lyr)
            styles.apply(lyr, self.parcel_types)
        for role in (S.ROLE_LINEAGE, S.ROLE_SETTINGS):
            tbl = self.layers.get(role)
            if tbl is None:
                continue
            project.addMapLayer(tbl, False)
            node = group.addLayer(tbl)
            node.setItemVisibilityChecked(False)
        rec_node = group.findLayer(self.records.id())
        if rec_node:
            rec_node.setItemVisibilityChecked(False)
        self.set_show_only_current(True)
        self.ensure_plss_layers(project)
        return group

    def ensure_plss_layers(self, project=None):
        """Show the default REST PLSS layers in this fabric's group (or drop them if a
        custom PLSS layer is configured).  Needs the group made by add_to_project."""
        project = project or QgsProject.instance()
        group = project.layerTreeRoot().findGroup(self.name)
        if group is None:
            return
        mine = [n for n in group.findLayers() if n.layer() and n.layer().customProperty(PROP_PLSS)]
        if self.settings.get("plss_source") or self.settings.get("plss_use_default", "1") != "1":
            for n in mine:
                project.removeMapLayer(n.layerId())
            return
        if mine:
            return
        # townships first so sections draw on top; unchecked because national layers draw slowly
        for spec in (S.PLSS_DEFAULT["townships"], S.PLSS_DEFAULT["sections"]):
            lyr = QgsVectorLayer(S.rest_uri(spec["url"]), spec["name"], "arcgisfeatureserver")
            if not lyr.isValid():
                continue        # offline: numbering reports it; try again next time
            lyr.setCustomProperty(PROP_PLSS, "1")
            project.addMapLayer(lyr, False)
            group.addLayer(lyr).setItemVisibilityChecked(False)

    def _configure_layers(self):
        """Default values and form widgets (dropdowns) for every layer."""
        added = []
        for role in (S.ROLE_PARCELS, S.ROLE_RECORDS):
            try:
                names = S.add_missing_fields(self.layers[role], role)
            except Exception as e:
                from qgis.core import QgsMessageLog
                QgsMessageLog.logMessage("Could not add %s fields: %s" % (role, e), "Parcel Fabric",
                                         Qgis.MessageLevel.Warning)
                names = []
            if role == S.ROLE_PARCELS:
                added = names
        guid_default = QgsDefaultValue("upper(uuid())")
        rec_default = QgsDefaultValue("@%s" % ACTIVE_RECORD_VAR)
        rec_rel = QgsEditorWidgetSetup("ValueRelation", {
            "Layer": self.records.id(), "Key": "GlobalID", "Value": "Name",
            "AllowNull": True, "OrderByValue": True})

        def vmap(d):
            return QgsEditorWidgetSetup("ValueMap", {"map": [{v: k} for k, v in d.items()]})

        def vlist(items):
            return QgsEditorWidgetSetup("ValueMap", {"map": [{v: v} for v in items]})

        for lyr in self.edit_layers():
            flds = lyr.fields()

            def setw(name, setup, lyr=lyr, flds=flds):
                i = flds.indexOf(name)
                if i >= 0:
                    lyr.setEditorWidgetSetup(i, setup)

            i = flds.indexOf("GlobalID")
            if i >= 0:
                lyr.setDefaultValueDefinition(i, guid_default)
            i = flds.indexOf("CreatedByRecord")
            if i >= 0:
                lyr.setDefaultValueDefinition(i, rec_default)
            setw("CreatedByRecord", rec_rel)
            setw("RetiredByRecord", rec_rel)
            for yn in ("IsSeed", "IsFixed", "IsCOGOGround", "QPF_IsControl"):
                setw(yn, vmap(S.YES_NO))
            setw("COGOType", vmap(S.COGO_TYPE))
            setw("StatedAreaUnit", vmap(S.AREA_UNIT))
            setw("AdjustmentConstraint", vmap(S.ADJ_CONSTRAINT))
            setw("QPF_DimSource", vlist(S.DIM_SOURCE))
            setw("QPF_CurveForm", vlist(S.CURVE_FORM))
            setw("RecordType", vlist(S.RECORD_TYPES))
            if self.parcel_types and lyr.customProperty(PROP_ROLE) != S.ROLE_CONNECTIONS:
                setw("ParcelType", vlist(self.parcel_types))
            setw("RecordedDate", QgsEditorWidgetSetup("DateTime", {
                "field_format": "yyyy-MM-dd", "display_format": "yyyy-MM-dd",
                "calendar_popup": True, "allow_null": True}))
        if added:
            try:
                from . import styles
                styles.apply(self.parcels, self.parcel_types)
            except Exception as e:
                from qgis.core import QgsMessageLog
                QgsMessageLog.logMessage("Could not restyle parcels: %s" % e, "Parcel Fabric",
                                         Qgis.MessageLevel.Warning)

    def set_show_only_current(self, on):
        """Hide retired features (sets a layer filter)."""
        for lyr in self.edit_layers():
            if lyr.customProperty(PROP_ROLE) == S.ROLE_RECORDS:
                continue
            if lyr.isEditable():
                continue  # QGIS can't change the filter while editing
            has_retired = lyr.fields().indexOf("RetiredByRecord") >= 0      # lineage has none
            lyr.setSubsetString(CURRENT if on and has_retired else "")

    # ------------------------------------------------------------------
    # Records
    # ------------------------------------------------------------------
    @staticmethod
    def active_record_id(project=None):
        project = project or QgsProject.instance()
        v = QgsExpressionContextUtils.projectScope(project).variable(ACTIVE_RECORD_VAR)
        return v or None

    @staticmethod
    def set_active_record_id(gid, project=None):
        project = project or QgsProject.instance()
        QgsExpressionContextUtils.setProjectVariable(project, ACTIVE_RECORD_VAR, gid or "")

    def record_by_id(self, gid):
        if not gid:
            return None
        req = QgsFeatureRequest().setFilterExpression('"GlobalID" = %s' % quote(gid))
        for f in self.records.getFeatures(req):
            return f
        return None

    @staticmethod
    def record_values(rec):
        """A Records feature as the record dialog's dict."""
        out = {}
        for key, field in RECORD_MAP.items():
            try:
                v = rec[field]
            except KeyError:
                v = None
            if v is not None and hasattr(v, "isNull") and v.isNull():
                v = None
            out[key] = v
        out["gid"] = rec["GlobalID"]
        return out

    def create_record(self, name, rtype="", date=None, book="", page="", doc_url="", basis="",
                      notes="", scale=1.0, rotation=0.0, parcel_id=""):
        lyr = self.records
        f = QgsFeature(lyr.fields())
        gid = S.new_guid()
        vals = {"GlobalID": gid, "ParcelCount": 0}
        vals.update({RECORD_MAP[k]: v for k, v in dict(
            name=name, rtype=rtype, date=date, book=book, page=page, parcel_id=parcel_id,
            doc_url=doc_url, basis=basis, notes=notes, scale=scale, rotation=rotation).items()})
        for k, v in vals.items():
            i = lyr.fields().indexOf(k)
            if i >= 0:
                f.setAttribute(i, v)
        with EditSession([lyr], "Create record") as ed:
            ed.add(lyr, f)
        return gid

    def update_record(self, gid, **values):
        rec = self.record_by_id(gid)
        if rec is None:
            raise RuntimeError("Record not found")
        with EditSession([self.records], "Edit record") as ed:
            for k, v in values.items():
                if k in RECORD_MAP:
                    ed.change(self.records, rec.id(), RECORD_MAP[k], v)

    # ------------------------------------------------------------------
    # Points
    # ------------------------------------------------------------------
    def find_point(self, x, y, tol=None):
        tol = self.tolerance if tol is None else tol
        rect = QgsRectangle(x - tol, y - tol, x + tol, y + tol)
        req = QgsFeatureRequest().setFilterRect(rect).setFilterExpression(CURRENT)
        best, bd = None, None
        p = QgsPointXY(x, y)
        for f in self.points.getFeatures(req):
            d = f.geometry().asPoint().distance(p)
            if d <= tol and (bd is None or d < bd):
                best, bd = f, d
        return best

    def new_point_feature(self, x, y, record_gid, created_by="", edited_by=""):
        lyr = self.points
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry(QgsPoint(x, y)))
        vals = {"GlobalID": S.new_guid(), "X": x, "Y": y, "IsFixed": 0,
                "AdjustmentConstraint": 0, "CreatedByRecord": record_gid,
                "QPF_IsControl": 0}
        if created_by:
            vals["CreatedByUser"] = str(created_by)[:100]
        if edited_by:
            vals["EditUser"] = str(edited_by)[:100]
        for k, v in vals.items():
            i = lyr.fields().indexOf(k)
            if i >= 0:
                f.setAttribute(i, v)
        return f

    # ------------------------------------------------------------------
    # Lines
    # ------------------------------------------------------------------
    @staticmethod
    def leg_geometry(leg):
        cc = QgsCompoundCurve()
        a = QgsPoint(*leg.start)
        b = QgsPoint(*leg.end)
        if leg.course.is_curve:
            cc.addCurve(QgsCircularString(a, QgsPoint(*leg.mid), b))
        else:
            cc.addCurve(QgsLineString([a, b]))
        return QgsGeometry(cc)

    def line_feature(self, lyr, leg, record_gid, dim_source="Record", curve_form="Chord",
                     rotation=0.0, scale=1.0, ptype=None):
        """A fabric line feature from a computed traverse leg.

        COGO fields keep the RECORD (ground) values; the geometry carries
        any ground-to-grid ``rotation`` (radians) and ``scale``, which are
        stored in the Rotation / Scale fields like Pro does.
        """
        c = leg.course
        f = QgsFeature(lyr.fields())
        f.setGeometry(self.leg_geometry(leg))
        corrected = abs(rotation) > 1e-12 or abs(scale - 1.0) > 1e-12
        vals = {"GlobalID": S.new_guid(),
                "Direction": U.azimuth_to_esri(getattr(leg, "record_az", leg.chord_az) - rotation),
                "COGOType": 1, "IsCOGOGround": 1 if corrected else 0,
                "CreatedByRecord": record_gid, "QPF_DimSource": dim_source,
                "ParcelType": ptype}
        if corrected:
            vals.update({"Rotation": math.degrees(rotation), "Scale": scale})
        if c.is_curve:
            vals.update({"Radius": c.radius, "ArcLength": c.arc_length,
                         "Distance": c.chord_length, "QPF_CurveForm": curve_form})
        else:
            vals.update({"Distance": c.distance})
        for k, v in vals.items():
            i = lyr.fields().indexOf(k)
            if i >= 0:
                f.setAttribute(i, v)
        return f

    @staticmethod
    def course_from_feature(f):
        """COGO course stored on a line feature (Esri fields), or None."""
        def val(name):
            try:
                v = f[name]
            except KeyError:
                return None
            if v is None:
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None   # NULL QVariant
        d = val("Direction")
        if d is None:
            return None
        az = U.esri_to_azimuth(d)
        r = val("Radius")
        arc = val("ArcLength")
        if r and arc:
            return Course(CURVE, az, None, r, arc)
        dist = val("Distance")
        if dist is None:
            return None
        return Course(LINE, az, dist)

    def add_traverse(self, target, legs, record_gid, dim_source="Record", rotation=0.0, scale=1.0,
                     session=None, mapped=False):
        """Write foot-based traverse offsets as map-unit lines and end points in one edit.

        ``session`` writes into an edit the caller already opened, and does not save it.
        ``mapped`` legs are already in map units.
        """
        if not mapped:
            legs = self.traverse_map_legs(legs)
        lyr = self.line_layer(target)
        pts = self.points
        new_pts = []
        seen = []

        def need_point(xy):
            if self.find_point(xy[0], xy[1]) is not None:
                return
            for s in seen:
                if abs(s[0] - xy[0]) <= self.tolerance and abs(s[1] - xy[1]) <= self.tolerance:
                    return
            seen.append(xy)
            new_pts.append(self.new_point_feature(xy[0], xy[1], record_gid))

        feats = []
        for leg in legs:
            form = _CURVE_FORM.get(leg.course.dir_kind, "Chord")
            feats.append(self.line_feature(lyr, leg, record_gid, dim_source, form, rotation, scale,
                                           None if target == CONNECTIONS else target))
            need_point(leg.start)
            need_point(leg.end)
        def write(ed):
            for f in feats:
                ed.add(lyr, f)
            for p in new_pts:
                ed.add(pts, p)

        if session is not None:
            write(session)
        else:
            with EditSession([lyr, pts], "Traverse") as ed:
                write(ed)
        return len(feats), len(new_pts)


def _sibling_table(layer, role, fabric):
    """Open (creating if needed) another table in the same database as ``layer``."""
    reg = QgsProviderRegistry.instance()
    prov = layer.providerType()
    parts = reg.decodeUri(prov, layer.source())
    if prov == "ogr":
        conn = reg.providerMetadata("ogr").createConnection(parts["path"], {})
        schema = ""
    else:
        conn = reg.providerMetadata(prov).createConnection(layer.source(), {})
        schema = parts.get("schema", "") or ""
    table = S.table_name(fabric.settings.get("prefix", ""), role)
    S.ensure_table(conn, schema, fabric.settings.get("prefix", ""), role, layer.crs(),
                   prov in S.MULTIPART_PROVIDERS)
    return S.load_table(conn, prov, schema, table, LAYER_LABELS[role])


class EditSession:
    """Add/change features on several layers as one edit.

    A layer that is NOT in edit mode is put in edit mode and saved when the
    block ends (rolled back if anything fails).  A layer the user is already
    editing just gets an undo step; the user saves it as usual.
    ``commit=False`` leaves that edit open. The caller saves or undoes it.
    """

    def __init__(self, layers, label="Parcel fabric edit", commit=True):
        self.layers = [lx for lx in layers if lx is not None]
        self.label = label
        self.commit = commit
        self._started = []
        self.held = []          # layers this session turned editable and did not save
        self.marks = []         # (layer, undo index before this command)

    def __enter__(self):
        for lx in self.layers:
            if not lx.isEditable():
                if not lx.startEditing():
                    raise RuntimeError("Layer %s can't be edited" % lx.name())
                self._started.append(lx)
            stack = lx.undoStack()
            self.marks.append((lx, stack.index() if stack is not None else 0))
            lx.beginEditCommand(self.label)
        return self

    # Fields stamped with the OS login name of whoever is editing, when a
    # layer has them and the feature doesn't already carry a value (an
    # import can set its own CreatedByUser, e.g. who ran the import).
    USER_FIELDS_ON_ADD = ("CreatedByUser", "EditUser")

    @staticmethod
    def _current_user():
        try:
            return QgsApplication.userLoginName() or ""
        except Exception:
            return ""

    def add(self, layer, feature):
        user = self._current_user()
        if user:
            fields = layer.fields()
            for name in self.USER_FIELDS_ON_ADD:
                i = fields.indexOf(name)
                if i < 0:
                    continue
                v = feature.attribute(i)
                if v is None or v == "" or (hasattr(v, "isNull") and v.isNull()):
                    feature.setAttribute(i, user)
        if not layer.addFeature(feature):
            raise RuntimeError("Could not add a feature to %s" % layer.name())

    def change(self, layer, fid, field, value):
        i = layer.fields().indexOf(field)
        if i < 0:
            return
        if not layer.changeAttributeValue(fid, i, value):
            raise RuntimeError("Could not change %s on %s" % (field, layer.name()))
        if field == "RetiredByRecord" and value:
            self._stamp(layer, fid, "RetiredByUser")

    def _stamp(self, layer, fid, field):
        user = self._current_user()
        j = layer.fields().indexOf(field)
        if user and j >= 0:
            layer.changeAttributeValue(fid, j, user)

    def set_geometry(self, layer, fid, geom):
        if not layer.changeGeometry(fid, geom):
            raise RuntimeError("Could not change geometry on %s" % layer.name())

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            for lx in self.layers:
                lx.destroyEditCommand()
            for lx in self._started:
                lx.rollBack()
            return False
        for lx in self.layers:
            lx.endEditCommand()
        if not self.commit:
            # Leave the edit open. The caller saves with commitChanges, or
            # undoes back to marks and rollBack on held.
            self.held = list(self._started)
            return False
        tr = self._db_transaction()
        errors = []
        for lx in self._started:
            if errors:
                if tr is None:
                    lx.rollBack()       # one layer failed: don't save the rest without it
            elif not lx.commitChanges():
                errors.append("%s: %s" % (lx.name(), "; ".join(lx.commitErrors())))
                if tr is None:
                    lx.rollBack()
        if tr is not None and (errors or not _result_ok(tr.commit())):
            try:
                tr.rollback()
            except Exception:
                pass
            if not errors:
                errors.append("the database transaction could not be committed")
        if errors:
            raise RuntimeError("Save failed - " + " | ".join(errors))
        from .parcel_numbers import drop_unfiltered
        for lx in self._started:
            drop_unfiltered(lx)
        return False

    def _db_transaction(self):
        """One DB transaction across every started layer that can share one, so a later
        layer's failed write rolls everything on this edit back - instead of leaving an
        earlier layer's commit (a parcel retired, say) stranded with nothing to replace it.

        Wraps even a single such layer: commitChanges() on one layer still sends
        attribute changes and added features as separate statements, so without a
        transaction a feature-add failure can leave that same layer's own attribute
        changes (a parcel marked retired, say) already saved with nothing to replace it.

        Tried for anything but a plain "memory" layer, which can't support one at all -
        not just "mssql"/"postgres": a SQL Server table reached through a generic ODBC
        connection (rather than QGIS's own MSSQL provider) shows up as an "ogr" layer,
        same as GeoPackage, with no reliable way to tell the two apart by provider type
        alone. QgsTransaction.create()/begin() themselves say whether it actually works;
        if it doesn't, this falls back to committing each layer on its own exactly as
        before, so trying costs nothing on a backend that can't do it.
        """
        started = [lx for lx in self._started if lx.providerType() != "memory"]
        if not started:
            return None
        try:
            tr = QgsTransaction.create(started)
            if tr is None or not _result_ok(tr.begin()):
                return None
            return tr
        except Exception:
            return None
