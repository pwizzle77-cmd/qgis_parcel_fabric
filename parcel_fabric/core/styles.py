# -*- coding: utf-8 -*-
"""Default symbols for fabric layers."""

from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsFillSymbol,
    QgsRendererCategory,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsPalLayerSettings,
    QgsSingleSymbolRenderer,
    QgsTextFormat,
    QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtGui import QColor

from . import schema as S
from .fabric import PROP_ROLE
from .geomio import unit_word

PARCEL_COLORS = ["#1f5fa8", "#b5541b", "#2f7d32", "#7b3fa0", "#8a6d00"]


def apply(lyr, parcel_types=()):
    role = lyr.customProperty(PROP_ROLE)
    if role == S.ROLE_POINTS:
        sym = QgsMarkerSymbol.createSimple({"name": "circle", "size": "1.6",
                                            "color": "255,255,255", "outline_color": "40,40,40",
                                            "outline_width": "0.3"})
    elif role == S.ROLE_LINES:
        sym = QgsLineSymbol.createSimple({"color": "60,60,60", "width": "0.45"})
    elif role == S.ROLE_CONNECTIONS:
        sym = QgsLineSymbol.createSimple({"color": "120,120,120", "width": "0.3",
                                          "line_style": "dash"})
    elif role == S.ROLE_RECORDS:
        sym = QgsFillSymbol.createSimple({"color": "255,200,0,40", "outline_color": "230,150,0",
                                          "outline_width": "0.6", "outline_style": "dash"})
    elif role == S.ROLE_PARCELS:
        cats = []
        for i, t in enumerate(parcel_types):
            c = QColor(PARCEL_COLORS[i % len(PARCEL_COLORS)])
            cats.append(QgsRendererCategory(t, _parcel_symbol(c), t))
        cats.append(QgsRendererCategory(None, _parcel_symbol(QColor("#777777")), "Other"))
        lyr.setRenderer(QgsCategorizedSymbolRenderer("ParcelType", cats))
        _label(lyr, parcel_label_expression(lyr.crs()), QColor(PARCEL_COLORS[0]))
        lyr.triggerRepaint()
        return
    elif role == S.ROLE_LOTS:
        sym = QgsFillSymbol.createSimple({"color": "140,90,180,25", "outline_color": "110,60,150",
                                          "outline_width": "0.25"})
        lyr.setRenderer(QgsSingleSymbolRenderer(sym))
        _label(lyr, "coalesce(\"Lot\", '')", QColor("#6e3c96"))
        lyr.triggerRepaint()
        return
    elif role == S.ROLE_SUBDIVISIONS:
        sym = QgsFillSymbol.createSimple({"color": "40,140,80,20", "outline_color": "30,110,60",
                                          "outline_width": "0.45", "outline_style": "dash"})
        lyr.setRenderer(QgsSingleSymbolRenderer(sym))
        _label(lyr, "coalesce(\"Name\", '')", QColor("#1e7a45"))
        lyr.triggerRepaint()
        return
    else:
        return
    lyr.setRenderer(QgsSingleSymbolRenderer(sym))
    lyr.triggerRepaint()


def _parcel_symbol(c):
    return QgsFillSymbol.createSimple({
        "color": "%d,%d,%d,25" % (c.red(), c.green(), c.blue()),
        "outline_color": "%d,%d,%d" % (c.red(), c.green(), c.blue()),
        "outline_width": "0.2"})


def parcel_label_expression(crs):
    """Number, lot, width x depth, and acres when the polygon is over 1 acre.

    area($geometry) stays in the layer CRS. $area follows the project ellipsoid
    and would disagree with the acres the fabric already stores.
    """
    per = 4046.8564224 if unit_word(crs) == "meters" else 43560.0
    acres = "area($geometry) / %s" % per
    return (
        "trim(concat("
        "coalesce(\"ParcelNumber\", \"Name\"), "
        "if(\"Lot\" is null or \"Lot\" = '', '', '\\nLot ' || \"Lot\"), "
        "if(\"LotWidth\" is null, '', '\\n' || format_number(\"LotWidth\", 1) || ' x ' || "
        "format_number(coalesce(\"LotDepth\", 0), 1)), "
        "if(%s > 1, '\\n' || format_number(%s, 2) || ' ac', '')"
        "))" % (acres, acres))


def _label(lyr, expression, color):
    s = QgsPalLayerSettings()
    s.fieldName = expression
    s.isExpression = True
    fmt = QgsTextFormat()
    fmt.setSize(9)
    fmt.setColor(color.darker(140))
    s.setFormat(fmt)
    lyr.setLabeling(QgsVectorLayerSimpleLabeling(s))
    lyr.setLabelsEnabled(True)
