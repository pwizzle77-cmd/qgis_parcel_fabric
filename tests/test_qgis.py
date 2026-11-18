"""Integration tests that need QGIS.

Run from the repository folder with the QGIS Python launcher, e.g. on Windows:
    set QT_QPA_PLATFORM=offscreen
    "C:\\Program Files\\QGIS 4.2.3\\bin\\python-qgis.bat" -m unittest tests.test_qgis -v
"""
import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from qgis.core import QgsApplication, QgsFeature, QgsFeatureRequest, QgsGeometry, QgsProject, QgsRectangle  # noqa: E402

APP = QgsApplication.instance() or QgsApplication([], False)
APP.initQgis()

from parcel_fabric.core import cogo as C  # noqa: E402
from parcel_fabric.core import schema as S  # noqa: E402
from parcel_fabric.core import units as U  # noqa: E402
from parcel_fabric.core.build import build_parcels  # noqa: E402
from parcel_fabric.core.fabric import CURRENT, Fabric  # noqa: E402
from parcel_fabric.core.qa import run_checks  # noqa: E402

X0, Y0 = 2600000.0, 300000.0       # Florida North State Plane-ish, US ft


def courses(*calls):
    out = []
    for c in calls:
        if c[0] == "curve":
            _, b, radius, arc, kind = c
            out.append(C.Course(C.CURVE, U.parse_bearing(b) if b else None, None, radius, arc, kind))
        else:
            out.append(C.Course(C.LINE, U.parse_bearing(c[0]), c[1]))
    return out


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "fabric.gpkg")
        S.create_fabric("ogr", self.path, "", "", ["Tax", "Lot"], "EPSG:2238",
                        {"name": "Test Fabric"})
        self.fab = Fabric.open("ogr", self.path)
        self.fab.add_to_project()
        self.rec = self.fab.create_record("PB 1 PG 1", "Plat")
        Fabric.set_active_record_id(self.rec)

    def tearDown(self):
        QgsProject.instance().clear()

    def traverse(self, target, cs, start=(X0, Y0)):
        res = C.compute(start, cs)
        self.fab.add_traverse(target, res.legs, self.rec)
        return res


