"""Splitting a parcel by a deed's calls, on a real GeoPackage and a parcel that isn't square to the grid."""
import math
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.test_qgis import USER_LEGAL, X0, Y0, _Base, courses
from qgis.core import QgsFeatureRequest
from qgis.gui import QgsMapCanvas
from qgis.PyQt.QtCore import QDate
from qgis.PyQt.QtWidgets import QWidget

from parcel_fabric.core import cogo as C
from parcel_fabric.core.build import build_parcels
from parcel_fabric.core.clear import apply_clear, plan_clear
from parcel_fabric.core.deed_split import apply_deed_split, plan_deed_split
from parcel_fabric.core.edits import EditError
from parcel_fabric.core.fabric import CURRENT, EditSession
from parcel_fabric.core.parcel_numbers import unfiltered

N, E_, S_, W_ = "N 1-41-12 W", "N 88-18-48 E", "S 1-41-12 E", "S 88-18-48 W"
TILT = math.radians(-(1 + 41 / 60 + 12 / 3600))


def at(north, east):
    """Map point ``north``/``east`` feet from the parent's SW corner, along its own sides."""
    return (X0 + north * math.sin(TILT) + east * math.cos(TILT), Y0 + north * math.cos(TILT) - east * math.sin(TILT))


class _Deed(_Base):
    """A 500 x 400 ft parent (number 12-4S-16-02942-000) and a quitclaim deed as the active record."""

    def setUp(self):
        super().setUp()
        self.fab.update_record(self.rec, date=QDate(1998, 4, 2))
        self.traverse("Tax", courses((N, 400), (E_, 500), (S_, 400), (W_, 500)))
        self.assertEqual(build_parcels(self.fab, "Tax", self.rec).created, 1)
        self.parent = next(self.fab.parcels.getFeatures())
        with EditSession([self.fab.parcels]) as ed:
            ed.change(self.fab.parcels, self.parent.id(), "ParcelNumber", "12-4S-16-02942-000")
        self.parent = next(self.fab.parcels.getFeatures())
        self.qcd = self.fab.create_record("ORB 1500 PG 22", "Deed", date=QDate(2020, 5, 6))

    def legs(self, start, *calls):
        return C.compute(start, courses(*calls)).legs

    def plan(self, start, *calls, fit=1.0):
        return plan_deed_split(self.fab, self.parent, self.legs(start, *calls), fit)

    def parcels(self):
        return {f["ParcelNumber"]: f for f in self.fab.parcels.getFeatures(
            QgsFeatureRequest().setFilterExpression(CURRENT)) if not f["RetiredByRecord"]}

    def state(self):
        out = []
        for lx in (self.fab.parcels, self.fab.lines, self.fab.points, self.fab.lineage):
            fs = [tuple(str(v) for v in f.attributes()) + (f.geometry().asWkt(6) if f.hasGeometry() else "",)
                  for f in unfiltered(lx).getFeatures()]
            out.append(sorted(fs))
        return out

    def nw(self, a=200, b=150):         # the NW corner lot, by deed calls that don't quite fit
        return at(400, 0), (E_, a), (S_, b), (W_, a), (N, b)


