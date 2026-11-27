"""One parcel number with several polygons, and converting single-polygon fabrics."""
import glob
import math
import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.test_qgis import X0, Y0, _Base, courses
from qgis.core import QgsFeatureRequest, QgsGeometry, QgsPointXY, QgsProject, QgsWkbTypes
from qgis.gui import QgsMapCanvas
from qgis.PyQt.QtWidgets import QWidget

from parcel_fabric.core import cogo as C
from parcel_fabric.core import edits as E
from parcel_fabric.core import mistake as M
from parcel_fabric.core import safeguards
from parcel_fabric.core import schema as S
from parcel_fabric.core.build import build_parcels
from parcel_fabric.core.deed_split import apply_deed_split, plan_deed_split
from parcel_fabric.core.fabric import CURRENT, EditSession, Fabric
from parcel_fabric.core.geomio import parts_of
from parcel_fabric.core.parcel_numbers import unfiltered
from parcel_fabric.core.qa import run_checks

N, E_, S_, W_ = "N 1-41-12 W", "N 88-18-48 E", "S 1-41-12 E", "S 88-18-48 W"
NUM = "12-4S-16-02942-000"


class _Multi(_Base):
    def setUp(self):
        super().setUp()
        self.a = self.make("A", 0, 100, 200)
        self.b = self.make("B", 1000, 100, 100)
        self.m = self.fab.create_record("Combination", "Deed")

    def make(self, name, dx, h, w):
        """A parcel, its record and its number; returns the record."""
        rec = self.fab.create_record(name, "Deed")
        self.rec = rec
        self.traverse("Tax", courses((N, h), (E_, w), (S_, h), (W_, w)), (X0 + dx, Y0))
        self.assertEqual(build_parcels(self.fab, "Tax", rec).created, 1)
        f = next(f for f in self.fab.parcels.getFeatures() if f["CreatedByRecord"] == rec)
        with EditSession([self.fab.parcels]) as ed:
            ed.change(self.fab.parcels, f.id(), "ParcelNumber", "12-4S-16-0%d-000" % (2942 + dx))
        return rec

    def now(self):
        return [f for f in self.fab.parcels.getFeatures(QgsFeatureRequest().setFilterExpression(CURRENT))
                if not f["RetiredByRecord"]]

    def join(self):
        E.merge_parcels(self.fab, self.now(), self.m, NUM, apart=True)
        got = self.now()
        self.assertEqual(len(got), 1)
        return got[0]

    def areas(self, f):
        return sorted(round(p.area(), 2) for p in parts_of(f.geometry()))

    def state(self):
        return [sorted(tuple(str(v) for v in f.attributes()) + (f.geometry().asWkt(5) if f.hasGeometry() else "",)
                       for f in unfiltered(lx).getFeatures())
                for lx in (self.fab.parcels, self.fab.lines, self.fab.points, self.fab.lineage)]

    def counts(self):
        return tuple(sum(1 for _ in lx.getFeatures()) for lx in (self.fab.parcels, self.fab.lines, self.fab.points))