class FabricTests(_Base):
    def test_schema_uses_esri_names(self):
        names = self.fab.lines.fields().names()
        for n in ("Direction", "Distance", "Radius", "ArcLength", "CreatedByRecord", "RetiredByRecord",
                  "ParcelType"):
            self.assertIn(n, names)
        for n in ("MiscloseRatio", "ParcelType", "ParcelNumber"):
            self.assertIn(n, self.fab.parcels.fields().names())
        self.assertIn("Book", self.fab.records.fields().names())
        self.assertEqual(self.fab.parcels.name(), "Parcels")
        self.assertEqual(len(Fabric.from_project()), 1)

    def test_square_builds_one_parcel_with_points(self):
        self.traverse("Tax", courses(("N 0 E", 100), ("N 90 E", 200), ("S 0 E", 100), ("S 90 W", 200)))
        rep = build_parcels(self.fab, "Tax", self.rec)
        self.assertEqual(rep.created, 1, rep.issues)
        p = next(self.fab.parcels.getFeatures())
        self.assertEqual(p["ParcelType"], "Tax")
        self.assertAlmostEqual(p.geometry().area(), 20000, places=3)
        self.assertAlmostEqual(p["CalculatedArea"], 20000, places=3)
        self.assertEqual(p["CreatedByRecord"], self.rec)
        self.assertEqual(self.fab.points.featureCount(), 4)
        rec = self.fab.record_by_id(self.rec)
        self.assertEqual(rec["ParcelCount"], 1)
        # building again rebuilds, never duplicates
        rep2 = build_parcels(self.fab, "Tax", self.rec)
        self.assertEqual((rep2.created, rep2.rebuilt), (0, 1))

    def test_curved_parcel_keeps_true_curve(self):
        r = 100.0
        self.traverse("Lot", courses(("N 0 E", 100),
                                     ("curve", "N 90 E", r, math.pi * r / 2, C.DIR_TANGENT),
                                     ("S 90 W", 100)))
        rep = build_parcels(self.fab, "Lot", self.rec)
        self.assertEqual(rep.created, 1, rep.issues)
        p = next(self.fab.parcels.getFeatures())
        self.assertTrue(p.geometry().constGet().hasCurvedSegments())
        self.assertAlmostEqual(p["CalculatedArea"], math.pi * r * r / 4, places=2)

    def test_two_lots_share_a_line(self):
        self.traverse("Lot", courses(("N 0 E", 100), ("N 90 E", 100), ("S 0 E", 100), ("S 90 W", 100)))
        # second lot to the east reuses the shared west line (no duplicate)
        self.traverse("Lot", courses(("N 90 E", 100), ("N 0 E", 100), ("S 90 W", 100)),
                      start=(X0 + 100, Y0))
        rep = build_parcels(self.fab, "Lot", self.rec)
        self.assertEqual(rep.created, 2, rep.issues)
        self.assertEqual(self.fab.points.featureCount(), 6)

    def test_types_are_kept_apart(self):
        sq = ("N 0 E", 100), ("N 90 E", 100), ("S 0 E", 100), ("S 90 W", 100)
        self.traverse("Tax", courses(*sq))
        self.traverse("Lot", courses(*sq))
        self.assertEqual(build_parcels(self.fab, "Tax", self.rec).created, 1)
        self.assertEqual(build_parcels(self.fab, "Lot", self.rec).created, 1)
        types = sorted(f["ParcelType"] for f in self.fab.parcels.getFeatures())
        self.assertEqual(types, ["Lot", "Tax"])

    def test_edit_record(self):
        self.fab.update_record(self.rec, name="Plat Book 5 Page 22", book="5", page="22", rtype="Plat")
        r = self.fab.record_by_id(self.rec)
        self.assertEqual((r["Name"], r["Book"], r["Page"]), ("Plat Book 5 Page 22", "5", "22"))

    def test_parcel_numbers_from_plss(self):
        from osgeo import ogr, osr
        from parcel_fabric.core import parcel_numbers as PN
        plss_path = os.path.join(self.tmp, "plss.gpkg")
        ds = ogr.GetDriverByName("GPKG").CreateDataSource(plss_path)
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(2238)
        ly = ds.CreateLayer("sections", srs, ogr.wkbPolygon)
        for name, typ in (("SEC", ogr.OFTInteger), ("TWP", ogr.OFTString), ("RNG", ogr.OFTString)):
            ly.CreateField(ogr.FieldDefn(name, typ))
        f = ogr.Feature(ly.GetLayerDefn())
        f.SetField("SEC", 12)
        f.SetField("TWP", "04S")
        f.SetField("RNG", "16E")
        f.SetGeometry(ogr.CreateGeometryFromWkt(
            "POLYGON((%f %f,%f %f,%f %f,%f %f,%f %f))" % (X0 - 1000, Y0 - 1000, X0 + 5000, Y0 - 1000,
                                                       X0 + 5000, Y0 + 5000, X0 - 1000, Y0 + 5000,
                                                       X0 - 1000, Y0 - 1000)))
        ly.CreateFeature(f)
        ds = None
        self.fab.save_settings({"plss_provider": "ogr", "plss_source": plss_path + "|layername=sections",
                                "plss_section_field": "SEC", "plss_township_field": "TWP",
                                "plss_range_field": "RNG"})
        sq = ("N 0 E", 100), ("N 90 E", 100), ("S 0 E", 100), ("S 90 W", 100)
        self.traverse("Tax", courses(*sq))
        self.traverse("Tax", courses(*sq), start=(X0 + 500, Y0))
        rep = build_parcels(self.fab, "Tax", self.rec)
        self.assertEqual(rep.created, 2, rep.issues)
        nums = sorted(f["ParcelNumber"] for f in self.fab.parcels.getFeatures())
        self.assertEqual(nums, ["12-4S-16-00001-000", "12-4S-16-00002-000"])
        ids = sorted((f["ParcelID"], f["ParcelSeq"]) for f in self.fab.parcels.getFeatures())
        self.assertEqual(ids, [("00001-000", 1), ("00002-000", 2)])
        # splits of the first one
        kids = [f for f in self.fab.parcels.getFeatures()]
        PN.number_splits(self.fab, "12-4S-16-00001-000", kids)
        self.assertEqual(sorted(f["ParcelNumber"] for f in kids),
                         ["12-4S-16-00001-001", "12-4S-16-00001-002"])

    def test_old_integer_parcel_id_is_migrated(self):
        from osgeo import ogr
        self.traverse("Tax", courses(("N 0 E", 100), ("N 90 E", 100), ("S 0 E", 100), ("S 90 W", 100)))
        build_parcels(self.fab, "Tax", self.rec)
        QgsProject.instance().clear()
        ds = ogr.Open(self.path, 1)               # make it look like a 0.8.0 fabric
        ds.ExecuteSQL('ALTER TABLE "Parcels" DROP COLUMN "ParcelID"')
        ds.ExecuteSQL('ALTER TABLE "Parcels" DROP COLUMN "ParcelSeq"')
        ds.ExecuteSQL('ALTER TABLE "Parcels" ADD COLUMN "ParcelID" INTEGER')
        ds.ExecuteSQL('UPDATE "Parcels" SET "ParcelNumber" = \'12-4S-16-02942-003\', "ParcelID" = 2942')
        ds = None
        fab = Fabric.open("ogr", self.path)
        f = next(fab.parcels.getFeatures())
        self.assertEqual(f["ParcelID"], "02942-003")
        self.assertEqual(f["ParcelSeq"], 2942)

    def test_check_finds_dangle(self):
        self.traverse("Tax", courses(("N 0 E", 100), ("N 90 E", 100)))
        issues = run_checks(self.fab, ["Tax"])
        self.assertTrue(any("Dangling" in i.message for i in issues))

    def test_build_in_view(self):
        self.traverse("Tax", courses(("N 0 E", 50), ("N 90 E", 50), ("S 0 E", 50), ("S 90 W", 50)))
        ext = QgsRectangle(X0 - 10, Y0 - 10, X0 + 60, Y0 + 60)
        rep = build_parcels(self.fab, "Tax", None, ext)
        self.assertEqual(rep.created, 1, rep.issues)
        n = len(list(self.fab.parcels.getFeatures(QgsFeatureRequest().setFilterExpression(CURRENT))))
        self.assertEqual(n, 1)

