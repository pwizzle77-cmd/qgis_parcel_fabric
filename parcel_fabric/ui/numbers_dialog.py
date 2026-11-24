# -*- coding: utf-8 -*-
"""Assign Parcel Numbers to the selected parcels.

Three cases:
  * New parcels - the next unused ParcelID in each parcel's
    section-township-range, split 000.
  * Splits of a parent - the parent's number with the next -001, -002...;
    optionally the parent is retired by the active record.
  * Type each roll ID - stored as typed. Strap parts are filled only when
    the text is a section-township-range number.
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QRadioButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from ..core import numbering as N
from ..core import parcel_numbers as PN
from ..core.fabric import EditSession


class NumbersDialog(QDialog):
    def __init__(self, iface, manager, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.manager = manager
        self.fabric = manager.active
        self.setWindowTitle("Assign Parcel Numbers")
        self.resize(560, 460)
        lay = QVBoxLayout(self)
        self.sel_lbl = QLabel()
        self.sel_lbl.setWordWrap(True)
        lay.addWidget(self.sel_lbl)

        self.rb_new = QRadioButton("New parcels - next unused ID in each parcel's section-township-range")
        self.rb_split = QRadioButton("Splits of a parent parcel")
        self.rb_type = QRadioButton("Type each parcel's roll ID")
        grp = QButtonGroup(self)
        grp.addButton(self.rb_new)
        grp.addButton(self.rb_split)
        grp.addButton(self.rb_type)
        self.rb_new.setChecked(True)
        lay.addWidget(self.rb_new)
        lay.addWidget(self.rb_split)
        lay.addWidget(self.rb_type)
        self.type_hint = QLabel("Type the roll's parcel ID in the last column. It does not have to be a "
                                "section-township-range number.")
        self.type_hint.setWordWrap(True)
        self.type_hint.setStyleSheet("color: palette(mid);")
        lay.addWidget(self.type_hint)
        prow = QHBoxLayout()
        prow.addSpacing(24)
        prow.addWidget(QLabel("Parent number"))
        self.parent_num = QLineEdit()
        self.parent_num.setPlaceholderText("12-4S-16-02942-000")
        prow.addWidget(self.parent_num, 1)
        pick = QPushButton("Use selected parcel")
        pick.setToolTip("Select the parent alone, press this, then select the new parcels")
        pick.clicked.connect(self._parent_from_selection)
        prow.addWidget(pick)
        lay.addLayout(prow)
        self.parent_lbl = QLabel()
        self.parent_lbl.setStyleSheet("color: palette(mid);")
        lay.addWidget(self.parent_lbl)
        self.retire = QCheckBox("Retire the parent with the active record")
        self.retire.setChecked(True)
        lay.addWidget(self.retire)
        self.overwrite = QCheckBox("Replace numbers the selected parcels already have")
        lay.addWidget(self.overwrite)

        prev = QPushButton("Preview")
        prev.clicked.connect(self._preview)
        lay.addWidget(prev)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Parcel", "Now", "New number"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.table, 1)
        self.bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Close)
        self.bb.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._apply)
        self.bb.rejected.connect(self.reject)
        lay.addWidget(self.bb)

        for w in (self.rb_new, self.rb_split, self.rb_type):
            w.toggled.connect(self._sync)
        self.parent_num.textChanged.connect(self._sync)
        self.fabric.parcels.selectionChanged.connect(self._sync)
        self._planned = []
        self._sync()

    def done(self, r):
        try:
            self.fabric.parcels.selectionChanged.disconnect(self._sync)
        except (TypeError, RuntimeError):
            pass
        super().done(r)

    # ------------------------------------------------------------------
    def _selected(self):
        return list(self.fabric.parcels.getSelectedFeatures())

    def _sync(self, *_):
        sel = self._selected()
        self.sel_lbl.setText("<b>%d parcel(s) selected</b> in the Parcels layer. Select the parcels "
                             "to number with the Select tool, then Preview." % len(sel))
        split = self.rb_split.isChecked()
        typed = self.rb_type.isChecked()
        self.parent_num.setEnabled(split)
        self.retire.setEnabled(split)
        self.type_hint.setVisible(typed)
        triggers = (QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed
                    | QAbstractItemView.EditTrigger.AnyKeyPressed) if typed else \
            QAbstractItemView.EditTrigger.NoEditTriggers
        self.table.setEditTriggers(triggers)
        self.parent_lbl.setText("")
        if split and self.parent_num.text().strip():
            p = N.parse_number(self.parent_num.text())
            if p is None:
                self.parent_lbl.setText("Not a section-township-range number. Type each child's roll ID instead.")
            else:
                f = PN.find_parcel(self.fabric, self.parent_num.text().strip())
                if f is None:
                    self.parent_lbl.setText("No parcel has this number - numbers will still follow it.")
                else:
                    state = "retired" if f["RetiredByRecord"] else "current"
                    self.parent_lbl.setText("Parent found (%s)." % state)
        self.table.setRowCount(0)
        self._planned = []

    def _parent_from_selection(self):
        sel = self._selected()
        if len(sel) != 1 or not sel[0]["ParcelNumber"]:
            QMessageBox.information(self, "Parent", "Select just the parent parcel (it must have a number).")
            return
        self.rb_split.setChecked(True)
        self.parent_num.setText(str(sel[0]["ParcelNumber"]))
        self.fabric.parcels.removeSelection()

    def _plan(self):
        sel = self._selected()
        if not sel:
            raise ValueError("Select the parcels to number first.")
        if not self.overwrite.isChecked():
            keep = [f for f in sel if f["ParcelNumber"]]
            sel = [f for f in sel if not f["ParcelNumber"]]
            if not sel:
                raise ValueError("All %d selected parcels already have numbers "
                                 "(tick 'Replace numbers' to renumber them)." % len(keep))
        parent = self.parent_num.text().strip()
        if self.rb_split.isChecked():
            if N.parse_number(parent) is None:
                raise ValueError("Enter the parent's number, like 12-4S-16-02942-000. "
                                 "If the parent is a roll ID, choose 'Type each parcel's roll ID' "
                                 "and type the children's numbers.")
            if any(f["ParcelNumber"] == parent for f in sel):
                raise ValueError("The parent is in the selection - select only the new parcels.")
            before = {f.id(): f["ParcelNumber"] for f in sel}
            PN.number_splits(self.fabric, parent, sel)
            return [(f, before[f.id()]) for f in sel]
        before = {f.id(): f["ParcelNumber"] for f in sel}
        problems = PN.number_new(self.fabric, sel, overwrite=True)
        if problems:
            raise ValueError("\n".join("Parcel %s: %s" % (f["Name"] or f.id(), why) for f, why in problems))
        return [(f, before[f.id()]) for f in sel]

    def _preview(self):
        if self.rb_type.isChecked():
            self._preview_typed()
            return
        try:
            self._planned = self._plan()
        except (ValueError, PN.NumberingNotSetUp, N.NumberError) as e:
            self._planned = []
            QMessageBox.warning(self, "Assign Parcel Numbers", str(e))
            return
        self.table.setRowCount(0)
        for f, before in sorted(self._planned, key=lambda x: x[0]["ParcelNumber"]):
            r = self.table.rowCount()
            self.table.insertRow(r)
            for c, v in enumerate([f["Name"] or "#%d" % f.id(), before or "", f["ParcelNumber"]]):
                self.table.setItem(r, c, QTableWidgetItem(str(v)))

    def _lock(self, item):
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return item

    def _preview_typed(self):
        sel = self._selected()
        if not sel:
            self._planned = []
            QMessageBox.warning(self, "Assign Parcel Numbers", "Select the parcels to number first.")
            return
        if not self.overwrite.isChecked():
            sel = [f for f in sel if not f["ParcelNumber"]]
            if not sel:
                self._planned = []
                QMessageBox.warning(self, "Assign Parcel Numbers",
                                    "All selected parcels already have numbers "
                                    "(tick 'Replace numbers' to renumber them).")
                return
        self.table.setRowCount(0)
        for f in sorted(sel, key=lambda f: f.id()):
            r = self.table.rowCount()
            self.table.insertRow(r)
            name = self._lock(QTableWidgetItem(str(f["Name"] or "#%d" % f.id())))
            name.setData(Qt.ItemDataRole.UserRole, f.id())
            now = self._lock(QTableWidgetItem(str(f["ParcelNumber"] or "")))
            self.table.setItem(r, 0, name)
            self.table.setItem(r, 1, now)
            self.table.setItem(r, 2, QTableWidgetItem(str(f["ParcelNumber"] or "")))
        self._planned = [1]

    def _apply_typed(self):
        if self.table.rowCount() == 0:
            self._preview_typed()
            if self.table.rowCount() == 0:
                return
        lyr = self.fabric.parcels
        rows = []
        seen = []
        for r in range(self.table.rowCount()):
            name = self.table.item(r, 0)
            new = self.table.item(r, 2)
            fid = name.data(Qt.ItemDataRole.UserRole) if name else None
            text = new.text().strip() if new else ""
            try:
                fields = N.fields_for(text)
            except N.NumberError as e:
                QMessageBox.warning(self, "Assign Parcel Numbers", str(e))
                return
            if not fields["ParcelNumber"]:
                QMessageBox.warning(self, "Assign Parcel Numbers",
                                    "Type a roll ID in the New number column for every parcel.")
                return
            if fields["ParcelNumber"] in seen:
                QMessageBox.warning(self, "Assign Parcel Numbers",
                                    "Parcel number %s is entered twice." % fields["ParcelNumber"])
                return
            seen.append(fields["ParcelNumber"])
            rows.append((fid, fields))
        used = set(PN.used_numbers(self.fabric))
        for fid, _fields in rows:
            feat = lyr.getFeature(fid)
            old = str(feat["ParcelNumber"] or "") if feat.isValid() else ""
            if old:
                used.discard(old)
        for _fid, fields in rows:
            if fields["ParcelNumber"] in used:
                QMessageBox.warning(self, "Assign Parcel Numbers",
                                    "%s is already used by another parcel." % fields["ParcelNumber"])
                return
            used.add(fields["ParcelNumber"])
        try:
            with EditSession([lyr], "Assign parcel numbers") as ed:
                for fid, fields in rows:
                    for k, v in fields.items():
                        ed.change(lyr, fid, k, v)
        except Exception as e:
            QMessageBox.critical(self, "Assign Parcel Numbers", str(e))
            return
        self.iface.messageBar().pushSuccess("Parcel Fabric", "Numbered %d parcel(s)." % len(rows))
        self.iface.mapCanvas().refresh()
        self._planned = []
        self.table.setRowCount(0)
        lyr.removeSelection()

    def _apply(self):
        if self.rb_type.isChecked():
            self._apply_typed()
            return
        if not self._planned:
            self._preview()
            if not self._planned:
                return
        lyr = self.fabric.parcels
        fields = ["ParcelNumber", "Section", "Township", "Range", "ParcelID", "ParcelSeq", "SplitNo", "ParentNumber"]
        rec = self.manager.active_record_id
        retire_parent = None
        if self.rb_split.isChecked() and self.retire.isChecked():
            if not rec:
                QMessageBox.warning(self, "Assign Parcel Numbers",
                                    "Set an active record to retire the parent, or untick that option.")
                return
            retire_parent = PN.find_parcel(self.fabric, self.parent_num.text().strip(), current_only=True)
        try:
            with EditSession([lyr], "Assign parcel numbers") as ed:
                for f, _before in self._planned:
                    for k in fields:
                        if k == "ParentNumber" and not self.rb_split.isChecked():
                            continue
                        ed.change(lyr, f.id(), k, f[k])
                if retire_parent is not None:
                    ed.change(lyr, retire_parent.id(), "RetiredByRecord", rec)
        except Exception as e:
            QMessageBox.critical(self, "Assign Parcel Numbers", str(e))
            return
        if retire_parent is not None:
            from ..core.build import _dates
            _dates(self.fabric, [rec])
        n = len(self._planned)
        msg = "Numbered %d parcel(s)." % n
        if retire_parent is not None:
            msg += " Parent %s retired." % self.parent_num.text().strip()
        self.iface.messageBar().pushSuccess("Parcel Fabric", msg)
        self.iface.mapCanvas().refresh()
        self._planned = []
        self.table.setRowCount(0)
        lyr.removeSelection()
