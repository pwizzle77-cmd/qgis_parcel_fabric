# -*- coding: utf-8 -*-
"""Split, Divide, Merge and Copy Parallel windows.

Each one works on the features selected in the fabric's Parcels (or Parcel
Lines) layer and the active record.  They stay open, so you can do several
edits in a row.
"""

import math

from qgis.core import Qgis, QgsGeometry, QgsPointXY
from qgis.gui import QgsRubberBand
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QRadioButton, QSpinBox, QVBoxLayout,
)

from ..core import edits as E
from ..core import units as U
from .issues_dialog import IssuesDialog
from .map_tools import DrawLineTool, PickLineTool


class _EditWindow(QDialog):
    TITLE = ""
    HELP = ""
    LAYER = "parcels"         # which layer's selection the window uses

    def __init__(self, iface, manager, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.manager = manager
        self.canvas = iface.mapCanvas()
        self.setWindowTitle(self.TITLE)
        self.setMinimumWidth(460)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.band = QgsRubberBand(self.canvas, Qgis.GeometryType.Line)
        self.band.setColor(QColor(200, 0, 120))
        self.band.setWidth(2)
        self.lay = QVBoxLayout(self)
        self.sel_lbl = QLabel()
        self.sel_lbl.setWordWrap(True)
        self.rec_lbl = QLabel()
        self.rec_lbl.setWordWrap(True)
        self.lay.addWidget(self.sel_lbl)
        self.lay.addWidget(self.rec_lbl)
        self._watched = []
        fab = manager.active
        if fab is not None:
            for lyr in self._layers():
                lyr.selectionChanged.connect(self._sel_changed)
                self._watched.append(lyr)
        manager.activeRecordChanged.connect(self._rec_changed)

    # -- to build in subclasses ----------------------------------------
    def _finish_ui(self):
        self.bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Close)
        self.bb.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._run)
        self.bb.rejected.connect(self.close)
        self.lay.addWidget(self.bb)
        self._sel_changed()
        self._rec_changed()

    def _layers(self):
        fab = self.manager.active
        return [fab.parcels] if self.LAYER == "parcels" else [fab.lines, fab.connections]

    def selected(self):
        fab = self.manager.active
        if fab is None:
            return []
        out = []
        for lyr in self._layers():
            out += list(lyr.getSelectedFeatures())
        return out

    def _sel_changed(self, *_):
        n = len(self.selected())
        what = "parcel" if self.LAYER == "parcels" else "line"
        self.sel_lbl.setText("<b>%d %s(s) selected.</b> %s" % (n, what, self.HELP))
        self.on_selection()

    def on_selection(self):
        pass

    def _rec_changed(self, *_):
        rec = self.manager.active_record()
        if rec is None:
            self.rec_lbl.setText("<span style='color:#b3261e'>No active record - set one in the "
                                 "Records panel first.</span>")
        else:
            self.rec_lbl.setText("Edits will belong to record <b>%s</b>." % (rec["Name"] or rec["GlobalID"]))

    def show_line(self, geoms):
        fab = self.manager.active
        self.band.reset(Qgis.GeometryType.Line)
        if fab is None or not geoms:
            return
        self.band.setToGeometry(QgsGeometry.collectGeometry(geoms), fab.points.crs())

    def report(self, res):
        self.iface.mapCanvas().refresh()
        msg = "%s: %s" % (self.TITLE, res.summary())
        if res.issues:
            IssuesDialog(self.iface, self.TITLE, msg, res.issues, self.manager.active.records.crs()).show()
        else:
            self.iface.messageBar().pushSuccess("Parcel Fabric", msg)

    def _run(self):
        fab = self.manager.active
        if fab is None:
            return
        try:
            res = self.apply(fab, self.manager.active_record_id)
        except E.EditError as e:
            QMessageBox.warning(self, self.TITLE, str(e))
            return
        except Exception as e:          # keep QGIS alive; show what happened
            QMessageBox.critical(self, self.TITLE, "%s failed: %s" % (self.TITLE, e))
            return
        if res is not None:
            self.band.reset(Qgis.GeometryType.Line)
            for lyr in self._layers():
                lyr.removeSelection()
            self.report(res)
            self.after_apply()

    def after_apply(self):
        pass

    def closeEvent(self, e):
        self.band.reset(Qgis.GeometryType.Line)
        self.canvas.scene().removeItem(self.band)
        for lyr in self._watched:
            try:
                lyr.selectionChanged.disconnect(self._sel_changed)
            except (TypeError, RuntimeError):
                pass
        try:
            self.manager.activeRecordChanged.disconnect(self._rec_changed)
        except (TypeError, RuntimeError):
            pass
        self.release_tools()
        super().closeEvent(e)

    def release_tools(self):
        pass