class Phase2Tests(_Base):
    """Split, divide, merge, copy parallel and lineage on a 200 x 100 ft parcel."""

    def setUp(self):
        super().setUp()
        self.traverse("Tax", courses(("N 0 E", 100), ("N 90 E", 200), ("S 0 E", 100), ("S 90 W", 200)))
        build_parcels(self.fab, "Tax", self.rec)
        self.parent = next(self.fab.parcels.getFeatures())
        # a second record does the edits
        self.rec2 = self.fab.create_record("PB 2 PG 1", "Plat")
        Fabric.set_active_record_id(self.rec2)

    def current(self):
        req = QgsFeatureRequest().setFilterExpression(CURRENT)
        return list(self.fab.parcels.getFeatures(req))

    def test_split_retires_parent_and_splits_boundary_lines(self):
        from qgis.core import QgsGeometry, QgsPointXY
        from parcel_fabric.core import edits as E
        cut = QgsGeometry.fromPolylineXY([QgsPointXY(X0 + 80, Y0 - 20), QgsPointXY(X0 + 80, Y0 + 120)])
        res = E.cut_parcel(self.fab, self.parent, [cut], self.rec2)
        self.assertEqual(res.lines_split, 2)        # north and south lines
        self.assertEqual(res.lines_added, 1)
        cur = self.current()
        self.assertEqual(len(cur), 2)
        areas = sorted(round(f.geometry().area()) for f in cur)
        self.assertEqual(areas, [8000, 12000])
        self.assertTrue(all(f["CreatedByRecord"] == self.rec2 for f in cur))
        # split halves keep the record distance in proportion
        req = QgsFeatureRequest().setFilterExpression('"ParentLineID" IS NOT NULL AND ' + CURRENT)
        halves = sorted(round(f["Distance"], 3) for f in self.fab.lines.getFeatures(req))
        self.assertEqual(halves, [80.0, 80.0, 120.0, 120.0])
        rows = list(self.fab.lineage.getFeatures())
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["ParentID"] for r in rows}, {self.parent["GlobalID"]})

    def test_split_remainder_keeps_parent(self):
        from qgis.core import QgsGeometry, QgsPointXY
        from parcel_fabric.core import edits as E
        cut = QgsGeometry.fromPolylineXY([QgsPointXY(X0 + 50, Y0 - 20), QgsPointXY(X0 + 50, Y0 + 120)])
        E.cut_parcel(self.fab, self.parent, [cut], self.rec2, remainder_keeps=True)
        cur = self.current()
        self.assertEqual(len(cur), 2)
        keep = [f for f in cur if f["GlobalID"] == self.parent["GlobalID"]]
        self.assertEqual(len(keep), 1)
        self.assertAlmostEqual(keep[0].geometry().area(), 15000, places=0)
        self.assertEqual(keep[0]["CreatedByRecord"], self.rec)

    def test_divide_equal_area(self):
        from parcel_fabric.core import edits as E
        cuts = E.divide_cuts(self.parent.geometry(), U.parse_bearing("N 0 E"), "equal_area", 4)
        E.cut_parcel(self.fab, self.parent, cuts, self.rec2, "Divide")
        areas = [f.geometry().area() for f in self.current()]
        self.assertEqual(len(areas), 4)
        for a in areas:
            self.assertAlmostEqual(a, 5000, places=1)

    def test_divide_set_area_with_remainder(self):
        from parcel_fabric.core import edits as E
        cuts = E.divide_cuts(self.parent.geometry(), U.parse_bearing("N 0 E"), "area", area=6000)
        self.assertEqual(len(cuts), 3)
        E.cut_parcel(self.fab, self.parent, cuts, self.rec2, "Divide")
        areas = sorted(round(f.geometry().area()) for f in self.current())
        self.assertEqual(areas, [2000, 6000, 6000, 6000])

    def test_merge_back(self):
        from qgis.core import QgsGeometry, QgsPointXY
        from parcel_fabric.core import edits as E
        cut = QgsGeometry.fromPolylineXY([QgsPointXY(X0 + 100, Y0 - 20), QgsPointXY(X0 + 100, Y0 + 120)])
        E.cut_parcel(self.fab, self.parent, [cut], self.rec2)
        rec3 = self.fab.create_record("Combination", "Data cleanup")
        res = E.merge_parcels(self.fab, self.current(), rec3)
        cur = self.current()
        self.assertEqual(len(cur), 1)
        self.assertAlmostEqual(cur[0].geometry().area(), 20000, places=0)
        self.assertEqual(res.lines_retired, 1)       # the cut line
        merges = [r for r in self.fab.lineage.getFeatures() if r["Operation"] == "Merge"]
        self.assertEqual(len(merges), 2)

    def test_history_dates_and_family(self):
        from qgis.core import QgsGeometry, QgsPointXY
        from qgis.PyQt.QtCore import QDate
        from parcel_fabric.core import edits as E
        from parcel_fabric.core import family as F
        from parcel_fabric.core import history as H
        from parcel_fabric.core import lineage as L
        self.fab.update_record(self.rec, date=QDate(1998, 4, 2))
        self.fab.update_record(self.rec2, date=QDate(2010, 7, 15))
        cut = QgsGeometry.fromPolylineXY([QgsPointXY(X0 + 80, Y0 - 20), QgsPointXY(X0 + 80, Y0 + 120)])
        E.cut_parcel(self.fab, self.parent, [cut], self.rec2)
        H.refresh_dates(self.fab)
        hist = H.history_layer(self.fab)
        self.assertTrue(hist.temporalProperties().isActive())
        allp = {f["GlobalID"]: f for f in hist.getFeatures()}
        self.assertEqual(len(allp), 3)                       # parent + 2 children
        par = allp[self.parent["GlobalID"]]
        self.assertEqual(par["QPF_StartDate"], QDate(1998, 4, 2))
        self.assertEqual(par["QPF_EndDate"], QDate(2010, 7, 15))
        kids = [f for g, f in allp.items() if g != self.parent["GlobalID"]]
        self.assertTrue(all(k["QPF_StartDate"] == QDate(2010, 7, 15) and not k["QPF_EndDate"] for k in kids))
        # family from one child goes back to the parent and over to its sibling
        up = L.walk(self.fab, kids[0]["GlobalID"], up=True)
        roots = F.roots(up, kids[0]["GlobalID"])
        self.assertEqual(roots, {self.parent["GlobalID"]})
        down = L.walk(self.fab, self.parent["GlobalID"], up=False)
        self.assertEqual(F.descendants(down, roots), set(allp))
        self.assertEqual(H.undated_records(self.fab), [])

    def test_copy_parallel(self):
        from parcel_fabric.core import edits as E
        south = [f for f in self.fab.lines.getFeatures()
                 if abs(f.geometry().boundingBox().yMaximum() - Y0) < 0.01][0]
        E.copy_parallel(self.fab, [south], 10, "left", "Connections", self.rec2)
        c = next(self.fab.connections.getFeatures())
        # the south line runs west, so its left side is south
        self.assertAlmostEqual(c.geometry().boundingBox().yMinimum(), Y0 - 10, places=6)
        self.assertAlmostEqual(c["Distance"], 200, places=6)

