# -*- coding: utf-8 -*-
"""Fabric Settings: general, documents, parcel numbers and profile-local AI.

Settings live in the fabric's QPF_Settings table, so everyone who opens the
fabric gets the same ones. AI settings belong only to the local QGIS profile.
"""

import json
import os

from qgis.core import Qgis, QgsFeatureRequest, QgsProject
from qgis.gui import QgsFieldComboBox, QgsMapLayerComboBox
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from ..core import documents as D
from ..core import schema as S
from ..core.fabric import type_expr
from ..core.numbering import NumberError, clean_pattern, number_pattern

SAMPLE = D.record_values("Sunny Acres", "Plat", "5", "22", "2024-03-01", "{0A1B2C3D-0000}")


class SettingsDialog(QDialog):
    def __init__(self, fabric, parent=None):
        super().__init__(parent)
        self.fabric = fabric
        s = fabric.settings
        self.setWindowTitle("Fabric Settings - %s" % fabric.name)
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs)

        # ---- General ---------------------------------------------------
        g = QWidget()
        gf = QFormLayout(g)
        self.name = QLineEdit(fabric.name)
        self.types = QLineEdit(", ".join(fabric.parcel_types))
        self.types.setToolTip("Values allowed in the ParcelType field, comma separated")
        self.tol = QDoubleSpinBox()
        self.tol.setDecimals(3)
        self.tol.setRange(0.001, 10)
        self.tol.setValue(fabric.tolerance)
        self.tol.setSuffix(" ft")
        self.ratio = QDoubleSpinBox()
        self.ratio.setDecimals(0)
        self.ratio.setRange(100, 1000000)
        self.ratio.setPrefix("1:")
        self.ratio.setValue(fabric.warn_ratio)
        self.packed = QCheckBox("Read 45.3015 as 45°30'15\" (packed DD.MMSS)")
        self.packed.setChecked(fabric.packed_dms)
        foot = {"us_survey": "US survey foot", "international": "International foot"}.get(
            s.get("foot"), s.get("foot"))
        gf.addRow("Fabric name", self.name)
        gf.addRow("Parcel types", self.types)
        gf.addRow("Snap tolerance", self.tol)
        gf.addRow("Flag misclose worse than", self.ratio)
        gf.addRow("", self.packed)
        multi = QHBoxLayout()
        self.multi_lbl = QLabel("One parcel can have several polygons" if fabric.multipart
                                else "One polygon per parcel")
        multi.addWidget(self.multi_lbl, 1)
        if not fabric.multipart and fabric.parcels.providerType() == "ogr":
            self.multi_btn = QPushButton("Allow multi-polygon parcels...")
            self.multi_btn.clicked.connect(self._allow_multipart)
            multi.addWidget(self.multi_btn)
        gf.addRow("Parcels", multi)
        gf.addRow("Distance unit", QLabel("%s (set when the fabric was created)" % foot))
        tabs.addTab(g, "General")

        # ---- Documents -------------------------------------------------
        d = QWidget()
        df = QFormLayout(d)
        row = QHBoxLayout()
        self.root = QLineEdit(s.get("doc_root", ""))
        self.root.setPlaceholderText(r"e.g. \\server\GIS\Records  or  E:\Records")
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse_root)
        row.addWidget(self.root, 1)
        row.addWidget(browse)
        self.folder_pat = QLineEdit(s.get("doc_folder_pattern") or D.DEFAULT_FOLDER)
        self.name_pat = QLineEdit(s.get("doc_name_pattern") or D.DEFAULT_NAME)
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        tokens = QLabel("Tokens: {type} {book} {page} {name} {year} {date} {id}. "
                        "Use / for sub-folders. Folders are created as needed and a file "
                        "is never overwritten.")
        tokens.setWordWrap(True)
        tokens.setStyleSheet("color: palette(mid);")
        df.addRow("Document folder", row)
        df.addRow("Folder pattern", self.folder_pat)
        df.addRow("File name pattern", self.name_pat)
        df.addRow("", tokens)
        df.addRow("Example", self.preview)
        for w in (self.root, self.folder_pat, self.name_pat):
            w.textChanged.connect(self._update_preview)
        self._update_preview()
        tabs.addTab(d, "Documents")

        # ---- Parcel numbers --------------------------------------------
        n = QWidget()
        nf = QFormLayout(n)
        pattern = QLabel("How an existing parcel number splits. 12345-002 is stem 12345 and "
                         "split 002 when the separator is -, the digit count is 3, and the "
                         "parent suffix is 000. A hyphen still reads a Florida strap on its "
                         "own last piece, such as 12-4S-16-02942-001.")
        pattern.setWordWrap(True)
        nf.addRow(pattern)
        sep, digits, parent = number_pattern(s)
        self.num_sep = QLineEdit(sep)
        self.num_sep.setMaximumWidth(48)
        self.num_sep.setToolTip("Between the parcel id and the split.")
        self.num_digits = QSpinBox()
        self.num_digits.setRange(1, 6)
        self.num_digits.setValue(digits)
        self.num_digits.setToolTip("The last piece is a split when it has at most this many digits.")
        self.num_parent = QLineEdit(parent)
        self.num_parent.setMaximumWidth(72)
        self.num_parent.setToolTip("The original parcel ends with this. 000 matches -000.")
        row = QHBoxLayout()
        row.addWidget(QLabel("Separator"))
        row.addWidget(self.num_sep)
        row.addWidget(QLabel("Digits"))
        row.addWidget(self.num_digits)
        row.addWidget(QLabel("Parent"))
        row.addWidget(self.num_parent)
        row.addStretch(1)
        nf.addRow("Parcel number", row)
        intro = QLabel("New parcels built here still look like <b>12-4S-16-02942-000</b>: "
                       "section, township, range from the sections layer, then the next unused "
                       "5-digit ID in that section-township-range, then the split (000 for a new parcel).")
        intro.setWordWrap(True)
        nf.addRow(intro)
        self.use_default = QCheckBox("Use the BLM National PLSS service (online; no setup needed)")
        self.use_default.setToolTip("Sections: %s\nTownships: %s\nUncheck to use your own PLSS layer(s)."
                                    % (S.PLSS_DEFAULT["sections"]["url"], S.PLSS_DEFAULT["townships"]["url"]))
        self.use_default.setChecked(not s.get("plss_source") and s.get("plss_use_default", "1") == "1")
        nf.addRow(self.use_default)
        self.plss = QgsMapLayerComboBox()
        self.plss.setFilters(Qgis.LayerFilter.PolygonLayer)
        self.plss.setAllowEmptyLayer(True)
        self.fsec, self.ftwp, self.ftdir, self.frng = (QgsFieldComboBox() for _ in range(4))
        self.ftdir.setAllowEmptyFieldName(True)
        self.plss.layerChanged.connect(self._plss_changed)
        self.plss_widgets = [self.plss, self.fsec, self.ftwp, self.ftdir, self.frng]
        nf.addRow("PLSS sections layer", self.plss)
        nf.addRow("Section field", self.fsec)
        nf.addRow("Township field", self.ftwp)
        nf.addRow("Township N/S field", self.ftdir)
        nf.addRow("Range field", self.frng)
        self.twp = QgsMapLayerComboBox()
        self.twp.setFilters(Qgis.LayerFilter.PolygonLayer)
        self.twp.setAllowEmptyLayer(True)
        self.ttwp, self.tdir, self.trng = (QgsFieldComboBox() for _ in range(3))
        self.tdir.setAllowEmptyFieldName(True)
        self.twp.layerChanged.connect(self._twp_changed)
        self.plss_widgets += [self.twp, self.ttwp, self.tdir, self.trng]
        nf.addRow("Township layer (optional)", self.twp)
        nf.addRow("Township field (township layer)", self.ttwp)
        nf.addRow("Township N/S field (township layer)", self.tdir)
        nf.addRow("Range field (township layer)", self.trng)
        split = QLabel("Online PLSS data is usually split: a sections layer plus a township/range layer. "
                       "Pick both, and the township, N/S and range come from the township layer. "
                       "Leave the township layer empty if your sections layer has them all.")
        split.setWordWrap(True)
        split.setStyleSheet("color: palette(mid);")
        nf.addRow("", split)
        self.use_default.toggled.connect(self._sync_plss)
        tip = QLabel("Leave Township N/S empty when the township field already holds it (e.g. 04S).")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: palette(mid);")
        nf.addRow("", tip)
        self.on_build = QCheckBox("Number new parcels when they are built")
        self.on_build.setChecked(s.get("number_on_build", "1") == "1")
        nf.addRow("", self.on_build)
        self.num_types = QListWidget()
        self.num_types.setMaximumHeight(90)
        chosen = set(fabric.number_types)
        for t in fabric.parcel_types:
            it = QListWidgetItem(t)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if t in chosen else Qt.CheckState.Unchecked)
            self.num_types.addItem(it)
        nf.addRow("Parcel types that get numbers", self.num_types)
        tabs.addTab(n, "Parcel numbers")
        self._select_plss()

        from .ai_settings import AISettingsWidget
        self.ai = AISettingsWidget(self)
        tabs.addTab(self.ai, "AI Services")

        mu = QWidget()
        ml = QVBoxLayout(mu)
        txt = QLabel(
            "<b>Shared fabrics</b><br>When several people edit the same fabric, install database rules "
            "so that a parcel retired by one editor can't be retired (split, merged) again by another, "
            "and no fabric feature can be deleted - history is retired, never removed. The rules live "
            "in the database (PostgreSQL, SQL Server or GeoPackage triggers), so they hold for QGIS, "
            "ArcGIS and scripts alike.<br><br>Splits and merges also re-read the parcel from the "
            "database before retiring it.")
        txt.setWordWrap(True)
        ml.addWidget(txt)
        mrow = QHBoxLayout()
        inst = QPushButton("Install Rules")
        rem = QPushButton("Remove Rules")
        inst.clicked.connect(lambda: self._safeguards(True))
        rem.clicked.connect(lambda: self._safeguards(False))
        mrow.addWidget(inst)
        mrow.addWidget(rem)
        mrow.addStretch(1)
        ml.addLayout(mrow)
        ml.addStretch(1)
        tabs.addTab(mu, "Multi-user")

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    # ------------------------------------------------------------------
    def _safeguards(self, install):
        from ..core import safeguards
        try:
            n = safeguards.run(self.fabric, install)
        except Exception as e:
            QMessageBox.critical(self, "Multi-user rules", "Couldn't %s the rules: %s\n\n"
                                 "Your database login needs permission to create triggers."
                                 % ("install" if install else "remove", e))
            return
        QMessageBox.information(self, "Multi-user rules", "Rules %s (%d statements)." %
                                ("installed" if install else "removed", n))

    # ------------------------------------------------------------------
    def _browse_root(self):
        p = QFileDialog.getExistingDirectory(self, "Document folder", self.root.text())
        if p:
            self.root.setText(p)

    def _update_preview(self):
        rel = D.relative_path(self.folder_pat.text(), self.name_pat.text(), SAMPLE, ".pdf")
        root = self.root.text().strip() or "<document folder>"
        self.preview.setText("Plat, book 5, page 22  →  %s" % os.path.join(root, *rel))

    def _select_plss(self):
        s = self.fabric.settings
        src = s.get("plss_source")
        if src:
            for lyr in QgsProject.instance().mapLayers().values():
                if getattr(lyr, "source", None) and lyr.source() == src:
                    self.plss.setLayer(lyr)
                    break
            else:
                self.plss.setLayer(None)
        else:
            self.plss.setLayer(None)
        self._plss_changed(self.plss.currentLayer())
        for combo, key in ((self.fsec, "plss_section_field"), (self.ftwp, "plss_township_field"),
                           (self.ftdir, "plss_township_dir_field"), (self.frng, "plss_range_field")):
            if s.get(key):
                combo.setField(s.get(key))
        if src and self.plss.currentLayer() is None:
            self.plss.setToolTip("Saved layer isn't in this project:\n%s" % src)
        tsrc = s.get("plss_twp_source")
        self.twp.setLayer(None)
        for lyr in QgsProject.instance().mapLayers().values():
            if tsrc and getattr(lyr, "source", None) and lyr.source() == tsrc:
                self.twp.setLayer(lyr)
                break
        self._twp_changed(self.twp.currentLayer())
        for combo, key in ((self.ttwp, "plss_twp_township_field"), (self.tdir, "plss_twp_dir_field"),
                           (self.trng, "plss_twp_range_field")):
            if s.get(key):
                combo.setField(s.get(key))
        self._sync_plss()

    def _sync_plss(self, *_):
        for w in self.plss_widgets:
            w.setEnabled(not self.use_default.isChecked())

    def _twp_changed(self, lyr):
        for c in (self.ttwp, self.tdir, self.trng):
            c.setLayer(lyr)
        if lyr is None:
            return
        names = {f.name().lower(): f.name() for f in lyr.fields()}
        for combo, cands in ((self.ttwp, ("twnshpno", "township", "twp")), (self.tdir, ("twnshpdir", "twpdir")),
                             (self.trng, ("rangeno", "range", "rng"))):
            for c in cands:
                if c in names and not combo.currentField():
                    combo.setField(names[c])

    def _plss_changed(self, lyr):
        for c in (self.fsec, self.ftwp, self.ftdir, self.frng):
            c.setLayer(lyr)
        if lyr is None:
            return
        names = {f.name().lower(): f.name() for f in lyr.fields()}

        def guess(combo, *cands):
            if combo.currentField():
                return
            for c in cands:
                if c in names:
                    combo.setField(names[c])
                    return
        guess(self.fsec, "frstdivno", "section", "sec", "sect")
        guess(self.ftwp, "township", "twp", "town", "twnshp")
        guess(self.frng, "range", "rng", "rge")
        guess(self.ftdir, "twpdir", "twp_dir", "tdir", "townshipdir")

    # ------------------------------------------------------------------
    def _ok(self):
        types = [t.strip() for t in self.types.text().split(",") if t.strip()]
        if not types:
            QMessageBox.warning(self, "Fabric Settings", "Keep at least one parcel type.")
            return
        removed = [t for t in self.fabric.parcel_types if t not in types]
        in_use = [t for t in removed if self._type_used(t)]
        if in_use:
            QMessageBox.warning(self, "Fabric Settings",
                                "These parcel types are still used by features: %s" % ", ".join(in_use))
            return
        root = self.root.text().strip()
        if root and not os.path.isdir(root):
            if QMessageBox.question(self, "Fabric Settings",
                                    "The document folder doesn't exist yet. Create it?\n%s" % root) \
                    != QMessageBox.StandardButton.Yes:
                return
            try:
                os.makedirs(root, exist_ok=True)
            except OSError as e:
                QMessageBox.critical(self, "Fabric Settings", str(e))
                return
        dflt = self.use_default.isChecked()
        lyr = None if dflt else self.plss.currentLayer()
        tl = None if (dflt or lyr is None) else self.twp.currentLayer()
        if lyr is not None and not self.fsec.currentField():
            QMessageBox.warning(self, "Fabric Settings", "Pick the section field.")
            return
        if lyr is not None and not (tl is not None and self.ttwp.currentField() and self.trng.currentField()
                                    or tl is None and self.ftwp.currentField() and self.frng.currentField()):
            QMessageBox.warning(self, "Fabric Settings", "Pick the township and range fields "
                                "(on the township layer if you chose one, else on the sections layer).")
            return
        num_types = [self.num_types.item(i).text() for i in range(self.num_types.count())
                     if self.num_types.item(i).checkState() == Qt.CheckState.Checked]
        try:
            sep, digits, parent = clean_pattern(
                self.num_sep.text(), self.num_digits.value(), self.num_parent.text())
        except NumberError as e:
            QMessageBox.warning(self, "Fabric Settings", str(e))
            return
        values = {
            "name": self.name.text().strip() or "Parcel Fabric",
            "parcel_types": json.dumps(types),
            "snap_tolerance": str(self.tol.value()),
            "misclose_warn_ratio": str(int(self.ratio.value())),
            "packed_dms": "1" if self.packed.isChecked() else "0",
            "doc_root": root,
            "doc_folder_pattern": self.folder_pat.text().strip() or D.DEFAULT_FOLDER,
            "doc_name_pattern": self.name_pat.text().strip() or D.DEFAULT_NAME,
            "plss_provider": lyr.providerType() if lyr else "",
            "plss_source": lyr.source() if lyr else "",
            "plss_section_field": self.fsec.currentField() if lyr else "",
            "plss_township_field": self.ftwp.currentField() if lyr else "",
            "plss_township_dir_field": self.ftdir.currentField() if lyr else "",
            "plss_range_field": self.frng.currentField() if lyr else "",
            "plss_twp_provider": tl.providerType() if tl else "",
            "plss_twp_source": tl.source() if tl else "",
            "plss_twp_township_field": self.ttwp.currentField() if tl else "",
            "plss_twp_dir_field": self.tdir.currentField() if tl else "",
            "plss_twp_range_field": self.trng.currentField() if tl else "",
            "plss_use_default": "1" if dflt else "0",
            "number_on_build": "1" if self.on_build.isChecked() else "0",
            "number_types": json.dumps(num_types if len(num_types) < len(types) else []),
            "number_separator": sep,
            "number_split_digits": str(digits),
            "number_parent_suffix": parent,
        }
        try:
            self.ai.save()
            self.fabric.save_settings(values)
        except Exception as e:
            QMessageBox.critical(self, "Fabric Settings", str(e))
            return
        self.accept()

    def _allow_multipart(self):
        if QMessageBox.question(
                self, "Multi-polygon parcels", "Change this fabric so one parcel number can have several polygons?\n\n"
                "A copy of the GeoPackage is saved next to it first. Everyone using the fabric needs Parcel Fabric "
                "0.10 or newer afterwards, and should close it while you do this.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        try:
            backup = self.fabric.convert_to_multipart()
        except Exception as e:
            QMessageBox.critical(self, "Multi-polygon parcels", "Nothing was changed: %s" % e)
            return
        self.multi_btn.hide()
        self.multi_lbl.setText("One parcel can have several polygons")
        QMessageBox.information(self, "Multi-polygon parcels", "Done. The fabric as it was is saved as\n%s" % backup)

    def _type_used(self, t):
        req = QgsFeatureRequest().setFilterExpression(type_expr(t)).setLimit(1)
        return any(True for _ in self.fabric.parcels.getFeatures(req)) or \
            any(True for _ in self.fabric.lines.getFeatures(req))