def _remainder_choice(lay):
    a = QRadioButton("Every piece becomes a new parcel (the parent is retired)")
    b = QRadioButton("The largest piece stays the parent parcel (keeps its number and record)")
    g = QButtonGroup(lay.parentWidget())
    g.addButton(a)
    g.addButton(b)
    a.setChecked(True)
    lay.addWidget(QLabel("<b>Remainder</b>"))
    lay.addWidget(a)
    lay.addWidget(b)
    return b


# --------------------------------------------------------------------------
class SplitWindow(_EditWindow):
    TITLE = "Split Parcel"
    HELP = "Select ONE parcel, click Draw Cut Line and draw across it (right-click to finish)."

    def __init__(self, iface, manager, parent=None):
        super().__init__(iface, manager, parent)
        self.cut = None
        self.tool = DrawLineTool(self.canvas, lambda: self.manager.active)
        self.tool.finished.connect(self._drawn)
        row = QHBoxLayout()
        draw = QPushButton("Draw Cut Line")
        draw.clicked.connect(lambda: self.canvas.setMapTool(self.tool))
        self.cut_lbl = QLabel("No cut line yet.")
        row.addWidget(draw)
        row.addWidget(self.cut_lbl, 1)
        self.lay.addLayout(row)
        tip = QLabel("The line snaps to fabric points. Where it crosses a boundary line, that line "
                     "is split in two (the old one is retired). Points along it become corners.\n\n"
                     "To split by a deed's calls, enter or paste them in Traverse, select the parent "
                     "parcel, and press Split Selected Parcel there.")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: palette(mid);")
        self.lay.addWidget(tip)
        self.keep = _remainder_choice(self.lay)
        self._finish_ui()

    def _drawn(self, pts):
        self.cut = QgsGeometry.fromPolylineXY(pts)
        self.show_line([self.cut])
        length = self.cut.length()
        self.cut_lbl.setText("Cut line: %d point(s), %.2f ft." % (len(pts), length))
        self.iface.actionPan().trigger()

    def apply(self, fab, record):
        sel = self.selected()
        if len(sel) != 1:
            raise E.EditError("Select exactly one parcel to split.")
        if self.cut is None:
            raise E.EditError("Draw the cut line first.")
        res = E.cut_parcel(fab, sel[0], [self.cut], record, "Split", self.keep.isChecked())
        return res

    def after_apply(self):
        self.cut = None
        self.cut_lbl.setText("No cut line yet.")

    def release_tools(self):
        if self.canvas.mapTool() is self.tool:
            self.canvas.unsetMapTool(self.tool)
        self.tool.cleanup()


