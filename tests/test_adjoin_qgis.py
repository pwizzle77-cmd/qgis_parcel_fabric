"""Traversing a parcel all the way round when it adjoins parcels already in the fabric."""
import math
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.test_qgis import X0, Y0, _Base, courses
from qgis.core import QgsFeatureRequest
from qgis.gui import QgsMapCanvas
from qgis.PyQt.QtWidgets import QWidget

from parcel_fabric.core import cogo as C
from parcel_fabric.core import mistake as M
from parcel_fabric.core.build import build_parcels
from parcel_fabric.core.fabric import CURRENT
from parcel_fabric.core.parcel_numbers import unfiltered
from parcel_fabric.core.qa import run_checks

N, E_, S_, W_ = "N 7-13-29 E", "S 82-46-31 E", "S 7-13-29 W", "N 82-46-31 W"
TILT = math.radians(7 + 13 / 60 + 29 / 3600)


def at(north, east):
    return (X0 + north * math.sin(TILT) + east * math.cos(TILT), Y0 + north * math.cos(TILT) - east * math.sin(TILT))


class AdjoinTests(_Base):
    def setUp(self):
        super().setUp()
        from parcel_fabric.ui import traverse_dock as T
        self.win, self.canvas, iface = QWidget(), QgsMapCanvas(), Mock()
        iface.mainWindow.return_value, iface.mapCanvas.return_value = self.win, self.canvas
        self.canvas.setDestinationCrs(self.fab.points.crs())
        self.mgr = SimpleNamespace(active=self.fab, active_record_id=self.rec, activeFabricChanged=Mock(),
                                   active_record=lambda: self.fab.record_by_id(self.mgr.active_record_id),
                                   fabricsChanged=Mock(), activeRecordChanged=Mock())
        self.dock = T.TraverseDock(iface, self.mgr)
        self.iface = iface
        for kind in ("warning", "critical", "information"):
            p = patch.object(T.QMessageBox, kind, side_effect=lambda *x: self.fail(x[2]))
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(T, "IssuesDialog")         # shown, not blocking
        p.start()
        self.addCleanup(p.stop)
        self.first = self.enter("First", at(0, 0), (N, 400), (E_, 300), (S_, 400), (W_, 300))

    def tearDown(self):
        self.dock.cleanup()
        super().tearDown()

    def preview(self, name, start, *calls):
        rec = self.fab.create_record(name, "Deed")
        self.mgr.active_record_id = rec
        self.dock.x.setText(repr(start[0]))
        self.dock.y.setText(repr(start[1]))
        self.dock.load_courses(courses(*calls))
        self.dock._create()
        return rec

    def enter(self, name, start, *calls):
        rec = self.preview(name, start, *calls)
        self.assertTrue(self.dock.save_btn.isEnabled(), self.dock._staged_why or self.dock.result_lbl.text())
        self.dock._save()
        self.assertFalse(any(lx.isEditable() for lx in self.fab.edit_layers()))
        return rec

    def counts(self):
        return tuple(sum(1 for _ in lx.getFeatures()) for lx in (self.fab.parcels, self.fab.lines, self.fab.points))

    def areas(self):
        return sorted(round(f.geometry().area(), 1) for f in self.fab.parcels.getFeatures(
            QgsFeatureRequest().setFilterExpression(CURRENT)))

    def sound(self):
        """No duplicate lines, dangles or parcels that don't build; every record rebuilds cleanly."""
        self.assertEqual([i.message for i in run_checks(self.fab, ["Tax"]) if i.severity == "error"], [])
        before = self.areas()
        for rec in {f["CreatedByRecord"] for f in self.fab.lines.getFeatures()}:
            rep = build_parcels(self.fab, "Tax", rec)
            self.assertEqual((rep.created, [i.message for i in rep.issues if i.severity == "error"]), (0, []))
        self.assertEqual(self.areas(), before)

    def test_whole_side_shared(self):
        self.enter("East", at(0, 300), (N, 400), (E_, 100), (S_, 400), (W_, 100))
        self.assertEqual(self.counts(), (2, 7, 6))
        self.assertEqual(self.areas(), [40000, 120000])
        self.assertIn("1 course(s) use existing lines", self.iface.messageBar().pushSuccess.call_args_list[-2][0][1])
        self.sound()

    def test_shared_side_entered_either_way_round(self):
        self.enter("East", at(400, 300), (S_, 400), (E_, 100), (N, 400), (W_, 100))
        self.assertEqual(self.counts(), (2, 7, 6))
        self.sound()

    def test_part_of_a_neighbours_side(self):
        rec = self.enter("East", at(100, 300), (N, 150), (E_, 100), (S_, 150), (W_, 100))
        self.assertEqual(self.counts(), (2, 9, 8))              # the 400 ft side is now three lines
        self.assertEqual(self.areas(), [15000, 120000])
        halves = [f for f in self.fab.lines.getFeatures() if f["ParentLineID"]]
        self.assertEqual(sorted(round(f["Distance"], 3) for f in halves), [100, 150, 150])
        self.assertTrue(all(f["CreatedByRecord"] == rec for f in halves))
        self.sound()

    def test_longer_than_the_neighbours_side(self):
        rec = self.enter("East", at(-50, 300), (N, 600), (E_, 100), (S_, 600), (W_, 100))
        self.assertEqual(self.areas(), [60000, 120000])
        mine = sorted(round(f["Distance"], 3) for f in self.fab.lines.getFeatures() if f["CreatedByRecord"] == rec)
        self.assertEqual(mine, [50, 100, 100, 150, 600])        # the call's two ends, in proportion
        self.sound()

    def test_corner_touches_a_neighbours_side(self):
        """A T: the new parcel only meets the neighbour's line at one corner."""
        self.enter("South", at(0, 100), (S_, 80), (E_, 60), (N, 80), (W_, 60))
        self.assertEqual(self.areas(), [4800, 120000])
        self.sound()

    def test_neighbour_corner_along_my_side(self):
        self.enter("E1", at(0, 300), (N, 200), (E_, 100), (S_, 200), (W_, 100))
        self.enter("E2", at(200, 300), (N, 200), (E_, 100), (S_, 200), (W_, 100))
        self.enter("Far", at(0, 400), (N, 400), (E_, 100), (S_, 400), (W_, 100))     # one call along two lots
        self.assertEqual(self.areas(), [20000, 20000, 40000, 120000])
        self.sound()

    def test_gap_closed_entirely_by_neighbours(self):
        self.enter("N", at(400, 0), (N, 100), (E_, 500), (S_, 100), (W_, 500))
        self.enter("E", at(0, 400), (N, 400), (E_, 100), (S_, 400), (W_, 100))
        self.enter("S", at(-100, 0), (N, 100), (E_, 500), (S_, 100), (W_, 500))
        lines = self.counts()[1]
        rec = self.enter("Gap", at(0, 300), (N, 400), (E_, 100), (S_, 400), (W_, 100))
        self.assertEqual(self.counts()[1], lines)               # nothing to add
        self.assertEqual(sum(1 for f in self.fab.parcels.getFeatures() if f["CreatedByRecord"] == rec), 1)
        self.sound()

    def test_small_misfit_is_held_and_record_dimensions_kept(self):
        s = at(0, 300)
        rec = self.enter("East", (s[0] + 0.2, s[1] - 0.1), (N, 400.15), (E_, 100), (S_, 400.15), (W_, 100))
        self.assertEqual(self.counts(), (2, 7, 6))
        mine = sorted(round(f["Distance"], 2) for f in self.fab.lines.getFeatures() if f["CreatedByRecord"] == rec)
        self.assertEqual(mine, [100, 100, 400.15])
        self.assertIn("largest adjustment 0.2", self.iface.messageBar().pushSuccess.call_args_list[-2][0][1])
        self.sound()

    def test_misfit_beyond_the_fit_distance_is_not_forced(self):
        s = at(0, 300)
        self.preview("East", (s[0] + 3, s[1]), (N, 400), (E_, 100), (S_, 400), (W_, 100))
        self.assertTrue(self.dock.save_btn.isEnabled())         # a 3 ft gap is its own parcel edge
        self.dock._clear()
        self.preview("Lap", (s[0] - 3, s[1]), (N, 380), (E_, 100), (S_, 380), (W_, 100))
        self.assertFalse(self.dock.save_btn.isEnabled())        # a 3 ft overlap has to be resolved
        self.dock._clear()
        self.assertEqual(self.counts(), (1, 4, 4))

    def test_curved_shared_line(self):
        r = 100.0
        self.enter("Curve", at(0, 1000), ("N 0 E", 100), ("curve", "N 90 E", r, math.pi * r / 2, C.DIR_TANGENT),
                   ("S 90 W", 100))
        x, y = at(0, 1000)
        self.enter("Outside", (x + 100, y), ("N 0 E", 100), ("S 90 W", 100),
                   ("curve", "N 90 E", r, math.pi * r / 2, C.DIR_TANGENT))
        curves = [f for f in self.fab.lines.getFeatures() if f["Radius"]]
        self.assertEqual(len(curves), 1)
        self.sound()

    def test_save_is_all_or_nothing_and_clear_puts_the_neighbour_back(self):
        def state():
            return [sorted(tuple(str(v) for v in f.attributes()) + (f.geometry().asWkt(6),)
                           for f in unfiltered(lx).getFeatures()) for lx in (self.fab.parcels, self.fab.lines, self.fab.points)]
        before = state()
        self.preview("East", at(100, 300), (N, 150), (E_, 100), (S_, 150), (W_, 100))
        self.assertEqual(sum(1 for _ in self.fab.parcels.getFeatures()), 2)
        self.dock._clear()
        self.assertEqual(state(), before)
        self.assertEqual(self.counts(), (1, 4, 4))
        self.assertFalse(any(lx.isEditable() for lx in self.fab.edit_layers()))

    def test_remove_mistake_restores_the_neighbours_line(self):
        rec = self.enter("East", at(100, 300), (N, 150), (E_, 100), (S_, 150), (W_, 100))
        M.apply(self.fab, rec, False)
        self.assertEqual(self.counts(), (1, 4, 4))
        self.assertEqual(self.areas(), [120000])
        self.sound()
        rec = self.enter("East", at(0, 300), (N, 400), (E_, 100), (S_, 400), (W_, 100))
        with self.assertRaises(M.MistakeError):
            M.apply(self.fab, self.first, False)                # the neighbour now uses its line
        M.apply(self.fab, rec, False)
        self.assertEqual(self.counts(), (1, 4, 4))

    def test_other_parcel_types_are_left_alone(self):
        self.dock.target.setCurrentText("Lot")
        self.enter("Lot", at(0, 300), (N, 400), (E_, 100), (S_, 400), (W_, 100))
        self.assertEqual(sum(1 for f in self.fab.lines.getFeatures() if f["ParcelType"] == "Lot"), 4)
        self.assertEqual(sum(1 for f in self.fab.lines.getFeatures() if f["ParcelType"] == "Tax"), 4)

    def test_open_traverse_reuses_too(self):
        self.dock.closed.setChecked(False)
        self.preview("Open", at(0, 300), (N, 400), (E_, 100), (S_, 400), (W_, 100))
        self.assertEqual(sum(1 for _ in self.fab.lines.getFeatures()), 7)
        self.dock._clear()

    def test_changing_the_fit_distance_needs_a_new_preview(self):
        self.preview("East", at(0, 300), (N, 400), (E_, 100), (S_, 400), (W_, 100))
        self.assertTrue(self.dock.save_btn.isEnabled())
        self.dock.fit.setValue(2)
        self.assertFalse(self.dock.save_btn.isEnabled())
        self.dock._clear()


del _Base
