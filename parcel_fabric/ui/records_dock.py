# -*- coding: utf-8 -*-
"""Records panel: pick the fabric, create records, set the active record, build."""

from qgis.core import QgsCoordinateTransform, QgsFeatureRequest, QgsProject
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDockWidget, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QToolButton,
    QVBoxLayout, QWidget,
)

from ..core.build import build_parcels
from .issues_dialog import IssuesDialog
from .record_dialog import RecordDialog, open_document

GID_ROLE = Qt.ItemDataRole.UserRole


class RecordsDock(QDockWidget):
    def __init__(self, iface, manager, parent=None):
        super().__init__("Parcel Fabric Records", parent or iface.mainWindow())
        self.setObjectName("ParcelFabricRecordsDock")
        self.iface = iface
        self.manager = manager
        self._watched = None
        w = QWidget()
        lay = QVBoxLayout(w)

        top = QHBoxLayout()
        top.addWidget(QLabel("Fabric"))
        self.fabric_combo = QComboBox()
        self.fabric_combo.currentIndexChanged.connect(self._fabric_picked)
        top.addWidget(self.fabric_combo, 1)
        lay.addLayout(top)

        ar = QHBoxLayout()
        self.active_lbl = QLabel()
        f = QFont()
        f.setBold(True)
        self.active_lbl.setFont(f)
        self.active_lbl.setWordWrap(True)
        clear = QToolButton()
        clear.setText("Clear")
        clear.setToolTip("No active record")
        clear.clicked.connect(lambda: self.manager.set_active_record(None))
        ar.addWidget(self.active_lbl, 1)
        ar.addWidget(clear)
        lay.addLayout(ar)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Find a record...")
        self.search.textChanged.connect(self._filter)
        lay.addWidget(self.search)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Name", "Type", "Book", "Page", "Recorded", "Parcels"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setSortingEnabled(True)
        self.table.cellDoubleClicked.connect(lambda *_: self._activate())
        lay.addWidget(self.table, 1)

        for row in [
            [("New Record...", self._new_record, "Create a record for a plat, deed or survey"),
             ("Edit...", self._edit_record, "Correct the record's name, type, book, page, document..."),
             ("Set Active", self._activate, "New lines and parcels will belong to this record")],
            [("Zoom To", self._zoom, "Zoom to the record's parcels"),
             ("Document", self._open_doc, "Open the record's scanned document"),
             ("Settings...", self._settings, "Fabric settings: documents, parcel numbers, tolerances")],
        ]:
            hb = QHBoxLayout()
            for text, slot, tip in row:
                b = QPushButton(text)
                b.setToolTip(tip)
                b.clicked.connect(slot)
                hb.addWidget(b)
            lay.addLayout(hb)

        b2 = QHBoxLayout()
        b2.addWidget(QLabel("Build"))
        self.type_combo = QComboBox()
        b2.addWidget(self.type_combo, 1)
        self.build_rec = QPushButton("Active Record")
        self.build_rec.setToolTip("Build parcels from closed loops of the active record's lines")
        self.build_rec.clicked.connect(lambda: self._build(False))
        self.build_ext = QPushButton("In View")
        self.build_ext.setToolTip("Build parcels from closed loops visible in the map")
        self.build_ext.clicked.connect(lambda: self._build(True))
        b2.addWidget(self.build_rec)
        b2.addWidget(self.build_ext)
        lay.addLayout(b2)

        self.current_only = QCheckBox("Show only current features (hide retired)")
        self.current_only.setChecked(True)
        self.current_only.toggled.connect(self._current_only)
        lay.addWidget(self.current_only)

        self.setWidget(w)
        manager.fabricsChanged.connect(self._load_fabrics)
        manager.activeFabricChanged.connect(self._reload)
        manager.activeRecordChanged.connect(lambda *_: self._update_active())
        self._load_fabrics()

    # ------------------------------------------------------------------
    def _load_fabrics(self):
        self.fabric_combo.blockSignals(True)
        self.fabric_combo.clear()
        for f in self.manager.fabrics:
            self.fabric_combo.addItem(f.name, f.id)
        a = self.manager.active
        if a:
            self.fabric_combo.setCurrentIndex(self.fabric_combo.findData(a.id))
        self.fabric_combo.blockSignals(False)
        self._reload()

    def _fabric_picked(self, _i):
        self.manager.set_active(self.fabric_combo.currentData())

    def _reload(self):
        fab = self.manager.active
        if self._watched is not None:
            try:
                self._watched.dataChanged.disconnect(self._fill)
            except (TypeError, RuntimeError):
                pass
            self._watched = None
        self.type_combo.clear()
        enabled = fab is not None
        for wdg in (self.table, self.build_rec, self.build_ext, self.type_combo, self.search):
            wdg.setEnabled(enabled)
        if fab is None:
            self.table.setRowCount(0)
            self.active_lbl.setText("No parcel fabric in this project. Use New Fabric or Add Fabric.")
            return
        self.type_combo.addItems(fab.parcel_types)
        self._watched = fab.records
        self._watched.dataChanged.connect(self._fill)
        self._fill()

    def _fill(self):
        fab = self.manager.active
        if fab is None:
            return
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        active = self.manager.active_record_id
        for f in fab.records.getFeatures(QgsFeatureRequest()):
            r = self.table.rowCount()
            self.table.insertRow(r)
            name = QTableWidgetItem(str(f["Name"] or ""))
            name.setData(GID_ROLE, f["GlobalID"])
            if f["GlobalID"] == active:
                fnt = name.font()
                fnt.setBold(True)
                name.setFont(fnt)
            d = f["RecordedDate"]
            dtxt = d.toString("yyyy-MM-dd") if hasattr(d, "toString") and d else ""
            cnt = QTableWidgetItem()
            cnt.setData(Qt.ItemDataRole.DisplayRole, int(f["ParcelCount"] or 0))
            for c, it in enumerate([name, QTableWidgetItem(str(f["RecordType"] or "")),
                                    QTableWidgetItem(str(f["Book"] or "")),
                                    QTableWidgetItem(str(f["Page"] or "")),
                                    QTableWidgetItem(dtxt), cnt]):
                self.table.setItem(r, c, it)
        self.table.setSortingEnabled(True)
        self._filter(self.search.text())
        self._update_active()

    def _filter(self, text):
        t = text.lower().strip()
        for r in range(self.table.rowCount()):
            hit = not t or any(t in (self.table.item(r, c).text().lower())
                               for c in range(self.table.columnCount()) if self.table.item(r, c))
            self.table.setRowHidden(r, not hit)

    def _update_active(self):
        fab = self.manager.active
        if fab is None:
            return
        rec = self.manager.active_record()
        if rec is None:
            self.active_lbl.setText("Active record: none")
        else:
            self.active_lbl.setText("Active record: %s" % (rec["Name"] or rec["GlobalID"]))

    def _selected_gid(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.table.item(rows[0].row(), 0).data(GID_ROLE)

    # ------------------------------------------------------------------
    def _new_record(self):
        fab = self.manager.active
        if fab is None:
            return
        dlg = RecordDialog(fab, parent=self)
        if not dlg.exec():
            return
        v = dlg.values()
        try:
            gid = fab.create_record(**v)
            if dlg.doc_src:
                fab.update_record(gid, doc_url=dlg.store_document(gid))
        except Exception as e:
            QMessageBox.critical(self, "New Record", str(e))
            return
        if dlg.activate.isChecked():
            self.manager.set_active_record(gid)
        self._fill()

    def _edit_record(self):
        fab = self.manager.active
        gid = self._selected_gid()
        if fab is None or not gid:
            QMessageBox.information(self, "Edit Record", "Select a record in the list first.")
            return
        rec = fab.record_by_id(gid)
        if rec is None:
            return
        dlg = RecordDialog(fab, fab.record_values(rec), parent=self)
        if not dlg.exec():
            return
        v = dlg.values()
        try:
            if dlg.doc_src:
                v["doc_url"] = dlg.store_document(gid)
            else:
                moved = dlg.rename_existing(gid)
                if moved:
                    v["doc_url"] = moved
            fab.update_record(gid, **v)
        except Exception as e:
            QMessageBox.critical(self, "Edit Record", str(e))
            return
        from ..core.build import _dates
        _dates(fab, [gid])          # a changed recorded date moves the history
        self._fill()
        self._update_active()

    def _open_doc(self):
        fab = self.manager.active
        gid = self._selected_gid() or self.manager.active_record_id
        rec = fab.record_by_id(gid) if fab and gid else None
        if rec is None:
            return
        path = rec["QPF_DocumentURL"]
        if not path:
            QMessageBox.information(self, "Document", "This record has no document. Use Edit... to attach one.")
            return
        open_document(self, str(path))

    def _settings(self):
        fab = self.manager.active
        if fab is None:
            return
        from .settings_dialog import SettingsDialog
        if SettingsDialog(fab, self).exec():
            self.manager.refresh()

    def _activate(self):
        gid = self._selected_gid()
        if gid:
            self.manager.set_active_record(gid)
            self._fill()

    def _zoom(self):
        fab = self.manager.active
        gid = self._selected_gid()
        if not fab or not gid:
            return
        expr = '"CreatedByRecord" = \'%s\'' % gid
        box = None
        for lyr in (fab.parcels, fab.lines):
            for f in lyr.getFeatures(QgsFeatureRequest().setFilterExpression(expr)):
                b = f.geometry().boundingBox()
                if box is None:
                    box = b
                else:
                    box.combineExtentWith(b)
        if box is None:
            self.iface.messageBar().pushInfo("Parcel Fabric", "That record has no features yet.")
            return
        canvas = self.iface.mapCanvas()
        xf = QgsCoordinateTransform(fab.records.crs(), canvas.mapSettings().destinationCrs(),
                                    QgsProject.instance())
        canvas.setExtent(xf.transformBoundingBox(box.buffered(max(box.width(), box.height()) * 0.1 + 1)))
        canvas.refresh()

    def _build(self, in_view):
        fab = self.manager.active
        if fab is None or not self.type_combo.currentText():
            return
        ptype = self.type_combo.currentText()
        rec = self.manager.active_record_id
        if not in_view and not rec:
            QMessageBox.information(self, "Build", "Set an active record first, or use In View.")
            return
        extent = None
        if in_view:
            canvas = self.iface.mapCanvas()
            xf = QgsCoordinateTransform(canvas.mapSettings().destinationCrs(), fab.records.crs(),
                                        QgsProject.instance())
            extent = xf.transformBoundingBox(canvas.extent())
        try:
            rep = build_parcels(fab, ptype, None if in_view else rec, extent)
        except Exception as e:
            QMessageBox.critical(self, "Build", "Build failed: %s" % e)
            return
        self.iface.mapCanvas().refresh()
        self._fill()
        msg = "%s: %s" % (ptype, rep.summary())
        if rep.issues:
            IssuesDialog(self.iface, "Build %s" % ptype, msg, rep.issues, fab.records.crs()).show()
        else:
            self.iface.messageBar().pushSuccess("Parcel Fabric", msg)

    def _current_only(self, on):
        fab = self.manager.active
        if fab:
            fab.set_show_only_current(on)
            self.iface.mapCanvas().refresh()
