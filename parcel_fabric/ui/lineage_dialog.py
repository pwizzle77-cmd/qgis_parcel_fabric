# -*- coding: utf-8 -*-
"""Lineage: where a parcel came from and what it became."""

from qgis.core import QgsCoordinateTransform, QgsFeatureRequest, QgsProject
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from ..core import lineage as L
from ..core.fabric import quote
from ..core.parcel_numbers import unfiltered

GID = Qt.ItemDataRole.UserRole


class LineageDialog(QDialog):
    def __init__(self, iface, manager, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.manager = manager
        self.fabric = manager.active
        self.gid = None
        self.truncated = False
        self.setWindowTitle("Parcel Lineage")
        self.resize(560, 520)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        lay = QVBoxLayout(self)
        row = QHBoxLayout()
        self.find = QLineEdit()
        self.find.setPlaceholderText("Parcel number (current or retired)")
        self.find.returnPressed.connect(self._find)
        go = QPushButton("Show")
        go.clicked.connect(self._find)
        sel = QPushButton("Use Selected Parcel")
        sel.clicked.connect(self._from_selection)
        row.addWidget(self.find, 1)
        row.addWidget(go)
        row.addWidget(sel)
        lay.addLayout(row)
        self.head = QLabel()
        self.head.setWordWrap(True)
        lay.addWidget(self.head)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Parcel", "How", "Record"])
        self.tree.setColumnWidth(0, 220)
        self.tree.itemDoubleClicked.connect(self._zoom)
        lay.addWidget(self.tree, 1)
        ex = QHBoxLayout()
        ex.addWidget(QLabel("Export"))
        for text, slot in (("CSV...", self._csv), ("Chart (DOT)...", self._dot),
                           ("Printable page...", self._html)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            ex.addWidget(b)
        ex.addStretch(1)
        lay.addLayout(ex)
        L.clear_cache()
        self._from_selection(quiet=True)

    # ------------------------------------------------------------------
    def _from_selection(self, quiet=False):
        sel = list(self.fabric.parcels.getSelectedFeatures())
        if len(sel) != 1:
            if not quiet:
                QMessageBox.information(self, "Lineage", "Select one parcel in the Parcels layer.")
            return
        self.show_gid(sel[0]["GlobalID"])

    def _find(self):
        num = self.find.text().strip()
        if not num:
            return
        req = QgsFeatureRequest().setFilterExpression('"ParcelNumber" = %s' % quote(num))
        for f in unfiltered(self.fabric.parcels).getFeatures(req):
            self.show_gid(f["GlobalID"])
            return
        QMessageBox.information(self, "Lineage", "No parcel has number %s." % num)

    def show_gid(self, gid):
        self.gid = gid
        L.clear_cache()
        i = L.parcel_info(self.fabric, gid)
        self.head.setText("<b>%s</b> %s &nbsp; <span style='color:gray'>%s, created by %s%s</span>" % (
            i["number"] or "(no number)", i["name"], i["type"], i["record"] or "?",
            ", retired" if i["retired"] else ""))
        self.up, up_trunc = L.walk(self.fabric, gid, up=True)
        self.down, down_trunc = L.walk(self.fabric, gid, up=False)
        self.truncated = up_trunc or down_trunc
        if self.truncated:
            self.head.setText(self.head.text() + "<br><span style='color:#a00'>INCOMPLETE: this "
                              "parcel's history is larger than shown below.</span>")
        self.tree.clear()
        for title, edges, up in (("Came from", self.up, True), ("Became", self.down, False)):
            top = QTreeWidgetItem([title + (" (none)" if not edges else ""), "", ""])
            self.tree.addTopLevelItem(top)
            self._grow(top, gid, edges, up, {gid})
            top.setExpanded(True)

    def _grow(self, item, gid, edges, up, seen):
        for p, c, op, rec in edges:
            if (c if up else p) != gid:
                continue
            other = p if up else c
            it = QTreeWidgetItem([L.label(self.fabric, other), op or "", L.record_name(self.fabric, rec)])
            it.setData(0, GID, other)
            item.addChild(it)
            if other not in seen:
                self._grow(it, other, edges, up, seen | {other})
            it.setExpanded(True)

    def _zoom(self, item, _col):
        gid = item.data(0, GID)
        if not gid:
            return
        req = QgsFeatureRequest().setFilterExpression('"GlobalID" = %s' % quote(gid))
        for f in unfiltered(self.fabric.parcels).getFeatures(req):
            canvas = self.iface.mapCanvas()
            xf = QgsCoordinateTransform(self.fabric.parcels.crs(), canvas.mapSettings().destinationCrs(),
                                        QgsProject.instance())
            box = f.geometry().boundingBox()
            canvas.setExtent(xf.transformBoundingBox(box.buffered(max(box.width(), box.height()) * 0.2 + 1)))
            canvas.flashGeometries([f.geometry()], self.fabric.parcels.crs())
            canvas.refresh()
            return

    def _ask(self, title, filt):
        if not self.gid:
            QMessageBox.information(self, "Lineage", "Show a parcel first.")
            return None
        name = (L.parcel_info(self.fabric, self.gid)["number"] or "lineage").replace("/", "-")
        p, _ = QFileDialog.getSaveFileName(self, title, name, filt)
        return p or None

    def _csv(self):
        p = self._ask("Export lineage", "CSV (*.csv)")
        if p:
            L.export_csv(self.fabric, self.up + self.down, p, self.truncated)

    def _dot(self):
        p = self._ask("Export lineage chart", "GraphViz (*.dot)")
        if p:
            L.export_dot(self.fabric, self.up + self.down, p, self.gid, self.truncated)

    def _html(self):
        p = self._ask("Printable lineage", "Web page (*.html)")
        if p:
            L.export_html(self.fabric, self.gid, self.up, self.down, p, self.truncated)
            from qgis.PyQt.QtCore import QUrl
            from qgis.PyQt.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(p))