# --------------------------------------------------------------------------
class DivideWindow(_EditWindow):
    TITLE = "Divide Parcel"
    HELP = "Select ONE parcel, set how to divide it, Preview, then Apply."

    def __init__(self, iface, manager, parent=None):
        super().__init__(iface, manager, parent)
        self.cuts = []
        form = QFormLayout()
        self.mode = QComboBox()
        self.mode.addItem("Into equal areas", "equal_area")
        self.mode.addItem("Into equal widths", "equal_width")
        self.mode.addItem("Into pieces of a set area (rest is the remainder)", "area")
        self.count = QSpinBox()
        self.count.setRange(2, 500)
        self.count.setValue(2)
        arow = QHBoxLayout()
        self.area = QLineEdit()
        self.area.setPlaceholderText("e.g. 1.0")
        self.area_unit = QComboBox()
        self.area_unit.addItem("acres", 43560.0)
        self.area_unit.addItem("sq ft", 1.0)
        arow.addWidget(self.area, 1)
        arow.addWidget(self.area_unit)
        brow = QHBoxLayout()
        self.bearing = QLineEdit("N 0-00-00 E")
        self.bearing.setToolTip("Cut lines run in this direction, e.g. parallel to a side line")
        pick = QPushButton("Pick a Line")
        pick.setToolTip("Click a fabric line to cut parallel to it")
        self.pick_tool = PickLineTool(self.canvas, lambda: self.manager.active)
        self.pick_tool.picked.connect(self._picked_line)
        pick.clicked.connect(lambda: self.canvas.setMapTool(self.pick_tool))
        brow.addWidget(self.bearing, 1)
        brow.addWidget(pick)
        self.flip = QCheckBox("Start measuring from the other side")
        form.addRow("Divide", self.mode)
        form.addRow("Number of parts", self.count)
        form.addRow("Area of each piece", arow)
        form.addRow("Cut lines run", brow)
        form.addRow("", self.flip)
        self.lay.addLayout(form)
        prev = QPushButton("Preview")
        prev.clicked.connect(self._preview)
        self.prev_lbl = QLabel()
        self.prev_lbl.setWordWrap(True)
        self.lay.addWidget(prev)
        self.lay.addWidget(self.prev_lbl)
        self.keep = _remainder_choice(self.lay)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        for w in (self.count, ):
            w.valueChanged.connect(self._clear_preview)
        self.flip.toggled.connect(self._clear_preview)
        self.bearing.textChanged.connect(self._clear_preview)
        self.area.textChanged.connect(self._clear_preview)
        self._mode_changed()
        self._finish_ui()

    def _mode_changed(self, *_):
        by_area = self.mode.currentData() == "area"
        self.count.setEnabled(not by_area)
        self.area.setEnabled(by_area)
        self.area_unit.setEnabled(by_area)
        self._clear_preview()

    def on_selection(self):
        self._clear_preview()

    def _clear_preview(self, *_):
        self.cuts = []
        if hasattr(self, "prev_lbl"):
            self.prev_lbl.setText("")
        self.band.reset(Qgis.GeometryType.Line)

    def _picked_line(self, f):
        try:
            d = float(f["Direction"])
            az = math.radians(d)
        except (TypeError, ValueError):
            g = f.geometry().constGet()
            s, e = g.startPoint(), g.endPoint()
            az, _ = U.inverse(s.x(), s.y(), e.x(), e.y())
        self.bearing.setText(U.azimuth_to_bearing(az))
        self.iface.actionPan().trigger()

    def _compute(self):
        fab = self.manager.active
        sel = self.selected()
        if len(sel) != 1:
            raise E.EditError("Select exactly one parcel to divide.")
        try:
            az = U.parse_bearing(self.bearing.text(), fab.packed_dms)
        except U.ParseError as e:
            raise E.EditError(str(e))
        area = None
        if self.mode.currentData() == "area":
            try:
                area = float(self.area.text().replace(",", "")) * self.area_unit.currentData()
            except ValueError:
                raise E.EditError("Enter the area of each piece.")
        cuts = E.divide_cuts(sel[0].geometry(), az, self.mode.currentData(), self.count.value(),
                             area, self.flip.isChecked())
        return sel[0], cuts

    def _preview(self):
        try:
            parent, cuts = self._compute()
            inside = E.clip_cuts(self.manager.active, parent.geometry(), cuts)
            pieces = E._pieces(parent.geometry(), inside)
        except E.EditError as e:
            QMessageBox.warning(self, self.TITLE, str(e))
            return
        self.cuts = cuts
        self.show_line([QgsGeometry.fromPolylineXY([QgsPointXY(*p) for p in c]) for c in inside])
        areas = sorted((g.area() for g in pieces), reverse=True)
        self.prev_lbl.setText("%d pieces: %s" % (len(areas), ", ".join(
            "%.3f ac" % (a / 43560.0) for a in areas)))

    def apply(self, fab, record):
        parent, cuts = self._compute()
        hint = None
        if self.mode.currentData() == "area" and self.keep.isChecked():
            # the remainder is the piece that isn't the set area
            target = float(self.area.text().replace(",", "")) * self.area_unit.currentData()
            pieces = E._pieces(parent.geometry(), E.clip_cuts(fab, parent.geometry(), cuts))
            hint = max(pieces, key=lambda g: abs(g.area() - target)).pointOnSurface()
        return E.cut_parcel(fab, parent, cuts, record, "Divide", self.keep.isChecked(),
                            remainder_point=hint)

    def release_tools(self):
        if self.canvas.mapTool() is self.pick_tool:
            self.canvas.unsetMapTool(self.pick_tool)


