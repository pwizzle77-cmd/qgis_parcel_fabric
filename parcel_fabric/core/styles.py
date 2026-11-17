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
        _label(lyr, 'coalesce("ParcelNumber", "Name")', QColor(PARCEL_COLORS[0]))
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