class AdjustTests(_Base):
    def setUp(self):
        super().setUp()
        self.traverse("Lot", courses(("N 0 E", 100), ("N 90 E", 200), ("S 0 E", 100), ("S 90 W", 200)))
        build_parcels(self.fab, "Lot", self.rec)
        self.box = QgsRectangle(X0 - 50, Y0 - 50, X0 + 250, Y0 + 150)

    def test_consistency_check_fits(self):
        from parcel_fabric.core import adjust as ADJ
        from parcel_fabric.core import lsa
        net = ADJ.collect(self.fab, "view", extent=self.box, use_control=False)
        self.assertEqual(len(net.points), 4)
        self.assertEqual(len(net.obs), 8)
        res = lsa.adjust(net.points, net.obs)
        self.assertTrue(res.converged)
        self.assertLess(max(abs(o.residual) for o in res.obs), 1e-4)

    def test_adjust_to_control_and_apply(self):
        from qgis.core import QgsGeometry, QgsPoint
        from parcel_fabric.core import adjust as ADJ
        from parcel_fabric.core import lsa
        from parcel_fabric.core.fabric import EditSession
        pts = {round(f.geometry().asPoint().x() - X0) * 1000 + round(f.geometry().asPoint().y() - Y0): f
               for f in self.fab.points.getFeatures()}
        sw, ne = pts[0], pts[200 * 1000 + 100]
        # the surveyed control puts the lot 0.5 ft east of where it was drawn
        with EditSession([self.fab.points]) as ed:
            for f in (sw, ne):
                q = f.geometry().asPoint()
                ed.set_geometry(self.fab.points, f.id(), QgsGeometry(QgsPoint(q.x() + 0.5, q.y())))
        ADJ.mark_control(self.fab, [sw.id(), ne.id()], 1)
        net = ADJ.collect(self.fab, "view", extent=self.box, use_control=True)
        res = lsa.adjust(net.points, net.obs)
        self.assertTrue(res.converged)
        moved, nlines, rebuilt = ADJ.apply(self.fab, net, res)
        self.assertEqual(moved, 2)               # the two free corners follow the control
        self.assertEqual(nlines, 4)
        p = next(self.fab.parcels.getFeatures(QgsFeatureRequest().setFilterExpression(CURRENT)))
        self.assertAlmostEqual(p.geometry().boundingBox().xMinimum(), X0 + 0.5, places=4)
        self.assertEqual(rebuilt, 1)


