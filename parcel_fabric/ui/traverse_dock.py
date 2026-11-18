# -*- coding: utf-8 -*-
"""Traverse: enter record courses, check closure, write fabric lines."""

import math

from qgis.core import Qgis, QgsGeometry, QgsLineString, QgsPoint
from qgis.gui import QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDockWidget, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core import cogo as C
from ..core import units as U
from ..core.build import build_parcels
from .issues_dialog import IssuesDialog
from .legal_dialog import LegalDialog
from .map_tools import PickPointTool

COL_TYPE, COL_BEARING, COL_VALUE, COL_RADIUS, COL_SIDE, COL_CURVE_BY, COL_DIR = range(7)
HEADERS = ["Type", "Bearing", "Distance / value", "Radius", "Side", "Curve by", "Bearing is"]
CURVE_BY = ["Arc", "Chord", "Delta", "Tangent"]
DIR_KINDS = [("Chord", C.DIR_CHORD), ("Tangent", C.DIR_TANGENT),
             ("Radial", C.DIR_RADIAL), ("Tangent to previous", C.DIR_TANGENT_PREV)]
ERR_BG = QColor(255, 220, 220)


class TraverseDock(QDockWidget):
    def __init__(self, iface, manager, parent=None):
        super().__init__("Parcel Fabric Traverse", parent or iface.mainWindow())
        self.setObjectName("ParcelFabricTraverseDock")
        self.iface = iface
        self.manager = manager
        self.canvas = iface.mapCanvas()
        self.result = None
        self.courses = []
        self._filling = False

        self.band = QgsRubberBand(self.canvas, Qgis.GeometryType.Line)
        self.band.setColor(QColor(220, 60, 20))
        self.band.setWidth(2)
        self.gap = QgsRubberBand(self.canvas, Qgis.GeometryType.Line)
        self.gap.setColor(QColor(220, 0, 160))
        self.gap.setWidth(2)
        self.gap.setLineStyle(Qt.PenStyle.DashLine)
        self.marker = QgsVertexMarker(self.canvas)
        self.marker.setIconType(QgsVertexMarker.IconType.ICON_CIRCLE)
        self.marker.setColor(QColor(220, 60, 20))
        self.marker.setIconSize(12)
        self.marker.hide()
        self.tool = PickPointTool(self.canvas, lambda: self.manager.active)
        self.tool.picked.connect(self._picked)

        w = QWidget()
        lay = QVBoxLayout(w)
        form = QFormLayout()
        self.target = QComboBox()
        self.target.setToolTip("Which lines layer the courses are written to")
        form.addRow("Lines go to", self.target)
        sp = QHBoxLayout()
        self.x = QLineEdit()
        self.y = QLineEdit()
        self.x.setPlaceholderText("X (easting)")
        self.y.setPlaceholderText("Y (northing)")
        self.x.editingFinished.connect(self._recompute)
        self.y.editingFinished.connect(self._recompute)
        pick = QPushButton("Pick")
        pick.setToolTip("Click the point of beginning on the map (snaps to fabric points)")
        pick.clicked.connect(lambda: self.canvas.setMapTool(self.tool))
        sp.addWidget(self.x)
        sp.addWidget(self.y)
        sp.addWidget(pick)
        form.addRow("Start point", sp)
        self.start_name = QLabel()
        self.start_name.setStyleSheet("color: palette(mid);")
        form.addRow("", self.start_name)
        self.g2g = QLabel()
        self.g2g.setWordWrap(True)
        form.addRow("Ground to grid", self.g2g)
        lay.addLayout(form)

        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.horizontalHeader().setSectionResizeMode(COL_BEARING, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.itemChanged.connect(self._item_changed)
        lay.addWidget(self.table, 1)

        rowb = QHBoxLayout()
        for text, slot in [("Add line", lambda: self._add_row("Line")),
                           ("Add curve", lambda: self._add_row("Curve")),
                           ("Delete", self._delete_rows), ("Clear", self._clear),
                           ("Paste description...", self._paste),
                           ("Read Deed (AI)...", self._read_deed)]:
            b = QPushButton(text)
            b.clicked.connect(slot)
            rowb.addWidget(b)
        lay.addLayout(rowb)

        opts = QHBoxLayout()
        self.closed = QCheckBox("Closed loop")
        self.closed.setChecked(True)
        self.closed.toggled.connect(self._recompute)
        self.adjust = QComboBox()
        self.adjust.addItem("No adjustment", "none")
        self.adjust.addItem("Compass rule", "compass")
        self.dimsrc = QComboBox()
        self.dimsrc.addItems(["Record", "Measured", "Computed", "Estimated"])
        self.dimsrc.setToolTip("Where these dimensions came from (saved on each line)")
        opts.addWidget(self.closed)
        opts.addWidget(self.adjust)
        opts.addWidget(QLabel("Source"))
        opts.addWidget(self.dimsrc)
        opts.addStretch(1)
        lay.addLayout(opts)

        self.result_lbl = QLabel()
        self.result_lbl.setWordWrap(True)
        self.result_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.result_lbl)

        cr = QHBoxLayout()
        self.build_after = QCheckBox("Build parcel after creating")
        self.build_after.setChecked(True)
        create = QPushButton("Create lines")
        create.setDefault(True)
        create.clicked.connect(self._create)
        cmp_btn = QPushButton("Compare to Selected Polygon")
        cmp_btn.setToolTip("How well this traverse fits the polygon selected in the active layer "
                           "(e.g. the existing county parcel)")
        cmp_btn.clicked.connect(self._compare)
        cr.addWidget(self.build_after)
        cr.addStretch(1)
        cr.addWidget(cmp_btn)
        cr.addWidget(create)
        lay.addLayout(cr)
        self.setWidget(w)

        manager.activeFabricChanged.connect(self._fabric_changed)
        manager.fabricsChanged.connect(self._fabric_changed)
        manager.activeRecordChanged.connect(lambda *_: self._recompute())
        self._fabric_changed()
        self._add_row("Line")

    # ------------------------------------------------------------------
    def cleanup(self):
        for item in (self.band, self.gap, self.marker):
            self.canvas.scene().removeItem(item)
        if self.canvas.mapTool() is self.tool:
            self.canvas.unsetMapTool(self.tool)

    def closeEvent(self, e):
        self.band.reset(Qgis.GeometryType.Line)
        self.gap.reset(Qgis.GeometryType.Line)
        self.marker.hide()
        super().closeEvent(e)

    def _fabric_changed(self):
        fab = self.manager.active
        cur = self.target.currentText()
        self.target.clear()
        if fab is None:
            self.result_lbl.setText("Add or create a parcel fabric first.")
            return
        self.target.addItems(fab.parcel_types + ["Connections"])
        i = self.target.findText(cur)
        if i >= 0:
            self.target.setCurrentIndex(i)
        self._recompute()

    # ------------------------------------------------------------------
    # Grid
    # ------------------------------------------------------------------
    def _combo(self, items, current=None):
        cb = QComboBox()
        for it in items:
            if isinstance(it, tuple):
                cb.addItem(it[0], it[1])
            else:
                cb.addItem(it, it)
        if current is not None:
            i = cb.findData(current)
            cb.setCurrentIndex(max(i, 0))
        cb.currentIndexChanged.connect(lambda *_: self._row_kind_changed())
        return cb

    def _add_row(self, kind="Line", values=None, at=None):
        self._filling = True
        r = self.table.rowCount() if at is None else at
        self.table.insertRow(r)
        self.table.setCellWidget(r, COL_TYPE, self._combo(["Line", "Curve"], kind))
        self.table.setCellWidget(r, COL_SIDE, self._combo([("R", "R"), ("L", "L")],
                                                          (values or {}).get("side", "R")))
        self.table.setCellWidget(r, COL_CURVE_BY, self._combo(CURVE_BY, (values or {}).get("by", "Arc")))
        self.table.setCellWidget(r, COL_DIR, self._combo(DIR_KINDS, (values or {}).get("dir", C.DIR_CHORD)))
        for col, key in ((COL_BEARING, "bearing"), (COL_VALUE, "value"), (COL_RADIUS, "radius")):
            self.table.setItem(r, col, QTableWidgetItem((values or {}).get(key, "")))
        self._filling = False
        self._row_kind_changed()
        self.table.setCurrentCell(r, COL_BEARING)

    def _row_kind_changed(self):
        for r in range(self.table.rowCount()):
            curve = self.table.cellWidget(r, COL_TYPE).currentData() == "Curve"
            for col in (COL_SIDE, COL_CURVE_BY, COL_DIR):
                self.table.cellWidget(r, col).setEnabled(curve)
            it = self.table.item(r, COL_RADIUS)
            if it is not None:
                flags = it.flags()
                if curve:
                    it.setFlags(flags | Qt.ItemFlag.ItemIsEditable | Qt.ItemFlag.ItemIsEnabled)
                else:
                    it.setFlags(flags & ~Qt.ItemFlag.ItemIsEditable)
        self._recompute()

    def _item_changed(self, item):
        if self._filling:
            return
        # finishing the distance/value of the last row starts a new row
        if item.row() == self.table.rowCount() - 1 and item.column() == COL_VALUE and item.text().strip():
            self._recompute()
            if self.result is not None:
                self._add_row("Line", values={})
            return
        self._recompute()

    def _delete_rows(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)
        if self.table.rowCount() == 0:
            self._add_row("Line")
        self._recompute()

    def _clear(self):
        self.table.setRowCount(0)
        self._add_row("Line")

    def _text(self, r, c):
        it = self.table.item(r, c)
        return it.text().strip() if it else ""

    def _courses(self, fab):
        """Courses from the grid; marks bad cells. Blank rows are skipped."""
        courses, errors = [], []
        packed = fab.packed_dms
        for r in range(self.table.rowCount()):
            for c in (COL_BEARING, COL_VALUE, COL_RADIUS):
                it = self.table.item(r, c)
                if it:
                    it.setBackground(QColor(0, 0, 0, 0))
            kind = self.table.cellWidget(r, COL_TYPE).currentData()
            b, v, rad = self._text(r, COL_BEARING), self._text(r, COL_VALUE), self._text(r, COL_RADIUS)
            dir_kind = self.table.cellWidget(r, COL_DIR).currentData()
            if not v and not b and not rad:
                continue
            col = COL_BEARING
            try:
                if kind == "Line":
                    az = U.parse_bearing(b, packed)
                    col = COL_VALUE
                    courses.append(C.Course(C.LINE, az, U.parse_distance(v, fab.foot_m)))
                else:
                    az = None
                    if dir_kind != C.DIR_TANGENT_PREV or b:
                        az = U.parse_bearing(b, packed)
                    col = COL_RADIUS
                    radius = U.parse_distance(rad, fab.foot_m)
                    if radius <= 0:
                        raise U.ParseError("Radius must be positive")
                    col = COL_VALUE
                    by = self.table.cellWidget(r, COL_CURVE_BY).currentData()
                    if by == "Delta":
                        arc = C.arc_length_from_delta(radius, math.radians(U.parse_angle(v, packed)))
                    elif by == "Chord":
                        arc = C.arc_length_from_chord(radius, U.parse_distance(v, fab.foot_m))
                    elif by == "Tangent":
                        arc = C.arc_length_from_tangent(radius, U.parse_distance(v, fab.foot_m))
                    else:
                        arc = U.parse_distance(v, fab.foot_m)
                    side = self.table.cellWidget(r, COL_SIDE).currentData()
                    courses.append(C.Course(C.CURVE, az, None, radius if side == "R" else -radius,
                                            arc, dir_kind))
                courses[-1].validate()
            except (U.ParseError, ValueError) as e:
                it = self.table.item(r, col)
                if it:
                    it.setBackground(ERR_BG)
                errors.append("Course %d: %s" % (r + 1, e))
        return courses, errors

    def _start(self):
        try:
            return float(self.x.text().replace(",", "")), float(self.y.text().replace(",", ""))
        except ValueError:
            return None

    def _record_g2g(self):
        rec = self.manager.active_record()
        scale, rot = 1.0, 0.0
        if rec is not None:
            try:
                scale = float(rec["QPF_ScaleFactor"] or 1.0) or 1.0
            except (TypeError, ValueError):
                scale = 1.0
            try:
                rot = float(rec["QPF_Rotation"] or 0.0)
            except (TypeError, ValueError):
                rot = 0.0
        return scale, rot

    # ------------------------------------------------------------------
    def _recompute(self):
        fab = self.manager.active
        self.result = None
        self.band.reset(Qgis.GeometryType.Line)
        self.gap.reset(Qgis.GeometryType.Line)
        if fab is None:
            return
        scale, rot = self._record_g2g()
        rec = self.manager.active_record()
        self.g2g.setText(("scale %.8f, rotation %s (from record %s)" % (
            scale, U.format_angle(rot, 1) if rot >= 0 else "-" + U.format_angle(-rot, 1),
            rec["Name"])) if rec is not None and (scale != 1.0 or rot != 0.0)
            else "none (set QPF_ScaleFactor / QPF_Rotation on the record)")
        start = self._start()
        courses, errors = self._courses(fab)
        self.courses = courses
        if start is None:
            self.marker.hide()
            self.result_lbl.setText("Enter or pick a start point.")
            return
        self.marker.setCenter(self._to_canvas_pt(fab, start))
        self.marker.show()
        if errors:
            self.result_lbl.setText("<span style='color:#b3261e'>%s</span>" % "<br>".join(errors[:5]))
            return
        if not courses:
            self.result_lbl.setText("Enter courses.")
            return
        try:
            res = C.compute(start, courses, close_to=start if self.closed.isChecked() else False,
                            rotation=math.radians(rot), scale=scale)
        except ValueError as e:
            self.result_lbl.setText("<span style='color:#b3261e'>%s</span>" % e)
            return
        self.result = res
        self._draw(fab, res, start)
        self.result_lbl.setText(self._summary(fab, res))

    def _summary(self, fab, res):
        lines = ["%d course(s), length %.2f ft" % (len(res.legs), res.perimeter)]
        if self.closed.isChecked():
            if res.misclose < 1e-6:
                lines.append("<b>Closes exactly.</b>")
            else:
                bad = res.ratio < fab.warn_ratio
                color = "#b3261e" if bad else "#1b5e20"
                lines.append("<span style='color:%s'><b>Misclose %.3f ft  %s  (1:%s)</b></span>" % (
                    color, res.misclose, U.azimuth_to_bearing(res.misclose_azimuth),
                    "{:,}".format(int(res.ratio))))
            lines.append("Area %s sq ft  =  %.4f acres" % ("{:,.1f}".format(res.area), res.area / 43560.0))
        else:
            e = res.end_point
            lines.append("Ends at %.3f, %.3f" % e)
        return "<br>".join(lines)

    def _to_canvas_pt(self, fab, xy):
        from qgis.core import QgsCoordinateTransform, QgsPointXY, QgsProject
        xf = QgsCoordinateTransform(fab.points.crs(), self.canvas.mapSettings().destinationCrs(),
                                    QgsProject.instance())
        return xf.transform(QgsPointXY(*xy))

    def _draw(self, fab, res, start):
        pts = []
        for leg in res.legs:
            g = fab.leg_geometry(leg)
            seg = g.constGet().segmentize()
            for i in range(seg.numPoints()):
                p = seg.pointN(i)
                if pts and abs(pts[-1].x() - p.x()) < 1e-9 and abs(pts[-1].y() - p.y()) < 1e-9:
                    continue
                pts.append(QgsPoint(p.x(), p.y()))
        crs = fab.points.crs()
        self.band.setToGeometry(QgsGeometry(QgsLineString(pts)), crs)
        if self.closed.isChecked() and res.misclose > 1e-6:
            e = res.end_point
            self.gap.setToGeometry(QgsGeometry(QgsLineString([QgsPoint(*e), QgsPoint(*start)])), crs)

    def _picked(self, pt, name):
        self.x.setText("%.4f" % pt.x())
        self.y.setText("%.4f" % pt.y())
        self.start_name.setText("Starts on %s" % name if name else "")
        self.iface.actionPan().trigger()
        self._recompute()

    # ------------------------------------------------------------------
    def _paste(self):
        fab = self.manager.active
        if fab is None:
            return
        dlg = LegalDialog(fab.foot_m, self)
        if not dlg.exec() or not dlg.courses:
            return
        self.load_courses(dlg.courses)

    def _read_deed(self):
        fab = self.manager.active
        if fab is None:
            return
        from .deed_reader_dialog import DeedReaderDialog
        dlg = DeedReaderDialog(self.iface, self.manager, self)
        if dlg.exec() and dlg.courses:
            self.load_courses(dlg.courses)
            self.dimsrc.setCurrentText("Record")

    def load_courses(self, courses):
        """Replace the grid with these courses (from a description or a deed)."""
        self.table.setRowCount(0)
        for c in courses:
            if c.is_curve:
                self._add_row("Curve", {
                    "bearing": U.azimuth_to_bearing(c.azimuth, 2),
                    "value": "%.4f" % c.arc_length, "radius": "%.4f" % abs(c.radius),
                    "side": "R" if c.radius > 0 else "L", "by": "Arc", "dir": c.dir_kind})
            else:
                self._add_row("Line", {"bearing": U.azimuth_to_bearing(c.azimuth, 2),
                                       "value": "%.4f" % c.distance})
        self._recompute()

    def _compare(self):
        from qgis.core import Qgis as _Q, QgsCoordinateTransform, QgsProject
        from ..core.compare import measure
        fab = self.manager.active
        self._recompute()
        if fab is None or self.result is None or not self.closed.isChecked():
            QMessageBox.information(self, "Compare", "Enter a closed traverse first.")
            return
        lyr = self.iface.activeLayer()
        sel = list(lyr.getSelectedFeatures()) if lyr is not None and hasattr(lyr, "getSelectedFeatures") else []
        if lyr is None or getattr(lyr, "geometryType", lambda: None)() != _Q.GeometryType.Polygon or len(sel) != 1:
            QMessageBox.information(self, "Compare", "Select ONE polygon in the active layer (e.g. the "
                                    "existing parcel), then press Compare again.")
            return
        ref = sel[0].geometry()
        ref.transform(QgsCoordinateTransform(lyr.crs(), fab.points.crs(), QgsProject.instance()))
        pts = []
        for leg in self.result.legs:
            seg = fab.leg_geometry(leg).constGet().segmentize()
            for i in range(seg.numPoints()):
                p = seg.pointN(i)
                pts.append(QgsPoint(p.x(), p.y()))
        pts.append(QgsPoint(pts[0].x(), pts[0].y()))
        from qgis.core import QgsPolygon
        poly = QgsGeometry(QgsPolygon(QgsLineString(pts)))
        m = measure(poly, ref)
        diff = (m["area"] - m["ref_area"]) / m["ref_area"] * 100 if m["ref_area"] else 0
        self.result_lbl.setText(self._summary(fab, self.result) + (
            "<br><b>Against the selected polygon:</b> area %.3f ac vs %.3f ac (%+.2f%%), largest offset "
            "%.2f ft, shifted %.2f ft %s, %.1f%% overlap." % (
                m["area"] / 43560.0, m["ref_area"] / 43560.0, diff, m["hausdorff"], m["shift"],
                U.azimuth_to_bearing(m["shift_az"]) if m["shift"] > 0.005 else "", m["overlap"] * 100)))

    def _create(self):
        fab = self.manager.active
        if fab is None:
            return
        self._recompute()
        if self.result is None:
            QMessageBox.warning(self, "Traverse", "Fix the start point and courses first.")
            return
        rec = self.manager.active_record_id
        if not rec:
            if QMessageBox.question(
                    self, "Traverse", "There is no active record, so these lines won't belong to "
                    "any plat or deed. Create them anyway?") != QMessageBox.StandardButton.Yes:
                return
        start = self._start()
        res = self.result
        scale, rot = self._record_g2g()
        if self.closed.isChecked() and self.adjust.currentData() == "compass" and res.misclose > 1e-9:
            adj = C.compass_rule(res, start)
            res = C.compute(start, adj, close_to=start, rotation=math.radians(rot), scale=scale)
            # adjusted geometry, but the lines keep the RECORD dimensions
            for leg, orig_leg in zip(res.legs, self.result.legs):
                leg.course = orig_leg.course
                leg.record_az = orig_leg.chord_az
        target = self.target.currentText()
        try:
            n_lines, n_pts = fab.add_traverse(target, res.legs, rec, self.dimsrc.currentText(),
                                              math.radians(rot), scale)
        except Exception as e:
            QMessageBox.critical(self, "Traverse", "Could not create lines: %s" % e)
            return
        msg = "%d line(s) and %d point(s) added to %s." % (n_lines, n_pts, target)
        if self.build_after.isChecked() and self.closed.isChecked() and target in fab.parcel_types and rec:
            try:
                rep = build_parcels(fab, target, rec)
                msg += " Build: " + rep.summary()
                if rep.issues:
                    IssuesDialog(self.iface, "Build %s" % target, rep.summary(), rep.issues,
                                 fab.records.crs()).show()
            except Exception as e:
                msg += " Build failed: %s" % e
        self.iface.messageBar().pushSuccess("Parcel Fabric", msg)
        self.canvas.refresh()
        self.band.reset(Qgis.GeometryType.Line)
        self.gap.reset(Qgis.GeometryType.Line)
