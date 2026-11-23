# -*- coding: utf-8 -*-
"""New Fabric / Add Fabric dialogs."""

import os

from qgis.core import QgsCoordinateReferenceSystem, QgsProject, QgsSettings
from qgis.gui import QgsProjectionSelectionWidget
from qgis.PyQt.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QRadioButton, QVBoxLayout, QWidget,
)

from ..core import schema as S
from ..core.geomio import unit_word

SETTINGS_KEY = "ParcelFabric/last_gpkg"


class StorageWidget(QWidget):
    """GeoPackage file or a saved QGIS database connection."""

    def __init__(self, new=True, parent=None):
        super().__init__(parent)
        self.new = new
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.rb_gpkg = QRadioButton("GeoPackage file")
        self.rb_db = QRadioButton("Database connection (PostgreSQL, SQL Server, Oracle...)")
        grp = QButtonGroup(self)
        grp.addButton(self.rb_gpkg)
        grp.addButton(self.rb_db)
        self.rb_gpkg.setChecked(True)

        row = QHBoxLayout()
        self.path = QLineEdit(QgsSettings().value(SETTINGS_KEY, "", type=str))
        self.path.setPlaceholderText("C:/GIS/parcels.gpkg")
        b = QPushButton("Browse...")
        b.clicked.connect(self._browse)
        row.addWidget(self.path)
        row.addWidget(b)

        self.conn = QComboBox()
        for provider, name in S.saved_connections():
            label = dict(S.DB_PROVIDERS).get(provider, provider)
            self.conn.addItem("%s  (%s)" % (name, label), (provider, name))
        self.schema = QLineEdit()
        self.schema.setPlaceholderText("public / dbo")
        self.conn.currentIndexChanged.connect(self._default_schema)
        self._default_schema()

        lay.addWidget(self.rb_gpkg)
        lay.addLayout(row)
        lay.addWidget(self.rb_db)
        form = QFormLayout()
        form.addRow("Connection", self.conn)
        form.addRow("Schema", self.schema)
        lay.addLayout(form)
        if self.conn.count() == 0:
            self.rb_db.setEnabled(False)
            self.rb_db.setText(self.rb_db.text() + " - none saved in QGIS yet")
        self.rb_gpkg.toggled.connect(self._sync)
        self._sync()

    def _sync(self):
        g = self.rb_gpkg.isChecked()
        self.path.setEnabled(g)
        self.conn.setEnabled(not g)
        self.schema.setEnabled(not g)

    def _default_schema(self):
        d = self.conn.currentData()
        if not d or self.schema.text():
            return
        self.schema.setText({"postgres": "public", "mssql": "dbo"}.get(d[0], ""))

    def _browse(self):
        start = self.path.text() or os.path.expanduser("~")
        if self.new:
            p, _ = QFileDialog.getSaveFileName(self, "GeoPackage", start, "GeoPackage (*.gpkg)",
                                               options=QFileDialog.Option.DontConfirmOverwrite)
        else:
            p, _ = QFileDialog.getOpenFileName(self, "GeoPackage", start, "GeoPackage (*.gpkg)")
        if p:
            if not p.lower().endswith(".gpkg"):
                p += ".gpkg"
            self.path.setText(p)

    def value(self):
        """(provider, source, schema) or raises ValueError."""
        if self.rb_gpkg.isChecked():
            p = self.path.text().strip()
            if not p:
                raise ValueError("Choose a GeoPackage file.")
            if not self.new and not os.path.exists(p):
                raise ValueError("That GeoPackage doesn't exist.")
            QgsSettings().setValue(SETTINGS_KEY, p)
            return "ogr", p, ""
        d = self.conn.currentData()
        if not d:
            raise ValueError("Choose a database connection.")
        return d[0], d[1], self.schema.text().strip()


class NewFabricDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Parcel Fabric")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)

        box = QGroupBox("Where to store it")
        bl = QVBoxLayout(box)
        self.storage = StorageWidget(new=True)
        bl.addWidget(self.storage)
        lay.addWidget(box)

        form = QFormLayout()
        self.name = QLineEdit("Parcel Fabric")
        self.prefix = QLineEdit()
        self.prefix.setPlaceholderText("optional - only to keep two fabrics in one database")
        self.crs = QgsProjectionSelectionWidget()
        pc = QgsProject.instance().crs()
        self.crs.setCrs(pc if pc.isValid() else QgsCoordinateReferenceSystem("EPSG:2238"))
        self.types = QLineEdit("Tax, Lot")
        self.types.setToolTip("Parcel types, comma separated (each gets a polygon and a lines layer)")
        self.foot = QComboBox()
        self.foot.addItem("US survey foot", "us_survey")
        self.foot.addItem("International foot", "international")
        self.packed = QCheckBox("Read 45.3015 as 45°30'15\" (packed DD.MMSS)")
        self.packed.setChecked(False)
        self.tol = QDoubleSpinBox()
        self.tol.setDecimals(3)
        self.tol.setRange(0.001, 10)
        self.tol.setValue(0.01)
        self.tol.setSuffix(" map units")
        self.ratio = QDoubleSpinBox()
        self.ratio.setDecimals(0)
        self.ratio.setRange(100, 1000000)
        self.ratio.setValue(10000)
        self.ratio.setPrefix("1:")
        form.addRow("Fabric name", self.name)
        form.addRow("Table prefix", self.prefix)
        form.addRow("Coordinate system", self.crs)
        form.addRow("Parcel types", self.types)
        form.addRow("Distance unit", self.foot)
        form.addRow("", self.packed)
        form.addRow("Snap tolerance", self.tol)
        form.addRow("Flag misclose worse than", self.ratio)
        lay.addLayout(form)
        note = QLabel("Field names match the ArcGIS Pro parcel fabric so data moves between them cleanly.")
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid);")
        lay.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.result_value = None

    def _ok(self):
        try:
            provider, source, schema = self.storage.value()
        except ValueError as e:
            QMessageBox.warning(self, "New Parcel Fabric", str(e))
            return
        if unit_word(self.crs.crs()) is None:
            QMessageBox.warning(self, "New Parcel Fabric", "Pick a State Plane (or other survey) coordinate system "
                                "in feet or meters, such as EPSG:2238. Web Mercator and latitude/longitude "
                                "distort distances and bearings.")
            return
        types = [t.strip() for t in self.types.text().split(",") if t.strip()]
        bad = [t for t in types if not t.replace("_", "").isalnum()]
        if not types or bad:
            QMessageBox.warning(self, "New Parcel Fabric",
                                "Parcel types must be letters, numbers or _ (e.g. Tax, Lot).")
            return
        prefix = self.prefix.text().strip()
        if prefix and not prefix.replace("_", "").isalnum():
            QMessageBox.warning(self, "New Parcel Fabric", "The prefix must be letters, numbers or _.")
            return
        self.result_value = {
            "provider": provider, "source": source, "schema": schema, "prefix": prefix,
            "types": types, "crs": self.crs.crs().authid(),
            "settings": {"name": self.name.text().strip() or "Parcel Fabric",
                         "foot": self.foot.currentData(),
                         "packed_dms": "1" if self.packed.isChecked() else "0",
                         "snap_tolerance": str(self.tol.value()),
                         "misclose_warn_ratio": str(int(self.ratio.value()))},
        }
        self.accept()


class OpenFabricDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Parcel Fabric to Map")
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)
        self.storage = StorageWidget(new=False)
        lay.addWidget(self.storage)
        form = QFormLayout()
        self.prefix = QLineEdit()
        self.prefix.setPlaceholderText("leave blank unless the fabric was created with one")
        form.addRow("Table prefix", self.prefix)
        lay.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.result_value = None

    def _ok(self):
        try:
            provider, source, schema = self.storage.value()
        except ValueError as e:
            QMessageBox.warning(self, "Add Parcel Fabric", str(e))
            return
        self.result_value = {"provider": provider, "source": source, "schema": schema,
                             "prefix": self.prefix.text().strip()}
        self.accept()
