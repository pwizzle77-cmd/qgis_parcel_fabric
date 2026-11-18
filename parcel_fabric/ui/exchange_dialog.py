# -*- coding: utf-8 -*-
"""Import an ArcGIS Pro fabric or LandXML; export DXF."""

import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QTabWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from ..core import exchange as X


def _file_row(edit, slot):
    row = QHBoxLayout()
    b = QPushButton("Browse...")
    b.clicked.connect(slot)
    row.addWidget(edit, 1)
    row.addWidget(b)
    return row


class ExchangeDialog(QDialog):
    def __init__(self, iface, manager, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.manager = manager
        self.setWindowTitle("Import / Export")
        self.resize(620, 480)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs)
        tabs.addTab(self._pro_tab(), "ArcGIS Pro fabric")
        tabs.addTab(self._xml_tab(), "LandXML")
        tabs.addTab(self._dxf_tab(), "DXF export")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(120)
        lay.addWidget(self.log)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.close)
        lay.addWidget(bb)

    @property
    def fab(self):
        return self.manager.active

    def _say(self, text):
        self.log.appendPlainText(text)
        from qgis.PyQt.QtWidgets import QApplication
        QApplication.processEvents()

    def _record(self):
        rid = self.manager.active_record_id
        if not rid:
            QMessageBox.warning(self, self.windowTitle(), "Set an active record first (Records panel).")
        return rid

    # ------------------------------------------------------------------
    def _pro_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        info = QLabel("Copies an ArcGIS Pro parcel fabric (exported to a File Geodatabase or GeoPackage) "
                      "into this fabric. Field names match, so records, points, lines and parcels come "
                      "across with their GlobalIDs and record links; each Pro parcel type goes into "
                      "Parcels / Parcel_Lines with its ParcelType. Z/M is dropped and coordinates are "
                      "reprojected to the fabric's CRS. Import into an empty fabric.")
        info.setWordWrap(True)
        f.addRow(info)
        self.pro_path = QLineEdit()
        f.addRow("Source", _file_row(self.pro_path, self._pick_pro))
        self.pro_tbl = QTableWidget(0, 3)
        self.pro_tbl.setHorizontalHeaderLabels(["Parcel type", "Polygons", "Lines"])
        self.pro_tbl.horizontalHeader().setStretchLastSection(True)
        f.addRow("Parcel types found", self.pro_tbl)
        self.pro_core = QLabel()
        f.addRow("Other tables", self.pro_core)
        b = QPushButton("Import")
        b.clicked.connect(self._import_pro)
        f.addRow("", b)
        self.pro_tables = None
        return w

    def _pick_pro(self):
        path = QFileDialog.getExistingDirectory(self, "File Geodatabase (.gdb folder)")
        if not path or not path.lower().endswith(".gdb"):
            path, _ = QFileDialog.getOpenFileName(self, "Or a GeoPackage", "", "GeoPackage (*.gpkg)")
        if not path:
            return
        self.pro_path.setText(path)
        try:
            t = X.detect_fabric(X.source_layers(path))
        except Exception as e:
            QMessageBox.warning(self, self.windowTitle(), str(e))
            return
        self.pro_tables = t
        self.pro_tbl.setRowCount(len(t["types"]))
        for i, (pt, (poly, lines)) in enumerate(sorted(t["types"].items())):
            for j, v in enumerate((pt, poly, lines)):
                self.pro_tbl.setItem(i, j, QTableWidgetItem(v))
        self.pro_core.setText("Records: %s   Points: %s   Connections: %s" % (
            t["records"] or "-", t["points"] or "-", t["connections"] or "-"))

    def _import_pro(self):
        if self.fab is None or not self.pro_tables:
            return
        # use the (possibly edited) parcel type names from the table
        types = {}
        for i in range(self.pro_tbl.rowCount()):
            name = self.pro_tbl.item(i, 0).text().strip()
            types[name] = (self.pro_tbl.item(i, 1).text(), self.pro_tbl.item(i, 2).text())
        tables = dict(self.pro_tables, types=types)
        try:
            for m in X.import_pro(self.fab, self.pro_path.text(), tables, feedback=self._say):
                self._say(m)
        except Exception as e:
            QMessageBox.critical(self, self.windowTitle(), "Import failed: %s" % e)
            return
        self.manager.refresh()
        self.iface.mapCanvas().refresh()
        self._say("Done. Run Check Fabric to review.")

    # ------------------------------------------------------------------
    def _xml_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        info = QLabel("Reads the Parcels in a LandXML file (Civil 3D, Carlson, TBC ...). Lines shared by "
                      "two parcels are added once, then the parcels are built under the active record "
                      "and get their LandXML names. Coordinates must already be in the fabric's CRS.")
        info.setWordWrap(True)
        f.addRow(info)
        self.xml_path = QLineEdit()
        f.addRow("LandXML file", _file_row(self.xml_path, lambda: self._pick(self.xml_path,
                                                                             "LandXML (*.xml *.landxml)")))
        self.xml_type = QComboBox()
        if self.fab is not None:
            self.xml_type.addItems(self.fab.parcel_types)
        f.addRow("Parcel type", self.xml_type)
        b = QPushButton("Import")
        b.clicked.connect(self._import_xml)
        f.addRow("", b)
        return w

    def _pick(self, edit, filt, save=False):
        if save:
            path, _ = QFileDialog.getSaveFileName(self, "Save", edit.text(), filt)
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Open", edit.text(), filt)
        if path:
            edit.setText(path)

    def _import_xml(self):
        rid = self._record()
        if self.fab is None or not rid or not os.path.exists(self.xml_path.text()):
            return
        try:
            nl, npt, rep, named = X.import_landxml(self.fab, self.xml_path.text(),
                                                   self.xml_type.currentText(), rid)
        except Exception as e:
            QMessageBox.critical(self, self.windowTitle(), "Import failed: %s" % e)
            return
        self._say("LandXML: %d lines, %d points, %d parcels built (%d named)%s" % (
            nl, npt, rep.created, named, "; %d issue(s) - run Check Fabric" % len(rep.issues) if rep.issues else ""))
        self.iface.mapCanvas().refresh()

    # ------------------------------------------------------------------
    def _dxf_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        info = QLabel("Writes the fabric's current lines, connections, points and parcels to a DXF for "
                      "CAD, with the layers' symbols and labels.")
        info.setWordWrap(True)
        f.addRow(info)
        self.dxf_path = QLineEdit()
        f.addRow("DXF file", _file_row(self.dxf_path, lambda: self._pick(self.dxf_path, "DXF (*.dxf)", True)))
        self.dxf_view = QCheckBox("Only the current map view")
        f.addRow("", self.dxf_view)
        b = QPushButton("Export")
        b.clicked.connect(self._export)
        f.addRow("", b)
        return w

    def _export(self):
        if self.fab is None or not self.dxf_path.text():
            return
        path = self.dxf_path.text()
        if not path.lower().endswith(".dxf"):
            path += ".dxf"
        ext = None
        if self.dxf_view.isChecked():
            from qgis.core import QgsCoordinateTransform, QgsProject
            c = self.iface.mapCanvas()
            ext = QgsCoordinateTransform(c.mapSettings().destinationCrs(), self.fab.parcels.crs(),
                                         QgsProject.instance()).transformBoundingBox(c.extent())
        try:
            X.export_dxf(self.fab, path, ext)
        except Exception as e:
            QMessageBox.critical(self, self.windowTitle(), str(e))
            return
        self._say("Wrote %s" % path)
