# -*- coding: utf-8 -*-
"""Parcel Fabric for QGIS - toolbar, menu and docks."""

import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QApplication, QLabel, QMenu, QMessageBox, QToolButton

try:
    from qgis.PyQt.QtGui import QAction
except ImportError:          # Qt5
    from qgis.PyQt.QtWidgets import QAction

MENU = "&Parcel Fabric"
HERE = os.path.dirname(__file__)


def icon(name):
    return QIcon(os.path.join(HERE, "icons", name + ".svg"))


class ParcelFabricPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.actions = []
        self.manager = None
        self.records_dock = None
        self.traverse_dock = None
        self.toolbar = None
        self._windows = {}

    # ------------------------------------------------------------------
    def initGui(self):
        from .core.manager import FabricManager
        self.manager = FabricManager()
        self.toolbar = self.iface.addToolBar("Parcel Fabric")
        self.toolbar.setObjectName("ParcelFabricToolbar")
        self._add("records", "Records", self.show_records,
                  "Records panel: create records, set the active record, build parcels")
        self._add("traverse", "Traverse", self.show_traverse,
                  "Enter courses from a plat or deed and create fabric lines")
        self._add("build", "Build Active Record", self.build_active,
                  "Build parcels from the active record's closed loops")
        self._add("numbers", "Assign Parcel Numbers", self.assign_numbers,
                  "Number the selected parcels, or type the roll's parcel ID")
        self._add("check", "Check Fabric", self.check_fabric,
                  "Find open loops, gaps, overlaps, missing COGO and other problems")
        self._rec_lbl = QLabel("No active record")
        self._rec_lbl.setStyleSheet("font-weight: bold; padding: 0 8px;")
        self.toolbar.addWidget(self._rec_lbl)
        self.manager.activeRecordChanged.connect(self._show_record)
        self.manager.activeFabricChanged.connect(self._show_record)
        self._show_record()
        self._more_menu = QMenu(self.toolbar)
        self._more_btn = QToolButton(self.toolbar)
        self._more_btn.setText("More")
        self._more_btn.setIcon(icon("fabric"))
        self._more_btn.setToolTip("The rest of the parcel tools")
        self._more_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._more_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._more_btn.setMenu(self._more_menu)
        self.toolbar.addWidget(self._more_btn)
        self._more("new_fabric", "New Fabric...", self.new_fabric,
                   "Create a parcel fabric in a GeoPackage or database")
        self._more("add_fabric", "Add Fabric to Map...", self.open_fabric,
                   "Add an existing parcel fabric to this project")
        self._more_menu.addSeparator()
        self._more("split", "Split Parcel", lambda: self._window("split"),
                   "Cut a parcel with a line you draw; pieces become new parcels")
        self._more("divide", "Divide Parcel", lambda: self._window("divide"),
                   "Divide a parcel into equal areas, equal widths or set-area lots")
        self._more("merge", "Merge Parcels", lambda: self._window("merge"),
                   "Combine parcels into one, touching or not")
        self._more("split", "Separate Multi-Polygon Parcel", lambda: self._window("separate"),
                   "Make each polygon of a parcel its own parcel")
        self._more("copy_parallel", "Copy Parallel", lambda: self._window("copy_parallel"),
                   "Offset copies of fabric lines (easements, right of way)")
        self._more("fabric", "Stacked Unit...", lambda: self._window("stack"),
                   "Another parcel number on the same polygon (condo or common area), with no new lines")
        self._more_menu.addSeparator()
        self._more("lineage", "Parcel Lineage", lambda: self._window("lineage"),
                   "Where a parcel came from and what it became")
        self._more("history", "Parcel History", lambda: self._window("history"),
                   "A parcel's splits and merges on the map, step by step through time")
        self._more("legal_writer", "Write Legal Description", lambda: self._window("legal_writer"),
                   "Metes-and-bounds description of the polygon selected in ANY layer")
        self._more("compare", "Compare with Existing Parcels", lambda: self._window("compare"),
                   "Area difference, offsets and slivers between fabric parcels and an existing parcel layer")
        self._more("adjust", "Least-Squares Adjustment", lambda: self._window("adjust"),
                   "Consistency check of record dimensions, or a weighted adjustment to control")
        self._more("subdivide", "Subdivide Block", lambda: self._window("subdivide"),
                   "Lay out lots along a street frontage and store the lot, width and depth")
        self._more("address", "Address Lots (NENA)", lambda: self._window("address"),
                   "House numbers for new lots from the NENA Addressing and RCL Tools plugin")
        self._more_menu.addSeparator()
        self._more("exchange", "Import / Export...", lambda: self._window("exchange"),
                   "Import a parcel layer, an ArcGIS Pro fabric, or LandXML; export a tax-roll shapefile or DXF")
        self._more("settings", "Fabric Settings...", self.fabric_settings,
                   "Parcel types, document folder and naming, parcel numbering, tolerances")

    def _action(self, icon_name, text, slot, tip):
        a = QAction(icon(icon_name), text, self.iface.mainWindow())
        a.setToolTip(tip)
        a.setStatusTip(tip)
        a.triggered.connect(slot)
        self.iface.addPluginToMenu(MENU, a)
        self.actions.append(a)
        return a

    def _add(self, icon_name, text, slot, tip):
        a = self._action(icon_name, text, slot, tip)
        self.toolbar.addAction(a)
        return a

    def _more(self, icon_name, text, slot, tip):
        a = self._action(icon_name, text, slot, tip)
        self._more_menu.addAction(a)
        return a

    def _show_record(self, *_):
        rec = self.manager.active_record() if self.manager is not None else None
        if rec is None:
            self._rec_lbl.setText("No active record")
        else:
            name = rec["Name"] or "Record"
            book, page = rec["Book"], rec["Page"]
            pid = rec["QPF_ParcelID"] if rec.fields().indexOf("QPF_ParcelID") >= 0 else ""
            if book or page:
                name += "   Book %s  Page %s" % (book or "-", page or "-")
            elif pid and str(pid) not in name:
                name += "   %s" % pid
            self._rec_lbl.setText(name)
        self._rec_lbl.adjustSize()

    def unload(self):
        if self.manager is not None:
            for sig in (self.manager.activeRecordChanged, self.manager.activeFabricChanged):
                try:
                    sig.disconnect(self._show_record)
                except (TypeError, RuntimeError):
                    pass
        for w in list(self._windows.values()):
            try:
                w.close()
            except RuntimeError:
                pass
        self._windows = {}
        for a in self.actions:
            self.iface.removePluginMenu(MENU, a)
            a.deleteLater()
        self.actions = []
        for dock in (self.records_dock, self.traverse_dock):
            if dock is not None:
                if hasattr(dock, "cleanup"):
                    dock.cleanup()
                self.iface.removeDockWidget(dock)
                dock.deleteLater()
        self.records_dock = self.traverse_dock = None
        if self.toolbar is not None:
            self.toolbar.deleteLater()
            self.toolbar = None
        if self.manager is not None:
            self.manager.deleteLater()
            self.manager = None

    # ------------------------------------------------------------------
    def _need_fabric(self):
        fab = self.manager.active
        if fab is None:
            QMessageBox.information(self.iface.mainWindow(), "Parcel Fabric",
                                    "There's no parcel fabric in this project yet. "
                                    "Use New Fabric or Add Fabric to Map first.")
        return fab

    def new_fabric(self):
        from .core import schema as S
        from .core.fabric import Fabric
        from .ui.fabric_dialog import NewFabricDialog
        dlg = NewFabricDialog(self.iface.mainWindow())
        if not dlg.exec():
            return
        v = dlg.result_value
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            S.create_fabric(v["provider"], v["source"], v["schema"], v["prefix"], v["types"],
                            v["crs"], v["settings"])
            fab = Fabric.open(v["provider"], v["source"], v["schema"], v["prefix"])
            fab.add_to_project()
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self.iface.mainWindow(), "New Parcel Fabric", str(e))
            return
        QApplication.restoreOverrideCursor()
        self.manager.refresh()
        self.manager.set_active(fab.id)
        self.iface.messageBar().pushSuccess("Parcel Fabric", "Created '%s' with parcel types %s."
                                            % (fab.name, ", ".join(fab.parcel_types)))
        self.show_records()

    def open_fabric(self):
        from .core.fabric import Fabric
        from .ui.fabric_dialog import OpenFabricDialog
        dlg = OpenFabricDialog(self.iface.mainWindow())
        if not dlg.exec():
            return
        v = dlg.result_value
        try:
            fab = Fabric.open(v["provider"], v["source"], v["schema"], v["prefix"])
            if any(f.id == fab.id for f in self.manager.fabrics):
                QMessageBox.information(self.iface.mainWindow(), "Parcel Fabric",
                                        "That fabric is already in this project.")
                return
            fab.add_to_project()
        except Exception as e:
            QMessageBox.critical(self.iface.mainWindow(), "Add Parcel Fabric", str(e))
            return
        self.manager.refresh()
        self.manager.set_active(fab.id)
        self.show_records()

    def show_records(self):
        if self.records_dock is None:
            from .ui.records_dock import RecordsDock
            self.records_dock = RecordsDock(self.iface, self.manager)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.records_dock)
        self.records_dock.show()
        self.records_dock.raise_()

    def show_traverse(self):
        if self._need_fabric() is None:
            return
        if self.traverse_dock is None:
            from .ui.traverse_dock import TraverseDock
            self.traverse_dock = TraverseDock(self.iface, self.manager)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.traverse_dock)
        self.traverse_dock.show()
        self.traverse_dock.raise_()

    def build_active(self):
        fab = self._need_fabric()
        if fab is None:
            return
        rec = self.manager.active_record_id
        if not rec:
            QMessageBox.information(self.iface.mainWindow(), "Build",
                                    "Set an active record first (Records panel).")
            return
        from .core.build import build_parcels
        from .ui.issues_dialog import IssuesDialog
        issues, parts = [], []
        try:
            for t in fab.parcel_types:
                rep = build_parcels(fab, t, rec)
                if rep.created or rep.rebuilt or rep.points_added:
                    parts.append("%s: %s" % (t, rep.summary()))
                issues += [i for i in rep.issues if i.severity != "info"]
        except Exception as e:
            QMessageBox.critical(self.iface.mainWindow(), "Build", "Build failed: %s" % e)
            return
        self.iface.mapCanvas().refresh()
        msg = "; ".join(parts) or "Nothing new to build for the active record."
        if issues:
            IssuesDialog(self.iface, "Build Active Record", msg, issues, fab.records.crs()).show()
        else:
            self.iface.messageBar().pushSuccess("Parcel Fabric", msg)

    def _window(self, kind):
        if kind != "legal_writer" and self._need_fabric() is None:
            return
        old = self._windows.get(kind)
        try:
            if old is not None and old.isVisible():
                old.raise_()
                old.activateWindow()
                return
        except RuntimeError:          # deleted on close
            pass
        from .ui import edit_dialogs as ED
        from .ui.adjust_dialog import AdjustDialog
        from .ui.compare_dialog import CompareDialog
        from .ui.history_dialog import HistoryDialog
        from .ui.legal_writer_dialog import LegalWriterDialog
        from .ui.address_lots_dialog import AddressLotsDialog
        from .ui.exchange_dialog import ExchangeDialog
        from .ui.lineage_dialog import LineageDialog
        from .ui.stack_dialog import StackDialog
        cls = {"subdivide": ED.SubdivideWindow, "address": AddressLotsDialog, "exchange": ExchangeDialog,
               "split": ED.SplitWindow, "divide": ED.DivideWindow, "merge": ED.MergeWindow,
               "separate": ED.SeparateWindow, "stack": StackDialog,
               "copy_parallel": ED.CopyParallelWindow, "lineage": LineageDialog,
               "history": HistoryDialog, "legal_writer": LegalWriterDialog,
               "compare": CompareDialog, "adjust": AdjustDialog}[kind]
        w = cls(self.iface, self.manager)
        w.show()
        self._windows[kind] = w

    def assign_numbers(self):
        if self._need_fabric() is None:
            return
        from .ui.numbers_dialog import NumbersDialog
        dlg = NumbersDialog(self.iface, self.manager)
        dlg.show()
        self._numbers_dialog = dlg     # keep it alive while open

    def fabric_settings(self):
        fab = self._need_fabric()
        if fab is None:
            return
        from .ui.settings_dialog import SettingsDialog
        if SettingsDialog(fab, self.iface.mainWindow()).exec():
            self.manager.refresh()

    def check_fabric(self):
        fab = self._need_fabric()
        if fab is None or getattr(self, "_checking", False):
            return
        from .core.qa import run_checks
        from .ui.issues_dialog import IssuesDialog
        dlg = IssuesDialog(self.iface, "Check Fabric - %s" % fab.name,
                           "Checking the fabric…", [], fab.records.crs())
        self._checking = True
        dlg._running = True
        try:
            dlg.show()
            dlg.set_progress(0, "Starting Check Fabric…")
            issues = run_checks(fab, progress=dlg.set_progress)
        except Exception as e:
            dlg._running = False
            dlg.close()
            QMessageBox.critical(self.iface.mainWindow(), "Check Fabric", str(e))
            return
        finally:
            dlg._running = False
            self._checking = False
        if not issues:
            dlg.show_issues([], "No problems found.")
            return
        errs = sum(1 for i in issues if i.severity == "error")
        warns = sum(1 for i in issues if i.severity == "warning")
        dlg.show_issues(issues, "%d problem(s), %d warning(s), %d note(s)."
                        % (errs, warns, len(issues) - errs - warns))
        dlg.raise_()
        dlg.activateWindow()
