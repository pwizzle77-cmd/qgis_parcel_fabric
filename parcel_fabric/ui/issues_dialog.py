# -*- coding: utf-8 -*-
"""A list of problems (from Build or Check Fabric) you can click to zoom to."""

from qgis.core import (Qgis, QgsCoordinateTransform, QgsGeometry, QgsPointXY, QgsProject,
                       QgsRectangle)
from qgis.gui import QgsRubberBand
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QDialogButtonBox, QHeaderView, QLabel,
    QProgressBar, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

SEV_COLORS = {"error": "#b3261e", "warning": "#8a5a00", "info": "#555555"}


class IssuesDialog(QDialog):
    def __init__(self, iface, title, summary, issues, crs, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.crs = crs  # CRS of the issue coordinates (the fabric's)
        self.issues = []
        self._band = None
        self.setWindowTitle(title)
        self.resize(620, 420)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        lay = QVBoxLayout(self)
        self.status = QLabel(summary)
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setFormat("%p%")
        self.bar.setVisible(False)
        lay.addWidget(self.bar)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Severity", "Problem"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.cellDoubleClicked.connect(self._zoom)
        lay.addWidget(self.table)
        self.show_issues(issues, summary)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        zoom = bb.addButton("Zoom to Error", QDialogButtonBox.ButtonRole.ActionRole)
        zoom.clicked.connect(self._zoom_selected)
        bb.rejected.connect(self.close)
        lay.addWidget(bb)

    def closeEvent(self, event):
        if getattr(self, "_running", False):
            event.ignore()
            return
        self._clear_marks()
        super().closeEvent(event)

    def set_progress(self, pct, text=None):
        """Show the bar and name the check that is running."""
        self.bar.setVisible(True)
        self.bar.setValue(max(0, min(100, int(pct))))
        if text:
            self.status.setText(text)
        QApplication.processEvents()

    def show_issues(self, issues, summary):
        self.bar.setVisible(False)
        self.status.setText(summary + "   Double-click a row, or Zoom to Error. "
                            "Marks stay on the map until this window closes.")
        self._mark()
        order = {"error": 0, "warning": 1, "info": 2}
        self.issues = sorted(issues or [], key=lambda i: order.get(i.severity, 3))
        t = self.table
        t.setSortingEnabled(False)
        t.setRowCount(len(self.issues))
        for r, i in enumerate(self.issues):
            a = QTableWidgetItem(i.severity.capitalize())
            a.setForeground(QColor(SEV_COLORS.get(i.severity, "#000")))
            t.setItem(r, 0, a)
            t.setItem(r, 1, QTableWidgetItem(i.message))

    def _zoom_selected(self):
        row = self.table.currentRow()
        if row < 0 and self.issues:
            row = 0
        if row >= 0:
            self._zoom(row, 0)

    def _clear_marks(self):
        if self._band is not None:
            self._band.reset()
            self._band = None

    def _mark(self):
        """Red marks for errors and warnings. Notes are left off the map."""
        self._clear_marks()
        pts = [i for i in self.issues if i.severity != "info" and i.x is not None]
        if not pts:
            return
        canvas = self.iface.mapCanvas()
        dest = canvas.mapSettings().destinationCrs()
        xf = QgsCoordinateTransform(self.crs, dest, QgsProject.instance())
        band = QgsRubberBand(canvas, Qgis.GeometryType.Point)
        band.setColor(QColor("#b3261e"))
        band.setIcon(QgsRubberBand.IconType.ICON_CIRCLE)
        band.setWidth(3)
        for i in pts:
            band.addPoint(xf.transform(QgsPointXY(i.x, i.y)), False)
        band.show()
        self._band = band

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
