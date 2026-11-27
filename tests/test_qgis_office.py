"""Roll IDs, gaps, the tax-roll file, stacked units, lot labels and legal fit.

Run from the repository folder:
    set QT_QPA_PLATFORM=offscreen
    "C:\\Program Files\\QGIS 4.2.3\\bin\\python-qgis.bat" -m unittest discover -s tests -p test_qgis_office.py -v
"""
import csv
import unittest
from datetime import date
from types import SimpleNamespace

from qgis.core import (
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointXY,
    QgsVectorLayer,
)

from tests.test_qgis import X0, Y0, _Base, courses

from parcel_fabric.core import edits as E
from parcel_fabric.core import exchange as X
from parcel_fabric.core.exchange import sync_parcel_layer
from parcel_fabric.core import numbering as N
from parcel_fabric.core import styles
from parcel_fabric.core import units as U
from parcel_fabric.core.build import _parcel_geometry, build_parcels
from parcel_fabric.core.fabric import CURRENT, EditSession
from parcel_fabric.core.geomio import fit_gap, polygon_rings
from parcel_fabric.core.qa import neighbor_issues, run_checks
from parcel_fabric.ui.legal_writer_dialog import LegalWriterDialog


def box(x0, y0, x1, y1):
    return QgsGeometry.fromPolygonXY([[
        QgsPointXY(x0, y0), QgsPointXY(x0, y1), QgsPointXY(x1, y1),
        QgsPointXY(x1, y0), QgsPointXY(x0, y0)]])


class NeighborTests(unittest.TestCase):
    def test_a_curved_subdivision_is_stored_as_a_straight_multipolygon(self):
        # SQL Server rejects MultiSurface WKT. Lots, blocks and subdivisions
        # are MultiPolygon, so a service curve has to be written as straight segments.
        from qgis.core import (
            QgsCircularString, QgsCompoundCurve, QgsCurvePolygon, QgsGeometry,
            QgsLineString, QgsPoint, QgsVectorLayer, QgsWkbTypes,
        )
        a, b = QgsPoint(X0, Y0), QgsPoint(X0, Y0 + 100)
        c, d = QgsPoint(X0 + 100, Y0 + 100), QgsPoint(X0 + 100, Y0)
        ring = QgsCompoundCurve()
        ring.addCurve(QgsLineString([a, b]))
        ring.addCurve(QgsCircularString(b, QgsPoint(X0 + 50, Y0 + 140), c))
        ring.addCurve(QgsLineString([c, d, a]))
        poly = QgsCurvePolygon()
        poly.setExteriorRing(ring)
        target = QgsVectorLayer("MultiPolygon?crs=epsg:2238", "subdivisions", "memory")
        geom = X._pack_parcel(target, [QgsGeometry(poly.clone())])
        self.assertTrue(geom.asWkt().startswith("MultiPolygon"))
        self.assertNotIn("Curve", geom.asWkt())
        self.assertGreater(geom.area(), 100 * 100)
        curved = QgsVectorLayer("CurvePolygon?crs=epsg:2238", "parcels", "memory")
        kept = X._pack_parcel(curved, [QgsGeometry(poly.clone())])
        self.assertTrue(QgsWkbTypes.isCurvedType(kept.wkbType()))

    def test_build_writes_a_straight_multipolygon_for_a_curved_parcel_on_sql_server(self):
        # Build/Split/Merge used to always hand back a curved QgsMultiSurface and
        # trust the caller to straighten it for SQL Server - nothing did, so a
        # merged or rebuilt parcel with any curve in it failed to save there.
        from types import SimpleNamespace
        from qgis.core import (
            QgsCircularString, QgsCompoundCurve, QgsCurvePolygon,
            QgsLineString, QgsPoint, QgsVectorLayer, QgsWkbTypes,
        )
        a, b = QgsPoint(X0, Y0), QgsPoint(X0, Y0 + 100)
        c, d = QgsPoint(X0 + 100, Y0 + 100), QgsPoint(X0 + 100, Y0)
        ring = QgsCompoundCurve()
        ring.addCurve(QgsLineString([a, b]))
        ring.addCurve(QgsCircularString(b, QgsPoint(X0 + 50, Y0 + 140), c))
        ring.addCurve(QgsLineString([c, d, a]))
        poly = QgsCurvePolygon()
        poly.setExteriorRing(ring)
        sql_server_parcels = QgsVectorLayer("MultiPolygon?crs=epsg:2238", "parcels", "memory")
        fake_fabric = SimpleNamespace(multipart=True, parcels=sql_server_parcels)
        geom = _parcel_geometry(fake_fabric, [poly])
        self.assertTrue(geom.asWkt().startswith("MultiPolygon"))
        self.assertNotIn("Curve", geom.asWkt())
        self.assertGreater(geom.area(), 100 * 100)

    def test_build_straightens_even_when_the_mssql_layer_itself_reports_a_curved_multi_type(self):
        # SQL Server's geometry column has no fixed subtype, so QGIS's mssql provider can
        # report the Parcels layer's own wkbType() as MultiSurface - a curved multi type -
        # even though SQL Server has no WKT label for MultiSurface at all. match_wkb_type
        # used to trust that reported type and build exactly the WKT SQL Server rejects.
        from types import SimpleNamespace
        from qgis.core import (
            QgsCircularString, QgsCompoundCurve, QgsCurvePolygon, QgsGeometry,
            QgsLineString, QgsPoint, QgsVectorLayer,
        )
        a, b = QgsPoint(X0, Y0), QgsPoint(X0, Y0 + 100)
        c, d = QgsPoint(X0 + 100, Y0 + 100), QgsPoint(X0 + 100, Y0)
        ring = QgsCompoundCurve()
        ring.addCurve(QgsLineString([a, b]))
        ring.addCurve(QgsCircularString(b, QgsPoint(X0 + 50, Y0 + 140), c))
        ring.addCurve(QgsLineString([c, d, a]))
        poly = QgsCurvePolygon()
        poly.setExteriorRing(ring)
        reported_type = QgsVectorLayer("MultiSurface?crs=epsg:2238", "parcels", "memory").wkbType()
        mssql_parcels = SimpleNamespace(wkbType=lambda: reported_type, providerType=lambda: "mssql")
        fake_fabric = SimpleNamespace(multipart=True, parcels=mssql_parcels)
        geom = _parcel_geometry(fake_fabric, [poly])
        self.assertNotIn("MultiSurface", geom.asWkt())
        self.assertTrue(geom.asWkt().startswith("MultiPolygon"))
        self.assertGreater(geom.area(), 100 * 100)
        single = X._pack_parcel(mssql_parcels, [QgsGeometry(poly.clone())])
        self.assertNotIn("MultiSurface", single.asWkt())
        self.assertTrue(single.asWkt().startswith("MultiPolygon"))

    def test_db_transaction_wraps_even_a_single_mssql_layer(self):
        # commitChanges() on one layer still sends attribute changes and added
        # features as separate statements, so a single started mssql layer needs
        # the transaction wrap too - not just two or more - or a feature-add
        # failure can leave that same layer's own attribute changes already saved.
        from unittest.mock import Mock, patch
        ed = EditSession.__new__(EditSession)
        ed._started = [Mock(providerType=Mock(return_value="mssql"))]
        with patch("parcel_fabric.core.fabric.QgsTransaction") as MockTransaction:
            MockTransaction.create.return_value.begin.return_value = True
            with patch("parcel_fabric.core.fabric._result_ok", return_value=True):
                tr = ed._db_transaction()
        self.assertIsNotNone(tr)
        MockTransaction.create.assert_called_once_with(ed._started)

    def test_default_style_hides_dense_layers_until_zoomed_in(self):
        from qgis.core import QgsVectorLayer
        from parcel_fabric.core.fabric import PROP_ROLE
        from parcel_fabric.core import schema as S
        pts = QgsVectorLayer("Point", "pts", "memory")
        pts.setCustomProperty(PROP_ROLE, S.ROLE_POINTS)
        styles.apply(pts)
        self.assertTrue(pts.hasScaleBasedVisibility())
        self.assertTrue(pts.isInScaleRange(1000))
        self.assertFalse(pts.isInScaleRange(100000))
        lines = QgsVectorLayer("LineString", "lines", "memory")
        lines.setCustomProperty(PROP_ROLE, S.ROLE_LINES)
        styles.apply(lines)
        self.assertTrue(lines.isInScaleRange(5000))
        self.assertFalse(lines.isInScaleRange(50000))
        lots = QgsVectorLayer("Polygon", "lots", "memory")
        lots.setCustomProperty(PROP_ROLE, S.ROLE_LOTS)
        styles.apply(lots)
        self.assertTrue(lots.isInScaleRange(10000))
        self.assertFalse(lots.isInScaleRange(100000))
        blocks = QgsVectorLayer("Polygon", "blocks", "memory")
        blocks.setCustomProperty(PROP_ROLE, S.ROLE_BLOCKS)
        styles.apply(blocks)
        self.assertTrue(blocks.isInScaleRange(20000))
        self.assertFalse(blocks.isInScaleRange(100000))
        parcels = QgsVectorLayer("Polygon", "parcels", "memory")
        parcels.setCustomProperty(PROP_ROLE, S.ROLE_PARCELS)
        styles.apply(parcels, ["Tax"])
        self.assertFalse(parcels.hasScaleBasedVisibility())

    def test_overlap_gap_stack_and_road(self):
        a = box(0, 0, 10, 10)
        overlap = neighbor_issues([("a", "Tax", "", a), ("b", "Tax", "", box(5, 0, 15, 10))], 0.01)
        self.assertTrue(any(sev == "error" and "overlap" in msg for sev, msg, _x, _y in overlap))
        gap = neighbor_issues([("a", "Tax", "", a), ("b", "Tax", "", box(10.5, 0, 20.5, 10))], 0.01)
        self.assertTrue(any(sev == "warning" and "gap" in msg for sev, msg, _x, _y in gap))
        far = neighbor_issues([("a", "Tax", "", a), ("b", "Tax", "", box(110, 0, 120, 10))], 0.01)
        self.assertEqual(far, [])
        road = neighbor_issues([("a", "Tax", "", a), ("b", "Tax", "", box(60, 0, 70, 10))], 0.01)
        self.assertEqual(road, [])
        shared = neighbor_issues([("a", "Tax", "", a), ("b", "Tax", "", box(10, 0, 20, 10))], 0.01)
        self.assertEqual(shared, [])
        same = QgsGeometry(a)
        stacked = neighbor_issues([("land", "Tax", "", a), ("unit", "Tax", "condo", same)], 0.01)
        self.assertEqual(stacked, [])
        both = neighbor_issues([("u1", "Tax", "condo", a), ("u2", "Tax", "common", QgsGeometry(a))], 0.01)
        self.assertEqual(both, [])
        other = neighbor_issues([("a", "Tax", "", a), ("b", "Lot", "", box(5, 0, 15, 10))], 0.01)
        self.assertEqual(other, [])
        # a shared edge digitized a hair apart (well inside tolerance) is a sliver, not an overlap -
        # Align Edge and the rest of the fabric already treat this as the same line.
        sliver = neighbor_issues([("a", "Tax", "", a), ("b", "Tax", "", box(9.995, 0, 20, 10))], 0.01)
        self.assertEqual(sliver, [])
        # the same offset, but a genuine overlap once it's wide enough to matter, is still caught.
        wide = neighbor_issues([("a", "Tax", "", a), ("b", "Tax", "", box(9.5, 0, 20, 10))], 0.01)
        self.assertTrue(any(sev == "error" and "overlap" in msg for sev, msg, _x, _y in wide))

    def test_fit_gap_catches_a_joined_bend(self):
        straight = box(0, 0, 100, 100)
        legs = [SimpleNamespace(start=(0, 0), end=(0, 100), mid=None),
                SimpleNamespace(start=(0, 100), end=(100, 100), mid=None),
                SimpleNamespace(start=(100, 100), end=(100, 0), mid=None),
                SimpleNamespace(start=(100, 0), end=(0, 0), mid=None)]
        self.assertLess(fit_gap(straight, legs), 0.01)
        kink = QgsGeometry.fromPolygonXY([[
            QgsPointXY(0, 0), QgsPointXY(100, 0), QgsPointXY(100, 100),
            QgsPointXY(50, 101), QgsPointXY(0, 100), QgsPointXY(0, 0)]])
        self.assertGreater(fit_gap(kink, legs), 0.1)


