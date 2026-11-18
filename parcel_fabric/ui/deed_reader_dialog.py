# -*- coding: utf-8 -*-
"""Read Deed (AI): deed file -> text -> calls -> review -> Traverse.

Uses a local Ollama server.  The COGO Workbench parser reads the calls
first; the AI model is only asked about clauses the parser couldn't read,
must quote the deed, and is checked against that quote.  Nothing loads into
Traverse until the reviewer ticks the box.
"""

import os

from qgis.core import QgsApplication, QgsSettings, QgsTask
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout,
)

from ..core import cogo as C
from ..core import deed_ai as A
from ..core import ollama as OL
from ..core import units as U
from ..core.legal_import import parse, to_course

S_URL, S_OCR, S_LLM, S_DIR = ("ParcelFabric/ollama_url", "ParcelFabric/ollama_ocr_model",
                              "ParcelFabric/ollama_llm_model", "ParcelFabric/deed_dir")
FILTER = "Deeds (*.pdf *.png *.jpg *.jpeg *.tif *.tiff);;All files (*)"
GREEN, ORANGE, RED, GREY = QColor("#1b5e20"), QColor("#a35a00"), QColor("#b3261e"), QColor("#666666")


class DeedReaderDialog(QDialog):
    def __init__(self, iface, manager, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.manager = manager
        self.fabric = manager.active
        self.courses = []
        self.rows = []           # dicts: phase, source, course, by, note
        self._task = None
        self.setWindowTitle("Read Deed (AI)")
        self.resize(900, 760)
        s = QgsSettings()
        lay = QVBoxLayout(self)

        frow = QHBoxLayout()
        self.path = QLineEdit()
        self.path.setPlaceholderText("Deed file: PDF, PNG, JPG or TIF")
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        frow.addWidget(self.path, 1)
        frow.addWidget(browse)
        lay.addLayout(frow)

        form = QFormLayout()
        urow = QHBoxLayout()
        self.url = QLineEdit(s.value(S_URL, OL.DEFAULT_URL, type=str))
        refresh = QPushButton("Find Models")
        refresh.clicked.connect(self._load_models)
        urow.addWidget(self.url, 1)
        urow.addWidget(refresh)
        form.addRow("Ollama", urow)
        mrow = QHBoxLayout()
        self.ocr_model = QComboBox()
        self.ocr_model.setEditable(True)
        self.llm_model = QComboBox()
        self.llm_model.setEditable(True)
        self.dpi = QSpinBox()
        self.dpi.setRange(100, 400)
        self.dpi.setValue(200)
        self.dpi.setSuffix(" dpi")
        mrow.addWidget(QLabel("Read pages with"))
        mrow.addWidget(self.ocr_model, 1)
        mrow.addWidget(QLabel("Unread calls with"))
        mrow.addWidget(self.llm_model, 1)
        mrow.addWidget(self.dpi)
        form.addRow("Models", mrow)
        lay.addLayout(form)

        steps = QHBoxLayout()
        self.read_btn = QPushButton("1. Read Document")
        self.read_btn.clicked.connect(self._read)
        find = QPushButton("2. Find Description")
        find.clicked.connect(self._find)
        find_ai = QPushButton("Ask AI to Find It")
        find_ai.clicked.connect(self._find_ai)
        ext = QPushButton("3. Extract Calls")
        ext.clicked.connect(self._extract)
        self.ai_btn = QPushButton("Ask AI About Unread Calls")
        self.ai_btn.clicked.connect(self._ask_ai)
        for b in (self.read_btn, find, find_ai, ext, self.ai_btn):
            steps.addWidget(b)
        lay.addLayout(steps)
        self.status = QLabel("Pick a deed and press Read Document. Text PDFs are read directly; scans "
                             "go through the page-reading model.")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        self.tabs = QTabWidget()
        self.deed_text = QPlainTextEdit()
        self.deed_text.setPlaceholderText("The deed's text appears here. You can fix misread characters.")
        self.desc = QPlainTextEdit()
        self.desc.setPlaceholderText("The legal description part. Fix anything that was misread, then "
                                     "Extract Calls.")
        self.desc.textChanged.connect(self._invalidate)
        self.tabs.addTab(self.deed_text, "Deed text")
        self.tabs.addTab(self.desc, "Legal description")
        lay.addWidget(self.tabs, 2)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Part", "Source clause", "Call", "Read by"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(True)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.table, 2)
        self.closure = QLabel()
        self.closure.setWordWrap(True)
        lay.addWidget(self.closure)

        bot = QHBoxLayout()
        self.attach = QCheckBox("Attach the deed file to the active record")
        self.attach.setChecked(True)
        self.reviewed = QCheckBox("I checked every call against the deed")
        self.reviewed.toggled.connect(self._sync)
        self.load_btn = QPushButton("Load into Traverse")
        self.load_btn.clicked.connect(self._accept)
        cancel = QPushButton("Close")
        cancel.clicked.connect(self.reject)
        bot.addWidget(self.attach)
        bot.addStretch(1)
        bot.addWidget(self.reviewed)
        bot.addWidget(self.load_btn)
        bot.addWidget(cancel)
        lay.addLayout(bot)
        self._sync()
        self._load_models(quiet=True)

    # ------------------------------------------------------------------
    def _load_models(self, quiet=False):
        url = self.url.text().strip() or OL.DEFAULT_URL
        s = QgsSettings()
        try:
            models = OL.list_models(url)
        except OL.OllamaError as e:
            if not quiet:
                QMessageBox.warning(self, self.windowTitle(), str(e))
            self.status.setText(str(e))
            models = []
        for combo, key, prefs in ((self.ocr_model, S_OCR, ("deepseek-ocr", "qwen2.5vl", "gemma4")),
                                  (self.llm_model, S_LLM, ("qwen2.5vl", "gemma4", "qwen2.5-coder"))):
            combo.clear()
            combo.addItems(models)
            saved = s.value(key, "", type=str)
            combo.setCurrentText(saved if saved and (saved in models or not models) else OL.pick(models, *prefs))
        if models:
            s.setValue(S_URL, url)

    def _save_choices(self):
        s = QgsSettings()
        s.setValue(S_URL, self.url.text().strip())
        s.setValue(S_OCR, self.ocr_model.currentText())
        s.setValue(S_LLM, self.llm_model.currentText())

    def _browse(self):
        start = QgsSettings().value(S_DIR, "", type=str)
        p, _ = QFileDialog.getOpenFileName(self, "Deed", start, FILTER)
        if p:
            self.path.setText(p)
            QgsSettings().setValue(S_DIR, os.path.dirname(p))

    # ------------------------------------------------------------------
    # Background work
    # ------------------------------------------------------------------
    def _run(self, label, fn, done):
        if self._task is not None:
            QMessageBox.information(self, self.windowTitle(), "Still working - please wait.")
            return
        self._save_choices()
        self.status.setText(label + "...")
        self.setCursor(Qt.CursorShape.BusyCursor)

        def finished(exception, result=None):
            self._task = None
            self.unsetCursor()
            if exception is not None:
                self.status.setText("<span style='color:#b3261e'>%s</span>" % exception)
                return
            done(result)

        self._task = QgsTask.fromFunction(label, fn, on_finished=finished)
        QgsApplication.taskManager().addTask(self._task)

    def _read(self):
        path = self.path.text().strip()
        if not os.path.isfile(path):
            QMessageBox.information(self, self.windowTitle(), "Pick the deed file first.")
            return
        url, model, dpi = self.url.text().strip(), self.ocr_model.currentText(), self.dpi.value()

        def work(task):
            from ..core import deed_pages
            images, text_pages = deed_pages.load(path, dpi)
            if text_pages:
                return "text", text_pages
            if not model:
                raise RuntimeError("Choose a model to read the pages with (Find Models).")
            out = []
            for i, img in enumerate(images):
                if task.isCanceled():
                    raise RuntimeError("Cancelled")
                task.setProgress(100.0 * i / max(1, len(images)))
                out.append(OL.chat(url, model, A.ocr_prompt(model), [img]))
            return "ocr", out

        def done(result):
            how, pages = result
            text = "\n\n".join("----- page %d -----\n%s" % (i + 1, t.strip()) for i, t in enumerate(pages))
            self.deed_text.setPlainText(text)
            self.status.setText("Read %d page(s) %s. Check the text, then Find Description." % (
                len(pages), "from the PDF's text layer" if how == "text" else "with " + model))
            self._find()

        self._run("Reading %s" % os.path.basename(path), work, done)

    def _find(self):
        d = A.find_description(self.deed_text.toPlainText())
        if not d:
            self.status.setText("Couldn't find where the description starts ('Commence at' / 'Begin at'). "
                                "Paste it into the Legal description tab, or Ask AI to Find It.")
            return
        self.desc.setPlainText(d)
        self.tabs.setCurrentWidget(self.desc)
        self._extract()

    def _find_ai(self):
        text = self.deed_text.toPlainText().strip()
        if not text:
            return
        url, model = self.url.text().strip(), self.llm_model.currentText()

        def work(task):
            return OL.chat(url, model, A.FIND_PROMPT % text[:20000])

        def done(result):
            # keep only what is really in the deed
            if not A.quote_in_text(result[:200], text):
                self.status.setText("<span style='color:#b3261e'>The model's answer isn't word-for-word from "
                                    "the deed, so it wasn't used.</span>")
                return
            self.desc.setPlainText(result.strip())
            self.tabs.setCurrentWidget(self.desc)
            self._extract()

        self._run("Finding the description", work, done)

    # ------------------------------------------------------------------
    # Calls
    # ------------------------------------------------------------------
    def _foot(self):
        return self.fabric.foot_m if self.fabric is not None else U.US_SURVEY_FOOT_M

    def _invalidate(self):
        self.courses = []
        self.reviewed.setChecked(False)
        self._sync()

    def _extract(self):
        self._invalidate()
        text = self.desc.toPlainText().strip()
        if not text:
            return
        try:
            rows, _courses, _problems = parse(text, self._foot())
        except ValueError as e:
            self.status.setText(str(e))
            return
        self.rows = []
        for r in rows:
            course, note = None, r.issue
            if r.course is not None and not r.issue:
                try:
                    course = to_course(r.course, r.unit, self._foot())
                except ValueError as e:
                    note = str(e)
            self.rows.append({"phase": r.phase, "source": r.source, "course": course,
                              "by": "Parser" if course else "", "note": note,
                              "is_call": bool(r.course is not None or r.issue)})
        self._show_rows()

    def _show_rows(self):
        t = self.table
        t.setRowCount(0)
        bad = 0
        for row in self.rows:
            r = t.rowCount()
            t.insertRow(r)
            if row["course"] is not None:
                call, color = _describe(row["course"]), GREEN if row["by"] == "Parser" else ORANGE
            elif row["note"]:
                call, color = row["note"], RED
                if row["phase"] == "Boundary":
                    bad += 1
            else:
                call, color = "context (not a call)", GREY
            if row["phase"] != "Boundary" and row["course"] is not None:
                call, color = "tie (excluded): " + call, GREY
            items = [row["phase"], row["source"], call, row["by"]]
            for c, txt in enumerate(items):
                it = QTableWidgetItem(txt)
                if c == 2:
                    it.setForeground(color)
                if c == 3 and row["by"] == "AI":
                    it.setForeground(ORANGE)
                t.setItem(r, c, it)
        t.resizeRowsToContents()
        boundary = [r for r in self.rows if r["phase"] == "Boundary"]
        courses = [r["course"] for r in boundary if r["course"] is not None]
        self.courses = courses if not bad else []
        if courses:
            res = C.compute((0.0, 0.0), courses)
            ratio = "closes exactly" if res.misclose < 1e-6 else "misclose %.3f ft (1:%s)" % (
                res.misclose, "{:,}".format(int(res.ratio)))
            self.closure.setText("%d boundary call(s): %s, area %.3f acres%s" % (
                len(courses), ratio, res.area / 43560.0,
                "" if not bad else " - <span style='color:#b3261e'>%d call(s) still unread</span>" % bad))
        else:
            self.closure.setText("No boundary calls read yet.")
        ai = sum(1 for r in boundary if r["by"] == "AI")
        self.status.setText("Parser read %d call(s)%s. %s" % (
            sum(1 for r in boundary if r["by"] == "Parser"),
            ", AI read %d (orange - check these against the deed)" % ai if ai else "",
            "Red calls need fixing in the description text, or Ask AI About Unread Calls." if bad
            else "Review every call, tick the box, then Load into Traverse."))
        self._sync()

    def _ask_ai(self):
        if not self.rows:
            # nothing parsed at all: give the whole description to the model
            text = self.desc.toPlainText().strip()
            if not text:
                return
            targets = [None]
        else:
            targets = [i for i, r in enumerate(self.rows)
                       if r["phase"] == "Boundary" and r["course"] is None and r["note"]]
            if not targets:
                QMessageBox.information(self, self.windowTitle(), "Every boundary call was read already.")
                return
        url, model = self.url.text().strip(), self.llm_model.currentText()
        desc = self.desc.toPlainText()
        sources = [desc if i is None else self.rows[i]["source"] for i in targets]

        def work(task):
            answers = []
            for k, src in enumerate(sources):
                if task.isCanceled():
                    raise RuntimeError("Cancelled")
                task.setProgress(100.0 * k / len(sources))
                answers.append(OL.chat_json(url, model, A.CALLS_PROMPT % src, A.CALL_SCHEMA))
            return answers

        def done(answers):
            if targets == [None]:
                self.rows = []
                for call in (answers[0] or {}).get("calls", []):
                    self.rows.append(self._ai_row(call, desc))
            else:
                for i, ans in zip(targets, answers):
                    calls = (ans or {}).get("calls", [])
                    if len(calls) != 1:
                        self.rows[i]["note"] = "AI found %d calls in this clause - split the clause or fix it by hand" \
                            % len(calls)
                        continue
                    new = self._ai_row(calls[0], self.rows[i]["source"])
                    if new["course"] is not None:
                        self.rows[i].update(course=new["course"], by="AI", note="")
                    else:
                        self.rows[i]["note"] = "AI answer rejected: " + new["note"]
            self._show_rows()

        self._run("Asking %s about %d clause(s)" % (model, len(sources)), work, done)

    def _ai_row(self, call, text):
        try:
            course = A.check_call(call, text, self._foot())
            return {"phase": "Boundary", "source": call.get("quote", ""), "course": course, "by": "AI",
                    "note": "", "is_call": True}
        except (ValueError, TypeError) as e:
            return {"phase": "Boundary", "source": call.get("quote", ""), "course": None, "by": "AI",
                    "note": "AI answer rejected: %s" % e, "is_call": True}

    # ------------------------------------------------------------------
    def _sync(self):
        ok = bool(self.courses)
        self.reviewed.setEnabled(ok)
        self.load_btn.setEnabled(ok and self.reviewed.isChecked())

    def _accept(self):
        if self.attach.isChecked():
            self._attach_to_record()
        self.accept()

    def _attach_to_record(self):
        fab = self.fabric
        gid = self.manager.active_record_id
        path = self.path.text().strip()
        if fab is None or not gid or not os.path.isfile(path):
            return
        rec = fab.record_by_id(gid)
        if rec is None or rec["QPF_DocumentURL"]:
            return            # never replace a document that's already attached
        from ..core import documents as D
        root = fab.settings.get("doc_root", "")
        new = path
        if root:
            v = fab.record_values(rec)
            vals = D.record_values(v["name"], v["rtype"], v["book"], v["page"], v["date"], gid)
            try:
                new = D.store(path, root, fab.settings.get("doc_folder_pattern") or D.DEFAULT_FOLDER,
                              fab.settings.get("doc_name_pattern") or D.DEFAULT_NAME, vals)
            except (OSError, ValueError):
                new = path
        fab.update_record(gid, doc_url=new)


def _describe(c):
    if c.is_curve:
        side = "right" if c.radius > 0 else "left"
        kind = "tangent" if c.dir_kind == C.DIR_TANGENT else "chord"
        return "Curve %s, R %.2f, arc %.2f, %s %s" % (side, abs(c.radius), c.arc_length, kind,
                                                      U.azimuth_to_bearing(c.azimuth))
    return "%s  %.2f ft" % (U.azimuth_to_bearing(c.azimuth), c.distance)
