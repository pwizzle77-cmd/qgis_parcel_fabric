# -*- coding: utf-8 -*-
"""Create Missing Lines and Points: the same boundary tracing Import Parcels
does for new rows (see exchange.derive_lines_and_points), run on parcels
already in the fabric that don't have it - an older import, say, from
before import did this on its own."""

from qgis.core import QgsFeatureRequest
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QMessageBox, QProgressDialog,
    QRadioButton, QVBoxLayout,
)

from ..core.fabric import CURRENT, type_expr


class MissingLinesDialog(QDialog):
    def __init__(self, iface, manager, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.manager = manager
        self.setWindowTitle("Create Missing Lines and Points")
        self.setMinimumWidth(460)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        lay = QVBoxLayout(self)
        note = QLabel(
            "Traces each parcel's boundary into fabric lines and corner points, the same way "
            "Import Parcels does for new rows - sharing a line between adjoining parcels instead "
            "of duplicating it. Safe to run again: a parcel that already has its lines is left "
            "alone. Use this on parcels that predate Import doing this on its own, or that were "
            "added some other way.")
        note.setWordWrap(True)
        lay.addWidget(note)

        fab = manager.active
        self.selected_opt = QRadioButton("Selected parcels only")
        self.all_opt = QRadioButton("Every current parcel")
        n_sel = len(list(fab.parcels.getSelectedFeatures())) if fab is not None else 0
        self.selected_opt.setText("Selected parcels only (%d selected)" % n_sel)
        self.selected_opt.setChecked(n_sel > 0)
        self.all_opt.setChecked(n_sel == 0)
        lay.addWidget(self.selected_opt)
        lay.addWidget(self.all_opt)

        form = QFormLayout()
        self.ptype = QComboBox()
        if fab is not None:
            self.ptype.addItem("All types", None)
            for t in fab.parcel_types:
                self.ptype.addItem(t, t)
        form.addRow("Parcel type (for \"Every current parcel\")", self.ptype)
        lay.addLayout(form)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Close)
        bb.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._run)
        bb.rejected.connect(self.close)
        lay.addWidget(bb)

    def _gids_by_type(self):
        fab = self.manager.active
        if fab is None:
            return {}
        out = {}
        if self.selected_opt.isChecked():
            for f in fab.parcels.getSelectedFeatures():
                out.setdefault(f["ParcelType"], []).append(f["GlobalID"])
            return out
        chosen = self.ptype.currentData()
        types = [chosen] if chosen else fab.parcel_types
        for ptype in types:
            expr = '%s AND %s AND ("IsSeed" IS NULL OR "IsSeed" = 0)' % (CURRENT, type_expr(ptype))
            gids = [f["GlobalID"] for f in fab.parcels.getFeatures(QgsFeatureRequest().setFilterExpression(expr))]
            if gids:
                out[ptype] = gids
        return out

    def _run(self):
        from ..core.exchange import derive_lines_and_points
        fab = self.manager.active
        if fab is None:
            return
        by_type = self._gids_by_type()
        total = sum(len(g) for g in by_type.values())
        if not total:
            QMessageBox.information(self, self.windowTitle(), "Nothing to do.")
            return
        progress = QProgressDialog("Tracing parcel boundaries...", "Cancel", 0, 100, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        done = 0
        lines_made = points_made = 0
        try:
            for ptype, gids in by_type.items():
                def tell(done_n, _count, base=done, overall=total):
                    progress.setValue(int(100 * (base + done_n) / overall))
                    progress.setLabelText("Creating lines: %d/%d" % (base + done_n, overall))
                try:
                    lm, pm = derive_lines_and_points(fab, ptype, gids, progress=tell)
                except Exception as e:
                    QMessageBox.critical(self, self.windowTitle(),
                                         "Failed on parcel type %s: %s" % (ptype, e))
                    return
                lines_made += lm
                points_made += pm
                done += len(gids)
        finally:
            progress.setValue(100)
        self.iface.mapCanvas().refresh()
        self.iface.messageBar().pushSuccess(
            "Parcel Fabric", "Create Missing Lines and Points: %d parcel(s), %d line(s), %d point(s) added."
            % (total, lines_made, points_made))
