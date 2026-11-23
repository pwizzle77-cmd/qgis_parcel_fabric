"""Remove mistake and the traverse Preview / Save gate, on a real GeoPackage."""
import math
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.test_qgis import USER_LEGAL, X0, Y0, _Base, courses
from qgis.gui import QgsMapCanvas
from qgis.PyQt.QtWidgets import QWidget

from parcel_fabric.core import mistake as M
from parcel_fabric.core import safeguards
from parcel_fabric.core import units as U
from parcel_fabric.core.build import build_parcels
from parcel_fabric.core.fabric import EditSession
from parcel_fabric.core.parcel_numbers import unfiltered

SQUARE = (("N 0 E", 100), ("N 90 E", 100), ("S 0 E", 100), ("S 90 W", 100))


class _Two(_Base):
    def setUp(self):
        super().setUp()
        self.other = self.fab.create_record("Other deed", "Deed")

    def counts(self):
        return tuple(unfiltered(lx).featureCount() for lx in (self.fab.parcels, self.fab.lines, self.fab.points))

    def add(self, rec, calls, start=(X0, Y0), build=True):
        self.rec, keep = rec, self.rec
        self.traverse("Tax", courses(*calls), start)
        self.rec = keep
        if build:
            rep = build_parcels(self.fab, "Tax", rec)
            self.assertEqual(rep.created, 1, rep.issues)

    def clear(self, rec):
        return M.apply(self.fab, rec, False)


class ClearTests(_Two):
    def test_clears_only_this_record_and_keeps_the_record(self):
        self.add(self.other, SQUARE, (X0 + 1000, Y0))
        self.add(self.rec, SQUARE)
        self.add(self.rec, SQUARE[:3], (X0 + 500, Y0), build=False)      # junk that never closed
        self.assertEqual(self.counts(), (2, 11, 12))
        self.clear(self.rec)
        self.assertEqual(self.counts(), (1, 4, 4))
        rec = self.fab.record_by_id(self.rec)
        self.assertEqual(rec["ParcelCount"], 0)
        self.assertTrue(rec.geometry().isNull() or rec.geometry().isEmpty())
        self.assertEqual(self.fab.record_by_id(self.other)["ParcelCount"], 1)

    def test_shared_corners_stay_and_duplicate_lines_go(self):
        self.add(self.other, SQUARE)
        self.add(self.rec, SQUARE, (X0 + 100, Y0), build=False)          # its west line lies on the neighbour
        self.assertEqual(self.counts(), (1, 8, 6))
        self.clear(self.rec)
        self.assertEqual(self.counts(), (1, 4, 4))

    def test_refuses_when_a_neighbour_parcel_needs_a_line(self):
        for tilt in ("N 0 E", "N 7-13-29 E"):           # a line off the grid must be recognised too
            with self.subTest(tilt):
                a = U.parse_bearing(tilt)
                n, e, s, w = (U.azimuth_to_bearing(a + k * math.pi / 2, 2) for k in range(4))
                o = (X0 + (2000 if a else 0), Y0)
                mine = self.fab.create_record("mine " + tilt, "Deed")
                theirs = self.fab.create_record("theirs " + tilt, "Deed")
                self.add(mine, ((n, 100), (e, 100), (s, 100), (w, 100)), o)
                self.add(theirs, ((e, 100), (n, 100), (w, 100)),
                         (o[0] + 100 * math.cos(a), o[1] - 100 * math.sin(a)))
                before = self.counts()
                with self.assertRaisesRegex(M.MistakeError, "still part of another parcel"):
                    self.clear(mine)
                self.assertEqual(self.counts(), before)

    def test_brings_back_what_it_retired(self):
        self.add(self.other, SQUARE)
        with EditSession([self.fab.parcels, self.fab.lines], "retire") as ed:
            for lx in (self.fab.parcels, self.fab.lines):
                for f in lx.getFeatures():
                    ed.change(lx, f.id(), "RetiredByRecord", self.rec)
        self.assertEqual(self.fab.parcels.featureCount(), 0)
        self.clear(self.rec)
        self.assertEqual(sum(1 for _ in self.fab.parcels.getFeatures()), 1)
        self.assertEqual(sum(1 for _ in self.fab.lines.getFeatures()), 4)
        self.assertEqual(self.fab.record_by_id(self.other)["ParcelCount"], 1)

    def test_refuses_when_a_later_record_changed_it(self):
        self.add(self.rec, SQUARE)
        p = next(self.fab.parcels.getFeatures())
        with EditSession([self.fab.parcels], "retire") as ed:
            ed.change(self.fab.parcels, p.id(), "RetiredByRecord", self.other)
        with self.assertRaisesRegex(M.MistakeError, "later record"):
            self.clear(self.rec)

    def test_refuses_unsaved_edits(self):
        self.add(self.rec, SQUARE)
        self.fab.lines.startEditing()
        with self.assertRaisesRegex(M.MistakeError, "Save or roll back"):
            self.clear(self.rec)
        self.fab.lines.rollBack()

    def test_works_under_the_database_rules_and_leaves_them_on(self):
        self.add(self.rec, SQUARE)
        self.add(self.other, SQUARE, (X0 + 1000, Y0))
        safeguards.run(self.fab)
        self.clear(self.rec)
        self.assertEqual(self.counts(), (1, 4, 4))
        with self.assertRaisesRegex(RuntimeError, "retired features are history"):
            with EditSession([self.fab.lines]):
                self.fab.lines.deleteFeatures([f.id() for f in self.fab.lines.getFeatures()])
        self.assertEqual(self.counts(), (1, 4, 4))

    def test_button_asks_first(self):
        from parcel_fabric.ui import records_dock as R
        self.add(self.rec, SQUARE)
        win, canvas = QWidget(), QgsMapCanvas()
        iface = Mock()
        iface.mainWindow.return_value, iface.mapCanvas.return_value = win, canvas
        manager = SimpleNamespace(active=self.fab, fabrics=[self.fab], active_record_id=self.rec,
                                  active_record=lambda: self.fab.record_by_id(self.rec), fabricsChanged=Mock(),
                                  activeFabricChanged=Mock(), activeRecordChanged=Mock())
        dock = R.RecordsDock(iface, manager)
        B = R.QMessageBox.StandardButton
        seen = []
        for kind in ("warning", "critical"):        # a real box would block the test run
            p = patch.object(R.QMessageBox, kind, side_effect=lambda *x: self.fail(x[2]))
            p.start()
            self.addCleanup(p.stop)

        def press(button):
            return lambda box: seen.append(box.text()) or button
        with patch.object(dock, "_selected_gid", return_value=self.rec):
            with patch.object(R.QMessageBox, "exec", new=press(B.Cancel)):
                dock._remove_mistake()
            self.assertIn("1 parcel(s), 4 line(s), 4 point(s)", seen[0])
            self.assertEqual(self.counts(), (1, 4, 4))
            with patch.object(R.QMessageBox, "exec", new=press(B.Ok)):
                dock._remove_mistake()
        self.assertEqual(self.counts(), (0, 0, 0))
        dock._watched.dataChanged.disconnect(dock._fill)


