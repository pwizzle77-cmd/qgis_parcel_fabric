# -*- coding: utf-8 -*-
"""Paste a legal description, review the calls, load them into Traverse.

Uses the COGO Workbench parser.  Nothing loads until every boundary call
parses and the reviewer ticks the review box.
"""

from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QHBoxLayout, QHeaderView, QLabel,
    QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)
from qgis.PyQt.QtCore import Qt

from ..core import units as U
from ..core.legal_import import parse


class LegalDialog(QDialog):
    def __init__(self, fabric_foot_m, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Paste Legal Description")
        self.resize(820, 600)
        self.foot_m = fabric_foot_m
        self.courses = []
        lay = QVBoxLayout(self)

        split = QSplitter(Qt.Orientation.Vertical)
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("Paste ONE parcel's metes-and-bounds description here...")
        self.text.textChanged.connect(self._invalidate)
        split.addWidget(self.text)

        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        opts = QHBoxLayout()
        self.all_boundary = QCheckBox("All calls are boundary (no separate commencement tie)")
        self.all_boundary.toggled.connect(self._invalidate)
        opts.addWidget(self.all_boundary)
        opts.addStretch(1)
        opts.addWidget(QLabel('Plain "feet" means'))
        self.feet = QComboBox()
        self.feet.addItem("the fabric's foot", None)
        self.feet.addItem("US survey feet", U.US_SURVEY_FOOT_M)
        self.feet.addItem("International feet", U.INTL_FOOT_M)
        self.feet.currentIndexChanged.connect(self._invalidate)
        opts.addWidget(self.feet)
        extract = QPushButton("Extract calls for review")
        extract.clicked.connect(self._extract)
        opts.addWidget(extract)
        bl.addLayout(opts)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Part", "Source clause", "Result"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(True)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        bl.addWidget(self.table)
        self.status = QLabel()
        self.status.setWordWrap(True)
        bl.addWidget(self.status)
        split.addWidget(bottom)
        lay.addWidget(split)

        row = QHBoxLayout()
        self.reviewed = QCheckBox("I checked every call against the document")
        self.reviewed.toggled.connect(self._sync)
        self.load = QPushButton("Load into Traverse")
        self.load.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        row.addWidget(self.reviewed)
        row.addStretch(1)
        row.addWidget(self.load)
        row.addWidget(cancel)
        lay.addLayout(row)
        self._ok = False
        self._sync()

    def _invalidate(self):
        self._ok = False
        self.courses = []
        self.reviewed.setChecked(False)
        self._sync()

    def _sync(self):
        self.reviewed.setEnabled(self._ok)
        self.load.setEnabled(self._ok and self.reviewed.isChecked())

    def _extract(self):
        self._invalidate()
        self.table.setRowCount(0)
        try:
            rows, courses, problems = parse(self.text.toPlainText(), self.foot_m,
                                            self.all_boundary.isChecked(), self.feet.currentData())
        except ValueError as e:
            self.status.setText(str(e))
            return
        ci = 0
        for row in rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            if row.issue:
                result, color = row.issue, QColor("#b3261e")
            elif row.course is None:
                result, color = "context (not a course)", QColor("#666666")
            elif row.phase != "Boundary":
                result, color = "excluded tie: %s" % _describe_wb(row.course), QColor("#666666")
            else:
                c = courses[ci] if ci < len(courses) else None
                ci += 1
                result, color = (_describe(c) if c else "?"), QColor("#1b5e20")
            for col, txt in enumerate([row.phase, row.source, result]):
                it = QTableWidgetItem(txt)
                if col == 2:
                    it.setForeground(color)
                self.table.setItem(r, col, it)
        self.table.resizeRowsToContents()
        if problems:
            self.status.setText("%d boundary call(s) need fixing - correct the text and extract "
                                "again, or type the calls in Traverse." % len(problems))
            return
        if not courses:
            self.status.setText("No boundary courses found.")
            return
        self.courses = courses
        self._ok = True
        self.status.setText("%d boundary course(s) ready. Review them, tick the box, then load."
                            % len(courses))
        self._sync()


def _describe(c):
    if c.is_curve:
        side = "right" if c.radius > 0 else "left"
        kind = {"tangent": "tangent", "chord": "chord"}.get(c.dir_kind, c.dir_kind)
        return "Curve %s R=%.2f arc %.2f, %s %s" % (side, abs(c.radius), c.arc_length, kind,
                                                    U.azimuth_to_bearing(c.azimuth))
    return "%s  %.2f" % (U.azimuth_to_bearing(c.azimuth), c.distance)


def _describe_wb(course):
    return "%s %s %s" % (course.kind, course.direction, course.distance)
