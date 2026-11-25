# -*- coding: utf-8 -*-
"""Import an ArcGIS Pro fabric, a parcel layer, or LandXML; export DXF."""

import os

from qgis.core import Qgis
from qgis.gui import QgsFieldComboBox, QgsMapLayerComboBox
from qgis.PyQt.QtCore import QDate, Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSpinBox,
    QStackedWidget, QTabWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core import exchange as X
from ..core import schema as S
from ..core.fabric import PROP_FABRIC
from ..core.numbering import NumberError, clean_pattern, number_pattern

_NUMBER_FIELDS = ("PARCELNO", "PARCEL_NO", "PARCELNUM", "PARCEL_NUM", "PARCELID", "PARCEL_ID",
                  "PIN", "STRAP", "FOLIO", "PARCEL", "ParcelNumber", "NAME")
_DATE_FIELDS = ("DEEDDATE", "DEED_DATE", "RECORDDATE", "RECORDED", "RECORD_DATE", "SALEDATE",
                "SALE_DATE", "DOCDATE", "DOC_DATE", "DATE")
_LOT_FIELDS = ("LOT", "LOT_NO", "LOTNO", "LOT_NUM", "LOTNUM")
_BLOCK_FIELDS = ("BLOCK", "BLK", "BLOCK_NO", "BLOCKNO")
_SUB_FIELDS = ("SUBDIVISION", "SUBDIVISIO", "SUBDIV", "SUB_NAME", "SUBNAME", "SUB")
_NAME_FIELDS = ("NAME", "SUBDIVISION", "SUBDIVISIO", "SUB_NAME", "SUBNAME", "SUBDIV")
_BOOK_FIELDS = ("BOOK", "DEEDBOOK", "DEED_BOOK", "ORBOOK", "OR_BOOK", "PLATBOOK", "PLAT_BOOK", "BK")
_PAGE_FIELDS = ("PAGE", "DEEDPAGE", "DEED_PAGE", "ORPAGE", "OR_PAGE", "PLATPAGE", "PLAT_PAGE", "PG")
_CREATED_FIELDS = ("CREATED_USER", "CREATEDBY", "CREATED_BY", "CREATEUSER", "CREATE_USER",
                   "CREATOR", "ADDED_BY", "ADDUSER", "created_user", "CreatedByUser")
_EDITED_FIELDS = ("LAST_EDITED_USER", "LASTEDITOR", "EDITED_BY", "EDITUSER", "EDIT_USER",
                  "UPDATED_BY", "UPDATE_USER", "MODIFIED_BY", "last_edited_user", "EditUser")

_LAYER_INFO = {
    "Parcels": "Each parcel id becomes its own record. The record name and parcel id are that "
               "number. Book, page, created-by, and last-edited-by can come from fields on the layer. "
               "Map a date field onto the recorded date. "
               "Split history uses the parcel number below: separator, how many trailing digits "
               "count as the split, and the parent suffix. Every parcel comes in current; nothing is "
               "retired. Leave it off when the suffix is not a split. Boundary lines and corner "
               "points are traced after the parcels are written. The same outline is imported once. A text "
               "field keeps leading zeros. Polygons with the same number become one parcel, all-zero "
               "numbers come in without a number, and true curves stay curves.",
    "Lots": "Copies lot polygons into the Lots table. Tax parcels stay in Parcels. A lot drawn "
            "with Subdivide Block stays in Parcels.",
    "Blocks": "Copies subdivision block polygons into the Blocks table. Tax parcels stay in "
              "Parcels. A lot drawn with Subdivide Block stays in Parcels.",
    "Subdivisions": "Copies subdivision polygons into the Subdivisions table.",
}
_PLAT_INFO = (" Each parcel's empty Subdivision, Block and Lot are filled from the Lots, Blocks and "
              "Subdivisions imported so far: lots by parcel number, the rest by location. Import the "
              "layers in any order.")


def _guess_field(layer, candidates):
    if layer is None:
        return ""
    names = {n.lower(): n for n in layer.fields().names()}
    for cand in candidates:
        if cand.lower() in names:
            return names[cand.lower()]
    return ""


