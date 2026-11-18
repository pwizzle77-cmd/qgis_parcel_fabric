# -*- coding: utf-8 -*-
"""Compare fabric parcels with an existing parcel layer."""

import statistics

from qgis.core import Qgis, QgsCoordinateTransform, QgsFeatureRequest, QgsProject
from qgis.gui import QgsFieldComboBox, QgsMapLayerComboBox
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout, QHeaderView,
    QLabel, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ..core import compare as CMP
from ..core import units as U
from ..core.fabric import CURRENT, PROP_FABRIC

COLS = ["Parcel", "Existing", "Fabric ac", "Existing ac", "Area diff %", "Max offset ft",
        "Shift ft", "Shift bearing", "Overlap %"]


class _Num(QTableWidgetItem):
    """Sorts by number, shows text."""

    def __init__(self, value, text):
        super().__init__(text)
        self.v = value if value is not None else float("-inf")

    def __lt__(self, other):
        return self.v < getattr(other, "v", 0)


class CompareDialog(QDialog):
    def __init__(self, iface, manager, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.manager = manager
        self.fabric = manager.active
        self.matches = []
        self.setWindowTitle("Compare with Existing Parcels")
        self.resize(820, 560)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.ref = QgsMapLayerComboBox()
        self.ref.setFilters(Qgis.LayerFilter.PolygonLayer)
        self.ref.setExceptedLayerList([lx for lx in QgsProject.instance().mapLayers().values()
                                       if lx.customProperty(PROP_FABRIC)])
        self.label_field = QgsFieldComboBox()
        self.match_field = QgsFieldComboBox()
        self.match_field.setAllowEmptyFieldName(True)
        self.ref.layerChanged.connect(self.label_field.setLayer)
        self.ref.layerChanged.connect(self.match_field.setLayer)
        self.label_field.setLayer(self.ref.currentLayer())
        self.match_field.setLayer(self.ref.currentLayer())
        self.match_field.setField("")
        self.scope = QComboBox()
        self.scope.addItem("Selected parcels", "selected")
        self.scope.addItem("Parcels in the map view", "view")
        self.scope.addItem("All current parcels", "all")
        self.flag = QDoubleSpinBox()
        self.flag.setRange(0.01, 1000)
        self.flag.setValue(1.0)
        self.flag.setSuffix(" ft")
        form.addRow("Existing parcel layer", self.ref)
        form.addRow("Show existing parcels by", self.label_field)
        form.addRow("Match on parcel number field", self.match_field)
        form.addRow("Compare", self.scope)
        form.addRow("Highlight offsets over", self.flag)
        lay.addLayout(form)
        tip = QLabel("Leave the match field empty to pair each fabric parcel with the existing polygon "
                     "that overlaps it most.")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: palette(mid);")
        lay.addWidget(tip)
        go = QPushButton("Compare")
        go.setDefault(True)
        go.clicked.connect(self._run)
        lay.addWidget(go)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.cellDoubleClicked.connect(self._zoom)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        diff = QPushButton("Show Differences on Map")
        diff.setToolTip("A scratch layer of the slivers where the two disagree (red: fabric only, "
                        "blue: existing only)")
        diff.clicked.connect(self._diff_layer)
        row.addStretch(1)
        row.addWidget(diff)
        lay.addLayout(row)

    def _items(self):
        fab = self.fabric
        lyr = fab.parcels
        mode = self.scope.currentData()
        if mode == "selected":
            feats = list(lyr.getSelectedFeatures())
        else:
            req = QgsFeatureRequest().setFilterExpression(CURRENT + ' AND ("IsSeed" IS NULL OR "IsSeed" = 0)')
            if mode == "view":
                xf = QgsCoordinateTransform(self.iface.mapCanvas().mapSettings().destinationCrs(), lyr.crs(),
                                            QgsProject.instance())
                req.setFilterRect(xf.transformBoundingBox(self.iface.mapCanvas().extent()))
            feats = list(lyr.getFeatures(req))
        return [(str(f["ParcelNumber"] or f["Name"] or "#%d" % f.id()), f.geometry(), f["ParcelNumber"])
                for f in feats if f.hasGeometry()]

    def _run(self):
        ref = self.ref.currentLayer()
        if ref is None:
            QMessageBox.information(self, self.windowTitle(), "Add your existing parcel layer to the map first.")
            return
        items = self._items()
        if not items:
            QMessageBox.information(self, self.windowTitle(), "No fabric parcels to compare - select some, "
                                    "or choose a different Compare option.")
            return
        try:
            self.matches = CMP.compare(items, ref, self.fabric.parcels.crs(), self.label_field.currentField(),
                                       self.match_field.currentField() or None)
        except Exception as e:
            QMessageBox.critical(self, self.windowTitle(), str(e))
            return
        self._fill()

    def _fill(self):
        flag = self.flag.value()
        t = self.table
        t.setSortingEnabled(False)
        t.setRowCount(0)
        offs = []
        for i, m in enumerate(self.matches):
            r = t.rowCount()
            t.insertRow(r)
            vals = [
                (None, m.label), (None, m.ref_label or "(no match)"),
                (m.area / 43560.0, "%.3f" % (m.area / 43560.0)),
                (m.ref_area / 43560.0 if m.ref_geom else None, "%.3f" % (m.ref_area / 43560.0) if m.ref_geom else ""),
                (m.diff_pct, "%+.2f" % m.diff_pct if m.diff_pct is not None else ""),
                (m.hausdorff, "%.2f" % m.hausdorff if m.hausdorff is not None else ""),
                (m.shift, "%.2f" % m.shift if m.shift is not None else ""),
                (None, U.azimuth_to_bearing(m.shift_az) if m.shift else ""),
                (m.overlap, "%.1f" % (m.overlap * 100) if m.overlap is not None else ""),
            ]
            for c, (v, txt) in enumerate(vals):
                it = _Num(v, txt) if v is not None else QTableWidgetItem(txt)
                it.setData(Qt.ItemDataRole.UserRole, i)
                if c == 5 and m.hausdorff is not None and m.hausdorff > flag:
                    it.setForeground(QColor("#b3261e"))
                t.setItem(r, c, it)
            if m.hausdorff is not None:
                offs.append(m.hausdorff)
        t.setSortingEnabled(True)
        nomatch = sum(1 for m in self.matches if m.ref_geom is None)
        if offs:
            self.summary.setText(
                "<b>%d parcels compared.</b> Max offset: median %.2f ft, worst %.2f ft; %d over %.2f ft. "
                "%d without a matching existing polygon. Double-click a row to zoom." % (
                    len(self.matches), statistics.median(offs), max(offs), sum(1 for o in offs if o > flag),
                    flag, nomatch))
        else:
            self.summary.setText("No existing polygons overlap these parcels.")

    def _zoom(self, row, _col):
        i = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        m = self.matches[i]
        canvas = self.iface.mapCanvas()
        xf = QgsCoordinateTransform(self.fabric.parcels.crs(), canvas.mapSettings().destinationCrs(),
                                    QgsProject.instance())
        box = m.geom.boundingBox()
        if m.ref_geom is not None:
            box.combineExtentWith(m.ref_geom.boundingBox())
        canvas.setExtent(xf.transformBoundingBox(box.buffered(max(box.width(), box.height()) * 0.2 + 2)))
        geoms = [m.geom] + ([m.ref_geom] if m.ref_geom is not None else [])
        canvas.flashGeometries(geoms, self.fabric.parcels.crs())
        canvas.refresh()

    def _diff_layer(self):
        if not self.matches:
            self._run()
            if not self.matches:
                return
        lyr = CMP.difference_layer(self.matches, self.fabric.parcels.crs(),
                                   "Fabric vs %s - differences" % self.ref.currentLayer().name())
        QgsProject.instance().addMapLayer(lyr)
        self.iface.messageBar().pushInfo("Parcel Fabric", "%d sliver(s) added as a scratch layer "
                                         "(not saved unless you export it)." % lyr.featureCount())