class DeedSplitTests(_Deed):
    def test_remainder_keeps_number_and_deed_dimensions_are_kept(self):
        plan = self.plan(*self.nw(200.12, 149.93))
        self.assertEqual((len(plan.inside), len(plan.along)), (2, 2))
        res = apply_deed_split(self.fab, plan, self.qcd)
        self.assertEqual(res.parents_retired, 0)
        got = self.parcels()
        self.assertEqual(sorted(got), ["12-4S-16-02942-000", "12-4S-16-02942-001"])
        kid, rem = got["12-4S-16-02942-001"], got["12-4S-16-02942-000"]
        self.assertAlmostEqual(kid.geometry().area(), 200.12 * 149.93, delta=0.5)
        self.assertAlmostEqual(kid.geometry().area() + rem.geometry().area(), 200000, places=3)
        self.assertEqual((kid["CreatedByRecord"], rem["CreatedByRecord"]), (self.qcd, self.rec))
        self.assertEqual(rem["GlobalID"], self.parent["GlobalID"])
        # the deed's calls are on its lines, including the halves of the parent's boundary it follows
        mine = [f for f in self.fab.lines.getFeatures() if f["CreatedByRecord"] == self.qcd]
        self.assertEqual(sorted(round(f["Distance"], 2) for f in mine), [149.93, 149.93, 200.12, 200.12, 250.07, 299.88])
        self.assertAlmostEqual(kid["CalculatedArea"], 200.12 * 149.93, delta=0.01)
        self.assertLess(kid["MiscloseDistance"], 0.01)
        self.assertEqual(kid["QPF_StartDate"], QDate(2020, 5, 6))
        self.assertEqual(sum(1 for _ in self.fab.lineage.getFeatures()) if not self.fab.lineage.subsetString() else 1, 1)
        self.assertFalse(any(lx.isEditable() for lx in self.fab.edit_layers()))

    def test_corners_are_held_to_the_senior_boundary(self):
        start, *calls = self.nw()
        plan = self.plan((start[0] + 0.4, start[1] - 0.3), *calls)
        self.assertAlmostEqual(max(m[1] for m in plan.moved), 0.5, places=6)
        apply_deed_split(self.fab, plan, self.qcd)
        kid = self.parcels()["12-4S-16-02942-001"]
        self.assertLess(min(math.dist(start, (v.x(), v.y())) for v in kid.geometry().vertices()), 1e-6)

    def test_all_pieces_new_retires_the_parent(self):
        res = apply_deed_split(self.fab, self.plan(*self.nw()), self.qcd, remainder_keeps=False)
        self.assertEqual(res.parents_retired, 1)
        self.assertEqual(sorted(self.parcels()), ["12-4S-16-02942-001", "12-4S-16-02942-002"])

    def test_small_remainder_still_keeps_the_number(self):
        plan = self.plan(at(400, 0), (E_, 500), (S_, 350), (W_, 500), (N, 350))     # conveys most of it
        apply_deed_split(self.fab, plan, self.qcd)
        got = self.parcels()
        self.assertAlmostEqual(got["12-4S-16-02942-000"].geometry().area(), 50 * 500, places=2)
        self.assertAlmostEqual(got["12-4S-16-02942-001"].geometry().area(), 350 * 500, places=2)

    def test_lot_in_the_middle_leaves_a_hole(self):
        plan = self.plan(at(300, 200), (E_, 100), (S_, 100), (W_, 100), (N, 100))
        self.assertEqual((len(plan.inside), len(plan.along)), (4, 0))
        apply_deed_split(self.fab, plan, self.qcd)
        rem = self.parcels()["12-4S-16-02942-000"]
        self.assertAlmostEqual(rem.geometry().area(), 190000, places=2)
        self.assertAlmostEqual(rem["CalculatedArea"], 190000, places=2)

    def test_strip_leaves_two_remainders(self):
        plan = self.plan(at(250, 0), (E_, 500), (S_, 100), (W_, 500), (N, 100))
        self.assertEqual(len(plan.remainders), 2)
        apply_deed_split(self.fab, plan, self.qcd)
        self.assertEqual(sorted(round(f.geometry().area()) for f in self.parcels().values()), [50000, 75000, 75000])

    def test_curved_deed_course(self):
        r = 100.0
        plan = self.plan(at(400, 0), (E_, 200), (S_, 100), ("curve", S_, r, math.pi * r / 2, C.DIR_TANGENT),
                         (W_, 100), (N, 200))
        apply_deed_split(self.fab, plan, self.qcd)
        kid = self.parcels()["12-4S-16-02942-001"]
        self.assertTrue(kid.geometry().constGet().hasCurvedSegments())
        self.assertAlmostEqual(kid["CalculatedArea"], 200 * 100 + 100 * 100 + math.pi * r * r / 4, places=1)

    def test_refusals_change_nothing(self):
        before = self.state()
        for text, start, calls in (
                ("outside the parcel", at(400, 0), ((E_, 200), (N, 50), (W_, 200), (S_, 50))),
                ("whole parcel", at(400, 0), ((E_, 500), (S_, 400), (W_, 500), (N, 400)))):
            with self.assertRaisesRegex(EditError, text):
                self.plan(start, *calls)
        self.assertEqual(self.state(), before)

    def test_course_that_leaves_a_notched_parent_is_refused(self):
        rec = self.fab.create_record("notch", "Plat")
        self.rec = rec
        o = (X0 + 3000, Y0)
        self.traverse("Tax", courses(("N 0 E", 300), ("N 90 E", 100), ("S 0 E", 200), ("N 90 E", 100), ("N 0 E", 200),
                                     ("N 90 E", 100), ("S 0 E", 300), ("S 90 W", 300)), o)
        build_parcels(self.fab, "Tax", rec)
        self.parent = next(f for f in self.fab.parcels.getFeatures() if f["CreatedByRecord"] == rec)
        with self.assertRaisesRegex(EditError, "leaves the parcel"):
            self.plan((o[0] + 50, o[1] + 250), ("N 90 E", 200), ("S 0 E", 200), ("S 90 W", 200), ("N 0 E", 200))

    def test_failed_build_rolls_everything_back(self):
        before = self.state()
        plan = self.plan(*self.nw())
        with patch("parcel_fabric.core.edits._number_children", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                apply_deed_split(self.fab, plan, self.qcd)
        self.assertEqual(self.state(), before)
        self.assertFalse(any(lx.isEditable() for lx in self.fab.edit_layers()))

    def test_parent_that_closes_only_within_tolerance(self):
        from parcel_fabric.core.legal_import import parse
        o = (X0 + 5000, Y0)
        rec = self.fab.create_record("real", "Deed")
        res = C.compute(o, parse(USER_LEGAL, self.fab.foot_m)[1])
        self.assertGreater(res.misclose, 1e-4)
        self.fab.add_traverse("Tax", res.legs, rec)
        build_parcels(self.fab, "Tax", rec)
        self.parent = next(f for f in self.fab.parcels.getFeatures() if f["CreatedByRecord"] == rec)
        plan = self.plan(o, ("N 88-07-47 E", 888.56), ("S 1-41-12 E", 490.25), ("S 88-07-47 W", 888.56),
                         ("N 1-41-12 W", 490.25))
        self.assertEqual((len(plan.inside), len(plan.along)), (1, 3))
        apply_deed_split(self.fab, plan, self.qcd)
        self.assertEqual(sum(1 for f in self.fab.parcels.getFeatures() if f["CreatedByRecord"] == self.qcd), 1)
        apply_clear(self.fab, plan_clear(self.fab, self.qcd))
        got = next(f for f in self.fab.parcels.getFeatures() if f["CreatedByRecord"] == rec)
        self.assertAlmostEqual(got.geometry().area() / 43560, 47.741, delta=0.001)

    def test_one_failed_layer_stops_the_rest(self):
        before = self.state()
        plan = self.plan(*self.nw())
        with patch.object(type(self.fab.parcels), "commitChanges", new=lambda lx, *a: False):
            with self.assertRaisesRegex(RuntimeError, "Save failed"):
                apply_deed_split(self.fab, plan, self.qcd)
        self.assertEqual(self.state(), before)

    def test_neighbour_shares_the_split_line(self):
        nb = self.fab.create_record("neighbour", "Deed")
        self.rec = nb
        self.traverse("Tax", courses((N, 300), (E_, 500), (S_, 300)), at(400, 0))
        self.assertEqual(build_parcels(self.fab, "Tax", nb).created, 1)
        apply_deed_split(self.fab, self.plan(*self.nw()), self.qcd)
        rep = build_parcels(self.fab, "Tax", nb)
        self.assertEqual((rep.created, [i for i in rep.issues if i.severity == "error"]), (0, []))

    def test_remove_mistake_undoes_it(self):
        area = self.parent.geometry().area()
        for calls in (self.nw(), (at(400, 0), (E_, 500), (S_, 350), (W_, 500), (N, 350)),
                      (at(300, 200), (E_, 100), (S_, 100), (W_, 100), (N, 100))):
            self.parent = next(self.fab.parcels.getFeatures())
            apply_deed_split(self.fab, self.plan(*calls), self.qcd)
            self.assertEqual(len(self.parcels()), 2)
            apply_clear(self.fab, plan_clear(self.fab, self.qcd))
            got = self.parcels()
            self.assertEqual(list(got), ["12-4S-16-02942-000"])
            self.assertAlmostEqual(got["12-4S-16-02942-000"].geometry().area(), area, places=4)
            self.assertEqual(sum(1 for _ in self.fab.lines.getFeatures()), 4)
            self.assertEqual(sum(1 for _ in self.fab.points.getFeatures()), 4)

    def test_remove_mistake_refuses_after_a_later_split(self):
        apply_deed_split(self.fab, self.plan(*self.nw()), self.qcd)
        self.parent = self.parcels()["12-4S-16-02942-000"]
        later = self.fab.create_record("Later deed", "Deed")
        plan = self.plan(at(100, 0), (E_, 50), (S_, 100), (W_, 50), (N, 100))
        apply_deed_split(self.fab, plan, later)
        with self.assertRaisesRegex(EditError, "Later deed"):
            plan_clear(self.fab, self.qcd)
        apply_clear(self.fab, plan_clear(self.fab, later))
        apply_clear(self.fab, plan_clear(self.fab, self.qcd))
        self.assertAlmostEqual(self.parcels()["12-4S-16-02942-000"].geometry().area(), 200000, places=3)


class DeedDockTests(_Deed):
    def setUp(self):
        super().setUp()
        from parcel_fabric.ui import traverse_dock as T
        self.T = T
        self.win, self.canvas, iface = QWidget(), QgsMapCanvas(), Mock()
        iface.mainWindow.return_value, iface.mapCanvas.return_value = self.win, self.canvas
        self.canvas.setDestinationCrs(self.fab.points.crs())
        manager = SimpleNamespace(active=self.fab, active_record_id=self.qcd, activeFabricChanged=Mock(),
                                  active_record=lambda: self.fab.record_by_id(self.qcd),
                                  fabricsChanged=Mock(), activeRecordChanged=Mock())
        self.dock = T.TraverseDock(iface, manager)
        for kind in ("warning", "critical"):        # a real box would block the test run
            p = patch.object(T.QMessageBox, kind, side_effect=lambda *x: self.fail(x[2]))
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.dock.cleanup()
        super().tearDown()

    DEED = ("COMMENCE at the Southwest corner of Section 12; thence N 1°41'12\" W, a distance of 400.00 feet to the "
            "POINT OF BEGINNING; thence N 88°18'48\" E, a distance of 200.00 feet; thence S 1°41'12\" E, a distance "
            "of 150.00 feet; thence S 88°18'48\" W, a distance of 200.00 feet; thence N 1°41'12\" W, a distance of "
            "150.00 feet to the Point of Beginning.")

    def load_deed(self):
        from parcel_fabric.ui.legal_dialog import LegalDialog
        dlg = LegalDialog(self.fab.foot_m)
        dlg.text.setPlainText(self.DEED)
        dlg._extract()
        self.assertEqual((len(dlg.courses), len(dlg.tie)), (4, 1), dlg.status.text())
        self.dock.x.setText(repr(X0))           # the point of commencement
        self.dock.y.setText(repr(Y0))
        self.dock.load_courses(dlg.courses, dlg.tie)

    def test_quitclaim_with_a_commencement_tie(self):
        self.load_deed()
        self.assertLess(math.dist(self.dock._start(), at(400, 0)), 1e-6)
        self.dock.use_tie.setChecked(False)
        self.assertEqual(self.dock._start(), (X0, Y0))
        self.dock.use_tie.setChecked(True)
        self.assertTrue(self.dock.split_btn.isEnabled(), self.dock.result_lbl.text())
        self.fab.parcels.select(self.parent.id())
        seen = []

        def press(box):
            seen.append(box.text())
            next(b for b in box.buttons() if "keeps" in b.text()).click()
        with patch.object(self.T.QMessageBox, "exec", new=press):
            self.dock._split()
        self.assertIn("Deed piece: 0.689 acres by the deed, 0.689 fitted", seen[0])
        self.assertIn("Remainder: 3.903 acres", seen[0])
        self.assertEqual(sorted(self.parcels()), ["12-4S-16-02942-000", "12-4S-16-02942-001"])
        self.assertEqual(self.dock.tie, [])

    def test_cancel_and_no_selection_change_nothing(self):
        before = self.state()
        self.load_deed()
        with patch.object(self.T.QMessageBox, "information") as info:
            self.dock._split()
        self.assertIn("Select ONE parcel", info.call_args[0][2])
        self.fab.parcels.select(self.parent.id())
        with patch.object(self.T.QMessageBox, "exec", new=lambda box: None):
            self.dock._split()
        self.assertEqual(self.state(), before)


del _Base, _Deed