class OfficeTests(_Base):
    def _square(self):
        self.traverse("Tax", courses(("N 0 E", 100), ("N 90 E", 200), ("S 0 E", 100), ("S 90 W", 200)))
        build_parcels(self.fab, "Tax", self.rec)
        return next(self.fab.parcels.getFeatures())

    def test_roll_id_stack_export_and_rebuild(self):
        parent = self._square()
        for name in ("Subdivision", "Block", "Lot", "LotWidth", "LotDepth", "QPF_Stack"):
            self.assertGreaterEqual(self.fab.parcels.fields().indexOf(name), 0)
        fields = N.fields_for("1234567-1234")
        with EditSession([self.fab.parcels], "Number") as ed:
            for key, value in fields.items():
                ed.change(self.fab.parcels, parent.id(), key, value)
        parent = self.fab.parcels.getFeature(parent.id())
        self.assertEqual(parent["ParcelNumber"], "1234567-1234")
        self.assertFalse(parent["Section"])
        self.assertFalse(parent["ParcelID"])
        clean = run_checks(self.fab, checks=["gaps"])
        self.assertFalse(any(i.severity == "error" for i in clean))
        self.fab.update_record(self.rec, book="12", page="34")
        E.add_stacked_unit(self.fab, parent, "CONDO-1", "condo", self.rec)
        with self.assertRaises(E.EditError):
            E.add_stacked_unit(self.fab, parent, "CONDO-1", "condo", self.rec)
        stacked = run_checks(self.fab, checks=["gaps"])
        self.assertFalse(any("overlap" in i.message for i in stacked))
        build_parcels(self.fab, "Tax", self.rec)
        current = list(self.fab.parcels.getFeatures(QgsFeatureRequest().setFilterExpression(CURRENT)))
        units = [f for f in current if f["QPF_Stack"]]
        lands = [f for f in current if not f["QPF_Stack"]]
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["ParcelNumber"], "CONDO-1")
        self.assertEqual(len(lands), 1)
        shp, csv_path, n_parcels, n_changes = X.export_tax_roll(
            self.fab, self.tmp, "Example County", date(2026, 11, 23), "2000-01-01")
        self.assertTrue(shp.endswith("F_ExampleCounty_11232026_parcels.shp"))
        self.assertEqual(n_parcels, 2)
        self.assertGreaterEqual(n_changes, 1)
        with open(csv_path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        stack_rows = [r for r in rows if r["operation"] == "Stack" and r["child_number"] == "CONDO-1"]
        self.assertEqual(len(stack_rows), 1)
        self.assertEqual(stack_rows[0]["parent_number"], "1234567-1234")
        self.assertEqual(stack_rows[0]["book"], "12")
        self.assertEqual(stack_rows[0]["page"], "34")
        self.assertEqual(X.change_rows(self.fab, "2999-01-01"), [])
        _shp, _csv, n2, later = X.export_tax_roll(
            self.fab, self.tmp, "Example County", date(2026, 11, 23), "2999-01-01")
        self.assertEqual(n2, 2)
        self.assertEqual(later, 0)
        layer = QgsVectorLayer(shp, "roll", "ogr")
        self.assertTrue(layer.isValid(), shp)
        nums = sorted(f["PARCELNO"] for f in layer.getFeatures())
        self.assertEqual(nums, ["1234567-1234", "CONDO-1"])
        acres = {f["PARCELNO"]: f["ACRES"] for f in layer.getFeatures()}
        self.assertAlmostEqual(acres["1234567-1234"], 20000 / 43560.0, places=2)
        del layer
        with EditSession([self.fab.parcels], "Unstack") as ed:
            ed.change(self.fab.parcels, units[0].id(), "QPF_Stack", None)
        flagged = run_checks(self.fab, checks=["gaps"])
        self.assertTrue(any("overlap" in i.message for i in flagged))

    def test_subdivide_stamps_lots(self):
        parent = self._square()
        gid = parent["GlobalID"]
        cuts = E.divide_cuts(parent.geometry(), U.parse_bearing("N 0 E"), "width", area=60)
        E.cut_parcel(self.fab, parent, cuts, self.rec, "Subdivide")
        n = E.stamp_lots(self.fab, gid, "Oaks", "2", 10, U.parse_bearing("N 90 E"))
        self.assertGreaterEqual(n, 2)
        lots = [f for f in self.fab.parcels.getFeatures() if f["Lot"] and not f["RetiredByRecord"]]
        self.assertEqual(sorted(int(f["Lot"]) for f in lots), list(range(10, 10 + n)))
        self.assertTrue(all(f["Subdivision"] == "Oaks" and f["Block"] == "2" for f in lots))
        self.assertTrue(all(f["LotWidth"] and f["LotDepth"] for f in lots))

    def test_written_calls_follow_the_square(self):
        from parcel_fabric.core import cogo as C
        from parcel_fabric.core import legal_writer as W
        from parcel_fabric.core.legal_import import parse
        self._square()
        self.fab.update_record(self.rec, scale=1.00022)
        feat = next(self.fab.parcels.getFeatures())
        ext, _holes = polygon_rings(feat.geometry())[0]
        ring = W.rotate_to(W.orient(ext, True), W.northwest_index(ext))
        text, _area = W.write(ring, "feet")
        _rows, cs, problems = parse(text.split("\n\nLESS AND EXCEPT")[0], self.fab.foot_m, boundary_only=False)
        self.assertFalse(problems, problems)
        res = C.compute(W._start(ring[0]), cs, close_to=W._start(ring[0]))
        self.assertLess(res.misclose, 0.05)
        self.assertLess(fit_gap(feat.geometry(), res.legs), 0.1)
        holder = SimpleNamespace(manager=SimpleNamespace(active=self.fab), feature=feat)
        note = LegalWriterDialog._scale_sentence(holder)
        self.assertIn("1.00022", note)

    def test_label_shows_lot_and_acres_over_one(self):
        expr = QgsExpression(styles.parcel_label_expression(self.fab.parcels.crs()))
        self.assertTrue(expr.isValid(), expr.parserErrorString())
        feat = QgsFeature(self.fab.parcels.fields())
        feat.setGeometry(box(0, 0, 400, 400))
        feat["ParcelNumber"] = "BIG"
        feat["Lot"] = "4"
        feat["LotWidth"] = 100
        feat["LotDepth"] = 200
        ctx = QgsExpressionContext()
        ctx.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(self.fab.parcels))
        ctx.setFeature(feat)
        text = expr.evaluate(ctx)
        self.assertIsNone(expr.evalErrorString() or None)
        self.assertIn("BIG", text)
        self.assertIn("Lot 4", text)
        self.assertIn("100", text)
        self.assertIn("200", text)
        self.assertIn("ac", text)

    def test_import_parcel_layer_without_book_or_page(self):
        from qgis.PyQt.QtCore import QDate, QVariant
        from qgis.core import QgsCoordinateReferenceSystem, QgsField
        bare = QgsVectorLayer("Polygon", "bare", "memory")
        bare.setCrs(QgsCoordinateReferenceSystem())
        rec = self.fab.create_record("Existing parcels", "Existing", date=QDate(2026, 11, 23))
        with self.assertRaises(RuntimeError):
            X.import_parcel_layer(self.fab, bare, "Tax", rec, None)
        with self.assertRaises(RuntimeError):
            X.open_polygon_layer(self.path)
        lyr = QgsVectorLayer("Polygon?crs=epsg:2238", "county", "memory")
        lyr.dataProvider().addAttributes([QgsField("PIN", QVariant.String)])
        lyr.updateFields()

        def feat(geom, pin):
            f = QgsFeature(lyr.fields())
            f.setGeometry(geom)
            f.setAttribute("PIN", pin)
            return f

        lyr.dataProvider().addFeatures([
            feat(box(X0, Y0, X0 + 100, Y0 + 200), "1234567-1234"),
            feat(box(X0 + 100, Y0, X0 + 180, Y0 + 200), "12-4S-16-02942-000"),
            feat(box(X0, Y0, X0 + 100, Y0 + 200), "CONDO-1"),
            feat(box(X0 + 300, Y0, X0 + 360, Y0 + 40), "THIS NUMBER IS WAY LONGER THAN THIRTY"),
            feat(box(X0 + 400, Y0, X0 + 460, Y0 + 40), ""),
        ])
        out = X.import_parcel_layer(self.fab, lyr, "Tax", rec, "PIN")
        self.assertEqual(out["copied"], 3)
        self.assertEqual(out["skipped"]["same"], 1)
        self.assertEqual(out["skipped"]["too_long"], 1)
        # the two adjoining parcels (100x200 and 80x200, sharing one side) and the
        # isolated 60x40 one: 7 + 4 unique lines, 6 + 4 unique points - the shared
        # side is traced once, not duplicated by both parcels that border it.
        self.assertEqual(out["lines_made"], 11)
        self.assertEqual(out["points_made"], 10)
        self.assertEqual(sum(1 for _ in self.fab.lines.getFeatures()), 11)
        self.assertEqual(sum(1 for _ in self.fab.points.getFeatures()), 10)
        saved = self.fab.record_by_id(rec)
        self.assertFalse(saved["Book"])
        self.assertFalse(saved["Page"])
        self.assertEqual(saved["ParcelCount"], 3)
        self.assertGreater(saved.geometry().area(), 0)
        by_num = {f["ParcelNumber"]: f for f in self.fab.parcels.getFeatures() if f["ParcelNumber"]}
        roll = by_num["1234567-1234"]
        self.assertFalse(roll["Section"])
        self.assertEqual(roll["CreatedByRecord"], rec)
        self.assertEqual(roll["IsSeed"], 0)
        strap = by_num["12-4S-16-02942-000"]
        self.assertEqual(strap["Section"], 12)
        self.assertEqual(strap["Township"], "4S")
        self.assertEqual(strap["ParcelID"], "02942-000")
        blanks = [f for f in self.fab.parcels.getFeatures() if not f["ParcelNumber"]]
        self.assertEqual(len(blanks), 1)
        again = X.import_parcel_layer(self.fab, lyr, "Tax", rec, "PIN")
        self.assertEqual(again["copied"], 0)
        self.assertEqual(self.fab.parcels.featureCount(), 3)

        stamped = QgsVectorLayer("Polygon?crs=epsg:2238", "stamped", "memory")
        stamped.dataProvider().addAttributes([QgsField("PIN", QVariant.String)])
        stamped.updateFields()
        one = QgsFeature(stamped.fields())
        one.setGeometry(box(X0 + 800, Y0, X0 + 860, Y0 + 40))
        one.setAttribute("PIN", "BOOKED-1")
        stamped.dataProvider().addFeatures([one])
        filed = X.import_parcel_layer(
            self.fab, stamped, "Tax", number_field="PIN", split_history=False, book="5", page="22")
        self.assertEqual(filed["records"], 1)
        booked = [r for r in self.fab.records.getFeatures() if r["QPF_ParcelID"] == "BOOKED-1"]
        self.assertEqual(booked[0]["Book"], "5")
        self.assertEqual(booked[0]["Page"], "22")
        sourced = QgsVectorLayer("Polygon?crs=epsg:2238", "sourced", "memory")
        sourced.dataProvider().addAttributes([
            QgsField("PIN", QVariant.String), QgsField("BOOK", QVariant.String),
            QgsField("PAGE", QVariant.String), QgsField("CREATED_BY", QVariant.String),
            QgsField("LAST_EDITED_USER", QVariant.String)])
        sourced.updateFields()

        def sourced_feat(pin, book, page, x, created="", edited=""):
            f = QgsFeature(sourced.fields())
            f.setGeometry(box(X0 + x, Y0 + 500, X0 + x + 40, Y0 + 540))
            f.setAttribute("PIN", pin)
            f.setAttribute("BOOK", book)
            f.setAttribute("PAGE", page)
            f.setAttribute("CREATED_BY", created)
            f.setAttribute("LAST_EDITED_USER", edited)
            return f

        sourced.dataProvider().addFeatures([
            sourced_feat("FIELD-1", "12", "4", 0, "ada", "bea"),
            sourced_feat("FIELD-2", "", "", 80),
        ])
        notes = []

        def note(_pct, text=None):
            if text:
                notes.append(text)

        filed_fields = X.import_parcel_layer(
            self.fab, sourced, "Tax", number_field="PIN", split_history=False,
            book="9", page="8", book_field="BOOK", page_field="PAGE", progress=note,
            created_field="CREATED_BY", edited_field="LAST_EDITED_USER")
        self.assertEqual(filed_fields["copied"], 2)
        self.assertTrue(any(t == "Creating lines: 1/2" or t == "Creating lines: 2/2" for t in notes), notes)
        by_pid = {r["QPF_ParcelID"]: r for r in self.fab.records.getFeatures()}
        self.assertEqual(by_pid["FIELD-1"]["Book"], "12")
        self.assertEqual(by_pid["FIELD-1"]["Page"], "4")
        self.assertEqual(by_pid["FIELD-2"]["Book"], "9")
        self.assertEqual(by_pid["FIELD-2"]["Page"], "8")
        self.assertEqual(by_pid["FIELD-1"]["CreatedByUser"], "ada")
        self.assertEqual(by_pid["FIELD-1"]["EditUser"], "bea")
        parcel = next(f for f in self.fab.parcels.getFeatures() if f["ParcelNumber"] == "FIELD-1")
        self.assertEqual(parcel["CreatedByUser"], "ada")
        self.assertEqual(parcel["EditUser"], "bea")
        traced = [f for f in self.fab.lines.getFeatures() if f["CreatedByRecord"] == parcel["CreatedByRecord"]]
        self.assertTrue(traced)
        self.assertTrue(all(f["CreatedByUser"] == "ada" and f["EditUser"] == "bea" for f in traced))
        blank = X.import_parcel_layer(
            self.fab, stamped, "Tax", number_field="PIN", split_history=False)
        self.assertEqual(blank["copied"], 0)

    def test_imported_adjoining_parcels_can_be_merged(self):
        # Merge (like Build) needs boundary lines, not just the parcel polygons - this is
        # what was missing before import derived them: two adjoining imported parcels could
        # not be merged because there were no lines for Build to find a closed loop in.
        from qgis.PyQt.QtCore import QVariant
        from qgis.core import QgsField
        lyr = QgsVectorLayer("Polygon?crs=epsg:2238", "county", "memory")
        lyr.dataProvider().addAttributes([QgsField("PIN", QVariant.String)])
        lyr.updateFields()

        def feat(geom, pin):
            f = QgsFeature(lyr.fields())
            f.setGeometry(geom)
            f.setAttribute("PIN", pin)
            return f

        lyr.dataProvider().addFeatures([
            feat(box(X0, Y0, X0 + 100, Y0 + 200), "100-001"),
            feat(box(X0 + 100, Y0, X0 + 180, Y0 + 200), "100-002"),
        ])
        rec = self.fab.create_record("Existing parcels", "Existing")
        out = X.import_parcel_layer(self.fab, lyr, "Tax", rec, "PIN")
        self.assertEqual(out["copied"], 2)
        self.assertEqual(out["lines_made"], 7)    # 4 + 4 sides, one shared
        self.assertEqual(out["points_made"], 6)   # 4 + 4 corners, two shared
        again_l, again_p = X.derive_lines_and_points(
            self.fab, "Tax", [f["GlobalID"] for f in self.fab.parcels.getFeatures()])
        self.assertEqual((again_l, again_p), (0, 0))
        by_num = {f["ParcelNumber"]: f for f in self.fab.parcels.getFeatures()}
        parents = [by_num["100-001"], by_num["100-002"]]
        res = E.merge_parcels(self.fab, parents, self.rec)
        self.assertEqual(len(res.children), 1)
        merged = next(self.fab.parcels.getFeatures(
            QgsFeatureRequest().setFilterExpression('"RetiredByRecord" IS NULL')))
        self.assertAlmostEqual(merged.geometry().area(), 100 * 200 + 80 * 200, places=3)
        self.assertEqual(sum(1 for _ in self.fab.parcels.getFeatures(
            QgsFeatureRequest().setFilterExpression('"RetiredByRecord" IS NULL'))), 1)

    def test_a_parcel_inside_another_can_be_merged(self):
        # The inner parcel has its own boundary, and one side lies on the outer
        # line without a shared corner. Merge keeps the outer outline.
        from qgis.PyQt.QtCore import QVariant
        from qgis.core import QgsField
        lyr = QgsVectorLayer("Polygon?crs=epsg:2238", "county", "memory")
        lyr.dataProvider().addAttributes([
            QgsField("PIN", QVariant.String), QgsField("PARCELID", QVariant.String)])
        lyr.updateFields()

        def feat(geom, pin, pid):
            f = QgsFeature(lyr.fields())
            f.setGeometry(geom)
            f.setAttribute("PIN", pin)
            f.setAttribute("PARCELID", pid)
            return f

        lyr.dataProvider().addFeatures([
            feat(box(X0, Y0, X0 + 200, Y0 + 200), "25-3S-15-00213-000", "213-0"),
            feat(box(X0, Y0 + 40, X0 + 90, Y0 + 90), "25-3S-15-00213-001", "25-3S-15-00213-001"),
        ])
        rec = self.fab.create_record("Existing parcels", "Existing")
        out = X.import_parcel_layer(self.fab, lyr, "Tax", rec, "PIN", parcel_id_field="PARCELID")
        self.assertEqual(out["copied"], 2, out)
        by_num = {f["ParcelNumber"]: f for f in self.fab.parcels.getFeatures()}
        self.assertEqual(by_num["25-3S-15-00213-000"]["ParcelID"], "213-0")
        self.assertEqual(by_num["25-3S-15-00213-000"]["Section"], 25)
        # A full strap does not fit the 12-character ParcelID, so the strap piece is kept.
        self.assertEqual(by_num["25-3S-15-00213-001"]["ParcelID"], "00213-001")
        parents = [by_num["25-3S-15-00213-000"], by_num["25-3S-15-00213-001"]]
        res = E.merge_parcels(self.fab, parents, self.rec, keep_number="25-3S-15-00213-001")
        merged = next(self.fab.parcels.getFeatures(
            QgsFeatureRequest().setFilterExpression('"RetiredByRecord" IS NULL')))
        self.assertEqual(merged["ParcelNumber"], "25-3S-15-00213-001")
        self.assertAlmostEqual(merged.geometry().area(), 200 * 200, places=0)
        self.assertEqual(len(res.children), 1)
        self.assertEqual(sum(1 for _ in self.fab.lines.getFeatures(
            QgsFeatureRequest().setFilterExpression('"RetiredByRecord" IS NULL'))), 4)

    def test_merge_reports_the_actual_gap_when_parcels_dont_touch(self):
        from qgis.PyQt.QtCore import QVariant
        from qgis.core import QgsField
        lyr = QgsVectorLayer("Polygon?crs=epsg:2238", "county", "memory")
        lyr.dataProvider().addAttributes([QgsField("PIN", QVariant.String)])
        lyr.updateFields()

        def feat(geom, pin):
            f = QgsFeature(lyr.fields())
            f.setGeometry(geom)
            f.setAttribute("PIN", pin)
            return f

        lyr.dataProvider().addFeatures([
            feat(box(X0, Y0, X0 + 100, Y0 + 100), "200-001"),
            feat(box(X0 + 101, Y0, X0 + 180, Y0 + 100), "200-002"),   # a real 1 ft gap, not a hair
        ])
        rec = self.fab.create_record("Existing parcels", "Existing")
        X.import_parcel_layer(self.fab, lyr, "Tax", rec, "PIN")
        by_num = {f["ParcelNumber"]: f for f in self.fab.parcels.getFeatures()}
        parents = [by_num["200-001"], by_num["200-002"]]
        with self.assertRaisesRegex(E.EditError, r"don't all touch \(the closest two are 1\.000") as ctx:
            E.merge_parcels(self.fab, parents, self.rec)
        self.assertIn("tolerance is 0.01", str(ctx.exception))

    def test_merge_joins_a_parcel_cut_from_the_side_of_its_neighbor(self):
        # A notch cut out of one side (not fully enclosed) touches the parent
        # along three edges. The old touch-check could still call that
        # "don't touch" even at zero gap - make sure it doesn't any more.
        from qgis.PyQt.QtCore import QVariant
        from qgis.core import QgsField
        lyr = QgsVectorLayer("Polygon?crs=epsg:2238", "county", "memory")
        lyr.dataProvider().addAttributes([QgsField("PIN", QVariant.String)])
        lyr.updateFields()

        def feat(geom, pin):
            f = QgsFeature(lyr.fields())
            f.setGeometry(geom)
            f.setAttribute("PIN", pin)
            return f

        whole = box(X0, Y0, X0 + 200, Y0 + 100)
        notch = box(X0 + 50, Y0, X0 + 100, Y0 + 40)
        lyr.dataProvider().addFeatures([
            feat(whole.difference(notch), "300-001"),
            feat(notch, "300-004"),
        ])
        rec = self.fab.create_record("Existing parcels", "Existing")
        X.import_parcel_layer(self.fab, lyr, "Tax", rec, "PIN")
        by_num = {f["ParcelNumber"]: f for f in self.fab.parcels.getFeatures()}
        parents = [by_num["300-001"], by_num["300-004"]]
        E.merge_parcels(self.fab, parents, self.rec)
        merged = next(self.fab.parcels.getFeatures(QgsFeatureRequest().setFilterExpression(CURRENT)))
        self.assertAlmostEqual(merged.geometry().area(), whole.area(), places=3)

    @staticmethod
    def _county_layer(rows):
        from qgis.core import QgsField
        from qgis.PyQt.QtCore import QVariant
        lyr = QgsVectorLayer("Polygon?crs=epsg:2238", "county", "memory")
        lyr.dataProvider().addAttributes([QgsField("PIN", QVariant.String)])
        lyr.updateFields()
        feats = []
        for geom, pin in rows:
            f = QgsFeature(lyr.fields())
            f.setGeometry(geom)
            f.setAttribute("PIN", pin)
            feats.append(f)
        lyr.dataProvider().addFeatures(feats)
        return lyr

    def test_sync_retires_a_parcel_missing_from_the_source(self):
        first = self._county_layer([
            (box(X0, Y0, X0 + 100, Y0 + 100), "400-001"),
            (box(X0 + 100, Y0, X0 + 200, Y0 + 100), "400-002"),
        ])
        rec = self.fab.create_record("Existing parcels", "Existing")
        X.import_parcel_layer(self.fab, first, "Tax", rec, "PIN")
        later = self._county_layer([(box(X0, Y0, X0 + 100, Y0 + 100), "400-001")])  # 400-002 dropped
        res = sync_parcel_layer(self.fab, later, "Tax", self.rec, "PIN")
        self.assertEqual(res["retired"], 1)
        self.assertEqual(res["missing"], ["400-002"])
        current = {f["ParcelNumber"] for f in self.fab.parcels.getFeatures(
            QgsFeatureRequest().setFilterExpression(CURRENT))}
        self.assertEqual(current, {"400-001"})

    def test_sync_adds_a_parcel_new_in_the_source(self):
        first = self._county_layer([(box(X0, Y0, X0 + 100, Y0 + 100), "401-001")])
        rec = self.fab.create_record("Existing parcels", "Existing")
        X.import_parcel_layer(self.fab, first, "Tax", rec, "PIN")
        later = self._county_layer([
            (box(X0, Y0, X0 + 100, Y0 + 100), "401-001"),
            (box(X0 + 100, Y0, X0 + 200, Y0 + 100), "401-002"),   # new
        ])
        res = sync_parcel_layer(self.fab, later, "Tax", self.rec, "PIN")
        self.assertEqual(res["copied"], 1)
        self.assertEqual(res["retired"], 0)
        current = {f["ParcelNumber"] for f in self.fab.parcels.getFeatures(
            QgsFeatureRequest().setFilterExpression(CURRENT))}
        self.assertEqual(current, {"401-001", "401-002"})

    def test_sync_flags_a_changed_outline_instead_of_touching_it(self):
        first = self._county_layer([(box(X0, Y0, X0 + 100, Y0 + 100), "402-001")])
        rec = self.fab.create_record("Existing parcels", "Existing")
        X.import_parcel_layer(self.fab, first, "Tax", rec, "PIN")
        before = next(self.fab.parcels.getFeatures(QgsFeatureRequest().setFilterExpression(CURRENT))).geometry()
        later = self._county_layer([(box(X0, Y0, X0 + 100, Y0 + 150), "402-001")])   # resurveyed bigger
        res = sync_parcel_layer(self.fab, later, "Tax", self.rec, "PIN")
        self.assertEqual(res["changed"], [{"number": "402-001", "old_area": 10000.0, "new_area": 15000.0}])
        after = next(self.fab.parcels.getFeatures(QgsFeatureRequest().setFilterExpression(CURRENT))).geometry()
        self.assertAlmostEqual(after.area(), before.area(), places=3)   # untouched

    def test_sync_dry_run_previews_without_changing_anything(self):
        first = self._county_layer([(box(X0, Y0, X0 + 100, Y0 + 100), "404-001")])
        rec = self.fab.create_record("Existing parcels", "Existing")
        X.import_parcel_layer(self.fab, first, "Tax", rec, "PIN")
        later = self._county_layer([(box(X0 + 100, Y0, X0 + 200, Y0 + 100), "404-002")])  # 001 dropped, 002 new
        res = sync_parcel_layer(self.fab, later, "Tax", None, "PIN", dry_run=True)
        self.assertEqual(res, {"missing": ["404-001"], "new": ["404-002"], "restorable": [],
                               "blocked": [], "changed": []})
        current = {f["ParcelNumber"] for f in self.fab.parcels.getFeatures(
            QgsFeatureRequest().setFilterExpression(CURRENT))}
        self.assertEqual(current, {"404-001"})   # nothing actually happened

    def test_sync_refuses_to_retire_without_an_active_record(self):
        first = self._county_layer([(box(X0, Y0, X0 + 100, Y0 + 100), "403-001")])
        rec = self.fab.create_record("Existing parcels", "Existing")
        X.import_parcel_layer(self.fab, first, "Tax", rec, "PIN")
        later = self._county_layer([])
        with self.assertRaisesRegex(RuntimeError, "no longer in the source"):
            sync_parcel_layer(self.fab, later, "Tax", None, "PIN")

    def test_sync_restores_a_parcel_erroneously_retired_by_a_failed_edit(self):
        # Mirrors a parcel left retired by an edit that then failed to build
        # (the bug 0.10.17 fixed): the source still has the number, unchanged.
        first = self._county_layer([(box(X0, Y0, X0 + 100, Y0 + 100), "406-001")])
        rec = self.fab.create_record("Existing parcels", "Existing")
        X.import_parcel_layer(self.fab, first, "Tax", rec, "PIN")
        victim = next(self.fab.parcels.getFeatures(QgsFeatureRequest().setFilterExpression(CURRENT)))
        with EditSession([self.fab.parcels], "Simulate a failed merge") as ed:
            ed.change(self.fab.parcels, victim.id(), "RetiredByRecord", rec)
        later = self._county_layer([(box(X0, Y0, X0 + 100, Y0 + 100), "406-001")])   # unchanged
        preview = sync_parcel_layer(self.fab, later, "Tax", self.rec, "PIN", dry_run=True)
        self.assertEqual(preview["restorable"], ["406-001"])
        self.assertEqual(preview["new"], [])
        res = sync_parcel_layer(self.fab, later, "Tax", self.rec, "PIN")
        self.assertEqual(res["restored"], 1)
        self.assertEqual(res["copied"], 0)
        current = list(self.fab.parcels.getFeatures(QgsFeatureRequest().setFilterExpression(CURRENT)))
        self.assertEqual([f["ParcelNumber"] for f in current], ["406-001"])
        self.assertEqual(current[0].id(), victim.id())   # the very same row, not a new one
        self.assertIsNone(current[0]["RetiredByUser"])

    def test_sync_blocks_a_retired_number_whose_outline_no_longer_matches(self):
        first = self._county_layer([(box(X0, Y0, X0 + 100, Y0 + 100), "407-001")])
        rec = self.fab.create_record("Existing parcels", "Existing")
        X.import_parcel_layer(self.fab, first, "Tax", rec, "PIN")
        victim = next(self.fab.parcels.getFeatures(QgsFeatureRequest().setFilterExpression(CURRENT)))
        with EditSession([self.fab.parcels], "Simulate a failed merge") as ed:
            ed.change(self.fab.parcels, victim.id(), "RetiredByRecord", rec)
        later = self._county_layer([(box(X0, Y0, X0 + 100, Y0 + 300), "407-001")])   # very different shape
        res = sync_parcel_layer(self.fab, later, "Tax", self.rec, "PIN")
        self.assertEqual(res["restored"], 0)
        self.assertEqual(res["copied"], 0)   # not silently re-imported under a used number either
        self.assertEqual(len(res["blocked"]), 1)
        self.assertEqual(res["blocked"][0]["number"], "407-001")

    def test_sync_button_in_the_exchange_dialog_runs_end_to_end(self):
        from unittest.mock import Mock, patch
        from qgis.gui import QgsMapCanvas
        from qgis.PyQt.QtWidgets import QWidget
        from parcel_fabric.ui.exchange_dialog import ExchangeDialog

        first = self._county_layer([(box(X0, Y0, X0 + 100, Y0 + 100), "405-001")])
        rec = self.fab.create_record("Existing parcels", "Existing")
        X.import_parcel_layer(self.fab, first, "Tax", rec, "PIN")
        later = self._county_layer([(box(X0 + 100, Y0, X0 + 200, Y0 + 100), "405-002")])  # 001 dropped, 002 new

        win, iface = QWidget(), Mock()
        iface.mainWindow.return_value, iface.mapCanvas.return_value = win, QgsMapCanvas()
        mgr = SimpleNamespace(active=self.fab, active_record_id=self.rec,
                              active_record=lambda: self.fab.record_by_id(self.rec), refresh=Mock())
        from parcel_fabric.ui import exchange_dialog as EX
        with patch.object(EX.QMessageBox, "question", return_value=EX.QMessageBox.StandardButton.Yes), \
                patch.object(EX.QMessageBox, "warning", side_effect=lambda *x: self.fail(x[2])), \
                patch.object(EX.QMessageBox, "critical", side_effect=lambda *x: self.fail(x[2])):
            dlg = ExchangeDialog(iface, mgr)
            dlg._file_layer = later
            dlg.num_field.setLayer(later)
            dlg.num_field.setField("PIN")
            dlg.layer_type.setCurrentText("Tax")
            dlg._sync_layer()
            dlg.close()
        current = {f["ParcelNumber"] for f in self.fab.parcels.getFeatures(
            QgsFeatureRequest().setFilterExpression(CURRENT))}
        self.assertEqual(current, {"405-002"})

    def test_import_snaps_a_shared_edge_a_hair_apart(self):
        # The shared side sits 0.009 ft apart, across a snap-grid cell, still inside
        # the 0.01 tolerance. It is one line and two corners, not a second copy.
        from qgis.PyQt.QtCore import QVariant
        from qgis.core import QgsField
        lyr = QgsVectorLayer("Polygon?crs=epsg:2238", "county", "memory")
        lyr.dataProvider().addAttributes([QgsField("PIN", QVariant.String)])
        lyr.updateFields()

        def feat(geom, pin):
            f = QgsFeature(lyr.fields())
            f.setGeometry(geom)
            f.setAttribute("PIN", pin)
            return f

        lyr.dataProvider().addFeatures([
            feat(box(X0, Y0, X0 + 100, Y0 + 200), "SNAP-1"),
            feat(box(X0 + 100 - 0.009, Y0, X0 + 180, Y0 + 200), "SNAP-2"),
        ])
        rec = self.fab.create_record("Existing parcels", "Existing")
        out = X.import_parcel_layer(self.fab, lyr, "Tax", rec, "PIN")
        self.assertEqual(out["copied"], 2)
        self.assertEqual(out["lines_made"], 7)
        self.assertEqual(out["points_made"], 6)

    def test_import_files_a_record_per_parcel_and_split_history(self):
        from qgis.PyQt.QtCore import QVariant
        from qgis.core import QgsField
        from parcel_fabric.core.parcel_numbers import unfiltered

        def ymd(value):
            if value is not None and hasattr(value, "toString"):
                return value.toString("yyyy-MM-dd")
            return "" if not value else str(value)[:10]

        def layer(name):
            lyr = QgsVectorLayer("Polygon?crs=epsg:2238", name, "memory")
            lyr.dataProvider().addAttributes([
                QgsField("PIN", QVariant.String), QgsField("DEEDDATE", QVariant.String)])
            lyr.updateFields()
            return lyr

        def feat(lyr, i, pin, when, y=0):
            f = QgsFeature(lyr.fields())
            f.setGeometry(box(X0 + i * 80, Y0 + y, X0 + i * 80 + 60, Y0 + y + 40))
            f.setAttribute("PIN", pin)
            f.setAttribute("DEEDDATE", when)
            return f

        lyr = layer("county")
        rows = [
            (0, "12-4S-16-02942-000", "2000-01-01"),
            (1, "12-4S-16-02942-001", "2010-06-01"),
            (2, "12-4S-16-02942-002", "6/15/2012"),
            (3, "R9-000", "1999-01-01"),
            (4, "R9-001", "2005-03-01"),
            (5, "55-001", "2001-01-01"),
            (6, "55-002", "2002-01-01"),
            (7, "88-000", ""),
            (8, "88-001", ""),
            (9, "", ""),
        ]
        lyr.dataProvider().addFeatures([feat(lyr, *row) for row in rows])
        out = X.import_parcel_layer(self.fab, lyr, "Tax", None, "PIN", "DEEDDATE", True, "000")
        self.assertEqual(out["copied"], 10)
        self.assertEqual(out["records"], 10)
        self.assertEqual(out["history_linked"], 4)
        # all 10 parcels are isolated (spaced 20 apart, well over tolerance): 4 sides/corners each
        self.assertEqual(sum(1 for _ in self.fab.lines.getFeatures()), 40)
        self.assertEqual(sum(1 for _ in self.fab.points.getFeatures()), 40)

        by_pid = {}
        for rec in self.fab.records.getFeatures():
            pid = rec["QPF_ParcelID"] if rec.fields().indexOf("QPF_ParcelID") >= 0 else ""
            if pid:
                by_pid[str(pid)] = rec
        parent = by_pid["12-4S-16-02942-000"]
        self.assertEqual(parent["Name"], "12-4S-16-02942-000")
        self.assertFalse(parent["Book"])
        self.assertFalse(parent["Page"])
        self.assertEqual(parent["ParcelCount"], 1)
        child = by_pid["12-4S-16-02942-001"]
        self.assertEqual(child["ParcelCount"], 1)
        self.assertGreater(child.geometry().area(), 0)
        self.assertEqual(ymd(child["RecordedDate"]), "2010-06-01")
        self.assertEqual(ymd(by_pid["12-4S-16-02942-002"]["RecordedDate"]), "2012-06-15")

        alln = {f["ParcelNumber"]: f for f in unfiltered(self.fab.parcels).getFeatures() if f["ParcelNumber"]}
        self.assertEqual(alln["12-4S-16-02942-000"]["Section"], 12)
        self.assertEqual(alln["12-4S-16-02942-000"]["SplitNo"], 0)
        self.assertEqual([n for n, f in alln.items() if f["RetiredByRecord"]], [])   # a later child date isn't a split
        self.assertEqual(alln["12-4S-16-02942-001"]["ParentNumber"], "12-4S-16-02942-000")
        self.assertFalse(alln["12-4S-16-02942-000"]["QPF_EndDate"])
        self.assertEqual({r["Operation"] for r in self.fab.lineage.getFeatures()}, {"Inferred split"})
        self.assertEqual(X.change_rows(self.fab, ""), [])
        pairs = {(r["ParentNumber"], r["ChildNumber"]) for r in self.fab.lineage.getFeatures()}
        self.assertIn(("12-4S-16-02942-000", "12-4S-16-02942-001"), pairs)
        self.assertIn(("12-4S-16-02942-000", "12-4S-16-02942-002"), pairs)
        self.assertIn(("R9-000", "R9-001"), pairs)
        self.assertIn(("88-000", "88-001"), pairs)
        self.assertFalse(any(str(a).startswith("55-") or str(b).startswith("55-") for a, b in pairs))  # don't touch

        off = layer("off")
        off.dataProvider().addFeatures([
            feat(off, 0, "77-000", "2000-01-01", 100), feat(off, 1, "77-001", "2011-01-01", 100)])
        quiet = X.import_parcel_layer(self.fab, off, "Tax", None, "PIN", "DEEDDATE", False, "000")
        self.assertEqual(quiet["copied"], 2)
        self.assertEqual(quiet["history_linked"], 0)
        later = {f["ParcelNumber"]: f for f in unfiltered(self.fab.parcels).getFeatures()
                 if f["ParcelNumber"] in ("77-000", "77-001")}
        self.assertFalse(later["77-000"]["RetiredByRecord"])

        custom = layer("custom")
        custom.dataProvider().addFeatures([
            feat(custom, 0, "66-001", "2001-01-01", 200), feat(custom, 1, "66-002", "2003-01-01", 200)])
        spun = X.import_parcel_layer(self.fab, custom, "Tax", None, "PIN", "DEEDDATE", True, "1")
        self.assertEqual(spun["history_linked"], 1)
        spun_rows = {f["ParcelNumber"]: f for f in unfiltered(self.fab.parcels).getFeatures()
                     if f["ParcelNumber"] in ("66-001", "66-002")}
        self.assertFalse(spun_rows["66-001"]["RetiredByRecord"])
        self.assertEqual(spun_rows["66-002"]["ParentNumber"], "66-001")

        wide = layer("wide")
        wide.dataProvider().addFeatures([
            feat(wide, 0, "1234567-0000", "2000-01-01", 300),
            feat(wide, 1, "1234567-0001", "2004-01-01", 300)])
        four = X.import_parcel_layer(
            self.fab, wide, "Tax", None, "PIN", "DEEDDATE", True, "0", "-", 4)
        self.assertEqual(four["history_linked"], 1)

    def county(self, *rows):
        from qgis.PyQt.QtCore import QVariant
        from qgis.core import QgsField
        lyr = QgsVectorLayer("Polygon?crs=epsg:2238", "county", "memory")
        lyr.dataProvider().addAttributes([QgsField("PIN", QVariant.String), QgsField("EDITED", QVariant.String)])
        lyr.updateFields()
        for (x0, y0, x1, y1), pin, when in rows:
            f = QgsFeature(lyr.fields())
            f.setGeometry(box(X0 + x0, Y0 + y0, X0 + x1, Y0 + y1))
            f.setAttributes([pin, when])
            lyr.dataProvider().addFeatures([f])
        return lyr

    def links(self):
        return sorted((r["ParentNumber"][-3:], r["ChildNumber"][-3:]) for r in self.fab.lineage.getFeatures())

    def test_a_split_is_linked_to_the_parcel_it_was_cut_out_of(self):
        n = "12-4S-16-02950-"
        lyr = self.county(((0, 0, 900, 400), n + "002", "2023-02-02"),
                          ((600, 600, 900, 800), n + "004", "2024-10-23"),          # a corner of 001
                          ((200, 800, 300, 900), n + "005", ""),                    # beside 001, not out of it
                          ((5000, 0, 5100, 100), n + "007", ""))
        f = QgsFeature(lyr.fields())
        f.setGeometry(box(X0, Y0 + 400, X0 + 900, Y0 + 800).difference(box(X0 + 600, Y0 + 600, X0 + 900, Y0 + 800)))
        f.setAttributes([n + "001", "2024-10-23"])
        lyr.dataProvider().addFeatures([f])
        out = X.import_parcel_layer(self.fab, lyr, "Tax", None, "PIN", "EDITED")
        self.assertEqual(out["copied"], 5)
        self.assertEqual(self.links(), [("001", "004")])
        self.assertEqual(X._link_splits(self.fab, "Tax", 0, {n[:-1]}), 0)
        X.import_parcel_layer(self.fab, self.county(((0, 2000, 900, 2400), n + "000", "")), "Tax", None, "PIN")
        # the original only takes the ones with no better evidence
        self.assertEqual(self.links(), [("000", "001"), ("000", "002"), ("000", "005"), ("000", "007"), ("001", "004")])

    def test_import_into_meters_and_web_mercator(self):
        import os
        from parcel_fabric.core import schema as S
        from parcel_fabric.core.fabric import Fabric
        for crs, size in (("EPSG:26917", 0.3048006096 ** 2), ("EPSG:3857", None)):
            path = os.path.join(self.tmp, crs[5:] + ".gpkg")
            S.create_fabric("ogr", path, "", "", ["Tax"], crs, {"plss_use_default": "0"})
            fab = Fabric.open("ogr", path)
            out = X.import_parcel_layer(fab, self.county(
                ((0, 0, 100, 100), "12-4S-16-02942-000", ""), ((100, 0, 200, 100), "12-4S-16-02942-001", "")),
                "Tax", None, "PIN")
            self.assertEqual((out["copied"], out["history_linked"]), (2, 1))
            for f in fab.parcels.getFeatures():
                if size is None:
                    self.assertFalse(f["CalculatedArea"])
                else:
                    self.assertAlmostEqual(f["CalculatedArea"], 10000, delta=10)     # grid scale differs slightly

    def plat(self, kind, *rows):
        from qgis.PyQt.QtCore import QVariant
        from qgis.core import QgsField
        lyr = QgsVectorLayer("Polygon?crs=epsg:2238", kind, "memory")
        lyr.dataProvider().addAttributes([QgsField(n, QVariant.String) for n in ("A", "B", "SUB", "PIN")])
        lyr.updateFields()
        for (x0, y0, x1, y1), *vals in rows:
            f = QgsFeature(lyr.fields())
            f.setGeometry(box(X0 + x0, Y0 + y0, X0 + x1, Y0 + y1))
            f.setAttributes(list(vals) + [""] * (4 - len(vals)))
            lyr.dataProvider().addFeatures([f])
        return lyr

    def platted(self):
        out = {}
        for f in self.fab.parcels.getFeatures():
            out[f["ParcelNumber"]] = tuple(f[k] or "" for k in ("Subdivision", "Block", "Lot"))
        return out

    PARCELS = (((0, 0, 300, 100), "P-ONE", ""),             # lots 1-3
               ((300, 0, 400, 100), "P-TWO", ""),           # lot 4 and, by number, lot 9 elsewhere
               ((400, 0, 450, 100), "P-HALF", ""),          # the west half of lot 5
               ((1000, 0, 1100, 100), "P-FAR", ""),         # outside the plat, beside its edge
               ((0, 100, 500, 300), "P-BLOCKB", ""))        # a whole block without lots

    def imports(self):
        lots = self.plat("lots", *[((100 * k, 0.4, 100 * k + 100, 99.8), str(k + 1), "", "OAKS UNIT 1") for k in range(5)],
                         ((2000, 0, 2100, 100), "9", "", "OAKS UNIT 1", "P-TWO"), ((2000, 200, 2100, 300), "", "", ""))
        blocks = self.plat("blocks", ((0, 0, 500, 100), "A"), ((0, 100, 500, 300), "B"))
        subs = self.plat("subs", ((-1, -1, 501, 301), "OAKS"), ((990, 0, 1005, 100), "EDGE"))
        return {"parcels": lambda: X.import_parcel_layer(self.fab, self.county(*self.PARCELS), "Tax", None, "PIN"),
                "lots": lambda: X.import_lots(self.fab, lots, "A", "SUB", "B", "PIN"),
                "blocks": lambda: X.import_blocks(self.fab, blocks, "A", "SUB"),
                "subs": lambda: X.import_subdivisions(self.fab, subs, "A")}

    def test_lot_block_and_subdivision_reach_the_parcels_in_any_order(self):
        import itertools
        want = {"P-ONE": ("OAKS", "A", "1-3"), "P-TWO": ("OAKS", "A", "4,9"), "P-HALF": ("OAKS", "A", "5"),
                "P-FAR": ("", "", ""), "P-BLOCKB": ("OAKS", "B", "")}
        for order in itertools.permutations(("parcels", "lots", "blocks", "subs")):
            with self.subTest(order=order):
                jobs = self.imports()
                results = [jobs[name]() for name in order]
                self.assertEqual(self.platted(), want)
                self.assertTrue(all("plat" in r for r in results))
                again = jobs["lots"]()
                self.assertEqual((again["copied"], sum(again["plat"].values())), (0, 0))
                self.tearDown()
                self.setUp()

    def test_typed_lot_and_subdivision_are_kept_and_long_lists_left_blank(self):
        X.import_parcel_layer(self.fab, self.county(
            ((0, 0, 300, 100), "P-ONE", ""), ((0, 200, 4000, 300), "P-MANY", "")), "Tax", None, "PIN")
        one = next(f for f in self.fab.parcels.getFeatures() if f["ParcelNumber"] == "P-ONE")
        from parcel_fabric.core.fabric import EditSession
        with EditSession([self.fab.parcels]) as ed:
            ed.change(self.fab.parcels, one.id(), "Lot", "TRACT 7")
            ed.change(self.fab.parcels, one.id(), "Subdivision", "MY NAME")
        out = X.import_lots(self.fab, self.plat(
            "lots", ((0, 0, 300, 100), "1", "", "OAKS"),
            *[((100 * k, 200, 100 * k + 100, 300), str(2 * k + 1), "", "OAKS") for k in range(40)]), "A", "SUB", "B", "PIN")
        X.import_subdivisions(self.fab, self.plat("subs", ((-1, -1, 4001, 301), "OAKS PLAT")), "A")
        self.assertEqual(self.platted(), {"P-ONE": ("MY NAME", "", "TRACT 7"), "P-MANY": ("OAKS PLAT", "", "")})
        self.assertEqual(out["plat"]["too_long"], 1)
        self.fab.parcels.startEditing()
        with self.assertRaises(RuntimeError):
            X.import_blocks(self.fab, self.plat("blocks", ((0, 0, 300, 100), "A")), "A", "SUB")
        self.assertEqual(self.fab.blocks.featureCount(), 0)
        self.fab.parcels.rollBack()

    def test_the_best_fitting_of_overlapping_lots_counts(self):
        X.import_parcel_layer(self.fab, self.county(
            ((0, 0, 100, 100), "P-LOT", ""), ((100, 0, 300, 100), "P-TWO", ""), ((0, 300, 1000, 900), "P-GOVT", ""),
            ((500, 0, 600, 100), "P-REST", "")), "Tax", None, "PIN")
        name = "GOV'T LOT 4 SECTION 20-12S-99E"
        out = X.import_lots(self.fab, self.plat(
            "lots", ((0, 0, 1000, 300), name), ((0, 0, 101, 100), "23"), ((0, 0, 100, 100), "", "", "OAKS"), ((100, 0, 200, 100), "24"),
            ((200, 0, 300, 100), "25"), ((40, 40, 60, 60), "WELL"),
            ((0, 300, 1000, 900), "GOV'T LOT 5"), ((0, 300, 50, 350), "1")), "A", "SUB", "B", "PIN")
        self.assertEqual({k: v[2] for k, v in self.platted().items()},
                         {"P-LOT": "23", "P-TWO": "24,25", "P-GOVT": "GOV'T LOT 5", "P-REST": name})
        self.assertEqual(out["plat"]["too_long"], 0)
        self.assertIn(name, [f["Lot"] for f in self.fab.lots.getFeatures()])

    def test_joined_lot_numbers(self):
        from parcel_fabric.core.plat import join_values
        for values, text in ((["3", "1", "2", "5"], "1-3,5"), (["1", "2"], "1,2"), (["10", "9", "A", "9"], "9,10,A"),
                             (["", "7"], "7"), ([], ""), (["LOT 10", "LOT 9"], "LOT 9,LOT 10")):
            self.assertEqual(join_values(values, 16), text)

    def test_neighbouring_lots_are_not_splits_of_each_other(self):
        n = "12-4S-16-02951-"
        X.import_parcel_layer(self.fab, self.county(*[
            ((100 * k, 0, 100 * k + 100, 150), n + "%03d" % (101 + k), "202%d-01-01" % k) for k in range(5)]),
            "Tax", None, "PIN", "EDITED")
        self.assertEqual(self.links(), [])

    def test_a_parcel_with_a_recorded_parent_is_not_given_another(self):
        from parcel_fabric.core import lineage as L
        n = "12-4S-16-02952-"
        X.import_parcel_layer(self.fab, self.county(
            ((0, 0, 100, 100), n + "002", ""), ((100, 0, 200, 100), n + "003", "")), "Tax", None, "PIN", None, False)
        have = {f["ParcelNumber"]: f for f in self.fab.parcels.getFeatures()}
        L.add(self.fab, None, "Split", [have[n + "002"]], [have[n + "003"]])
        X.import_parcel_layer(self.fab, self.county(((0, 100, 200, 200), n + "000", "")), "Tax", None, "PIN")
        self.assertEqual(self.links(), [("000", "002"), ("002", "003")])

    def test_polygons_sharing_a_number_become_one_parcel_and_placeholders_stay(self):
        n, zero = "12-4S-16-02960-000", "00-00-00-00000-000"
        rows = [((0, 0, 100, 100), n, "2024-01-01"), ((300, 0, 400, 100), n, "2024-01-01"),
                ((600, 0, 700, 100), n, "2024-01-01"), ((0, 300, 50, 350), zero, ""), ((100, 300, 150, 350), zero, ""),
                ((1000, 0, 1100, 100), "12-4S-16-02961-000", ""), ((1050, 0, 1150, 100), "12-4S-16-02961-000", "")]
        out = X.import_parcel_layer(self.fab, self.county(*rows), "Tax", None, "PIN", "EDITED")
        self.assertEqual((out["copied"], out["joined"], out["overlapping"], sum(out["skipped"].values())), (5, 1, 1, 0))
        feats = list(self.fab.parcels.getFeatures())
        self.assertAlmostEqual(sum(f.geometry().area() for f in feats), 3 * 10000 + 2 * 2500 + 2 * 10000, places=3)
        parcel, = [f for f in feats if f["ParcelNumber"] == n]
        geom = parcel.geometry()
        self.assertEqual(geom.constGet().numGeometries(), 3)
        self.assertAlmostEqual(parcel["CalculatedArea"], 30000, places=3)
        self.assertAlmostEqual(max(f["CalculatedArea"] for f in feats if not f["ParcelNumber"]), 2500, places=3)
        self.assertAlmostEqual(self.fab.record_by_id(parcel["CreatedByRecord"]).geometry().area(), 30000, places=3)
        side = X.import_parcel_layer(self.fab, self.county(
            ((0, 600, 100, 700), "12-4S-16-02962-000", ""), ((100, 600, 200, 700), "12-4S-16-02962-000", "")),
            "Tax", None, "PIN")
        self.assertEqual((side["copied"], side["joined"]), (1, 1))
        beside, = [f.geometry() for f in self.fab.parcels.getFeatures() if f["ParcelNumber"] == "12-4S-16-02962-000"]
        self.assertTrue(QgsGeometry(beside.constGet().segmentize()).isGeosValid())
        self.assertAlmostEqual(beside.area(), 20000, places=3)
        unnumbered = [f for f in feats if not f["ParcelNumber"]]
        self.assertEqual([f["Name"] for f in unnumbered], [zero, zero])
        again = X.import_parcel_layer(self.fab, self.county(*rows), "Tax", None, "PIN", "EDITED")
        self.assertEqual(again["copied"], 0)

    def test_sql_server_parcels_store_several_polygons(self):
        # SQL Server rejects MultiSurface. A new fabric is MultiPolygon, and a
        # multi-part county parcel is one straight feature.
        import json
        from qgis.core import Qgis, QgsFeature, QgsField, QgsVectorLayer, QgsWkbTypes
        from qgis.PyQt.QtCore import QVariant
        from parcel_fabric.core import schema as S
        from parcel_fabric.core.fabric import Fabric

        def parcel_wkb(provider, multipart=True):
            for role, _name, wkb, _fields in S.table_specs("", multipart, provider):
                if role == S.ROLE_PARCELS:
                    return wkb

        self.assertEqual(parcel_wkb("mssql"), Qgis.WkbType.MultiPolygon)
        self.assertEqual(parcel_wkb("postgres"), Qgis.WkbType.MultiSurface)
        self.assertEqual(parcel_wkb("ogr", False), Qgis.WkbType.CurvePolygon)

        class Conn:
            def __init__(self, types, promote=False):
                self.types = list(types)
                self.promote = promote
                self.sql = []

            def executeSql(self, sql):
                self.sql.append(sql)
                if sql.startswith("UPDATE") and self.promote:
                    self.types = ["MULTIPOLYGON"]
                if sql.startswith("SELECT"):
                    return [[t] for t in self.types]
                return []

        conn = Conn(["CURVEPOLYGON"], promote=True)
        self.assertTrue(S.mssql_parcels_multipolygon(conn, "dbo", "Parcels"))
        self.assertIn("MULTIPOLYGON", conn.sql[0])
        self.assertIn("f_table_name = 'Parcels'", conn.sql[0])
        self.assertFalse(S.mssql_parcels_multipolygon(Conn([]), "dbo", "Parcels"))
        self.assertFalse(S.mssql_parcels_multipolygon(Conn(["CURVEPOLYGON"]), "dbo", "Parcels"))

        layers = {}
        for role, name, wkb, fields in S.table_specs("", provider="mssql"):
            layer = QgsVectorLayer(QgsWkbTypes.displayString(wkb) + "?crs=epsg:2238", name, "memory")
            self.assertTrue(layer.isValid(), name)
            layer.dataProvider().addAttributes(S._fields(fields))
            layer.updateFields()
            layers[role] = layer
        settings = dict(S.DEFAULT_SETTINGS, crs="EPSG:2238", parcel_types=json.dumps(["Tax"]),
                        plss_use_default="0")
        fab = Fabric(S.new_guid(), settings, layers)
        self.assertTrue(fab.multipart)
        lyr = QgsVectorLayer("MultiPolygon?crs=epsg:2238", "county", "memory")
        lyr.dataProvider().addAttributes([QgsField("PIN", QVariant.String)])
        lyr.updateFields()
        feat = QgsFeature(lyr.fields())
        feat.setGeometry(QgsGeometry.fromMultiPolygonXY([
            box(X0, Y0, X0 + 100, Y0 + 100).asPolygon(),
            box(X0 + 300, Y0, X0 + 400, Y0 + 100).asPolygon()]))
        feat.setAttribute("PIN", "02-5S-17-09063-000")
        self.assertTrue(lyr.dataProvider().addFeatures([feat])[0])
        out = X.import_parcel_layer(fab, lyr, "Tax", None, "PIN", split_history=False)
        self.assertEqual(out["copied"], 1, out)
        self.assertEqual((out["separate"], out["joined"], out["skipped"]["multipart"]), (0, 0, 0))
        saved, = list(fab.parcels.getFeatures())
        geom = saved.geometry()
        self.assertTrue(geom.asWkt().startswith("MultiPolygon"), geom.asWkt()[:40])
        self.assertNotIn("Curve", geom.asWkt())
        self.assertEqual(geom.constGet().numGeometries(), 2)
        self.assertEqual(saved["ParcelNumber"], "02-5S-17-09063-000")
        self.assertAlmostEqual(geom.area(), 20000, places=3)

    def test_a_one_polygon_database_imports_every_part_as_current(self):
        # A table that really stores one polygon per parcel. A multi-part county
        # parcel is still current, so each polygon is its own row instead of being left out.
        import os
        from unittest.mock import patch
        from qgis.PyQt.QtCore import QVariant
        from qgis.core import QgsFeature, QgsFeatureRequest, QgsField, QgsGeometry, QgsVectorLayer
        from parcel_fabric.core import schema as S
        from parcel_fabric.core.fabric import CURRENT, Fabric

        lyr = QgsVectorLayer("MultiPolygon?crs=epsg:2238", "county", "memory")
        lyr.dataProvider().addAttributes([QgsField("PIN", QVariant.String)])
        lyr.updateFields()

        def add(geom, pin):
            feat = QgsFeature(lyr.fields())
            feat.setGeometry(geom)
            feat.setAttribute("PIN", pin)
            self.assertTrue(lyr.dataProvider().addFeatures([feat])[0])

        add(QgsGeometry.fromMultiPolygonXY([
            box(X0, Y0, X0 + 100, Y0 + 100).asPolygon(),
            box(X0 + 300, Y0, X0 + 400, Y0 + 100).asPolygon()]), "02-5S-17-09063-000")
        add(box(X0, Y0 + 300, X0 + 100, Y0 + 400), "05-4S-17-07600-000")
        add(box(X0 + 300, Y0 + 300, X0 + 400, Y0 + 400), "05-4S-17-07600-000")
        path = os.path.join(self.tmp, "onepoly.gpkg")
        with patch.object(S, "MULTIPART_PROVIDERS", ()):
            S.create_fabric("ogr", path, "", "", ["Tax"], "EPSG:2238", {"plss_use_default": "0"})
        single = Fabric.open("ogr", path)
        self.assertFalse(single.multipart)
        with patch.object(single.parcels, "source", return_value="one-polygon-table"):
            out = X.import_parcel_layer(single, lyr, "Tax", None, "PIN")
        self.assertEqual(out["skipped"], {"empty": 0, "multipart": 0, "too_long": 0, "duplicate": 0, "same": 0})
        self.assertEqual((out["copied"], out["separate"], out["joined"]), (4, 2, 0), out)
        self.assertFalse(single.multipart)
        self.assertIsNone(out["backup"])
        rows = list(single.parcels.getFeatures(QgsFeatureRequest().setFilterExpression(CURRENT)))
        self.assertEqual(sorted(f["ParcelNumber"] for f in rows), [
            "02-5S-17-09063-000", "02-5S-17-09063-000", "05-4S-17-07600-000", "05-4S-17-07600-000"])
        self.assertTrue(all(not f["RetiredByRecord"] for f in rows))
        self.assertAlmostEqual(sum(f.geometry().area() for f in rows), 40000, places=3)

    def test_import_lots_and_subdivisions(self):
        from qgis.PyQt.QtCore import QVariant
        from qgis.core import QgsField
        lots = QgsVectorLayer("Polygon?crs=epsg:2238", "lots", "memory")
        lots.dataProvider().addAttributes([
            QgsField("LOT", QVariant.String), QgsField("SUBDIVISION", QVariant.String),
            QgsField("BLOCK", QVariant.String), QgsField("PARCELNO", QVariant.String)])
        lots.updateFields()

        def lot(i, number):
            f = QgsFeature(lots.fields())
            f.setGeometry(box(X0 + i * 50, Y0, X0 + i * 50 + 40, Y0 + 80))
            f.setAttribute("LOT", number)
            f.setAttribute("SUBDIVISION", "OAKS")
            f.setAttribute("BLOCK", "2")
            f.setAttribute("PARCELNO", "12-4S-16-02942-000")
            return f

        lots.dataProvider().addFeatures([lot(0, "10"), lot(1, "11")])
        before = sum(1 for _ in self.fab.parcels.getFeatures())
        out = X.import_lots(self.fab, lots, "LOT", "SUBDIVISION", "BLOCK", "PARCELNO")
        self.assertEqual(out["copied"], 2)
        saved = list(self.fab.lots.getFeatures())
        self.assertEqual(sorted(f["Lot"] for f in saved), ["10", "11"])
        self.assertTrue(all(f["Subdivision"] == "OAKS" and f["Block"] == "2" for f in saved))
        self.assertTrue(all(f["ParcelNumber"] == "12-4S-16-02942-000" for f in saved))
        self.assertEqual(sum(1 for _ in self.fab.parcels.getFeatures()), before)
        again = X.import_lots(self.fab, lots, "LOT", "SUBDIVISION", "BLOCK", "PARCELNO")
        self.assertEqual(again["copied"], 0)
        self.assertEqual(again["skipped"]["same"], 2)

        subs = QgsVectorLayer("Polygon?crs=epsg:2238", "subs", "memory")
        subs.dataProvider().addAttributes([QgsField("NAME", QVariant.String)])
        subs.updateFields()
        one = QgsFeature(subs.fields())
        one.setGeometry(box(X0, Y0, X0 + 200, Y0 + 200))
        one.setAttribute("NAME", "OAKS")
        subs.dataProvider().addFeatures([one])
        sub_out = X.import_subdivisions(self.fab, subs, "NAME")
        self.assertEqual(sub_out["copied"], 1)
        self.assertEqual([f["Name"] for f in self.fab.subdivisions.getFeatures()], ["OAKS"])

        blocks = QgsVectorLayer("Polygon?crs=epsg:2238", "blocks", "memory")
        blocks.dataProvider().addAttributes([
            QgsField("BLOCK", QVariant.String), QgsField("SUBDIVISION", QVariant.String)])
        blocks.updateFields()
        blk = QgsFeature(blocks.fields())
        blk.setGeometry(box(X0, Y0, X0 + 200, Y0 + 200))
        blk.setAttribute("BLOCK", "2")
        blk.setAttribute("SUBDIVISION", "OAKS")
        blocks.dataProvider().addFeatures([blk])
        lines_before = sum(1 for _ in self.fab.lines.getFeatures())
        block_out = X.import_blocks(self.fab, blocks, "BLOCK", "SUBDIVISION")
        self.assertEqual(block_out["copied"], 1)
        saved_blocks = list(self.fab.blocks.getFeatures())
        self.assertEqual([(f["Block"], f["Subdivision"]) for f in saved_blocks], [("2", "OAKS")])
        again_blocks = X.import_blocks(self.fab, blocks, "BLOCK", "SUBDIVISION")
        self.assertEqual(again_blocks["copied"], 0)
        self.assertEqual(again_blocks["skipped"]["same"], 1)
        self.assertEqual(sum(1 for _ in self.fab.parcels.getFeatures()), before)
        self.assertEqual(sum(1 for _ in self.fab.lines.getFeatures()), lines_before)

    def test_a_multipart_subdivision_splits_into_rows_on_a_single_polygon_table(self):
        # Lots/Blocks/Subdivisions are always multi-polygon on a fabric created now (the fix for
        # this), but an older SQL Server fabric's table may still be single-polygon-per-row -
        # a multi-part subdivision must still import, as several same-named rows, not be skipped.
        from qgis.PyQt.QtCore import QVariant
        from qgis.core import QgsField
        target = QgsVectorLayer("CurvePolygon?crs=epsg:2238", "subdivisions", "memory")
        target.dataProvider().addAttributes([QgsField("Name", QVariant.String)])
        target.updateFields()

        source = QgsVectorLayer("MultiPolygon?crs=epsg:2238", "source", "memory")
        source.dataProvider().addAttributes([QgsField("NAME", QVariant.String)])
        source.updateFields()
        multi = QgsGeometry.collectGeometry([box(X0, Y0, X0 + 50, Y0 + 50), box(X0 + 100, Y0, X0 + 150, Y0 + 50)])
        f = QgsFeature(source.fields())
        f.setGeometry(multi)
        f.setAttribute("NAME", "SPLIT OAKS")
        source.dataProvider().addFeatures([f])

        out = X._copy_outlines(target, source, lambda feat: {"Name": feat["NAME"]}, label="subdivisions")
        self.assertEqual(out["copied"], 2)
        self.assertEqual(out["skipped"]["multipart"], 0)
        saved = list(target.getFeatures())
        self.assertEqual([f["Name"] for f in saved], ["SPLIT OAKS", "SPLIT OAKS"])
        self.assertAlmostEqual(sum(f.geometry().area() for f in saved), 50 * 50 * 2)

    def test_curved_multipolygon_imports(self):
        import os
        from unittest.mock import patch
        from qgis.PyQt.QtCore import QVariant
        from qgis.core import (
            QgsCircularString, QgsCompoundCurve, QgsCurvePolygon, QgsFeature, QgsField,
            QgsGeometry, QgsGeometryCollection, QgsLineString, QgsMultiSurface, QgsPoint,
            QgsVectorLayer, QgsWkbTypes,
        )
        from parcel_fabric.core import schema as S
        from parcel_fabric.core.fabric import Fabric

        def part(x, y):
            a, b = QgsPoint(x, y), QgsPoint(x, y + 100)
            c, d = QgsPoint(x + 100, y + 100), QgsPoint(x + 100, y)
            ring = QgsCompoundCurve()
            ring.addCurve(QgsLineString([a, b]))
            ring.addCurve(QgsCircularString(b, QgsPoint(x + 50, y + 140), c))
            ring.addCurve(QgsLineString([c, d, a]))
            poly = QgsCurvePolygon()
            poly.setExteriorRing(ring)
            return poly

        def feature(layer, geom, pin):
            feat = QgsFeature(layer.fields())
            feat.setGeometry(geom)
            feat.setAttribute("PIN", pin)
            return feat

        surface = QgsMultiSurface()
        surface.addGeometry(part(X0, Y0))
        surface.addGeometry(part(X0 + 300, Y0))
        collection = QgsGeometryCollection()
        collection.addGeometry(part(X0, Y0 + 400))
        collection.addGeometry(part(X0 + 300, Y0 + 400))
        lyr = QgsVectorLayer("MultiSurface?crs=epsg:2238", "county", "memory")
        lyr.dataProvider().addAttributes([QgsField("PIN", QVariant.String)])
        lyr.updateFields()
        self.assertTrue(lyr.dataProvider().addFeatures([
            feature(lyr, QgsGeometry(surface), "CURVE-SURFACE"),
        ])[0])
        # A file can hold a curve collection even though a memory layer will not.
        src_path = os.path.join(self.tmp, "curves.gpkg")
        from qgis.core import QgsCoordinateTransformContext, QgsVectorFileWriter
        err = QgsVectorFileWriter.writeAsVectorFormatV3(
            lyr, src_path, QgsCoordinateTransformContext(), QgsVectorFileWriter.SaveVectorOptions())
        self.assertEqual(err[0], QgsVectorFileWriter.NoError, err)
        filed = QgsVectorLayer("%s|layername=curves" % src_path, "curves", "ogr")
        if not filed.isValid():
            filed = QgsVectorLayer(src_path, "curves", "ogr")
        self.assertTrue(filed.isValid(), filed.error().message() if hasattr(filed, "error") else src_path)
        self.assertTrue(filed.dataProvider().addFeatures([
            feature(filed, QgsGeometry(collection), "CURVE-COLLECTION"),
        ])[0], "curve collection was not stored")
        rec = self.fab.create_record("Existing parcels", "Existing")
        out = X.import_parcel_layer(self.fab, filed, "Tax", rec, "PIN")
        self.assertEqual(out["copied"], 2, out)
        self.assertIsNone(out["backup"])
        saved = {f["ParcelNumber"]: f.geometry() for f in self.fab.parcels.getFeatures()}
        for number in ("CURVE-COLLECTION", "CURVE-SURFACE"):
            geom = saved[number]
            self.assertIn("MultiSurface", QgsWkbTypes.displayString(geom.wkbType()), number)
            self.assertTrue(geom.constGet().hasCurvedSegments(), number)
            self.assertEqual(geom.constGet().numGeometries(), 2, number)

        path = os.path.join(self.tmp, "single.gpkg")
        with patch.object(S, "MULTIPART_PROVIDERS", ()):
            S.create_fabric("ogr", path, "", "", ["Tax"], "EPSG:2238", {"plss_use_default": "0"})
        single = Fabric.open("ogr", path)
        self.assertFalse(single.multipart)
        rec2 = single.create_record("Existing parcels", "Existing")
        switched = X.import_parcel_layer(single, filed, "Tax", rec2, "PIN")
        self.assertEqual(switched["copied"], 2, switched)
        self.assertTrue(single.multipart)
        self.assertTrue(os.path.exists(switched["backup"]))
        for feat in single.parcels.getFeatures():
            geom = feat.geometry()
            self.assertTrue(geom.constGet().hasCurvedSegments(), feat["ParcelNumber"])
            self.assertEqual(geom.constGet().numGeometries(), 2, feat["ParcelNumber"])
