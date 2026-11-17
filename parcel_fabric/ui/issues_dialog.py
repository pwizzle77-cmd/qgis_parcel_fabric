# -*- coding: utf-8 -*-
"""A list of problems (from Build or Check Fabric) you can click to zoom to."""

from qgis.core import (QgsCoordinateTransform, QgsGeometry, QgsPointXY, QgsProject,
                       QgsRectangle)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHeaderView, QLabel, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

SEV_COLORS = {"error": "#b3261e", "warning": "#8a5a00", "info": "#555555"}


class IssuesDialog(QDialog):
    def __init__(self, iface, title, summary, issues, crs, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.crs = crs  # CRS of the issue coordinates (the fabric's)
        self.issues = issues
        self.setWindowTitle(title)
        self.resize(620, 380)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        lay = QVBoxLayout(self)
        lbl = QLabel(summary + "   Double-click a row to zoom to it.")
        lbl.setWordWrap(True)
        lay.addWidget(lbl)
        t = QTableWidget(len(issues), 2)
        t.setHorizontalHeaderLabels(["Severity", "Problem"])
        t.verticalHeader().setVisible(False)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        order = {"error": 0, "warning": 1, "info": 2}
        self.issues = sorted(issues, key=lambda i: order.get(i.severity, 3))
        for r, i in enumerate(self.issues):
            a = QTableWidgetItem(i.severity.capitalize())
            a.setForeground(QColor(SEV_COLORS.get(i.severity, "#000")))
            t.setItem(r, 0, a)
            t.setItem(r, 1, QTableWidgetItem(i.message))
        t.cellDoubleClicked.connect(self._zoom)
        lay.addWidget(t)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.close)
        lay.addWidget(bb)

    def _zoom(self, row, _col):
        i = self.issues[row]
        canvas = self.iface.mapCanvas()
        lyr = QgsProject.instance().mapLayer(i.layer_id) if i.layer_id else None
        if lyr is not None and i.fid is not None:
            lyr.selectByIds([i.fid])
        if i.x is None and lyr is not None and i.fid is not None:
            f = lyr.getFeature(i.fid)
            if f.hasGeometry():
                xf = QgsCoordinateTransform(lyr.crs(), canvas.mapSettings().destinationCrs(),
                                            QgsProject.instance())
                canvas.setExtent(xf.transformBoundingBox(f.geometry().boundingBox().buffered(20)))
                canvas.flashGeometries([f.geometry()], lyr.crs())
                canvas.refresh()
            return
        if i.x is None:
            return
        dest = canvas.mapSettings().destinationCrs()
        xf = QgsCoordinateTransform(self.crs, dest, QgsProject.instance())
        p = xf.transform(QgsPointXY(i.x, i.y))
        ext = canvas.extent()
        if not ext.contains(p) or ext.width() > 2000:
            w = min(ext.width(), 400.0)
            canvas.setExtent(QgsRectangle(p.x() - w / 2, p.y() - w / 2, p.x() + w / 2, p.y() + w / 2))
        canvas.flashGeometries([QgsGeometry.fromPointXY(p)], dest)
        canvas.refresh()
