# -*- coding: utf-8 -*-
"""Create Missing Lines and Points: the same boundary tracing Import Parcels
does for new rows (see exchange.derive_lines_and_points), run on parcels
already in the fabric that don't have it - an older import, say, from
before import did this on its own."""

from qgis.core import QgsFeatureRequest
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QMessageBox,
    QProgressBar, QRadioButton, QVBoxLayout,
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

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setVisible(False)
        lay.addWidget(self.status)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.bar.setFormat("%v of %m")
        self.bar.setVisible(False)
        lay.addWidget(self.bar)
        self._running = False

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Close)
        self.apply_btn = bb.button(QDialogButtonBox.StandardButton.Apply)
        self.apply_btn.clicked.connect(self._run)
        bb.rejected.connect(self.close)
        lay.addWidget(bb)

    def closeEvent(self, event):
        if self._running:
            event.ignore()
            return
        super().closeEvent(event)

    def _show(self, so_far, total, text):
        self.bar.setVisible(True)
        self.status.setVisible(True)
        self.bar.setRange(0, max(total, 1))
        self.bar.setValue(min(max(so_far, 0), max(total, 1)))
        self.bar.setFormat("%v of %m" if total else "")
        self.status.setText(text)
        QApplication.processEvents()

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
        if fab is None or self._running:
            return
        self._running = True
        self.apply_btn.setEnabled(False)
        done = 0
        lines_made = points_made = 0
        total = 0
        try:
            self._show(0, 0, "Counting parcels...")
            by_type = self._gids_by_type()
            total = sum(len(g) for g in by_type.values())
            if not total:
                self.bar.setVisible(False)
                self.status.setText("Nothing to do.")
                QMessageBox.information(self, self.windowTitle(), "Nothing to do.")
                return
            for ptype, gids in by_type.items():
                def tell(done_n, _count, base=done, overall=total):
                    so_far = base + max(done_n, 0)
                    left = max(overall - so_far, 0)
                    if done_n <= 0 and base == 0:
                        text = "Reading existing lines. %d parcels, %d left" % (overall, overall)
                    else:
                        text = "Creating lines: %d of %d, %d left" % (so_far, overall, left)
                    self._show(so_far, overall, text)
                try:
                    lm, pm = derive_lines_and_points(fab, ptype, gids, progress=tell)
                except Exception as e:
                    self.status.setText("Failed on parcel type %s: %s" % (ptype, e))
                    QMessageBox.critical(self, self.windowTitle(),
                                         "Failed on parcel type %s: %s" % (ptype, e))
                    return
                lines_made += lm
                points_made += pm
                done += len(gids)
            self._show(total, total, "Done: %d parcels, %d lines, %d points added."
                       % (total, lines_made, points_made))
        finally:
            self._running = False
            self.apply_btn.setEnabled(True)
        if not total:
            return
        self.iface.mapCanvas().refresh()
        self.iface.messageBar().pushSuccess(
            "Parcel Fabric", "Create Missing Lines and Points: %d parcel(s), %d line(s), %d point(s) added."
            % (total, lines_made, points_made))