# --------------------------------------------------------------------------
class MergeWindow(_EditWindow):
    TITLE = "Merge Parcels"
    HELP = "Select two or more parcels of the same type."

    def __init__(self, iface, manager, parent=None):
        super().__init__(iface, manager, parent)
        form = QFormLayout()
        self.number = QComboBox()
        form.addRow("Merged parcel number", self.number)
        self.apart = QCheckBox("Join parcels that don't touch (one multi-polygon parcel)")
        fab = manager.active
        if fab is not None and not fab.multipart:
            self.apart.setEnabled(False)
            self.apart.setToolTip("Turn on multi-polygon parcels in Fabric Settings first")
        form.addRow("", self.apart)
        self.lay.addLayout(form)
        tip = QLabel("The parents and the lines between them are retired by the active record; "
                     "the outside lines stay.")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: palette(mid);")
        self.lay.addWidget(tip)
        self._finish_ui()

    def on_selection(self):
        if not hasattr(self, "number"):
            return
        self.number.clear()
        self.number.addItem("New number (next unused)", None)
        for f in self.selected():
            if f["ParcelNumber"]:
                self.number.addItem("Keep %s" % f["ParcelNumber"], str(f["ParcelNumber"]))

    def apply(self, fab, record):
        return E.merge_parcels(fab, self.selected(), record, self.number.currentData(), self.apart.isChecked())


# --------------------------------------------------------------------------
class SeparateWindow(_EditWindow):
    TITLE = "Separate Parcel"
    HELP = "Select ONE multi-polygon parcel. Each of its polygons becomes its own parcel."

    def __init__(self, iface, manager, parent=None):
        super().__init__(iface, manager, parent)
        self.keep = _remainder_choice(self.lay)
        self.keep.setChecked(True)
        self._finish_ui()

    def apply(self, fab, record):
        sel = self.selected()
        if len(sel) != 1:
            raise E.EditError("Select exactly one parcel to separate.")
        return E.separate_parcel(fab, sel[0], record, self.keep.isChecked())


