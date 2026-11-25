# -*- coding: utf-8 -*-
"""Parcel Report: a one-page handout for a single parcel (counter/citizen use)."""

from qgis.core import QgsFeatureRequest
from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout,
)

from ..core import reports as R
from ..core.fabric import quote
from ..core.parcel_numbers import unfiltered


class ParcelReportDialog(QDialog):
    """Pick a parcel (by number or from the Parcels selection) and save a
    one-page report: identification, area, the record that created it, one
    level of lineage, and a footprint sketch."""

    def __init__(self, iface, manager, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.manager = manager
        self.fabric = manager.active
        self.gid = None
        self.setWindowTitle("Parcel Report")
        self.resize(420, 160)
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
        self.head = QLabel("Pick a parcel, then save its report.")
        self.head.setWordWrap(True)
        lay.addWidget(self.head)
        save = QPushButton("Save Report...")
        save.clicked.connect(self._save)
        lay.addWidget(save)
        lay.addStretch(1)
        self._from_selection(quiet=True)

    def _from_selection(self, quiet=False):
        sel = list(self.fabric.parcels.getSelectedFeatures())
        if len(sel) != 1:
            if not quiet:
                QMessageBox.information(self, "Parcel Report", "Select one parcel in the Parcels layer.")
            return
        self._show(sel[0]["GlobalID"], sel[0]["ParcelNumber"] or sel[0]["Name"])

    def _find(self):
        num = self.find.text().strip()
        if not num:
            return
        req = QgsFeatureRequest().setFilterExpression('"ParcelNumber" = %s' % quote(num))
        for f in unfiltered(self.fabric.parcels).getFeatures(req):
            self._show(f["GlobalID"], f["ParcelNumber"] or f["Name"])
            return
        QMessageBox.information(self, "Parcel Report", "No parcel has number %s." % num)

    def _show(self, gid, label):
        self.gid = gid
        self.head.setText("Ready: <b>%s</b>" % (label or gid))

    def _save(self):
        if not self.gid:
            QMessageBox.information(self, "Parcel Report", "Show a parcel first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Parcel Report", "parcel report.html",
                                              "Web page (*.html)")
        if not path:
            return
        try:
            R.parcel_report_html(self.fabric, self.gid, path)
        except Exception as e:
            QMessageBox.critical(self, "Parcel Report", str(e))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
