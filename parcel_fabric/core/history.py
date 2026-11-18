# -*- coding: utf-8 -*-
"""Parcel history through time.

Every parcel gets QPF_StartDate (the recorded date of the record that
created it) and QPF_EndDate (the recorded date of the record that retired
it).  A "Parcel History" layer shows ALL parcels, current and retired, and
is time-enabled, so QGIS's Temporal Controller (or the Parcel History
window) shows the parcel map as it stood on any date.
"""

from qgis.core import (
    Qgis,
    QgsDateTimeRange,
    QgsFeatureRequest,
    QgsProject,
    QgsRuleBasedRenderer,
    QgsFillSymbol,
    QgsPalLayerSettings,
    QgsTextFormat,
    QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtCore import QDate, QDateTime, QTime
from qgis.PyQt.QtGui import QColor

from . import schema as S
from .fabric import PROP_FABRIC, PROP_ROLE, EditSession, quote
from .parcel_numbers import unfiltered

ROLE_HISTORY = "history"
GEN_COLORS = ["#6b6b6b", "#1f5fa8", "#b5541b", "#2f7d32", "#7b3fa0", "#8a6d00", "#c8007a"]


def _qdate(v):
    if v is None:
        return None
    if isinstance(v, QDate):
        return v if v.isValid() else None
    if isinstance(v, QDateTime):
        return v.date() if v.isValid() else None
    try:
        if hasattr(v, "isNull") and v.isNull():
            return None
    except TypeError:
        pass
    d = QDate.fromString(str(v)[:10], "yyyy-MM-dd")
    return d if d.isValid() else None


def record_dates(fabric):
    """Record GlobalID -> QDate (records without a date are left out)."""
    out = {}
    for r in fabric.records.getFeatures():
        d = _qdate(r["RecordedDate"])
        if d is not None:
            out[r["GlobalID"]] = d
    return out


def record_names(fabric):
    return {r["GlobalID"]: (r["Name"] or "") for r in fabric.records.getFeatures()}


def refresh_dates(fabric, records=None):
    """Set QPF_StartDate / QPF_EndDate from the records' recorded dates.

    ``records``: only parcels created or retired by these record GlobalIDs
    (default: every parcel).  Returns how many parcels changed.
    """
    lyr = unfiltered(fabric.parcels)
    if lyr.fields().indexOf("QPF_StartDate") < 0:
        S.add_missing_fields(lyr, S.ROLE_PARCELS)
    dates = record_dates(fabric)
    req = QgsFeatureRequest()
    if records:
        lst = ", ".join(quote(r) for r in records if r)
        req.setFilterExpression('"CreatedByRecord" IN (%s) OR "RetiredByRecord" IN (%s)' % (lst, lst))
    req.setFlags(Qgis.FeatureRequestFlag.NoGeometry)
    changes = []
    for f in lyr.getFeatures(req):
        start = dates.get(f["CreatedByRecord"])
        end = dates.get(f["RetiredByRecord"]) if f["RetiredByRecord"] else None
        if _qdate(f["QPF_StartDate"]) != start or _qdate(f["QPF_EndDate"]) != end:
            changes.append((f.id(), start, end))
    if changes:
        with EditSession([lyr], "History dates") as ed:
            for fid, start, end in changes:
                ed.change(lyr, fid, "QPF_StartDate", start)
                ed.change(lyr, fid, "QPF_EndDate", end)
        fabric.parcels.reload()
        h = find_history_layer(fabric)
        if h is not None:
            h.reload()
    return len(changes)


def undated_records(fabric):
    """Records that created or retired parcels but have no recorded date."""
    dates = record_dates(fabric)
    used = set()
    req = QgsFeatureRequest().setFlags(Qgis.FeatureRequestFlag.NoGeometry)
    for f in unfiltered(fabric.parcels).getFeatures(req):
        for k in ("CreatedByRecord", "RetiredByRecord"):
            if f[k]:
                used.add(f[k])
    return [g for g in used if g not in dates]


# --------------------------------------------------------------------------
# The time-enabled layer
# --------------------------------------------------------------------------
def find_history_layer(fabric, project=None):
    project = project or QgsProject.instance()
    for lyr in project.mapLayers().values():
        if lyr.customProperty(PROP_FABRIC) == fabric.id and lyr.customProperty(PROP_ROLE) == ROLE_HISTORY:
            return lyr
    return None


def history_layer(fabric, project=None):
    """The fabric's Parcel History layer (created and added if missing)."""
    project = project or QgsProject.instance()
    lyr = find_history_layer(fabric, project)
    if lyr is not None:
        return lyr
    lyr = unfiltered(fabric.parcels)
    lyr.setName("Parcel History")
    lyr.setCustomProperty(PROP_FABRIC, fabric.id)
    lyr.setCustomProperty(PROP_ROLE, ROLE_HISTORY)
    tp = lyr.temporalProperties()
    tp.setMode(Qgis.VectorTemporalMode.FeatureDateTimeStartAndEndFromFields)
    tp.setStartField("QPF_StartDate")
    tp.setEndField("QPF_EndDate")
    tp.setLimitMode(Qgis.VectorTemporalLimitMode.IncludeBeginExcludeEnd)
    tp.setIsActive(True)
    style_history(lyr, None)
    project.addMapLayer(lyr, False)
    root = project.layerTreeRoot()
    parcels_node = root.findLayer(fabric.parcels.id())
    group = parcels_node.parent() if parcels_node is not None else root
    node = group.insertLayer(0, lyr)
    node.setItemVisibilityChecked(True)
    return lyr


def style_history(lyr, generation=None):
    """Retired parcels dashed grey, current blue; or colour by generation.

    ``generation`` maps GlobalID -> generation number for a family view.
    """
    root = QgsRuleBasedRenderer.Rule(None)
    if generation:
        by_gen = {}
        for gid, g in generation.items():
            by_gen.setdefault(g, []).append(gid)
        for g in sorted(by_gen):
            c = QColor(GEN_COLORS[g % len(GEN_COLORS)])
            sym = QgsFillSymbol.createSimple({
                "color": "%d,%d,%d,70" % (c.red(), c.green(), c.blue()),
                "outline_color": c.name(), "outline_width": "0.6"})
            label = "Original" if g == 0 else "Generation %d" % g
            expr = '"GlobalID" IN (%s)' % ", ".join(quote(x) for x in by_gen[g])
            root.appendChild(QgsRuleBasedRenderer.Rule(sym, 0, 0, expr, label))
    else:
        cur = QgsFillSymbol.createSimple({"color": "31,95,168,40", "outline_color": "#1f5fa8",
                                          "outline_width": "0.4"})
        old = QgsFillSymbol.createSimple({"color": "120,120,120,25", "outline_color": "#777777",
                                          "outline_width": "0.3", "outline_style": "dash"})
        root.appendChild(QgsRuleBasedRenderer.Rule(cur, 0, 0, '"RetiredByRecord" IS NULL', "Current"))
        root.appendChild(QgsRuleBasedRenderer.Rule(old, 0, 0, '"RetiredByRecord" IS NOT NULL', "Retired"))
    lyr.setRenderer(QgsRuleBasedRenderer(root))
    s = QgsPalLayerSettings()
    s.fieldName = 'coalesce("ParcelNumber", "Name")'
    s.isExpression = True
    fmt = QgsTextFormat()
    fmt.setSize(9)
    fmt.setColor(QColor("#222222"))
    s.setFormat(fmt)
    lyr.setLabeling(QgsVectorLayerSimpleLabeling(s))
    lyr.setLabelsEnabled(True)
    lyr.triggerRepaint()


def day_range(qdate):
    """The temporal range covering one day."""
    start = QDateTime(qdate, QTime(0, 0, 0))
    return QgsDateTimeRange(start, start.addDays(1), True, False)


def show_date(iface, qdate):
    """Show the map as it stood on ``qdate`` (through the Temporal Controller)."""
    nav = iface.mapCanvas().temporalController()
    rng = day_range(qdate)
    try:
        nav.setNavigationMode(Qgis.TemporalNavigationMode.FixedRange)
        nav.setTemporalExtents(rng)
    except AttributeError:
        iface.mapCanvas().setTemporalRange(rng)
    iface.mapCanvas().refresh()


def set_project_time_range(fabric):
    """Make the project's time range span every parcel date."""
    dates = list(record_dates(fabric).values())
    if not dates:
        return
    lo, hi = min(dates), max(dates)
    QgsProject.instance().timeSettings().setTemporalRange(
        QgsDateTimeRange(QDateTime(lo, QTime(0, 0)), QDateTime(hi.addDays(1), QTime(0, 0))))