class Release05Tests(_Base):
    """Legal description writer, Compare and the closure report."""

    def setUp(self):
        super().setUp()
        self.traverse("Lot", courses(("N 0 E", 100), ("N 90 E", 200), ("S 0 E", 100), ("S 90 W", 200)))
        build_parcels(self.fab, "Lot", self.rec)
        self.parcel = next(self.fab.parcels.getFeatures())

    def test_legal_description_of_a_built_parcel(self):
        from parcel_fabric.core import legal_writer as W
        from parcel_fabric.core.geomio import polygon_rings, unit_word
        self.assertEqual(unit_word(self.fab.parcels.crs()), "feet")
        ext, holes = polygon_rings(self.parcel.geometry())[0]
        ring = W.rotate_to(W.orient(W.merge_collinear(ext), True), 0)
        ring = W.rotate_to(ring, W.northwest_index(ring))
        text, area = W.write(ring)
        self.assertAlmostEqual(area, 20000, places=3)
        self.assertIn("N 90°00'00\" E, a distance of 200.00 feet", text)

    def test_compare_with_shifted_copy(self):
        from qgis.core import QgsFeature, QgsVectorLayer
        from parcel_fabric.core import compare as CMP
        ref = QgsVectorLayer("Polygon?crs=EPSG:2238&field=PIN:string", "county", "memory")
        g = QgsGeometry(self.parcel.geometry().constGet().segmentize())
        g.translate(0.5, 0.0)
        f = QgsFeature(ref.fields())
        f.setGeometry(g)
        f.setAttributes(["12-4S-16-00001-000"])
        ref.dataProvider().addFeatures([f])
        m = CMP.compare([("lot", self.parcel.geometry(), None)], ref, self.fab.parcels.crs(), "PIN")[0]
        self.assertEqual(m.ref_label, "12-4S-16-00001-000")
        self.assertAlmostEqual(m.shift, 0.5, places=6)
        self.assertAlmostEqual(m.hausdorff, 0.5, places=6)
        self.assertAlmostEqual(m.overlap, 199.5 / 200, places=6)

    def test_closure_report(self):
        from parcel_fabric.core.reports import boundary_chain, closure_report
        chain = boundary_chain(self.fab, self.parcel)
        self.assertEqual(len(chain), 4)
        path = os.path.join(self.tmp, "closure.html")
        self.assertEqual(closure_report(self.fab, self.rec, path), 1)
        with open(path, encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("closed", html)
        self.assertIn("0.459", html)


class Release08Tests(_Base):
    """Subdivide by lot width, retire guard, LandXML, safeguards, Pro import, DXF."""

    def setUp(self):
        super().setUp()
        self.traverse("Tax", courses(("N 0 E", 100), ("N 90 E", 200), ("S 0 E", 100), ("S 90 W", 200)))
        build_parcels(self.fab, "Tax", self.rec)
        self.parent = next(self.fab.parcels.getFeatures())
        self.rec2 = self.fab.create_record("PB 2 PG 1", "Plat")
        Fabric.set_active_record_id(self.rec2)

    def current(self):
        return list(self.fab.parcels.getFeatures(QgsFeatureRequest().setFilterExpression(CURRENT)))

    def test_lots_by_width(self):
        from parcel_fabric.core import edits as E
        cuts = E.divide_cuts(self.parent.geometry(), U.parse_bearing("N 0 E"), "width", area=60)
        self.assertEqual(len(cuts), 3)
        E.cut_parcel(self.fab, self.parent, cuts, self.rec2, "Subdivide")
        self.assertEqual(sorted(round(f.geometry().area()) for f in self.current()), [2000, 6000, 6000, 6000])

    def test_cannot_retire_twice(self):
        from parcel_fabric.core import edits as E
        cuts = E.divide_cuts(self.parent.geometry(), U.parse_bearing("N 0 E"), "equal_area", 2)
        stale = QgsFeature(self.parent)          # another editor's copy, read before the split
        E.cut_parcel(self.fab, self.parent, cuts, self.rec2, "Divide")
        with self.assertRaises(E.EditError):
            E.cut_parcel(self.fab, stale, cuts, self.rec2, "Divide")

    def test_gpkg_safeguards(self):
        from parcel_fabric.core import safeguards as G
        self.assertGreater(G.run(self.fab, True), 0)
        self.fab.parcels.reload()
        f = next(self.fab.parcels.getFeatures())
        ok = self.fab.parcels.dataProvider().deleteFeatures([f.id()])
        self.assertFalse(ok)
        G.run(self.fab, False)

    def test_landxml_import(self):
        from parcel_fabric.core import exchange as XC
        x0, y0 = X0 + 1000, Y0
        xml = os.path.join(self.tmp, "lots.xml")
        with open(xml, "w") as fh:
            fh.write("""<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2"><Parcels>
<Parcel name="Lot 1" area="10000"><CoordGeom>
<Line><Start>{y0} {x0}</Start><End>{y1} {x0}</End></Line>
<Line><Start>{y1} {x0}</Start><End>{y1} {xm}</End></Line>
<Line><Start>{y1} {xm}</Start><End>{y0} {xm}</End></Line>
<Line><Start>{y0} {xm}</Start><End>{y0} {x0}</End></Line></CoordGeom></Parcel>
<Parcel name="Lot 2"><CoordGeom>
<Line><Start>{y0} {xm}</Start><End>{y1} {xm}</End></Line>
<Line><Start>{y1} {xm}</Start><End>{y1} {x1}</End></Line>
<Line><Start>{y1} {x1}</Start><End>{y0} {x1}</End></Line>
<Line><Start>{y0} {x1}</Start><End>{y0} {xm}</End></Line></CoordGeom></Parcel>
</Parcels></LandXML>""".format(x0=x0, xm=x0 + 100, x1=x0 + 200, y0=y0, y1=y0 + 100))
        nl, npt, rep, named = XC.import_landxml(self.fab, xml, "Lot", self.rec2)
        self.assertEqual(nl, 7)          # the shared line once
        self.assertEqual(rep.created, 2)
        self.assertEqual(named, 2)
        names = sorted(f["Name"] for f in self.fab.parcels.getFeatures(
            QgsFeatureRequest().setFilterExpression("\"ParcelType\" = 'Lot'")))
        self.assertEqual(names, ["Lot 1", "Lot 2"])

    def test_pro_import_and_dxf(self):
        from parcel_fabric.core import exchange as XC
        # pretend this fabric is a Pro export: copy its tables into a "Pro" GeoPackage
        from osgeo import gdal
        pro = os.path.join(self.tmp, "pro.gpkg")
        gdal.VectorTranslate(pro, self.path, layers=["Records", "Points", "Connections"])
        gdal.VectorTranslate(pro, self.path, accessMode="append", layers=["Parcels"], layerName="Tax")
        gdal.VectorTranslate(pro, self.path, accessMode="append", layers=["Parcel_Lines"], layerName="Tax_Lines")
        t = XC.detect_fabric(XC.source_layers(pro))
        self.assertEqual(t["records"], "Records")
        self.assertIn("Tax", t["types"])
        path2 = os.path.join(self.tmp, "new.gpkg")
        S.create_fabric("ogr", path2, "", "", ["Tax"], "EPSG:2238", {"name": "Imported"})
        fab2 = Fabric.open("ogr", path2)
        XC.import_pro(fab2, pro, t)
        self.assertEqual(fab2.parcels.featureCount(), self.fab.parcels.featureCount())
        self.assertEqual(fab2.lines.featureCount(), self.fab.lines.featureCount())
        out = XC.export_dxf(self.fab, os.path.join(self.tmp, "fabric.dxf"))
        self.assertGreater(os.path.getsize(out), 1000)


if __name__ == "__main__":
    unittest.main()