# --------------------------------------------------------------------------
class CopyParallelWindow(_EditWindow):
    TITLE = "Copy Parallel"
    LAYER = "lines"
    HELP = "Select fabric lines (Parcel Lines or Connections) to copy at an offset."

    def __init__(self, iface, manager, parent=None):
        super().__init__(iface, manager, parent)
        form = QFormLayout()
        self.dist = QLineEdit("10")
        self.dist.setToolTip("Feet, or add m / ch / rd")
        self.side = QComboBox()
        self.side.addItem("Right of each line's direction", "right")
        self.side.addItem("Left of each line's direction", "left")
        self.target = QComboBox()
        fab = manager.active
        if fab is not None:
            self.target.addItems(fab.parcel_types + ["Connections"])
            self.target.setCurrentText("Connections")
        form.addRow("Offset", self.dist)
        form.addRow("Side", self.side)
        form.addRow("Copies go to", self.target)
        self.lay.addLayout(form)
        prev = QPushButton("Preview")
        prev.clicked.connect(self._preview)
        self.lay.addWidget(prev)
        self._finish_ui()

    def _distance(self, fab):
        try:
            return U.parse_distance(self.dist.text(), fab.foot_m)
        except U.ParseError as e:
            raise E.EditError(str(e))

    def _preview(self):
        fab = self.manager.active
        try:
            d = self._distance(fab)
            sign = 1 if self.side.currentData() == "right" else -1
            geoms = []
            from ..core import geomops as G
            from ..core.build import _single_curve
            for f in self.selected():
                c = _single_curve(f.geometry())
                kind, data, _ = E._parts(c)
                if kind == "arc" and G.arc_center(*data) is not None:
                    s, m, e = G.offset_arc(*data, sign * d)
                    geoms.append(QgsGeometry(E._arc_geom(s, m, e).constGet().segmentize()))
                elif len(data) >= 2:
                    a, b = G.offset_segment(data[0], data[-1], sign * d)
                    geoms.append(QgsGeometry.fromPolylineXY([QgsPointXY(*a), QgsPointXY(*b)]))
        except (E.EditError, ValueError) as e:
            QMessageBox.warning(self, self.TITLE, str(e))
            return
        self.show_line(geoms)

    def apply(self, fab, record):
        return E.copy_parallel(fab, self.selected(), self._distance(fab), self.side.currentData(),
                               self.target.currentText(), record)


