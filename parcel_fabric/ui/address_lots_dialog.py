# -*- coding: utf-8 -*-
"""Address new lots with the NENA Addressing and RCL Tools plugin."""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QLabel, QMessageBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from ..core import nena_link as NL


class AddressLotsDialog(QDialog):
    """Select lots in the Parcels layer, review the planned numbers, write them."""

    def __init__(self, iface, manager, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.manager = manager
        self.setWindowTitle("Address Lots (NENA)")
        self.resize(640, 380)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        lay = QVBoxLayout(self)
        self.info = QLabel()
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Write", "Lot", "Number", "Street", "Notes"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.table)
        bb = QDialogButtonBox()
        self.plan_btn = bb.addButton("Plan Selected Lots", QDialogButtonBox.ButtonRole.ActionRole)
        self.write_btn = bb.addButton("Write Addresses", QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QDialogButtonBox.StandardButton.Close)
        self.plan_btn.clicked.connect(self.plan)
        self.write_btn.clicked.connect(self.write)
        bb.rejected.connect(self.close)
        lay.addWidget(bb)
        self.engine = self.ctx = None
        self.plans = []
        try:
            self.engine, self.ctx = NL.context()
            self.info.setText("Select the new lots in the Parcels layer and click <b>Plan Selected Lots</b>. "
                              "Numbers come from the NENA plugin's ranging on <b>%s</b>; each lot is "
                              "addressed to its nearest centerline. Check corner lots before writing." %
                              self.ctx.rcl.name())
        except NL.NenaUnavailable as e:
            self.info.setText("<span style='color:#b3261e'>%s</span><br>Install and set up the NENA "
                              "Addressing and RCL Tools plugin (centerline and address layers) to use this."
                              % e)
            self.plan_btn.setEnabled(False)
        self.write_btn.setEnabled(False)

    def plan(self):
        fab = self.manager.active
        if fab is None:
            return
        feats = list(fab.parcels.selectedFeatures())
        if not feats:
            QMessageBox.information(self, self.windowTitle(), "Select one or more lots in the Parcels layer.")
            return
        lots = [(f["ParcelNumber"] or f["Name"] or str(f.id()), f.geometry()) for f in feats]
        rows = NL.plan_lots(self.engine, self.ctx, lots, fab.parcels.crs())
        self.plans = []
        self.table.setRowCount(len(rows))
        for i, (label, plan, msg) in enumerate(rows):
            ok = plan is not None and plan.result.number is not None
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Checked if ok and plan.result.ok else Qt.CheckState.Unchecked)
            self.table.setItem(i, 0, chk)
            self.table.setItem(i, 1, QTableWidgetItem(label))
            num = "%s%s" % (plan.result.number, plan.result.suffix or "") if ok else ""
            self.table.setItem(i, 2, QTableWidgetItem(num))
            self.table.setItem(i, 3, QTableWidgetItem(plan.full_name if plan is not None else ""))
            self.table.setItem(i, 4, QTableWidgetItem(msg))
            self.plans.append(plan if ok else None)
        self.table.resizeColumnsToContents()
        self.write_btn.setEnabled(any(self.plans))

    def write(self):
        rows = []
        for i, plan in enumerate(self.plans):
            if plan is None or self.table.item(i, 0).checkState() != Qt.CheckState.Checked:
                continue
            txt = self.table.item(i, 2).text().strip()
            digits = "".join(ch for ch in txt if ch.isdigit())
            if not digits:
                continue
            rows.append((plan, int(digits), txt[len(digits):].strip()))
        if not rows:
            return
        try:
            n = NL.commit(self.engine, self.ctx, rows)
        except Exception as e:
            QMessageBox.critical(self, self.windowTitle(), "Writing addresses failed: %s" % e)
            return
        self.iface.messageBar().pushSuccess(
            "Parcel Fabric", "%d address(es) added to '%s' - review and save its edits." % (n, self.ctx.addr.name()))
        self.write_btn.setEnabled(False)