def _field_box():
    box = QgsFieldComboBox()
    box.setAllowEmptyFieldName(True)
    return box


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
        self.resize(680, 640)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs)
        tabs.addTab(self._layer_tab(), "Parcel layer")
        tabs.addTab(self._pro_tab(), "ArcGIS Pro fabric")
        tabs.addTab(self._xml_tab(), "LandXML")
        tabs.addTab(self._dxf_tab(), "DXF export")
        tabs.addTab(self._roll_tab(), "Tax roll")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(120)
        lay.addWidget(self.log)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setVisible(False)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setFormat("%p%")
        self.bar.setVisible(False)
        lay.addWidget(self.status)
        lay.addWidget(self.bar)
        self._importing = False
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

    def _progress(self, pct, text=None):
        self.bar.setVisible(True)
        self.status.setVisible(True)
        self.bar.setValue(max(0, min(100, int(pct))))
        if text:
            self.status.setText(text)
        from qgis.PyQt.QtWidgets import QApplication
        QApplication.processEvents()

    def _end_progress(self):
        self.bar.setVisible(False)
        self.status.setVisible(False)
        self._importing = False

    def _record(self):
        rid = self.manager.active_record_id
        if not rid:
            QMessageBox.warning(self, self.windowTitle(), "Set an active record first (Records panel).")
        return rid

    # ------------------------------------------------------------------
    def _layer_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.layer_info = QLabel()
        self.layer_info.setWordWrap(True)
        lay.addWidget(self.layer_info)
        form = QFormLayout()
        self.src_layer = QgsMapLayerComboBox()
        self.src_layer.setFilters(Qgis.LayerFilter.PolygonLayer)
        from qgis.core import QgsProject
        self.src_layer.setExceptedLayerList([lx for lx in QgsProject.instance().mapLayers().values()
                                             if lx.customProperty(PROP_FABRIC)])
        self.src_file = QLineEdit()
        self.src_file.setReadOnly(True)
        self.src_file.setPlaceholderText("shapefile or GeoPackage with one polygon layer")
        form.addRow("Layer on the map", self.src_layer)
        form.addRow("Or a file", _file_row(self.src_file, self._pick_parcel_file))
        self.target = QComboBox()
        self.target.addItems(["Parcels", "Lots", "Blocks", "Subdivisions"])
        self.target.currentTextChanged.connect(self._target_changed)
        form.addRow("Import into", self.target)
        lay.addLayout(form)
        self.num_field = _field_box()
        self.date_field = _field_box()
        self.book_field = _field_box()
        self.page_field = _field_box()
        self.created_field = _field_box()
        self.edited_field = _field_box()
        self.lot_field = _field_box()
        self.lot_sub_field = _field_box()
        self.lot_block_field = _field_box()
        self.lot_parcel_field = _field_box()
        self.sub_name_field = _field_box()
        self.block_field = _field_box()
        self.block_sub_field = _field_box()
        self._field_boxes = (
            (self.num_field, _NUMBER_FIELDS), (self.date_field, _DATE_FIELDS),
            (self.book_field, _BOOK_FIELDS), (self.page_field, _PAGE_FIELDS),
            (self.created_field, _CREATED_FIELDS), (self.edited_field, _EDITED_FIELDS),
            (self.lot_field, _LOT_FIELDS), (self.lot_sub_field, _SUB_FIELDS),
            (self.lot_block_field, _BLOCK_FIELDS), (self.lot_parcel_field, _NUMBER_FIELDS),
            (self.sub_name_field, _NAME_FIELDS),
            (self.block_field, _BLOCK_FIELDS), (self.block_sub_field, _SUB_FIELDS))
        self.stack = QStackedWidget()
        self.stack.addWidget(self._parcel_page())
        self.stack.addWidget(self._lots_page())
        self.stack.addWidget(self._blocks_page())
        self.stack.addWidget(self._sub_page())
        lay.addWidget(self.stack)
        b = QPushButton("Import")
        b.clicked.connect(self._import_layer)
        lay.addWidget(b)
        self._file_layer = None
        self.src_layer.layerChanged.connect(self._use_map_layer)
        self._use_map_layer(self.src_layer.currentLayer())
        self._target_changed("Parcels")
        return w

    def _parcel_page(self):
        w = QWidget()
        f = QFormLayout(w)
        self.layer_type = QComboBox()
        if self.fab is not None:
            self.layer_type.addItems(self.fab.parcel_types)
        self.use_active = QCheckBox("Use the active record instead of one record per parcel id")
        self.use_active.setEnabled(bool(self.manager.active_record_id))
        self.use_active.setToolTip("File every polygon under the active record. The date, book, and page "
                                   "boxes are not used.")
        self.book = QLineEdit()
        self.book.setPlaceholderText("if the field is blank")
        self.book.setMaximumWidth(140)
        self.book.setToolTip("Used when the book field is empty, or when no book field is chosen.")
        self.page = QLineEdit()
        self.page.setPlaceholderText("if the field is blank")
        self.page.setMaximumWidth(140)
        self.page.setToolTip("Used when the page field is empty, or when no page field is chosen.")
        self.book_field.setToolTip("Each new record takes its book from this field.")
        self.page_field.setToolTip("Each new record takes its page from this field.")
        self.created_field.setToolTip("Who created this parcel in the source. Written on the parcel, "
                                      "its new record, and the lines and points traced from it.")
        self.edited_field.setToolTip("Who last edited this parcel in the source. Written the same way.")
        self.use_active.toggled.connect(self._sync_book_page)
        book_row = QHBoxLayout()
        book_row.addWidget(self.book_field, 1)
        book_row.addWidget(self.book)
        page_row = QHBoxLayout()
        page_row.addWidget(self.page_field, 1)
        page_row.addWidget(self.page)
        self.history = QCheckBox("Build split history")
        self.history.setChecked(True)
        self.history.setToolTip("Link each split, as an inferred split, to the lower number it was cut out of "
                                "(its shape fills a notch), or else to the parent suffix. "
                                "Nothing is retired.")
        self.date_field.setToolTip("The date each parcel was recorded. With a last-edited date, Parcel "
                                   "History shows parcels from when they were last edited.")
        sep, digits, parent = number_pattern(self.fab.settings if self.fab is not None else {})
        self.num_sep = QLineEdit(sep)
        self.num_sep.setMaximumWidth(48)
        self.num_sep.setToolTip("Between the parcel id and the split. A hyphen matches 12345-002 "
                                "and a Florida strap such as 12-4S-16-02942-001.")
        self.num_digits = QSpinBox()
        self.num_digits.setRange(1, 6)
        self.num_digits.setValue(digits)
        self.num_digits.setToolTip("The last piece is a split when it has at most this many digits. "
                                   "3 means -002 is a split and -1234 is still part of the number.")
        self.num_parent = QLineEdit(parent)
        self.num_parent.setMaximumWidth(72)
        self.num_parent.setToolTip("The original parcel ends with this. 000 matches -000.")
        pattern = QHBoxLayout()
        pattern.addWidget(QLabel("Separator"))
        pattern.addWidget(self.num_sep)
        pattern.addWidget(QLabel("Digits"))
        pattern.addWidget(self.num_digits)
        pattern.addWidget(QLabel("Parent"))
        pattern.addWidget(self.num_parent)
        pattern.addStretch(1)
        f.addRow("Parcel number field", self.num_field)
        f.addRow("Date field", self.date_field)
        f.addRow("Parcel type", self.layer_type)
        f.addRow("Book", book_row)
        f.addRow("Page", page_row)
        f.addRow("Created by", self.created_field)
        f.addRow("Last edited by", self.edited_field)
        f.addRow("Parcel number", pattern)
        f.addRow("", self.use_active)
        f.addRow("", self.history)
        return w

    def _lots_page(self):
        w = QWidget()
        f = QFormLayout(w)
        f.addRow("Lot field", self.lot_field)
        f.addRow("Subdivision field", self.lot_sub_field)
        f.addRow("Block field", self.lot_block_field)
        f.addRow("Parcel number field", self.lot_parcel_field)
        return w

    def _blocks_page(self):
        w = QWidget()
        f = QFormLayout(w)
        f.addRow("Block field", self.block_field)
        f.addRow("Subdivision field", self.block_sub_field)
        return w

    def _sub_page(self):
        w = QWidget()
        f = QFormLayout(w)
        f.addRow("Name field", self.sub_name_field)
        return w

    def _sync_book_page(self, use_active):
        for w in (self.book, self.page, self.book_field, self.page_field):
            w.setEnabled(not use_active)

    def _target_changed(self, name):
        names = ["Parcels", "Lots", "Blocks", "Subdivisions"]
        self.stack.setCurrentIndex(names.index(name) if name in names else 0)
        self.layer_info.setText(_LAYER_INFO.get(name, _LAYER_INFO["Parcels"]) + _PLAT_INFO)

    def _bind_fields(self, layer):
        for box, names in self._field_boxes:
            box.setLayer(layer)
            box.setField(_guess_field(layer, names))

    def _use_map_layer(self, layer):
        self._file_layer = None
        self.src_file.clear()
        self._bind_fields(layer)

    def _pick_parcel_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Parcel layer", self.src_file.text(), "Shapefile or GeoPackage (*.shp *.gpkg)")
        if not path:
            return
        try:
            layer = X.open_polygon_layer(path)
        except Exception as e:
            QMessageBox.warning(self, self.windowTitle(), str(e))
            return
        self._file_layer = layer
        self.src_file.setText(path)
        self._bind_fields(layer)

    def _import_layer(self):
        if self._importing:
            return
        if self.fab is None:
            QMessageBox.warning(self, self.windowTitle(), "Open a fabric first.")
            return
        self._importing = True
        src = self._file_layer if self._file_layer is not None else self.src_layer.currentLayer()
        if src is None:
            self._end_progress()
            QMessageBox.warning(self, self.windowTitle(), "Choose a polygon layer.")
            return
        kind = self.target.currentText()
        try:
            if kind == "Lots":
                result = X.import_lots(self.fab, src, self.lot_field.currentField(),
                                       self.lot_sub_field.currentField(),
                                       self.lot_block_field.currentField(),
                                       self.lot_parcel_field.currentField(), self._progress)
                label = "lots"
            elif kind == "Blocks":
                result = X.import_blocks(self.fab, src, self.block_field.currentField(),
                                         self.block_sub_field.currentField(), self._progress)
                label = "blocks"
            elif kind == "Subdivisions":
                result = X.import_subdivisions(self.fab, src, self.sub_name_field.currentField(),
                                               self._progress)
                label = "subdivisions"
            else:
                rid = None
                if self.use_active.isChecked():
                    rid = self.manager.active_record_id
                    if not rid:
                        self._end_progress()
                        QMessageBox.warning(self, self.windowTitle(),
                                            "Set an active record first, or leave that box off.")
                        return
                try:
                    sep, digits, parent = clean_pattern(
                        self.num_sep.text(), self.num_digits.value(), self.num_parent.text())
                except NumberError as e:
                    self._end_progress()
                    QMessageBox.warning(self, self.windowTitle(), str(e))
                    return
                S.write_settings(self.fab.layers[S.ROLE_SETTINGS], {
                    "number_separator": sep, "number_split_digits": str(digits),
                    "number_parent_suffix": parent})
                self.fab.settings.update({
                    "number_separator": sep, "number_split_digits": str(digits),
                    "number_parent_suffix": parent})
                use_active = self.use_active.isChecked()
                book = "" if use_active else self.book.text().strip()
                page = "" if use_active else self.page.text().strip()
                result = X.import_parcel_layer(
                    self.fab, src, self.layer_type.currentText(), rid,
                    self.num_field.currentField() or None,
                    self.date_field.currentField() or None,
                    self.history.isChecked(), parent, sep, digits, book, page,
                    progress=self._progress,
                    book_field=None if use_active else (self.book_field.currentField() or None),
                    page_field=None if use_active else (self.page_field.currentField() or None),
                    created_field=self.created_field.currentField() or None,
                    edited_field=self.edited_field.currentField() or None)
                label = "parcels"
        except Exception as e:
            self._end_progress()
            QMessageBox.critical(self, self.windowTitle(), "Import failed: %s" % e)
            return
        self._end_progress()
        ids = result.get("record_ids") or []
        if kind == "Parcels" and not self.use_active.isChecked() and len(ids) == 1:
            self.manager.set_active_record(ids[0])
        self.manager.refresh()
        self.iface.mapCanvas().refresh()
        skipped = result.get("skipped") or {}
        names = (("duplicate", "duplicate number"), ("same", "same outline"),
                 ("too_long", "number longer than 30 characters"),
                 ("multipart", "more than one polygon"), ("empty", "empty"))
        left = ["%d %s" % (skipped[k], text) for k, text in names if skipped.get(k)]
        bits = ["Imported %d %s." % (result["copied"], label)]
        if result.get("records"):
            bits.append("Filed %d records, one per parcel id." % result["records"])
        if result.get("history_linked"):
            bits.append("Split history linked %d (inferred from the numbers)." % result["history_linked"])
        if result.get("joined"):
            bits.append("%d parcels have several polygons." % result["joined"])
        if result.get("overlapping"):
            bits.append("%d polygons overlap another with the same number; run Check Fabric."
                        % result["overlapping"])
        plat = result.get("plat") or {}
        filled = ["%d %s" % (plat[k], k.lower()) for k in ("Subdivision", "Block", "Lot") if plat.get(k)]
        if filled:
            bits.append("Filled on parcels: %s." % ", ".join(filled))
        if plat.get("too_long"):
            bits.append("%d lists of lots or blocks are longer than the field and were left blank%s."
                        % (plat["too_long"], "" if self.fab.parcels.fields().field("Lot").length() != 16
                           else "; a new fabric has a longer field"))
        if left:
            bits.append("Left out: %s." % ", ".join(left))
        self._say(" ".join(bits))
        if result.get("backup"):
            self._say("Several polygons can be one parcel now, curves included. A backup of the fabric was saved at %s."
                      % result["backup"])
        elif skipped.get("multipart") and result.get("multipart_note"):
            self._say(result["multipart_note"])

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
        if self._importing or self.fab is None or not self.pro_tables:
            return
        self._importing = True
        try:
            # use the (possibly edited) parcel type names from the table
            types = {}
            for i in range(self.pro_tbl.rowCount()):
                name = self.pro_tbl.item(i, 0).text().strip()
                types[name] = (self.pro_tbl.item(i, 1).text(), self.pro_tbl.item(i, 2).text())
            tables = dict(self.pro_tables, types=types)
            for m in X.import_pro(self.fab, self.pro_path.text(), tables, feedback=self._say,
                                  progress=self._progress):
                self._say(m)
        except Exception as e:
            QMessageBox.critical(self, self.windowTitle(), "Import failed: %s" % e)
            return
        finally:
            self._end_progress()
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
        if self._importing:
            return
        rid = self._record()
        if self.fab is None or not rid or not os.path.exists(self.xml_path.text()):
            return
        self._importing = True
        try:
            nl, npt, rep, named = X.import_landxml(self.fab, self.xml_path.text(),
                                                   self.xml_type.currentText(), rid, self._progress)
        except Exception as e:
            self._end_progress()
            QMessageBox.critical(self, self.windowTitle(), "Import failed: %s" % e)
            return
        self._end_progress()
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

    def _roll_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        note = QLabel("Current parcels, in this fabric's coordinate system, as "
                      "F_county_mmddyyyy_parcels.shp with PARCELNO, ACRES, TYPE, SUBDIV, BLOCK and LOT. "
                      "Curves become straight segments in a shapefile. The CSV lists lineage since the date "
                      "you pick: date, operation, parent number, child number, book, page and child acres.")
        note.setWordWrap(True)
        f.addRow(note)
        self.roll_dir = QLineEdit()
        f.addRow("Folder", _file_row(self.roll_dir, self._pick_roll_dir))
        self.roll_county = QLineEdit(self.fab.name if self.fab is not None else "")
        f.addRow("County", self.roll_county)
        self.roll_on = QDateEdit(QDate.currentDate())
        self.roll_on.setCalendarPopup(True)
        self.roll_on.setDisplayFormat("MM/dd/yyyy")
        f.addRow("File date", self.roll_on)
        self.roll_since = QDateEdit(QDate(QDate.currentDate().year(), 1, 1))
        self.roll_since.setCalendarPopup(True)
        self.roll_since.setDisplayFormat("MM/dd/yyyy")
        f.addRow("Changes since", self.roll_since)
        b = QPushButton("Export tax roll")
        b.clicked.connect(self._export_roll)
        f.addRow("", b)
        return w

    def _pick_roll_dir(self):
        path = QFileDialog.getExistingDirectory(self, "Folder for the tax-roll shapefile")
        if path:
            self.roll_dir.setText(path)

    def _export_roll(self):
        if self.fab is None:
            return
        if not self.roll_dir.text().strip():
            QMessageBox.warning(self, self.windowTitle(), "Choose a folder.")
            return
        try:
            shp, csv_path, n_parcels, n_changes = X.export_tax_roll(
                self.fab, self.roll_dir.text().strip(), self.roll_county.text().strip(),
                self.roll_on.date().toPyDate(), self.roll_since.date().toString("yyyy-MM-dd"))
        except Exception as e:
            QMessageBox.critical(self, self.windowTitle(), str(e))
            return
        self._say("Wrote %s (%d parcels) and %s (%d changes). Curves are straight segments in the shapefile."
                  % (shp, n_parcels, csv_path, n_changes))
