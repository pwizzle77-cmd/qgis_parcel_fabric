# -*- coding: utf-8 -*-
"""New / Edit Record (plat, deed, survey...)."""

import os

from qgis.PyQt.QtCore import QDate, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QVBoxLayout,
)

from ..core import documents as D
from ..core import schema as S

DOC_FILTER = "Documents (*.pdf *.tif *.tiff *.jpg *.jpeg *.png *.dwg *.dxf *.doc *.docx);;All files (*)"


def open_document(parent, path):
    if not path:
        return
    url = QUrl(path) if "://" in path else QUrl.fromLocalFile(path)
    if url.isLocalFile() and not os.path.exists(url.toLocalFile()):
        QMessageBox.warning(parent, "Document", "File not found:\n%s" % path)
        return
    QDesktopServices.openUrl(url)


class RecordDialog(QDialog):
    """``values`` (from Fabric.record_values) puts the dialog in edit mode."""

    def __init__(self, fabric, values=None, parent=None):
        super().__init__(parent)
        self.fabric = fabric
        self.editing = values is not None
        values = values or {}
        self.setWindowTitle("Edit Record" if self.editing else "New Record")
        self.setMinimumWidth(480)
        self.doc_src = None          # a newly chosen file to store on OK

        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.rtype = QComboBox()
        self.rtype.setEditable(True)
        self.rtype.addItems(S.RECORD_TYPES)
        if values.get("rtype"):
            self.rtype.setCurrentText(str(values["rtype"]))
        bp = QHBoxLayout()
        self.book = QLineEdit(str(values.get("book") or ""))
        self.page = QLineEdit(str(values.get("page") or ""))
        self.book.setPlaceholderText("Book")
        self.page.setPlaceholderText("Page")
        bp.addWidget(self.book)
        bp.addWidget(QLabel("Page"))
        bp.addWidget(self.page)
        self.name = QLineEdit(str(values.get("name") or ""))
        self.name.setPlaceholderText("Leave blank to use type, book and page")
        self.has_date = QCheckBox("Recorded on")
        d = values.get("date")
        self.date = QDateEdit(d if isinstance(d, QDate) and d.isValid() else QDate.currentDate())
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat("yyyy-MM-dd")
        self.has_date.setChecked(isinstance(d, QDate) and d.isValid())
        self.date.setEnabled(self.has_date.isChecked())
        self.has_date.toggled.connect(self.date.setEnabled)

        drow = QHBoxLayout()
        self.doc = QLineEdit(str(values.get("doc_url") or ""))
        self.doc.setPlaceholderText("Attach a scan, or paste a link")
        self.doc.textEdited.connect(lambda *_: setattr(self, "doc_src", None))
        attach = QPushButton("Attach...")
        attach.setToolTip("Pick a file; it is copied into the document folder and renamed")
        attach.clicked.connect(self._attach)
        opn = QPushButton("Open")
        opn.clicked.connect(lambda: open_document(self, self.doc_src or self.doc.text().strip()))
        drow.addWidget(self.doc, 1)
        drow.addWidget(attach)
        drow.addWidget(opn)
        self.doc_hint = QLabel()
        self.doc_hint.setWordWrap(True)
        self.doc_hint.setStyleSheet("color: palette(mid);")

        self.basis = QLineEdit(str(values.get("basis") or ""))
        self.basis.setPlaceholderText("e.g. East line of Section 12 = N 0°12'30\" W")
        g2g = QHBoxLayout()
        self.scale = QDoubleSpinBox()
        self.scale.setDecimals(8)
        self.scale.setRange(0.9, 1.1)
        self.scale.setSingleStep(0.00001)
        self.scale.setValue(float(values.get("scale") or 1.0))
        self.rotation = QDoubleSpinBox()
        self.rotation.setDecimals(6)
        self.rotation.setRange(-45, 45)
        self.rotation.setSuffix("°")
        self.rotation.setValue(float(values.get("rotation") or 0.0))
        g2g.addWidget(QLabel("Scale"))
        g2g.addWidget(self.scale)
        g2g.addWidget(QLabel("Rotation"))
        g2g.addWidget(self.rotation)
        self.notes = QPlainTextEdit(str(values.get("notes") or ""))
        self.notes.setMaximumHeight(70)

        form.addRow("Type", self.rtype)
        form.addRow("Book", bp)
        form.addRow("Name", self.name)
        form.addRow(self.has_date, self.date)
        form.addRow("Document", drow)
        form.addRow("", self.doc_hint)
        form.addRow("Basis of bearing", self.basis)
        form.addRow("Ground to grid", g2g)
        form.addRow("Notes", self.notes)
        lay.addLayout(form)
        self.activate = QCheckBox("Start mapping now (make this the active record)")
        self.activate.setToolTip("Leave this off to file the deed now and draw it later")
        self.activate.setChecked(False)
        self.activate.setVisible(not self.editing)
        lay.addWidget(self.activate)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        for w in (self.book, self.page, self.name):
            w.textChanged.connect(self._update_hint)
        self.rtype.currentTextChanged.connect(self._update_hint)
        self._update_hint()

    # ------------------------------------------------------------------
    def _doc_settings(self):
        s = self.fabric.settings
        return s.get("doc_root", ""), s.get("doc_folder_pattern") or D.DEFAULT_FOLDER, \
            s.get("doc_name_pattern") or D.DEFAULT_NAME

    def _token_values(self):
        v = self.values()
        return D.record_values(v["name"], v["rtype"], v["book"], v["page"], v["date"])

    def _update_hint(self):
        root, fpat, npat = self._doc_settings()
        src = self.doc_src
        if not root:
            self.doc_hint.setText("No document folder is set, so attached files stay where they are "
                                  "(Fabric Settings > Documents).")
            return
        if not src:
            self.doc_hint.setText("")
            return
        rel = D.relative_path(fpat, npat, self._token_values(), os.path.splitext(src)[1])
        self.doc_hint.setText("Will be copied to:  %s" % os.path.join(root, *rel))

    def _attach(self):
        start = os.path.dirname(self.doc.text()) if os.path.isfile(self.doc.text()) else ""
        p, _ = QFileDialog.getOpenFileName(self, "Attach document", start, DOC_FILTER)
        if not p:
            return
        self.doc_src = p
        self.doc.setText(p)
        self._update_hint()

    def _ok(self):
        if not (self.name.text().strip() or self.book.text().strip() or self.page.text().strip()):
            QMessageBox.warning(self, self.windowTitle(), "Enter a book and page, or a name.")
            return
        self.accept()

    # ------------------------------------------------------------------
    def values(self):
        rtype = self.rtype.currentText().strip()
        book, page = self.book.text().strip(), self.page.text().strip()
        name = self.name.text().strip()
        if not name:
            name = " ".join(p for p in [rtype, "Book %s" % book if book else "",
                                        "Page %s" % page if page else ""] if p)
        return {
            "name": name, "rtype": rtype, "book": book, "page": page,
            "date": self.date.date() if self.has_date.isChecked() else None,
            "doc_url": self.doc.text().strip(), "basis": self.basis.text().strip(),
            "notes": self.notes.toPlainText().strip(),
            "scale": self.scale.value(), "rotation": self.rotation.value(),
        }

    def store_document(self, gid):
        """Copy a newly attached file into the document folder.

        Returns the path to save on the record (unchanged if no folder is set).
        """
        root, fpat, npat = self._doc_settings()
        if not self.doc_src or not root:
            return self.doc.text().strip()
        v = self.values()
        vals = D.record_values(v["name"], v["rtype"], v["book"], v["page"], v["date"], gid)
        return D.store(self.doc_src, root, fpat, npat, vals)

    def rename_existing(self, gid):
        """After an edit: move a stored document to the name the record now calls for.

        Returns the new path, or None when nothing should change.
        """
        root, fpat, npat = self._doc_settings()
        path = self.doc.text().strip()
        if self.doc_src or not root or not path or not os.path.isfile(path) or not D.is_inside(path, root):
            return None
        v = self.values()
        vals = D.record_values(v["name"], v["rtype"], v["book"], v["page"], v["date"], gid)
        target = D.target_for(root, fpat, npat, vals, os.path.splitext(path)[1])
        if os.path.normcase(os.path.abspath(target)) == os.path.normcase(os.path.abspath(path)):
            return None
        if QMessageBox.question(self, "Rename document",
                                "Rename the stored document to match the record?\n\n%s\n→ %s"
                                % (path, target)) != QMessageBox.StandardButton.Yes:
            return None
        return D.store(path, root, fpat, npat, vals, move=True)
