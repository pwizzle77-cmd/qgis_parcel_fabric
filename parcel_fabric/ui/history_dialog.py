# -*- coding: utf-8 -*-
"""Parcel History: a parcel's family on the map, step by step through time.

Pick any parcel (current or retired).  The window finds its original
parcel(s) and everything split, divided or merged from them, colours them by
generation on the time-enabled Parcel History layer, and lists each step
(record, date, what became what).  Click a step - or press Play - to see
the map as it stood on that date.  History Handout writes a printable page
with a map for every step.
"""

import datetime
import html
import os

from qgis.core import (
    QgsCoordinateTransform,
    QgsFeatureRequest,
    QgsMapRendererSequentialJob,
    QgsMapSettings,
    QgsProject,
)
from qgis.PyQt.QtCore import QDate, QSize, Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QColor, QDesktopServices
from qgis.PyQt.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QSlider, QVBoxLayout,
)

from ..core import family as F
from ..core import history as H
from ..core import lineage as L
from ..core.fabric import PROP_FABRIC, quote
from ..core.parcel_numbers import unfiltered

STEP = Qt.ItemDataRole.UserRole


class HistoryDialog(QDialog):
    def __init__(self, iface, manager, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.manager = manager
        self.fabric = manager.active
        self.canvas = iface.mapCanvas()
        self.setWindowTitle("Parcel History")
        self.resize(560, 600)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.members, self.gen, self.steps, self.focus = set(), {}, [], None
        self._hidden_nodes = []

        lay = QVBoxLayout(self)
        row = QHBoxLayout()
        self.find = QLineEdit()
        self.find.setPlaceholderText("Parcel number (current or retired)")
        self.find.returnPressed.connect(self._find)
        go = QPushButton("Show")
        go.clicked.connect(self._find)
        sel = QPushButton("Use Selected Parcel")
        sel.clicked.connect(lambda: self._from_selection(False))
        row.addWidget(self.find, 1)
        row.addWidget(go)
        row.addWidget(sel)
        lay.addLayout(row)
        self.whole = QCheckBox("Whole family from the original parcel(s) "
                               "(untick: this parcel and what came from it)")
        self.whole.setChecked(True)
        self.whole.toggled.connect(lambda *_: self.focus and self.show_family(self.focus))
        lay.addWidget(self.whole)
        self.head = QLabel("Pick a parcel to see its history.")
        self.head.setWordWrap(True)
        lay.addWidget(self.head)

        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.currentRowChanged.connect(self._step_selected)
        lay.addWidget(self.list, 1)

        play = QHBoxLayout()
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.valueChanged.connect(lambda v: self.list.setCurrentRow(v))
        self.play_btn = QPushButton("Play")
        self.play_btn.clicked.connect(self._toggle_play)
        today = QPushButton("Today")
        today.setToolTip("Show the parcels as they are now")
        today.clicked.connect(lambda: self._show_date(QDate.currentDate()))
        play.addWidget(self.slider, 1)
        play.addWidget(self.play_btn)
        play.addWidget(today)
        lay.addLayout(play)

        btns = QHBoxLayout()
        for text, slot, tip in (
                ("Zoom to Family", self._zoom, "Zoom to every parcel in the family"),
                ("History Handout...", self._handout, "Printable page with a map for every step"),
                ("Show All Parcels", self._clear_family, "Stop filtering the history layer to one family"),
                ("Update Dates", self._update_dates, "Recompute every parcel's dates from its records")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            btns.addWidget(b)
        lay.addLayout(btns)
        note = QLabel("Dates come from each record's recorded date. Records without one sort last "
                      "and can't be shown on the map (Check Fabric lists them). For a free time slider "
                      "use QGIS's View > Panels > Temporal Controller.")
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid);")
        lay.addWidget(note)

        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self._next)

        try:
            H.refresh_dates(self.fabric)
        except Exception as e:      # still usable; dates may just be stale
            self.head.setText("Couldn't update history dates: %s" % e)
        self.hist = H.history_layer(self.fabric)
        H.set_project_time_range(self.fabric)
        self._from_selection(True)

    # ------------------------------------------------------------------
    # Choosing the parcel
    # ------------------------------------------------------------------
    def _from_selection(self, quiet):
        sel = list(self.fabric.parcels.getSelectedFeatures()) or list(self.hist.getSelectedFeatures())
        if len(sel) != 1:
            if not quiet:
                QMessageBox.information(self, "Parcel History", "Select one parcel first.")
            return
        self.show_family(sel[0]["GlobalID"])

    def _find(self):
        num = self.find.text().strip()
        if not num:
            return
        req = QgsFeatureRequest().setFilterExpression('"ParcelNumber" = %s' % quote(num))
        for f in unfiltered(self.fabric.parcels).getFeatures(req):
            self.show_family(f["GlobalID"])
            return
        QMessageBox.information(self, "Parcel History", "No parcel has number %s." % num)

    # ------------------------------------------------------------------
    def _all_edges(self, gids, up):
        edges = []
        for g in gids:
            edges += L.walk(self.fabric, g, up=up, limit=2000)
        return list(dict.fromkeys(edges))

    def show_family(self, gid):
        self._stop()
        L.clear_cache()
        self.focus = gid
        up = self._all_edges([gid], True)
        roots = F.roots(up, gid) if self.whole.isChecked() else {gid}
        down = self._all_edges(roots, False)
        edges = list(dict.fromkeys(up + down))
        self.members = F.descendants(down, roots) | (F.roots(up, gid) if self.whole.isChecked() else set())
        self.members |= {gid}
        self.gen = F.generations(edges, self.members)
        dates = {k: v.toString("yyyy-MM-dd") for k, v in H.record_dates(self.fabric).items()}
        names = H.record_names(self.fabric)
        steps = []
        # step 0: the original parcel(s) when they were created
        starts = [self._start(r) for r in roots]
        starts = [s for s in starts if s]
        steps.append({"date": min(starts) if starts else "", "operation": "Original",
                      "record": None, "record_name": "", "parents": [], "children": sorted(roots)})
        steps += F.events(edges, self.members, dates, names)
        self.steps = steps

        # map: only this family, coloured by generation; hide the plain Parcels layer
        self.hist.setSubsetString('"GlobalID" IN (%s)' % ", ".join(quote(m) for m in self.members))
        H.style_history(self.hist, self.gen)
        self._hide_parcels(True)

        i = L.parcel_info(self.fabric, gid)
        n_gen = (max(self.gen.values()) + 1) if self.gen else 1
        self.head.setText("<b>%s</b> %s &nbsp;<span style='color:gray'>%d parcels, %d generation(s), %d step(s)"
                          "</span>" % (i["number"] or "(no number)", html.escape(i["name"]),
                                       len(self.members), n_gen, len(steps)))
        self.list.blockSignals(True)
        self.list.clear()
        for s in steps:
            it = QListWidgetItem(self._describe(s))
            if not s["date"]:
                it.setForeground(QColor("#8a5a00"))
            self.list.addItem(it)
        self.list.blockSignals(False)
        self.slider.setRange(0, max(0, len(steps) - 1))
        self._zoom()
        self.list.setCurrentRow(0)

    def _start(self, gid):
        req = QgsFeatureRequest().setFilterExpression('"GlobalID" = %s' % quote(gid))
        for f in unfiltered(self.fabric.parcels).getFeatures(req):
            d = H._qdate(f["QPF_StartDate"])
            return d.toString("yyyy-MM-dd") if d else ""
        return ""

    def _label(self, gid):
        i = L.parcel_info(self.fabric, gid)
        return i["number"] or i["name"] or gid[:9]

    def _describe(self, s):
        date = s["date"] or "no date"
        if s["operation"] == "Original":
            return "%s - Original: %s" % (date, ", ".join(self._label(g) for g in s["children"]))
        return "%s - %s by %s: %s  →  %s" % (
            date, s["operation"], s["record_name"] or "?",
            ", ".join(self._label(g) for g in s["parents"]),
            ", ".join(self._label(g) for g in s["children"]))

    # ------------------------------------------------------------------
    # Showing a step
    # ------------------------------------------------------------------
    def _step_selected(self, row):
        if row < 0 or row >= len(self.steps):
            return
        self.slider.blockSignals(True)
        self.slider.setValue(row)
        self.slider.blockSignals(False)
        s = self.steps[row]
        if not s["date"]:
            self.iface.messageBar().pushWarning("Parcel History", "That step's record has no recorded date, "
                                                "so it can't be shown on the map.")
            return
        self._show_date(QDate.fromString(s["date"], "yyyy-MM-dd"))
        ids = [f.id() for f in self.hist.getFeatures(QgsFeatureRequest().setFilterExpression(
            '"GlobalID" IN (%s)' % ", ".join(quote(g) for g in s["children"])))]
        self.hist.selectByIds(ids)

    def _show_date(self, qdate):
        H.show_date(self.iface, qdate)
        self.setWindowTitle("Parcel History - %s" % qdate.toString("yyyy-MM-dd"))

    def _toggle_play(self):
        if self.timer.isActive():
            self._stop()
            return
        if not self.steps:
            return
        if self.list.currentRow() >= len(self.steps) - 1:
            self.list.setCurrentRow(0)
        self.play_btn.setText("Stop")
        self.timer.start()

    def _next(self):
        r = self.list.currentRow() + 1
        while r < len(self.steps) and not self.steps[r]["date"]:
            r += 1
        if r >= len(self.steps):
            self._stop()
            return
        self.list.setCurrentRow(r)

    def _stop(self):
        self.timer.stop()
        self.play_btn.setText("Play")

    # ------------------------------------------------------------------
    def _family_extent(self, crs=None):
        box = None
        req = QgsFeatureRequest().setFilterExpression(
            '"GlobalID" IN (%s)' % ", ".join(quote(m) for m in self.members))
        for f in unfiltered(self.fabric.parcels).getFeatures(req):
            b = f.geometry().boundingBox()
            if box is None:
                box = b
            else:
                box.combineExtentWith(b)
        if box is None:
            return None
        box = box.buffered(max(box.width(), box.height()) * 0.15 + 5)
        crs = crs or self.canvas.mapSettings().destinationCrs()
        xf = QgsCoordinateTransform(self.fabric.parcels.crs(), crs, QgsProject.instance())
        return xf.transformBoundingBox(box)

    def _zoom(self):
        ext = self._family_extent()
        if ext is not None:
            self.canvas.setExtent(ext)
            self.canvas.refresh()

    def _hide_parcels(self, hide):
        root = QgsProject.instance().layerTreeRoot()
        if hide:
            node = root.findLayer(self.fabric.parcels.id())
            if node is not None and node.isVisible():
                node.setItemVisibilityChecked(False)
                self._hidden_nodes.append(node)
            hnode = root.findLayer(self.hist.id())
            if hnode is not None:
                hnode.setItemVisibilityChecked(True)
        else:
            for node in self._hidden_nodes:
                try:
                    node.setItemVisibilityChecked(True)
                except RuntimeError:
                    pass
            self._hidden_nodes = []

    def _clear_family(self):
        self._stop()
        self.hist.setSubsetString("")
        H.style_history(self.hist, None)
        self.members, self.steps, self.focus = set(), [], None
        self.list.clear()
        self.head.setText("Showing every parcel. Pick a parcel to see one family.")

    def _update_dates(self):
        try:
            n = H.refresh_dates(self.fabric)
        except Exception as e:
            QMessageBox.critical(self, "Parcel History", str(e))
            return
        H.set_project_time_range(self.fabric)
        self.iface.messageBar().pushSuccess("Parcel History", "%d parcel(s) had their dates updated." % n)
        if self.focus:
            self.show_family(self.focus)

    def closeEvent(self, e):
        self._stop()
        self._hide_parcels(False)
        super().closeEvent(e)

    # ------------------------------------------------------------------
    # Handout
    # ------------------------------------------------------------------
    def _render(self, qdate, path):
        ms = QgsMapSettings(self.canvas.mapSettings())
        ext = self._family_extent(ms.destinationCrs())
        layers = [self.hist]
        for lyr in self.canvas.layers():           # keep basemaps, drop fabric layers
            if lyr.id() != self.hist.id() and not lyr.customProperty(PROP_FABRIC):
                layers.append(lyr)
        ms.setLayers(layers)
        ms.setOutputSize(QSize(720, 480))
        if ext is not None:
            ms.setExtent(ext)
        ms.setIsTemporal(True)
        ms.setTemporalRange(H.day_range(qdate))
        job = QgsMapRendererSequentialJob(ms)
        job.start()
        job.waitForFinished()
        job.renderedImage().save(path, "PNG")

    def _handout(self):
        if not self.steps:
            QMessageBox.information(self, "Parcel History", "Show a parcel's family first.")
            return
        name = (self._label(self.focus) or "history").replace("/", "-")
        path, _ = QFileDialog.getSaveFileName(self, "History handout", name + " history.html",
                                              "Web page (*.html)")
        if not path:
            return
        folder = os.path.splitext(path)[0] + "_maps"
        os.makedirs(folder, exist_ok=True)
        self.hist.removeSelection()
        rows = []
        for n, s in enumerate(self.steps):
            img = ""
            if s["date"]:
                fn = "step_%02d.png" % n
                self._render(QDate.fromString(s["date"], "yyyy-MM-dd"), os.path.join(folder, fn))
                img = '<img src="%s/%s" alt="map on %s">' % (html.escape(os.path.basename(folder)), fn, s["date"])
            rows.append(self._handout_row(s, img))
        i = L.parcel_info(self.fabric, self.focus)
        page = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>History of %s</title>