class SaveGateTests(_Two):
    def setUp(self):
        super().setUp()
        from parcel_fabric.ui.traverse_dock import TraverseDock
        self.win, self.canvas, iface = QWidget(), QgsMapCanvas(), Mock()
        iface.mainWindow.return_value, iface.mapCanvas.return_value = self.win, self.canvas
        self.canvas.setDestinationCrs(self.fab.points.crs())
        self.manager = SimpleNamespace(active=self.fab, active_record_id=self.rec,
                                       active_record=lambda: self.fab.record_by_id(self.manager.active_record_id),
                                       activeFabricChanged=Mock(), fabricsChanged=Mock(), activeRecordChanged=Mock())
        self.dock = TraverseDock(iface, self.manager)
        self.dock.x.setText(str(X0))
        self.dock.y.setText(str(Y0))
        box = patch("parcel_fabric.ui.traverse_dock.QMessageBox")
        self.box = box.start()
        self.addCleanup(box.stop)

    def tearDown(self):
        self.dock.cleanup()
        super().tearDown()

    def load(self, *calls):
        self.dock.load_courses(courses(*calls))

    def blocked(self, text):
        """It can be previewed, never saved."""
        self.assertIn(text, self.dock.result_lbl.text())
        self.dock._create()
        self.assertFalse(self.dock.save_btn.isEnabled())
        self.dock._save()
        self.assertEqual(self.counts(), (0, 0, 0))
        self.dock._clear()
        self.assertFalse(any(lx.isEditable() for lx in self.fab.edit_layers()))

    def save(self):
        self.dock._create()
        self.assertTrue(self.dock.save_btn.isEnabled(), self.box.mock_calls)
        self.dock._save()

    def test_good_traverse_is_preview_until_saved(self):
        self.load(*SQUARE)
        self.dock._create()
        self.assertEqual(sum(1 for _ in self.fab.lines.getFeatures()), 4)      # on the map
        self.assertEqual(self.counts(), (0, 0, 0))                            # not in the file
        self.dock._clear()
        self.assertEqual(sum(1 for _ in self.fab.lines.getFeatures()), 0)
        self.assertEqual(len(self.dock.courses), 4)                           # Clear leaves the courses
        self.save()
        self.assertEqual(self.counts(), (1, 4, 4))
        self.assertFalse(self.dock.save_btn.isEnabled())
        self.assertFalse(self.fab.lines.isEditable())

    def test_a_preview_goes_out_of_date(self):
        self.load(*SQUARE)
        self.dock._create()
        item = self.dock.table.item(0, 2)
        old = item.text()
        item.setText("140")
        self.assertFalse(self.dock.save_btn.isEnabled())
        self.dock._save()
        self.assertEqual(self.counts(), (0, 0, 0))
        item.setText(old)
        self.assertTrue(self.dock.save_btn.isEnabled())
        self.dock._clear()

    def test_bad_misclose(self):
        self.load(*(SQUARE[:3] + (("S 90 W", 95),)))
        self.blocked("misclose")
        self.dock.adjust.setCurrentIndex(1)                    # adjusting doesn't make it good
        self.blocked("misclose")

    def test_never_closes(self):
        self.load(*SQUARE[:3])
        self.blocked("misclose")
        self.dock.closed.setChecked(False)                     # nor does calling it open
        self.blocked("existing fabric points")
        self.blocked("existing fabric points")                 # its own previewed points don't count

    def test_small_gap_needs_compass_rule(self):
        self.load(("N 0 E", 1000), ("N 90 E", 1000), ("S 0 E", 1000), ("S 90 W", 999.8))
        self.blocked("Compass rule")
        self.dock.adjust.setCurrentIndex(1)
        self.save()
        self.assertEqual(self.counts(), (1, 4, 4))
        self.assertIn(999.8, [f["Distance"] for f in self.fab.lines.getFeatures()])

    def test_crossing_courses(self):
        self.load(("N 45 E", 100), ("S 0 E", 70.7107), ("N 45 W", 100), ("S 0 E", 70.7107))
        self.blocked("cross")

    def test_no_active_record(self):
        self.manager.active_record_id = None
        self.load(*SQUARE)
        self.blocked("active record")

    def test_open_traverse_between_existing_points(self):
        self.add(self.other, SQUARE)
        self.dock.x.setText(str(X0 + 100))
        self.dock.closed.setChecked(False)
        self.load(("N 90 E", 100), ("N 0 E", 100), ("S 90 W", 100))
        self.save()
        self.assertEqual(self.counts(), (1, 7, 6))

    def test_unbuildable_parcel_cannot_be_saved(self):
        self.load(*SQUARE)
        self.save()
        self.dock._create()                                    # the same lines again can't build
        self.assertFalse(self.dock.save_btn.isEnabled())
        self.assertIn("couldn't be built", self.dock._staged_why)
        self.dock._clear()
        self.assertEqual(self.counts(), (1, 4, 4))
        self.assertFalse(self.fab.lines.isEditable() or self.fab.parcels.isEditable())

    def test_clear_leaves_the_users_own_edits(self):
        self.load(*SQUARE)
        self.save()
        for lx in (self.fab.lines, self.fab.points, self.fab.parcels, self.fab.records):
            lx.startEditing()
        f = next(self.fab.points.getFeatures())
        self.fab.points.changeAttributeValue(f.id(), self.fab.points.fields().indexOf("Name"), "mine")
        self.dock._create()
        self.dock._clear()
        self.assertEqual(sum(1 for _ in self.fab.lines.getFeatures()), 4)
        self.assertEqual(self.fab.points.getFeature(f.id())["Name"], "mine")
        for lx in (self.fab.lines, self.fab.points, self.fab.parcels, self.fab.records):
            lx.rollBack()

    def test_failed_save_stops_the_rest(self):
        self.load(*SQUARE)
        self.dock._create()
        with patch.object(type(self.fab.parcels), "commitChanges", new=lambda lx, *a: False):
            self.dock._save()
        self.assertIn("Save failed", self.box.critical.call_args[0][2])
        self.assertEqual(self.counts(), (0, 0, 0))
        self.assertFalse(any(lx.isEditable() for lx in self.fab.edit_layers()))

    def test_reported_legal_saves(self):
        from parcel_fabric.core.legal_import import parse
        self.dock.load_courses(parse(USER_LEGAL, self.fab.foot_m)[1])
        self.save()
        self.assertEqual(self.counts(), (1, 6, 6), self.box.mock_calls)
        p = next(self.fab.parcels.getFeatures())
        self.assertAlmostEqual(p.geometry().area() / 43560.0, 47.741, delta=0.001)


del _Base, _Two
