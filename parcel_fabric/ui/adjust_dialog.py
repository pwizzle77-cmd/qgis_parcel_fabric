# -*- coding: utf-8 -*-
"""Least-squares adjustment window: check consistency, adjust to control,
review the results, then Apply."""

import math

from qgis.core import QgsCoordinateTransform, QgsProject
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QButtonGroup, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QMessageBox, QPushButton, QRadioButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout,
)

from ..core import adjust as ADJ
from ..core import lsa


class AdjustDialog(QDialog):
    def __init__(self, iface, manager, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.manager = manager
        self.fabric = manager.active
        self.net = self.res = None
        self.setWindowTitle("Least-Squares Adjustment")
        self.resize(760, 640)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        lay = QVBoxLayout(self)

        self.rb_check = QRadioButton("Consistency check - no control; find record dimensions that disagree")
        self.rb_adjust = QRadioButton("Weighted adjustment - fit to control points, then Apply")
        g = QButtonGroup(self)
        g.addButton(self.rb_check)
        g.addButton(self.rb_adjust)
        self.rb_check.setChecked(True)
        lay.addWidget(self.rb_check)
        lay.addWidget(self.rb_adjust)

        form = QFormLayout()
        self.scope = QComboBox()
        self.scope.addItem("Lines in the map view", "view")
        self.scope.addItem("Lines of the active record", "record")
        self.scope.addItem("Lines around the selected parcels", "parcels")
        form.addRow("Use", self.scope)
        sd = QHBoxLayout()
        self.sd_dist = QDoubleSpinBox()
        self.sd_dist.setDecimals(3)
        self.sd_dist.setRange(0.001, 10)
        self.sd_dist.setValue(0.05)
        self.sd_dist.setSuffix(" ft")
        self.sd_ppm = QDoubleSpinBox()
        self.sd_ppm.setRange(0, 10000)
        self.sd_ppm.setValue(100)
        self.sd_ppm.setSuffix(" ppm")
        self.sd_dir = QDoubleSpinBox()
        self.sd_dir.setRange(0.1, 3600)
        self.sd_dir.setValue(20)
        self.sd_dir.setSuffix(" sec")
        sd.addWidget(QLabel("Distance ±"))
        sd.addWidget(self.sd_dist)
        sd.addWidget(QLabel("+"))
        sd.addWidget(self.sd_ppm)
        sd.addWidget(QLabel("Direction ±"))
        sd.addWidget(self.sd_dir)
        sd.addStretch(1)
        form.addRow("Record accuracy", sd)
        self.scale = QDoubleSpinBox()
        self.scale.setRange(1, 100000)
        self.scale.setValue(100)
        self.scale.setPrefix("x")
        form.addRow("Draw error ellipses", self.scale)
        lay.addLayout(form)
        tip = QLabel("Measured dimensions count twice as much as record ones; computed and estimated "
                     "ones much less. Control: select fabric points and use Mark as Control (fixed) or "
                     "set AdjustmentConstraint = XY weighted with a standard deviation.")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: palette(mid);")
        lay.addWidget(tip)

        row = QHBoxLayout()
        ctl = QPushButton("Mark Selected Points as Control")
        ctl.clicked.connect(self._mark_control)
        run = QPushButton("Run")
        run.setDefault(True)
        run.clicked.connect(self._run)
        row.addWidget(ctl)
        row.addStretch(1)
        row.addWidget(run)
        lay.addLayout(row)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.summary)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Line", "Kind", "Record", "Fits as", "Residual", "Normalized"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.cellDoubleClicked.connect(self._zoom)
        lay.addWidget(self.table, 1)
        bot = QHBoxLayout()
        show = QPushButton("Show Results on Map")
        show.clicked.connect(self._show)
        self.apply_btn = QPushButton("Apply to Fabric...")
        self.apply_btn.clicked.connect(self._apply)
        self.apply_btn.setEnabled(False)
        bot.addStretch(1)
        bot.addWidget(show)
        bot.addWidget(self.apply_btn)
        lay.addLayout(bot)

    # ------------------------------------------------------------------
    def _mark_control(self):
        sel = [f.id() for f in self.fabric.points.getSelectedFeatures()]
        if not sel:
            QMessageBox.information(self, self.windowTitle(), "Select control points in the Points layer first.")
            return
        ADJ.mark_control(self.fabric, sel, 1)
        self.iface.messageBar().pushSuccess("Parcel Fabric", "%d point(s) marked as fixed control." % len(sel))

    def _run(self):
        fab = self.fabric
        scope = self.scope.currentData()
        rec = self.manager.active_record_id
        if scope == "record" and not rec:
            QMessageBox.information(self, self.windowTitle(), "Set an active record first.")
            return
        canvas = self.iface.mapCanvas()
        xf = QgsCoordinateTransform(canvas.mapSettings().destinationCrs(), fab.lines.crs(), QgsProject.instance())
        parcels = list(fab.parcels.getSelectedFeatures()) if scope == "parcels" else None
        if scope == "parcels" and not parcels:
            QMessageBox.information(self, self.windowTitle(), "Select parcels first.")
            return
        adjust = self.rb_adjust.isChecked()
        self.net = ADJ.collect(fab, scope, rec, xf.transformBoundingBox(canvas.extent()), parcels,
                               use_control=adjust, sd_dist=self.sd_dist.value(), sd_ppm=self.sd_ppm.value(),
                               sd_dir_sec=self.sd_dir.value())
        if not self.net.obs:
            QMessageBox.information(self, self.windowTitle(), "No lines with COGO dimensions and points at "
                                    "both ends were found.%s" % (
                                        "\n\n" + "\n".join(self.net.skipped[:8]) if self.net.skipped else ""))
            return
        if adjust and not any(p.mode != lsa.FREE for p in self.net.points.values()):
            QMessageBox.information(self, self.windowTitle(), "No control points among these lines. Mark "
                                    "some as control, or run a consistency check.")
            return
        self.res = lsa.adjust(self.net.points, self.net.obs)
        self._fill()
        self.apply_btn.setEnabled(adjust and bool(self.res.points) and self.res.converged)

    def _fill(self):
        r = self.res
        n_fixed = sum(1 for p in self.net.points.values() if p.mode == lsa.FIXED)
        n_w = sum(1 for p in self.net.points.values() if p.mode == lsa.WEIGHTED)
        flagged = sum(1 for o in r.obs if abs(o.normalized) > 3)
        moves = [math.hypot(p.dx, p.dy) for p in r.points.values()]
        chi = r.chi_square_ok()
        lines = [
            "<b>%d points, %d lines (%d observations), %d redundant.</b> %s" % (
                len(self.net.points), len(self.net.lines), len(self.net.obs), r.dof,
                "Held point #%s still." % r.held if r.held is not None else
                "%d fixed and %d weighted control point(s)." % (n_fixed, n_w)),
            "Standard error of unit weight %.2f (1.0 means the record fits as well as the accuracy you set)%s." % (
                r.sigma0, "" if chi is None else ", chi-square test " + ("<span style='color:#1b5e20'>passes</span>"
                                                                       if chi else "<span style='color:#b3261e'>fails</span>")),
            "%s%d line observation(s) disagree (normalized residual over 3)%s. Largest point move %.3f ft." % (
                "<span style='color:#b3261e'>" if flagged else "", flagged, "</span>" if flagged else "",
                max(moves) if moves else 0.0),
        ]
        if r.warnings:
            lines.append("<span style='color:#b3261e'>%s</span>" % " ".join(r.warnings))
        if self.net.skipped:
            lines.append("%d line(s) left out (no points or no COGO)." % len(self.net.skipped))
        self.summary.setText("<br>".join(lines))
        t = self.table
        t.setRowCount(0)
        for o in r.worst(200):
            lyr, feat, _a, _b = self.net.lines[o.obs.key]
            if o.obs.kind == "dist":
                rec, fit, resid = "%.3f ft" % o.obs.value, "%.3f ft" % o.computed, "%+.3f ft" % o.residual
            else:
                from ..core import units as U
                rec, fit = U.azimuth_to_bearing(o.obs.value, 1), U.azimuth_to_bearing(o.computed, 1)
                resid = "%+.1f sec" % (math.degrees(o.residual) * 3600)
            row = t.rowCount()
            t.insertRow(row)
            for c, txt in enumerate(["%s #%d" % (lyr.name(), feat.id()), "Distance" if o.obs.kind == "dist"
                                     else "Direction", rec, fit, resid, "%.2f" % o.normalized]):
                it = QTableWidgetItem(txt)
                it.setData(Qt.ItemDataRole.UserRole, o.obs.key)
                if c == 5 and abs(o.normalized) > 3:
                    it.setForeground(QColor("#b3261e"))
                t.setItem(row, c, it)

    def _zoom(self, row, _col):
        key = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        lyr, feat, _a, _b = self.net.lines[key]
        canvas = self.iface.mapCanvas()
        xf = QgsCoordinateTransform(lyr.crs(), canvas.mapSettings().destinationCrs(), QgsProject.instance())
        box = feat.geometry().boundingBox()
        canvas.setExtent(xf.transformBoundingBox(box.buffered(max(box.width(), box.height()) + 10)))
        canvas.flashGeometries([feat.geometry()], lyr.crs())
        canvas.refresh()

    def _show(self):
        if self.res is None:
            self._run()
            if self.res is None:
                return
        layers = ADJ.result_layers(self.fabric, self.net, self.res, self.scale.value())
        root = QgsProject.instance().layerTreeRoot()
        group = root.insertGroup(0, "Adjustment results")
        for lyr in layers:
            QgsProject.instance().addMapLayer(lyr, False)
            group.addLayer(lyr)
        self.iface.messageBar().pushInfo("Parcel Fabric", "Results added as scratch layers (red lines disagree).")

    def _apply(self):
        if self.res is None or not self.rb_adjust.isChecked():
            return
        moves = [math.hypot(p.dx, p.dy) for p in self.res.points.values()]
        n = sum(1 for m in moves if m > 1e-6)
        if QMessageBox.question(self, self.windowTitle(),
                                "Move %d point(s) (largest move %.3f ft), reshape their lines and rebuild the "
                                "parcels?\n\nCOGO dimensions are not changed." % (n, max(moves) if moves else 0)) \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            pts, lines, parcels = ADJ.apply(self.fabric, self.net, self.res)
        except Exception as e:
            QMessageBox.critical(self, self.windowTitle(), "Apply failed: %s" % e)
            return
        self.iface.mapCanvas().refresh()
        self.iface.messageBar().pushSuccess("Parcel Fabric", "Moved %d point(s), %d line(s); rebuilt %d parcel(s)."
                                            % (pts, lines, parcels))
        self.apply_btn.setEnabled(False)
        self.res = None