class MultipartTests(_Multi):
    def test_join_makes_one_parcel_with_one_number(self):
        f = self.join()
        self.assertEqual(f["ParcelNumber"], NUM)
        self.assertEqual(self.areas(f), [10000, 20000])
        self.assertAlmostEqual(f["CalculatedArea"], 30000, places=2)
        self.assertEqual(self.fab.record_by_id(self.m)["ParcelCount"], 1)
        self.assertEqual(self.counts(), (1, 8, 8))              # no line or point was touched
        bad = [i.message for i in run_checks(self.fab, ["Tax"]) if i.severity == "error"]
        self.assertEqual(bad, [])

    def test_join_must_be_asked_for(self):
        before = self.state()
        with self.assertRaisesRegex(E.EditError, "don't all touch"):
            E.merge_parcels(self.fab, self.now(), self.m, NUM)
        self.assertEqual(self.state(), before)

    def test_touching_and_apart_together(self):
        az = math.radians(88 + 18 / 60 + 48 / 3600)
        self.rec = rec = self.fab.create_record("C", "Deed")        # beside A, sharing its east line
        self.traverse("Tax", courses((E_, 50), (N, 100), (W_, 50)), (X0 + 200 * math.sin(az), Y0 + 200 * math.cos(az)))
        self.assertEqual(build_parcels(self.fab, "Tax", rec).created, 1)
        f = self.fab.parcels
        self.assertEqual(len(self.now()), 3)
        E.merge_parcels(self.fab, self.now(), self.m, NUM, apart=True)
        self.assertEqual(len(self.now()), 1)
        self.assertEqual(self.areas(self.now()[0]), [10000, 25000])
        self.assertTrue(QgsWkbTypes.isMultiType(f.wkbType()))

    def test_rebuilding_one_loop_keeps_the_other_polygon(self):
        before = self.join()
        for rec in (self.a, self.b):
            rep = build_parcels(self.fab, "Tax", rec)
            self.assertEqual((rep.created, rep.rebuilt, rep.issues), (0, 1, []))
            f = self.now()
            self.assertEqual(len(f), 1)
            self.assertEqual(f[0]["GlobalID"], before["GlobalID"])
            self.assertEqual(self.areas(f[0]), [10000, 20000])
            self.assertAlmostEqual(f[0]["CalculatedArea"], 30000, places=2)

    def test_a_polygon_that_no_longer_closes_stays(self):
        self.join()
        line = next(f for f in self.fab.lines.getFeatures() if f["CreatedByRecord"] == self.b)
        with EditSession([self.fab.lines]):
            self.fab.lines.deleteFeature(line.id())
        rep = build_parcels(self.fab, "Tax", self.a)
        self.assertTrue(any("don't close" in i.message for i in rep.issues))
        self.assertEqual(self.areas(self.now()[0]), [10000, 20000])
        self.assertFalse(self.now()[0]["CalculatedArea"])

    def test_misclose_is_the_worst_polygons(self):
        line = next(f for f in self.fab.lines.getFeatures() if f["CreatedByRecord"] == self.b)
        with EditSession([self.fab.lines]) as ed:
            ed.change(self.fab.lines, line.id(), "Distance", line["Distance"] + 0.5)
        f = self.join()
        self.assertAlmostEqual(f["MiscloseDistance"], 0.5, places=3)

    def test_separate(self):
        self.join()
        res = E.separate_parcel(self.fab, self.now()[0], self.m)
        got = {f["ParcelNumber"]: f for f in self.now()}
        self.assertEqual(sorted(got), [NUM, NUM[:-3] + "001"])
        self.assertEqual(self.areas(got[NUM]), [20000])
        self.assertEqual(res.parents_retired, 0)
        self.assertEqual(self.counts()[1:], (8, 8))
        self.assertEqual(self.fab.record_by_id(self.m)["ParcelCount"], 2)
        with self.assertRaisesRegex(E.EditError, "single polygon"):
            E.separate_parcel(self.fab, got[NUM], self.m)

    def test_separate_into_all_new(self):
        self.join()
        E.separate_parcel(self.fab, self.now()[0], self.m, remainder_keeps=False)
        self.assertEqual(sorted(f["ParcelNumber"] for f in self.now()), [NUM[:-3] + "001", NUM[:-3] + "002"])

    def test_cutting_one_polygon_leaves_the_other_with_the_parent(self):
        self.join()
        cut = QgsGeometry.fromPolylineXY([QgsPointXY(X0 + 60, Y0 - 50), QgsPointXY(X0 + 60, Y0 + 200)])
        E.cut_parcel(self.fab, self.now()[0], [cut], self.rec2(), remainder_keeps=True)
        got = {f["ParcelNumber"]: f for f in self.now()}
        self.assertEqual(sorted(got), [NUM, NUM[:-3] + "001"])
        self.assertEqual(len(self.areas(got[NUM])), 2)
        self.assertEqual(self.areas(got[NUM])[0], 10000)
        self.assertAlmostEqual(sum(self.areas(got[NUM])) + sum(self.areas(got[NUM[:-3] + "001"])), 30000, places=1)

    def rec2(self):
        return self.fab.create_record("Split", "Deed")

    def test_a_cut_that_misses_is_refused(self):
        self.join()
        for x in (X0 + 500, X0 - 500):
            cut = QgsGeometry.fromPolylineXY([QgsPointXY(x, Y0 - 50), QgsPointXY(x, Y0 + 200)])
            with self.assertRaises(E.EditError):
                E.cut_parcel(self.fab, self.now()[0], [cut], self.m)
        with self.assertRaisesRegex(E.EditError, "Separate it first"):
            E.divide_cuts(self.now()[0].geometry(), 0.0)

    def test_deed_split_leaves_every_remaining_polygon_with_the_parent(self):
        parent = self.join()
        qcd = self.rec2()
        a = math.radians(-(1 + 41 / 60 + 12 / 3600))
        start = (X0 + 75 * math.sin(a), Y0 + 75 * math.cos(a))          # a strip across A
        for keeps in (True, False):
            with self.subTest(keeps):
                parent = self.now()[0]
                legs = C.compute(start, courses((E_, 200), (S_, 50), (W_, 200), (N, 50))).legs
                plan = plan_deed_split(self.fab, parent, legs, 1.0)
                self.assertEqual(len(plan.remainders), 3)
                apply_deed_split(self.fab, plan, qcd, keeps)
                got = sorted(self.areas(f) for f in self.now())
                self.assertEqual(got, [[5000, 5000, 10000], [10000]])
                M.apply(self.fab, qcd, False)
                self.assertEqual(self.areas(self.now()[0]), [10000, 20000])
                self.assertEqual(self.counts(), (1, 8, 8))

    def test_undo_join_and_separate(self):
        self.join()
        M.apply(self.fab, self.m, False)
        self.assertEqual(sorted(self.areas(f)[0] for f in self.now()), [10000, 20000])
        self.join()
        sep = self.rec2()
        E.separate_parcel(self.fab, self.now()[0], sep)
        self.assertEqual(len(self.now()), 2)
        M.apply(self.fab, sep, False)
        f = self.now()
        self.assertEqual(len(f), 1)
        self.assertEqual((f[0]["ParcelNumber"], self.areas(f[0])), (NUM, [10000, 20000]))

    def test_failed_join_changes_nothing(self):
        before = self.state()
        with patch("parcel_fabric.core.edits.L.add", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                E.merge_parcels(self.fab, self.now(), self.m, NUM, apart=True)
        self.assertEqual(self.state(), before)
        self.assertFalse(any(lx.isEditable() for lx in self.fab.edit_layers()))

    def test_remove_mistake_sees_a_joined_parcels_lines(self):
        self.join()
        with self.assertRaises(M.MistakeError):
            M.apply(self.fab, self.b, False)

    def test_closure_report_covers_every_polygon(self):
        from parcel_fabric.core.reports import closure_report
        self.join()
        path = os.path.join(self.tmp, "r.html")
        self.assertEqual(closure_report(self.fab, self.m, path), 1)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("Polygon 1 of 2", text)
        self.assertIn("Polygon 2 of 2", text)
        self.assertIn("0.689", text)                # 30,000 sq ft in all
        self.assertNotIn("class=bad>", text)

    def test_legal_description_covers_every_polygon(self):
        from parcel_fabric.ui.legal_writer_dialog import LegalWriterDialog
        f = self.join()
        self.fab.parcels.select(f.id())
        win, iface = QWidget(), Mock()
        iface.mainWindow.return_value, iface.mapCanvas.return_value = win, QgsMapCanvas()
        iface.activeLayer.return_value = self.fab.parcels
        dlg = LegalWriterDialog(iface, SimpleNamespace(active=self.fab))
        dlg.header.setPlainText("Two tracts of land:")
        dlg._write()
        text = dlg.out.toPlainText()
        self.assertTrue(text.startswith("Two tracts of land:\n\nTRACT 1"), text[:80])
        self.assertEqual((text.count("TRACT"), text.count("Containing")), (2, 2))
        self.assertIn("20,000 square feet", text)
        self.assertEqual(dlg.check.text().count("#1b5e20"), 2, dlg.check.text())
        dlg.close()

    def test_pro_import_keeps_multipart(self):
        from parcel_fabric.core import exchange as XC
        self.join()
        path2 = os.path.join(self.tmp, "new.gpkg")
        S.create_fabric("ogr", path2, "", "", ["Tax"], "EPSG:2238", {"plss_use_default": "0"})
        fab2 = Fabric.open("ogr", path2)
        src = self.fab.parcels.clone()
        src.setSubsetString(CURRENT)
        self.assertEqual(XC._copy(src, fab2.parcels, None), (1, 0))
        self.assertEqual(self.areas(next(fab2.parcels.getFeatures())), [10000, 20000])

    def test_curves_survive(self):
        rec = self.fab.create_record("curve", "Deed")
        self.rec = rec
        r = 100.0
        self.traverse("Tax", courses(("N 0 E", 100), ("curve", "N 90 E", r, math.pi * r / 2, C.DIR_TANGENT),
                                     ("S 90 W", 100)), (X0 + 3000, Y0))
        build_parcels(self.fab, "Tax", rec)
        f = self.join()
        self.assertTrue(f.geometry().constGet().hasCurvedSegments())
        self.assertAlmostEqual(f["CalculatedArea"], 30000 + math.pi * r * r / 4, places=1)

    def test_merge_window_and_separate_window(self):
        from parcel_fabric.ui import edit_dialogs as ED
        win, iface = QWidget(), Mock()
        iface.mainWindow.return_value, iface.mapCanvas.return_value = win, QgsMapCanvas()
        mgr = SimpleNamespace(active=self.fab, active_record_id=self.m, activeRecordChanged=Mock(),
                              active_record=lambda: self.fab.record_by_id(self.m))
        with patch.object(ED.QMessageBox, "warning", side_effect=lambda *x: self.fail(x[2])), \
                patch.object(ED.QMessageBox, "critical", side_effect=lambda *x: self.fail(x[2])), \
                patch.object(ED.QMessageBox, "information") as info:
            self.fab.parcels.selectByIds([f.id() for f in self.now()])
            w = ED.MergeWindow(iface, mgr)
            self.assertTrue(w.apart.isEnabled())
            w.apart.setChecked(True)
            w.number.setCurrentIndex(1)
            w._run()
            self.assertEqual(len(self.now()), 1)
            info.assert_called_once()
            self.assertIn("Merge succeeded", info.call_args[0][2])
            w.close()
            self.fab.parcels.selectByIds([self.now()[0].id()])
            w = ED.SeparateWindow(iface, mgr)
            w._run()
            self.assertEqual(len(self.now()), 2)
            w.close()


class SingleFabricTests(_Multi):
    """A fabric made before multi-polygon parcels."""

    def setUp(self):
        with patch.object(S, "MULTIPART_PROVIDERS", ()):
            super().setUp()

    def test_it_is_single_and_still_works(self):
        self.assertFalse(self.fab.multipart)
        before = self.state()
        with self.assertRaisesRegex(E.EditError, "Fabric Settings"):
            E.merge_parcels(self.fab, self.now(), self.m, NUM, apart=True)
        self.assertEqual(self.state(), before)
        parent = max(self.now(), key=lambda f: f.geometry().area())
        a = math.radians(-(1 + 41 / 60 + 12 / 3600))
        legs = C.compute((X0 + 75 * math.sin(a), Y0 + 75 * math.cos(a)),
                         courses((E_, 200), (S_, 50), (W_, 200), (N, 50))).legs
        apply_deed_split(self.fab, plan_deed_split(self.fab, parent, legs, 1.0), self.m)
        self.assertEqual(sorted(round(f.geometry().area()) for f in self.now()), [5000, 5000, 10000, 10000])

    def test_convert(self):
        safeguards.run(self.fab)                # database rules must not get in the way
        before = {f["GlobalID"]: (f["ParcelNumber"], round(f.geometry().area(), 6)) for f in self.now()}
        name, source_id = self.fab.parcels.name(), self.fab.parcels.id()
        backup = self.fab.convert_to_multipart()
        self.assertTrue(self.fab.multipart)
        self.assertEqual((self.fab.parcels.name(), self.fab.parcels.id()), (name, source_id))
        self.assertEqual({f["GlobalID"]: (f["ParcelNumber"], round(f.geometry().area(), 6)) for f in self.now()}, before)
        self.assertTrue(all(len(parts_of(f.geometry())) == 1 for f in self.now()))
        old = Fabric.open("ogr", backup)        # the copy is the fabric as it was
        self.assertFalse(old.multipart)
        self.assertEqual(old.parcels.featureCount(), 2)
        self.assertEqual(self.areas(self.join()), [10000, 20000])          # works at once, in this session
        self.assertTrue(Fabric.open("ogr", self.path).multipart)
        with self.assertRaisesRegex(RuntimeError, "already"):
            self.fab.convert_to_multipart()

    def test_converted_file_is_a_valid_geopackage_for_other_software(self):
        from osgeo import ogr
        self.fab.convert_to_multipart()
        self.join()
        QgsProject.instance().clear()
        ds = ogr.Open(self.path)
        ly = ds.GetLayerByName("Parcels")
        self.assertEqual(ogr.GeometryTypeToName(ly.GetGeomType()), "Multi Surface")
        self.assertEqual({f.GetGeometryRef().GetGeometryName() for f in ly}, {"MULTISURFACE"})
        ly.SetSpatialFilterRect(X0 + 990, Y0 - 10, X0 + 1110, Y0 + 110)         # the spatial index still works
        self.assertGreaterEqual(ly.GetFeatureCount(), 1)
        ds = ly = None
        con = sqlite3.connect(self.path)
        self.assertIn("MULTISURFACE", con.execute("select sql from sqlite_master where name = 'Parcels'").fetchone()[0])
        self.assertEqual(con.execute("pragma integrity_check").fetchone()[0], "ok")
        self.assertEqual([r[0] for r in con.execute("select extension_name from gpkg_extensions where table_name = "
                                                    "'Parcels' and extension_name = 'gpkg_geom_MULTISURFACE'")],
                         ["gpkg_geom_MULTISURFACE"])
        con.close()

    def test_failed_conversion_changes_nothing(self):
        con = sqlite3.connect(self.path)
        con.execute("CREATE TRIGGER stop BEFORE UPDATE ON \"Parcels\" WHEN OLD.fid = 2 "
                    "BEGIN SELECT RAISE(ABORT, 'no'); END")
        con.commit()
        con.close()
        before = self.state()
        with self.assertRaises(RuntimeError):
            self.fab.convert_to_multipart()
        self.assertFalse(self.fab.multipart)
        self.assertFalse(Fabric.open("ogr", self.path).multipart)
        self.assertEqual(self.state(), before)
        self.assertEqual(len(glob.glob(os.path.join(self.tmp, "*.before-multipart-*.gpkg"))), 1)

    def test_unsaved_edits_stop_it(self):
        self.fab.lines.startEditing()
        with self.assertRaisesRegex(RuntimeError, "Save or discard"):
            self.fab.convert_to_multipart()
        self.fab.lines.rollBack()

    def test_settings_button(self):
        from parcel_fabric.ui import settings_dialog as SD
        dlg = SD.SettingsDialog(self.fab)
        with patch.object(SD.QMessageBox, "question", return_value=SD.QMessageBox.StandardButton.No):
            dlg._allow_multipart()
        self.assertFalse(self.fab.multipart)
        with patch.object(SD.QMessageBox, "question", return_value=SD.QMessageBox.StandardButton.Yes), \
                patch.object(SD.QMessageBox, "information") as info:
            dlg._allow_multipart()
        self.assertTrue(self.fab.multipart)
        self.assertIn("before-multipart", info.call_args[0][2])


del _Base, _Multi