# --------------------------------------------------------------------------
class SubdivideWindow(_EditWindow):
    TITLE = "Subdivide Block"
    HELP = "Select ONE block parcel, pick its street frontage line, set the lots, Preview, then Apply."

    def __init__(self, iface, manager, parent=None):
        super().__init__(iface, manager, parent)
        self.cuts, self.pieces = [], []
        form = QFormLayout()
        frow = QHBoxLayout()
        self.frontage = QLineEdit("N 90-00-00 E")
        self.frontage.setToolTip("Bearing of the street frontage; lot lines run square to it")
        pick = QPushButton("Pick Frontage Line")
        self.pick_tool = PickLineTool(self.canvas, lambda: self.manager.active)
        self.pick_tool.picked.connect(self._picked)
        pick.clicked.connect(lambda: self.canvas.setMapTool(self.pick_tool))
        frow.addWidget(self.frontage, 1)
        frow.addWidget(pick)
        form.addRow("Frontage runs", frow)
        lrow = QHBoxLayout()
        self.by_width = QRadioButton("Lots")
        self.by_count = QRadioButton("or")
        g = QButtonGroup(self)
        g.addButton(self.by_width)
        g.addButton(self.by_count)
        self.by_width.setChecked(True)
        self.width = QLineEdit("100")
        self.width.setToolTip("Frontage of each lot (feet); the rest is the remainder lot")
        self.count = QSpinBox()
        self.count.setRange(2, 500)
        self.count.setValue(4)
        lrow.addWidget(self.by_width)
        lrow.addWidget(self.width)
        lrow.addWidget(QLabel("ft wide"))
        lrow.addWidget(self.by_count)
        lrow.addWidget(self.count)
        lrow.addWidget(QLabel("lots of equal frontage"))
        lrow.addStretch(1)
        form.addRow("Lots", lrow)
        self.double = QCheckBox("Lots on both sides (split the block front-to-back first)")
        self.flip = QCheckBox("Start from the other end")
        form.addRow("", self.double)
        form.addRow("", self.flip)
        zrow = QHBoxLayout()
        self.min_area = QLineEdit("")
        self.min_area.setPlaceholderText("e.g. 0.5")
        self.min_unit = QComboBox()
        self.min_unit.addItem("acres", 43560.0)
        self.min_unit.addItem("sq ft", 1.0)
        self.min_front = QLineEdit("")
        self.min_front.setPlaceholderText("e.g. 75")
        zrow.addWidget(QLabel("Min area"))
        zrow.addWidget(self.min_area)
        zrow.addWidget(self.min_unit)
        zrow.addWidget(QLabel("Min frontage (ft)"))
        zrow.addWidget(self.min_front)
        form.addRow("Zoning", zrow)
        self.lay.addLayout(form)
        prev = QPushButton("Preview")
        prev.clicked.connect(self._preview)
        self.lay.addWidget(prev)
        self.prev_lbl = QLabel()
        self.prev_lbl.setWordWrap(True)
        self.prev_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.lay.addWidget(self.prev_lbl)
        self._finish_ui()

    def _picked(self, f):
        try:
            az = math.radians(float(f["Direction"]))
        except (TypeError, ValueError):
            g = f.geometry().constGet()
            s, e = g.startPoint(), g.endPoint()
            az, _ = U.inverse(s.x(), s.y(), e.x(), e.y())
        self.frontage.setText(U.azimuth_to_bearing(az))
        self.iface.actionPan().trigger()

    def _num(self, w):
        t = w.text().replace(",", "").strip()
        return float(t) if t else None

    def _compute(self):
        fab = self.manager.active
        sel = self.selected()
        if len(sel) != 1:
            raise E.EditError("Select exactly one block parcel.")
        try:
            front = U.parse_bearing(self.frontage.text(), fab.packed_dms)
        except U.ParseError as e:
            raise E.EditError(str(e))
        geom = sel[0].geometry()
        lot_az = (front + math.pi / 2) % (2 * math.pi)      # lot lines run square to the street
        cuts = []
        halves = [geom]
        if self.double.isChecked():
            mid = E.divide_cuts(geom, front, "equal_area", 2)
            cuts += mid
            inside = E.clip_cuts(fab, geom, mid)
            halves = E._pieces(geom, inside)
        for h in halves:
            try:
                if self.by_width.isChecked():
                    w = self._num(self.width)
                    cuts += E.divide_cuts(h, lot_az, "width", area=w, flip=self.flip.isChecked())
                else:
                    cuts += E.divide_cuts(h, lot_az, "equal_width", self.count.value(), flip=self.flip.isChecked())
            except ValueError as e:
                raise E.EditError(str(e))
        return sel[0], cuts, front

    def _preview(self):
        fab = self.manager.active
        try:
            parent, cuts, front = self._compute()
            inside = E.clip_cuts(fab, parent.geometry(), cuts)
            pieces = E._pieces(parent.geometry(), inside)
        except (E.EditError, ValueError) as e:
            QMessageBox.warning(self, self.TITLE, str(e))
            return
        self.cuts = cuts
        self.show_line([QgsGeometry.fromPolylineXY([QgsPointXY(*p) for p in c]) for c in inside])
        min_a = (self._num(self.min_area) or 0) * self.min_unit.currentData()
        min_f = self._num(self.min_front) or 0
        rows, bad = [], 0
        for i, g in enumerate(sorted(pieces, key=lambda g: (-round(g.centroid().asPoint().y(), 1),
                                                            round(g.centroid().asPoint().x(), 1))), 1):
            r = QgsGeometry(g)
            r.rotate(-math.degrees(front) + 90, g.centroid().asPoint())   # frontage now runs east-west
            width = r.boundingBox().width()
            fails = []
            if min_a and g.area() < min_a:
                fails.append("area")
            if min_f and width < min_f - 0.005:
                fails.append("frontage")
            bad += bool(fails)
            rows.append("<tr><td>%d</td><td>%.3f</td><td>%.1f</td><td style='color:%s'>%s</td></tr>" % (
                i, g.area() / 43560.0, width, "#b3261e" if fails else "#1b5e20",
                ("too small: " + ", ".join(fails)) if fails else "ok"))
        self.prev_lbl.setText("<b>%d lots</b>%s<table><tr><th>Lot</th><th>Acres</th><th>Frontage ft</th>"
                              "<th>Zoning</th></tr>%s</table>" % (
                                  len(pieces), " - <span style='color:#b3261e'>%d fail zoning</span>" % bad
                                  if bad else "", "".join(rows)))

    def apply(self, fab, record):
        parent, cuts, _front = self._compute()
        return E.cut_parcel(fab, parent, cuts, record, "Subdivide")

    def release_tools(self):
        if self.canvas.mapTool() is self.pick_tool:
            self.canvas.unsetMapTool(self.pick_tool)