<style>body{font-family:Arial,sans-serif;margin:28px;color:#222;max-width:900px}
h1{font-size:20px;margin-bottom:4px} .m{color:#666;font-size:12px}
.step{border-top:1px solid #ccc;padding:14px 0;page-break-inside:avoid}
.step h2{font-size:15px;margin:0 0 6px} table{border-collapse:collapse;font-size:13px;margin:6px 0}
td,th{border:1px solid #ddd;padding:3px 8px;text-align:left} img{max-width:100%%;border:1px solid #ccc}
</style></head><body><h1>Parcel history: %s</h1>
<p class=m>%s &middot; %d parcels in the family &middot; prepared %s</p>%s</body></html>""" % (
            html.escape(i["number"] or ""), html.escape(i["number"] or i["name"] or ""),
            html.escape(i["name"] or ""), len(self.members), datetime.date.today().isoformat(),
            "".join(rows))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(page)
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        if self.list.currentRow() >= 0:
            self._step_selected(self.list.currentRow())

    def _handout_row(self, s, img):
        rec = self.fabric.record_by_id(s["record"]) if s["record"] else None
        rec_html = ""
        if rec is not None:
            bits = [html.escape(rec["Name"] or "")]
            if rec["Book"] or rec["Page"]:
                bits.append("Book %s, Page %s" % (html.escape(str(rec["Book"] or "")),
                                                 html.escape(str(rec["Page"] or ""))))
            if rec["QPF_DocumentURL"]:
                url = str(rec["QPF_DocumentURL"])
                href = url if "://" in url else QUrl.fromLocalFile(url).toString()
                bits.append('<a href="%s">document</a>' % html.escape(href))
            rec_html = "<p class=m>%s</p>" % " &middot; ".join(bits)
        table = "<table><tr><th>Parcel</th><th>Name</th><th>Acres</th><th>Status</th></tr>%s</table>" % "".join(
            self._parcel_row(g) for g in s["children"])
        before = ""
        if s["parents"]:
            before = "<p>From: %s</p>" % ", ".join(html.escape(self._label(g)) for g in s["parents"])
        title = "%s &mdash; %s" % (html.escape(s["date"] or "no recorded date"), html.escape(s["operation"]))
        return '<div class="step"><h2>%s</h2>%s%s%s%s</div>' % (title, rec_html, before, table, img)

    def _parcel_row(self, gid):
        req = QgsFeatureRequest().setFilterExpression('"GlobalID" = %s' % quote(gid))
        for f in unfiltered(self.fabric.parcels).getFeatures(req):
            ac = f.geometry().area() / 43560.0 if f.hasGeometry() else 0
            status = "retired %s" % (H._qdate(f["QPF_EndDate"]).toString("yyyy-MM-dd")
                                     if H._qdate(f["QPF_EndDate"]) else "") if f["RetiredByRecord"] else "current"
            return "<tr><td>%s</td><td>%s</td><td>%.3f</td><td>%s</td></tr>" % (
                html.escape(str(f["ParcelNumber"] or "")), html.escape(str(f["Name"] or "")), ac, status)
        return ""
