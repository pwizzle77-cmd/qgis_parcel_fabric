# -*- coding: utf-8 -*-
"""A condo or common-area parcel on an existing polygon. No new lines."""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QVBoxLayout,
)

from ..core import edits as E
from ..core import numbering as N
from .edit_dialogs import pick_record_from_selection, records_of


class StackDialog(QDialog):
    def __init__(self, iface, manager, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.manager = manager
        self.setWindowTitle("Stacked Unit")
        self.setMinimumWidth(460)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        lay = QVBoxLayout(self)
        note = QLabel("Copies the selected parcel's polygon as another current parcel with its own "
                      "roll number. No new boundary lines are drawn. Build will not turn this copy "
                      "into the land parcel. Building the same area again does not create another loop.")
        note.setWordWrap(True)
        lay.addWidget(note)
        rec_row = QHBoxLayout()
        self.rec_lbl = QLabel()
        self.rec_lbl.setWordWrap(True)
        rec_row.addWidget(self.rec_lbl, 1)
        self.use_rec_btn = QPushButton("Use selected parcel's record")
        self.use_rec_btn.setToolTip("Set the active record to the one that created the "
                                    "selected parcel.")
        self.use_rec_btn.clicked.connect(
            lambda: pick_record_from_selection(self, self.manager,
                                               list(self.manager.active.parcels.getSelectedFeatures())
                                               if self.manager.active else []))
        rec_row.addWidget(self.use_rec_btn)
        lay.addLayout(rec_row)
        fab = manager.active
        if fab is not None:
            fab.parcels.selectionChanged.connect(self._refresh_record_ui)
        manager.activeRecordChanged.connect(self._refresh_record_ui)
        self._refresh_record_ui()
        form = QFormLayout()
        self.number = QLineEdit()
        self.number.setPlaceholderText("Roll parcel ID")
        self.kind = QComboBox()
        self.kind.addItem("Condo", "condo")
        self.kind.addItem("Common area", "common")
        form.addRow("Parcel number", self.number)
        form.addRow("Kind", self.kind)
        lay.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Close)
        bb.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._apply)
        bb.rejected.connect(self.close)
        lay.addWidget(bb)

    def _refresh_record_ui(self, *_):
        fab = self.manager.active
        rec = self.manager.active_record() if fab is not None else None
        if rec is None:
            self.rec_lbl.setText("<span style='color:#b3261e'>No active record - set one in the "
                                 "Records panel first.</span>")
        else:
            self.rec_lbl.setText("Edits will belong to record <b>%s</b>." % (rec["Name"] or rec["GlobalID"]))
        sel = list(fab.parcels.getSelectedFeatures()) if fab is not None else []
        self.use_rec_btn.setEnabled(bool(fab is not None and records_of(fab, sel)))

    def _apply(self):
        fab = self.manager.active
        if fab is None:
            return
        sel = list(fab.parcels.getSelectedFeatures())
        if len(sel) != 1:
            QMessageBox.warning(self, self.windowTitle(), "Select the one land parcel to stack a unit on.")
            return
        try:
            res = E.add_stacked_unit(fab, sel[0], self.number.text(), self.kind.currentData(),
                                     self.manager.active_record_id)
        except (E.EditError, N.NumberError) as e:
            QMessageBox.warning(self, self.windowTitle(), str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, self.windowTitle(), str(e))
            return
        self.iface.mapCanvas().refresh()
        self.iface.messageBar().pushSuccess("Parcel Fabric", res.summary())
        self.number.clear()
