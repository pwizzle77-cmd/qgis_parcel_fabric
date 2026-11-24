# -*- coding: utf-8 -*-
"""A condo or common-area parcel on an existing polygon. No new lines."""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QMessageBox, QVBoxLayout,
)

from ..core import edits as E
from ..core import numbering as N


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
