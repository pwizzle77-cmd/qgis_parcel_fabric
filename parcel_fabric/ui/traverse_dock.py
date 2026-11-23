# -*- coding: utf-8 -*-
"""Traverse: enter record courses, check closure, save good ones to the active record.

Preview puts the lines, points and parcel in an unsaved edit; Save keeps it when the traverse is good.
"""

import math

from qgis.core import Qgis, QgsGeometry, QgsLineString, QgsPoint, QgsPointXY, QgsPolygon
from qgis.gui import QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDockWidget, QDoubleSpinBox, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core import cogo as C
from ..core import units as U
from ..core.build import _dates, build_parcels
from ..core.deed_split import apply_deed_split, plan_deed_split
from ..core.edits import EditError
from ..core.fabric import CONNECTIONS, EditSession
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
        self.tie = []                   # calls from the point of commencement to the POB
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
        self._staged = None             # the unsaved preview (an open EditSession)
        self._staged_why = ""           # why that preview can't be saved
        self._staged_sig = None         # what it was made from
        self._staged_rec = None

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
        self.use_tie = QCheckBox()
        self.use_tie.setToolTip("Pick the point of commencement (e.g. the section corner) as the start point; "
                                "the description's tie calls are run from it to the point of beginning")
        self.use_tie.toggled.connect(self._recompute)
        self.use_tie.hide()
        form.addRow("", self.use_tie)
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
        for text, slot, tip in (
                ("Add line", lambda: self._add_row("Line"), ""),
                ("Add curve", lambda: self._add_row("Curve"), ""),
                ("Delete", self._delete_rows, ""),
                ("Clear", self._clear, "Throw away the preview. The courses stay so you can adjust them"),
                ("Paste description...", self._paste, ""),
                ("Read Deed (AI)...", self._read_deed, "")):
            b = QPushButton(text)
            if tip:
                b.setToolTip(tip)
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
        self.adjust.currentIndexChanged.connect(lambda *_: self._recompute())
        self.target.currentIndexChanged.connect(lambda *_: self._recompute())
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
        self.build_after = QCheckBox("Build parcel in the preview")
        self.build_after.setChecked(True)
        self.build_after.setToolTip("The parcel is previewed with the lines and points, and saved with them")
        create = QPushButton("Preview")
        create.setToolTip("Put the lines, points and parcel on the map without saving")
        create.setDefault(True)
        create.clicked.connect(self._create)
        self.save_btn = QPushButton("Save")
        self.save_btn.setToolTip("Save the previewed lines, points and parcels to the active record. "
                                 "Available when the traverse is good.")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self._save)
        cmp_btn = QPushButton("Compare to Selected Polygon")
        cmp_btn.setToolTip("How well this traverse fits the polygon selected in the active layer "
                           "(e.g. the existing county parcel)")
        cmp_btn.clicked.connect(self._compare)
        cr.addWidget(self.build_after)
        cr.addStretch(1)
        cr.addWidget(cmp_btn)
        cr.addWidget(create)
        cr.addWidget(self.save_btn)
        lay.addLayout(cr)
        sr = QHBoxLayout()
        self.fit = QDoubleSpinBox()
        self.fit.setRange(0.01, 50)
        self.fit.setValue(1)
        self.fit.setSuffix(" ft")
        self.fit.setToolTip("Deed corners this close to the parent's corners or lines are held to them; "
                            "corners further outside the parent are refused")
        self.split_btn = QPushButton("Split Selected Parcel...")
        self.split_btn.setToolTip("These courses are the land a deed conveys out of the selected fabric parcel")
        self.split_btn.setEnabled(False)
        self.split_btn.clicked.connect(self._split)
        sr.addStretch(1)
        sr.addWidget(QLabel("Fit to parent within"))
        sr.addWidget(self.fit)
        sr.addWidget(self.split_btn)
        lay.addLayout(sr)
        self.setWidget(w)

        manager.activeFabricChanged.connect(self._fabric_changed)
        manager.fabricsChanged.connect(self._fabric_changed)
        manager.activeRecordChanged.connect(lambda *_: self._recompute())
        self._fabric_changed()
        self._add_row("Line")

    # ------------------------------------------------------------------
    def cleanup(self):
        try:
            self._discard_preview()
        except RuntimeError:            # the layers went with the project
            pass
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

    def _set_tie(self, tie):
        self.tie = list(tie)
        self.use_tie.setText("Start point is the point of commencement (run %d tie call(s) to the POB)"
                             % len(self.tie))
        self.use_tie.setVisible(bool(self.tie))
        self.use_tie.setChecked(bool(self.tie))

    def _clear(self):
        had = self._staged is not None
        self._discard_preview()
        if had:
            self.canvas.refresh()

    def _discard_preview(self):
        """Undo the unsaved traverse and leave layers we opened."""
        staged = self._staged
        if staged is None:
            return
        self._staged = None
        self.save_btn.setEnabled(False)
        for lx, mark in staged.marks:
            if not lx.isEditable():
                continue
            stack = lx.undoStack()
            while stack is not None and stack.index() > mark:
                stack.undo()
        for lx in staged.held:
            stack = lx.undoStack()
            if lx.isEditable() and (stack is None or stack.index() == 0):
                lx.rollBack()

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
        """The point of beginning: the start point, or the end of the tie calls from it."""
        try:
            xy = float(self.x.text().replace(",", "")), float(self.y.text().replace(",", ""))
        except ValueError:
            return None
        if not (self.tie and self.use_tie.isChecked()):
            return xy
        scale, rot = self._record_g2g()
        res = C.compute(xy, self.tie, close_to=False, rotation=math.radians(rot), scale=scale)
        return self.manager.active.traverse_map_legs(res.legs)[-1].end

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
        self._sync_save()
        self.split_btn.setEnabled(False)
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
        try:
            start = self._start()
        except ValueError as e:
            self.result_lbl.setText("<span style='color:#b3261e'>%s</span>" % e)
            return
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
            self._draw(fab, res, start)
            why = self._blocker(fab, res, start)
        except ValueError as e:
            self.result_lbl.setText("<span style='color:#b3261e'>%s</span>" % e)
            return
        self.result = res
        self._sync_save()
        self.split_btn.setEnabled(not why and self.closed.isChecked())
        self.result_lbl.setText(self._summary(fab, res) + (
            "<br><span style='color:#b3261e'>Can't be saved: %s</span>" % why if why
            else "<br>Good. Preview it, then Save."))

    def _signature(self):
        """Everything a preview is made from, to tell when it is out of date."""
        rows = [tuple(self._text(r, c) for c in (COL_BEARING, COL_VALUE, COL_RADIUS)) + tuple(
            self.table.cellWidget(r, c).currentData() for c in (COL_TYPE, COL_SIDE, COL_CURVE_BY, COL_DIR))
            for r in range(self.table.rowCount())]
        return ([r for r in rows if any(r[:3])], self.x.text(), self.y.text(), self.use_tie.isChecked(),
                self.closed.isChecked(), self.adjust.currentData(), self.target.currentText(),
                self.dimsrc.currentText(), self.build_after.isChecked(), self.manager.active_record_id,
                self._record_g2g())

    def _sync_save(self):
        self.save_btn.setEnabled(self._staged is not None and not self._staged_why
                                 and self._staged_sig == self._signature())

    def _final(self, res, start):
        """The traverse as it would be saved (compass rule applied if chosen)."""
        if not (self.closed.isChecked() and self.adjust.currentData() == "compass" and res.misclose > 1e-9):
            return res
        scale, rot = self._record_g2g()
        out = C.compute(start, C.compass_rule(res, start), close_to=start, rotation=math.radians(rot), scale=scale)
        # adjusted geometry, but the lines keep the RECORD dimensions
        for leg, orig_leg in zip(out.legs, res.legs):
            leg.course = orig_leg.course
            leg.record_az = orig_leg.chord_az
        return out

    def _blocker(self, fab, res, start):
        """Why this traverse isn't good enough to save ('' when it is)."""
        if self.manager.active_record() is None:
            return "set an active record first (Records panel)."
        legs = fab.traverse_map_legs(self._final(res, start).legs)
        a, b = legs[0].start, legs[-1].end
        if not self.closed.isChecked():
            if self.target.currentText() == CONNECTIONS or (
                    fab.find_point(*a) is not None and fab.find_point(*b) is not None):
                return ""
            return "an open traverse must start and end on existing fabric points."
        if res.misclose > 1e-6 and res.ratio < fab.warn_ratio:
            return "misclose 1:%s is worse than 1:%s (Fabric Settings)." % (
                "{:,}".format(int(res.ratio)), "{:,}".format(int(fab.warn_ratio)))
        if max(abs(a[0] - b[0]), abs(a[1] - b[1])) > fab.tolerance:
            return "the ends don't meet. Choose Compass rule to close the gap."
        ring = self._map_points(fab, legs)
        ring[-1] = ring[0].clone()
        if not QgsGeometry(QgsLineString(ring)).isSimple():
            return "the courses cross each other."
        return ""

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
            e = fab.traverse_map_legs(res.legs)[-1].end
            lines.append("Ends at %.3f, %.3f" % e)
        return "<br>".join(lines)

    def _to_canvas_pt(self, fab, xy):
        from qgis.core import QgsCoordinateTransform, QgsPointXY, QgsProject
        xf = QgsCoordinateTransform(fab.points.crs(), self.canvas.mapSettings().destinationCrs(),
                                    QgsProject.instance())
        return xf.transform(QgsPointXY(*xy))

    @staticmethod
    def _map_points(fab, legs):
        pts = []
        for leg in legs:
            g = fab.leg_geometry(leg)
            seg = g.constGet().segmentize()
            for i in range(seg.numPoints()):
                p = seg.pointN(i)
                if pts and abs(pts[-1].x() - p.x()) < 1e-9 and abs(pts[-1].y() - p.y()) < 1e-9:
                    continue
                pts.append(QgsPoint(p.x(), p.y()))
        return pts

    def _draw(self, fab, res, start):
        legs = fab.traverse_map_legs(res.legs)
        pts = self._map_points(fab, legs)
        crs = fab.points.crs()
        self.band.setToGeometry(QgsGeometry(QgsLineString(pts)), crs)
        if self.closed.isChecked() and res.misclose > 1e-6:
            e = legs[-1].end
            self.gap.setToGeometry(QgsGeometry(QgsLineString([QgsPoint(*e), QgsPoint(*start)])), crs)

    def _picked(self, pt, name):
        self.x.setText(repr(pt.x()))        # every digit, so the start is exactly the picked point
        self.y.setText(repr(pt.y()))
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
        self.load_courses(dlg.courses, dlg.tie)

    def _read_deed(self):
        fab = self.manager.active
        if fab is None:
            return
        from .deed_reader_dialog import DeedReaderDialog
        dlg = DeedReaderDialog(self.iface, self.manager, self)
        if dlg.exec() and dlg.courses:
            self.load_courses(dlg.courses, dlg.tie)
            self.dimsrc.setCurrentText("Record")

    def load_courses(self, courses, tie=()):
        """Replace the grid with these courses (from a description or a deed).
        ``tie``: the calls from the point of commencement to the POB."""
        self._set_tie(tie)
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
        for leg in fab.traverse_map_legs(self.result.legs):
            geometry = fab.leg_geometry(leg)
            seg = geometry.constGet().segmentize()
            for i in range(seg.numPoints()):
                p = seg.pointN(i)
                pts.append(QgsPoint(p.x(), p.y()))
        pts.append(QgsPoint(pts[0].x(), pts[0].y()))
        poly = QgsGeometry(QgsPolygon(QgsLineString(pts)))
        m = measure(poly, ref)
        factor = fab.map_units_per_foot
        for key in ("area", "ref_area"):
            m[key] /= factor ** 2
        for key in ("hausdorff", "shift"):
            m[key] /= factor
        diff = (m["area"] - m["ref_area"]) / m["ref_area"] * 100 if m["ref_area"] else 0
        self.result_lbl.setText(self._summary(fab, self.result) + (
            "<br><b>Against the selected polygon:</b> area %.3f ac vs %.3f ac (%+.2f%%), largest offset "
            "%.2f ft, shifted %.2f ft %s, %.1f%% overlap." % (
                m["area"] / 43560.0, m["ref_area"] / 43560.0, diff, m["hausdorff"], m["shift"],
                U.azimuth_to_bearing(m["shift_az"]) if m["shift"] > 0.005 else "", m["overlap"] * 100)))

    def _create(self):
        """Preview: put the lines, points and parcel on the map in an edit that isn't saved."""
        fab = self.manager.active
        if fab is None:
            return
        self._discard_preview()         # its own points must not count as fabric points below
        self._recompute()
        if self.result is None:
            QMessageBox.warning(self, "Traverse", "Fix the start point and courses first.")
            return
        rec = self.manager.active_record_id
        if self.manager.active_record() is None:
            QMessageBox.information(self, "Traverse", "Set an active record first (Records panel), so the lines "
                                    "belong to a plat or deed.")
            return
        start = self._start()
        why = self._blocker(fab, self.result, start)
        final = self._final(self.result, start)
        scale, rot = self._record_g2g()
        target = self.target.currentText()
        building = self.build_after.isChecked() and self.closed.isChecked() and target in fab.parcel_types
        layers = [fab.line_layer(target), fab.points]
        if building:
            layers = [fab.parcels] + layers + [fab.records]      # the layer likeliest to be refused saves first
        rep = None
        try:
            with EditSession(layers, "Traverse", commit=False) as ed:
                n_lines, n_pts = fab.add_traverse(target, final.legs, rec, self.dimsrc.currentText(),
                                                  math.radians(rot), scale, session=ed)
                if building:
                    rep = build_parcels(fab, target, rec, session=ed)
        except Exception as e:
            QMessageBox.critical(self, "Traverse", "Could not preview the traverse: %s" % e)
            return
        if rep is not None and not why:
            why = self._build_problem(fab, final, rep)
        self._staged, self._staged_why = ed, why
        self._staged_sig, self._staged_rec = self._signature(), rec
        self._sync_save()
        msg = "%d line(s) and %d point(s) previewed on %s. Not saved." % (n_lines, n_pts, target)
        if rep is not None:
            msg += " " + rep.summary()
            if rep.issues:
                IssuesDialog(self.iface, "Build %s" % target, rep.summary(), rep.issues,
                             fab.records.crs()).show()
        self.canvas.refresh()
        self.band.reset(Qgis.GeometryType.Line)
        self.gap.reset(Qgis.GeometryType.Line)
        if why:
            self.iface.messageBar().pushWarning("Parcel Fabric", msg + " It can't be saved: %s Clear throws it away "
                                                "and leaves the courses." % why)
        else:
            self.iface.messageBar().pushSuccess(
                "Parcel Fabric", msg + " Save keeps them. Clear throws them away and leaves the courses.")

    def _save(self):
        staged = self._staged
        fab = self.manager.active
        self._sync_save()
        if staged is None or fab is None or not self.save_btn.isEnabled():
            QMessageBox.information(self, "Traverse", "Preview a good traverse first. Save writes that preview."
                                    + (" " + self._staged_why if staged is not None else ""))
            return
        errors = []
        for lx in staged.held:
            if not lx.isEditable():
                continue
            if errors:
                lx.rollBack()           # one layer failed: don't save the rest without it
            elif not lx.commitChanges():
                errors.append("%s: %s" % (lx.name(), "; ".join(lx.commitErrors())))
                lx.rollBack()
        left = [lx.name() for lx, _mark in staged.marks
                if lx not in staged.held and lx.isEditable()]
        self._staged = None
        self._sync_save()
        self.canvas.refresh()
        if errors:
            QMessageBox.critical(self, "Traverse", "Save failed - " + " | ".join(errors))
            return
        _dates(fab, [self._staged_rec])
        msg = "Traverse saved."
        if left:
            msg += " %s already had edits, so Save left those layers open." % ", ".join(left)
        self.iface.messageBar().pushSuccess("Parcel Fabric", msg)

    def _split(self):
        fab = self.manager.active
        if fab is None:
            return
        self._recompute()
        if not self.split_btn.isEnabled():
            return
        sel = list(fab.parcels.getSelectedFeatures())
        if len(sel) != 1:
            QMessageBox.information(self, "Split Parcel", "Select ONE parcel in the fabric's Parcels layer: the "
                                    "parent this deed conveys land out of. It must already be in the fabric.")
            return
        self._discard_preview()
        parent = sel[0]
        start = self._start()
        scale, rot = self._record_g2g()
        final = self._final(self.result, start)
        factor = fab.map_units_per_foot
        title = "Split Parcel"
        try:
            plan = plan_deed_split(fab, parent, fab.traverse_map_legs(final.legs), self.fit.value() * factor)
            acre = 43560.0 * factor ** 2
            moved = max((m[1] for m in plan.moved), default=0.0) / factor
            text = ("Parent: %s\n\nDeed piece: %.3f acres by the deed, %.3f fitted to the parent.\n"
                    "Remainder: %.3f acres.\nLargest corner adjustment: %.2f ft.\n"
                    "New lines: %d with the deed's dimensions; %d course(s) follow the parent's boundary." % (
                        parent["ParcelNumber"] or parent["Name"] or "#%d" % parent.id(), final.area / 43560.0,
                        plan.piece.area() / acre, plan.left / acre, moved,
                        len(plan.inside), len(plan.along)))
            if len(plan.remainders) > 1:
                text += ("\n\nThe deed leaves %d separate remainder pieces. Only the largest can keep the "
                         "parent's number; the others become new parcels." % len(plan.remainders))
            box = QMessageBox(QMessageBox.Icon.Question, title, text, parent=self)
            keep = box.addButton("Remainder keeps the number", QMessageBox.ButtonRole.AcceptRole)
            new = box.addButton("All pieces are new", QMessageBox.ButtonRole.AcceptRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(keep)
            box.exec()
            if box.clickedButton() not in (keep, new):
                return
            res = apply_deed_split(fab, plan, self.manager.active_record_id, box.clickedButton() is keep,
                                   self.dimsrc.currentText(), math.radians(rot), scale)
        except EditError as e:
            QMessageBox.warning(self, title, str(e))
            return
        except Exception as e:          # keep QGIS alive; show what happened
            QMessageBox.critical(self, title, "Nothing was saved: %s" % e)
            return
        fab.parcels.removeSelection()
        self.canvas.refresh()
        if res.issues:
            IssuesDialog(self.iface, title, res.summary(), res.issues, fab.records.crs()).show()
        else:
            self.iface.messageBar().pushSuccess("Parcel Fabric", "Split: " + res.summary())

    def _build_problem(self, fab, res, rep):
        """Why the previewed parcel isn't good ('' when it built)."""
        ring = self._map_points(fab, fab.traverse_map_legs(res.legs))
        ring[-1] = ring[0].clone()
        poly = QgsGeometry(QgsPolygon(QgsLineString(ring)))
        bad = [i for i in rep.issues if i.severity == "error" and i.x is not None
               and poly.contains(QgsGeometry.fromPointXY(QgsPointXY(i.x, i.y)))]
        if not bad and (rep.created or rep.rebuilt or rep.seeds_used):
            return ""
        return "the parcel couldn't be built. %s" % (bad[0].message if bad else "")
