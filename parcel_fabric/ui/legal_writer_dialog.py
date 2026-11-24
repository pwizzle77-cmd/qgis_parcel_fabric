# -*- coding: utf-8 -*-
"""Write a legal description for any selected polygon (any layer)."""

from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsProject,
    QgsSettings,
)
from qgis.gui import QgsMapToolEmitPoint, QgsProjectionSelectionWidget, QgsVertexMarker
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QGuiApplication
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout,
)

from ..core import cogo as C
from ..core import legal_writer as W
from ..core import units as U
from ..core.geomio import fit_gap, parts_of, polygon_rings, to_crs, unit_word
from ..core.legal_import import parse

HEADER_KEY = "ParcelFabric/legal_header"
DEFAULT_HEADER = ("A parcel of land lying in {str}, Columbia County, Florida, being more particularly "
                  "described as follows:")


class LegalWriterDialog(QDialog):
    def __init__(self, iface, manager, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.manager = manager
        self.canvas = iface.mapCanvas()
        self.setWindowTitle("Write Legal Description")
        self.resize(640, 720)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.layer = self.feature = None
        self.pob_xy = self.commence_xy = None
        self._pick_for = None
        self.marker = QgsVertexMarker(self.canvas)
        self.marker.setColor(QColor(200, 0, 120))
        self.marker.setIconSize(12)
        self.marker.setIconType(QgsVertexMarker.IconType.ICON_CIRCLE)
        self.marker.hide()
        self.tool = QgsMapToolEmitPoint(self.canvas)
        self.tool.canvasClicked.connect(self._picked)

        lay = QVBoxLayout(self)
        row = QHBoxLayout()
        self.src = QLabel()
        self.src.setWordWrap(True)
        use = QPushButton("Use Selection")
        use.setToolTip("Use the polygon selected in the active layer (any layer)")
        use.clicked.connect(self._use_selection)
        row.addWidget(self.src, 1)
        row.addWidget(use)
        lay.addLayout(row)

        form = QFormLayout()
        self.crs = QgsProjectionSelectionWidget()
        fab = manager.active
        self.crs.setCrs(fab.parcels.crs() if fab is not None else QgsProject.instance().crs())
        form.addRow("Measure in", self.crs)
        pob = QHBoxLayout()
        self.pob_mode = QComboBox()
        self.pob_mode.addItem("Most northwesterly corner", "nw")
        self.pob_mode.addItem("Corner nearest a point I click", "pick")
        pick_pob = QPushButton("Pick")
        pick_pob.clicked.connect(lambda: self._start_pick("pob"))
        pob.addWidget(self.pob_mode, 1)
        pob.addWidget(pick_pob)
        form.addRow("Point of beginning", pob)
        self.direction = QComboBox()
        self.direction.addItem("Clockwise", True)
        self.direction.addItem("Counter-clockwise", False)
        form.addRow("Direction", self.direction)
        com = QHBoxLayout()
        self.commence = QCheckBox("Commence at")
        self.commence_text = QLineEdit("the Northwest corner of Section 12, Township 4 South, Range 16 East")
        pick_c = QPushButton("Pick")
        pick_c.setToolTip("Click the commencement point (e.g. a section corner)")
        pick_c.clicked.connect(lambda: self._start_pick("commence"))
        com.addWidget(self.commence)
        com.addWidget(self.commence_text, 1)
        com.addWidget(pick_c)
        form.addRow("Tie", com)
        fmt = QHBoxLayout()
        self.sec_dec = QSpinBox()
        self.sec_dec.setRange(0, 2)
        self.dist_dec = QSpinBox()
        self.dist_dec.setRange(0, 4)
        self.dist_dec.setValue(2)
        fmt.addWidget(QLabel("Seconds decimals"))
        fmt.addWidget(self.sec_dec)
        fmt.addWidget(QLabel("Distance decimals"))
        fmt.addWidget(self.dist_dec)
        fmt.addStretch(1)
        form.addRow("Format", fmt)
        cl = QHBoxLayout()
        self.merge = QDoubleSpinBox()
        self.merge.setRange(0, 3600)
        self.merge.setValue(1)
        self.merge.setSuffix(" sec")
        self.merge.setToolTip("Join straight courses whose bearings differ by less than this")
        self.short = QDoubleSpinBox()
        self.short.setRange(0, 10)
        self.short.setDecimals(2)
        self.short.setSuffix(" ft")
        self.short.setToolTip("Fold courses shorter than this into the course before")
        cl.addWidget(QLabel("Join bearings within"))
        cl.addWidget(self.merge)
        cl.addWidget(QLabel("Drop courses under"))
        cl.addWidget(self.short)
        cl.addStretch(1)
        form.addRow("Clean up", cl)
        ar = QHBoxLayout()
        self.arcs = QCheckBox("Find curves in densified arcs, within")
        self.arcs.setChecked(True)
        self.arc_tol = QDoubleSpinBox()
        self.arc_tol.setDecimals(3)
        self.arc_tol.setRange(0.001, 5)
        self.arc_tol.setValue(0.02)
        self.arc_tol.setSuffix(" ft")
        ar.addWidget(self.arcs)
        ar.addWidget(self.arc_tol)
        ar.addStretch(1)
        form.addRow("", ar)
        self.header = QPlainTextEdit(QgsSettings().value(HEADER_KEY, DEFAULT_HEADER, type=str))
        self.header.setMaximumHeight(60)
        self.header.setToolTip("{str} is replaced by the section, township and range (if numbering is set up)")
        form.addRow("Opening", self.header)
        lay.addLayout(form)

        gen = QPushButton("Write Description")
        gen.setDefault(True)
        gen.clicked.connect(self._write)
        lay.addWidget(gen)
        self.out = QPlainTextEdit()
        lay.addWidget(self.out, 1)
        self.check = QLabel()
        self.check.setWordWrap(True)
        lay.addWidget(self.check)
        bot = QHBoxLayout()
        copy = QPushButton("Copy")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.out.toPlainText()))
        save = QPushButton("Save as Text...")
        save.clicked.connect(self._save)
        bot.addStretch(1)
        bot.addWidget(copy)
        bot.addWidget(save)
        lay.addLayout(bot)
        self._use_selection(quiet=True)

    # ------------------------------------------------------------------
    def _use_selection(self, quiet=False):
        lyr = self.iface.activeLayer()
        ok = lyr is not None and getattr(lyr, "geometryType", None) is not None and \
            lyr.geometryType() == Qgis.GeometryType.Polygon
        sel = list(lyr.getSelectedFeatures()) if ok else []
        if not ok or len(sel) != 1:
            self.layer = self.feature = None
            self.src.setText("<span style='color:#b3261e'>Select ONE polygon in the active layer, then "
                             "press Use Selection.</span>")
            if not quiet:
                QMessageBox.information(self, self.windowTitle(), "Select one polygon in the active layer.")
            return
        self.layer, self.feature = lyr, sel[0]
        label = ""
        for k in ("ParcelNumber", "PIN", "PARCELID", "PARCEL_ID", "Name"):
            if k in lyr.fields().names() and sel[0][k]:
                label = str(sel[0][k])
                break
        self.src.setText("Describing <b>%s</b> from layer <b>%s</b>." % (label or "#%d" % sel[0].id(), lyr.name()))
        self.pob_xy = None
        self.marker.hide()

    def _start_pick(self, what):
        self._pick_for = what
        if what == "pob":
            self.pob_mode.setCurrentIndex(1)
        else:
            self.commence.setChecked(True)
        self.canvas.setMapTool(self.tool)

    def _picked(self, pt, _button):
        xf = QgsCoordinateTransform(self.canvas.mapSettings().destinationCrs(), self.crs.crs(),
                                    QgsProject.instance())
        p = xf.transform(pt)
        if self._pick_for == "pob":
            self.pob_xy = (p.x(), p.y())
        else:
            self.commence_xy = (p.x(), p.y())
        self.marker.setCenter(pt)
        self.marker.show()
        self.iface.actionPan().trigger()

    # ------------------------------------------------------------------
    def _str_text(self, geom_layer_crs):
        fab = self.manager.active
        if fab is None:
            return "Section ___, Township ___, Range ___ East"
        try:
            from ..core.parcel_numbers import PlssLookup
            plss = PlssLookup(fab)
            p = to_crs(geom_layer_crs, self.layer.crs(), fab.parcels.crs()).pointOnSurface().asPoint()
            hit = plss.at(p)
        except Exception:
            hit = None
        if not hit:
            return "Section ___, Township ___, Range ___ East"
        sec, twp, tdir, rng = hit
        return "Section %d, Township %d %s, Range %d East" % (sec, twp, "South" if tdir == "S" else "North", rng)

    def _prepare(self, segs, clockwise, start_xy=None):
        if self.arcs.isChecked():
            segs = W.find_arcs(segs, tol=self.arc_tol.value())
        segs = W.merge_collinear(segs, self.merge.value(), self.short.value())
        segs = W.orient(segs, clockwise)
        idx = W.nearest_index(segs, start_xy) if start_xy else W.northwest_index(segs)
        return W.rotate_to(segs, idx)

    def _write(self):
        if self.feature is None:
            self._use_selection()
            if self.feature is None:
                return
        crs = self.crs.crs()
        unit = unit_word(crs)
        if unit is None:
            QMessageBox.warning(self, self.windowTitle(), "Pick a projected coordinate system in feet or "
                                "meters under 'Measure in' (e.g. your State Plane zone). Web Mercator can't "
                                "be used: it distorts distances and bearings.")
            return
        geom = to_crs(self.feature.geometry(), self.layer.crs(), crs)
        parts = polygon_rings(geom)
        if not parts:
            return
        parts.sort(key=lambda p: abs(W.signed_area(p[0])), reverse=True)
        cw = self.direction.currentData()
        start = self.pob_xy if self.pob_mode.currentData() == "pick" else None
        commence = None
        if self.commence.isChecked():
            if self.commence_xy is None:
                QMessageBox.information(self, self.windowTitle(), "Pick the commencement point on the map "
                                        "(or untick Commence at).")
                return
            commence = (self.commence_text.text().strip(), self.commence_xy)
        header = self.header.toPlainText()
        QgsSettings().setValue(HEADER_KEY, header)
        if "{str}" in header:
            header = header.replace("{str}", self._str_text(self.feature.geometry()))
        texts, checks = [], []
        part_geoms = parts_of(geom)
        for k, (ext, holes) in enumerate(parts, 1):
            prepared = self._prepare(ext, cw, start)
            text, area = W.write(prepared, unit, self.sec_dec.value(), self.dist_dec.value(),
                                 "" if len(parts) > 1 else header, commence,
                                 [self._prepare(h, cw) for h in holes])
            poly_area = abs(W.signed_area(ext)) - sum(abs(W.signed_area(h)) for h in holes)
            part = part_geoms[k - 1] if k - 1 < len(part_geoms) else geom
            good, msg = self._self_check(text, area, poly_area, unit, part, prepared)
            if len(parts) > 1:
                text, msg = "TRACT %d\n\n%s" % (k, text), "Tract %d: %s" % (k, msg)
            texts.append(text)
            checks.append("<span style='color:%s'>%s</span>" % ("#1b5e20" if good else "#b3261e", msg))
        if len(parts) > 1:
            texts.insert(0, header.strip())
        self.out.setPlainText("\n\n".join(texts))
        self.check.setText("<br>".join(checks))

    def _scale_sentence(self):
        fab = self.manager.active
        feat = self.feature
        if fab is None or feat is None or feat.fields().indexOf("CreatedByRecord") < 0:
            return ""
        rec = fab.record_by_id(feat["CreatedByRecord"])
        if rec is None or rec.fields().indexOf("QPF_ScaleFactor") < 0:
            return ""
        try:
            scale = float(rec["QPF_ScaleFactor"])
        except (TypeError, ValueError):
            return ""
        if abs(scale - 1.0) <= 1e-9:
            return ""
        return (" The record's ground-to-grid scale is %s, and these distances are the record distances."
                % scale)

    def _self_check(self, text, area, poly_area, unit, geom, ring):
        """Read the boundary calls back from the real point of beginning.

        Commencement ties are left out. Not good when a written point is more
        than about 0.1 ft from the outer boundary. Returns (good, message).
        """
        boundary = text.split("\n\nLESS AND EXCEPT")[0]
        foot = U.US_SURVEY_FOOT_M if unit == "feet" else 1.0
        try:
            # False splits a commencement tie off, so only the boundary courses are walked.
            _rows, courses, problems = parse(boundary, foot, boundary_only=False)
            pob = W._start(ring[0])
            res = C.compute(pob, courses, close_to=pob)
            gap = fit_gap(geom, res.legs) if res.legs else None
        except Exception as e:      # parser gave up - say so rather than hide it
            return False, "Self-check couldn't read the description back: %s" % e
        big = 43560.0 if unit == "feet" else 10000.0
        short = "ft" if unit == "feet" else "m"
        limit = 0.1 if unit == "feet" else 0.1 * 0.3048
        msg = ("Self-check: read back as %d courses; closes within %.3f %s%s. "
               "Describes %.3f %s; the polygon is %.3f %s.") % (
            len(courses), res.misclose, short,
            "" if res.misclose < 1e-6 else " (1:%s)" % "{:,}".format(int(res.ratio)),
            area / big, "ac" if unit == "feet" else "ha", poly_area / big, "ac" if unit == "feet" else "ha")
        good = not problems and res.misclose < 0.05
        if gap is None:
            pass
        elif gap == float("inf"):
            good = False
            msg += " Could not measure the polygon boundary."
        else:
            msg += " Farthest call is %.3f %s from the boundary." % (gap, short)
            if gap > limit:
                good = False
                msg += " Over 0.1 ft, the description does not follow the polygon."
        if problems:
            msg += " Couldn't read back: " + "; ".join(problems[:3])
        return good, msg + self._scale_sentence()

    def _save(self):
        p, _ = QFileDialog.getSaveFileName(self, "Save legal description", "legal description.txt",
                                           "Text (*.txt)")
        if p:
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(self.out.toPlainText())

    def closeEvent(self, e):
        self.marker.hide()
        self.canvas.scene().removeItem(self.marker)
        if self.canvas.mapTool() is self.tool:
            self.iface.actionPan().trigger()
        super().closeEvent(e)
