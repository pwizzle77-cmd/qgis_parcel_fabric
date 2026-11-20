"""Traverse unit regression checks using the real QGIS edit and preview code."""
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from qgis.core import QgsApplication, QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer, QgsWkbTypes
    from qgis.gui import QgsMapCanvas
    from qgis.PyQt.QtWidgets import QWidget
    HAS_QGIS = True
except ImportError:
    HAS_QGIS = False


@unittest.skipUnless(HAS_QGIS, 'Requires QGIS/Qt')
class TraverseUnitsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QgsApplication.instance() or QgsApplication([], False)
        cls.app.initQgis()

    def setUp(self):
        self.dock = None
        self.canvas = QgsMapCanvas()
        self.window = QWidget()

    def tearDown(self):
        if self.dock is not None:
            self.dock.cleanup()
            self.dock.deleteLater()
        self.canvas.deleteLater()
        self.window.deleteLater()
        QgsProject.instance().clear()

    def fabric(self, crs='EPSG:26960', foot='us_survey'):
        from parcel_fabric.core import schema as S
        from parcel_fabric.core.fabric import Fabric
        layers = {}
        for role, name, wkb, fields in S.table_specs(''):
            layer = QgsVectorLayer(QgsWkbTypes.displayString(wkb) + '?crs=' + crs, name, 'memory')
            self.assertTrue(layer.isValid(), name)
            layer.dataProvider().addAttributes(S._fields(fields))
            layer.updateFields()
            layers[role] = layer
        settings = dict(S.DEFAULT_SETTINGS, foot=foot, crs=crs, parcel_types=json.dumps(['Tax', 'Lot']),
                        number_on_build='0')
        return Fabric(S.new_guid(), settings, layers)

    def square(self):
        from parcel_fabric.core.legal_import import parse
        from parcel_fabric.core.legal_writer import write
        from parcel_fabric.core.units import US_SURVEY_FOOT_M
        # The same writer -> paste workflow reported by the user.
        ring = [('line', (0, 0), (0, 100)), ('line', (0, 100), (200, 100)),
                ('line', (200, 100), (200, 0)), ('line', (200, 0), (0, 0))]
        text, area = write(ring, unit='feet')
        _, courses, problems = parse(text, US_SURVEY_FOOT_M)
        self.assertFalse(problems, problems)
        self.assertEqual(area, 20000)
        return courses

    def traverse(self, fabric, courses=None):
        from parcel_fabric.ui.traverse_dock import TraverseDock
        record = fabric.create_record('Unit regression', 'Deed')
        manager = SimpleNamespace(active=fabric, active_record_id=record,
                                  active_record=lambda: fabric.record_by_id(record),
                                  activeFabricChanged=Mock(), fabricsChanged=Mock(), activeRecordChanged=Mock())
        self.canvas.setDestinationCrs(fabric.points.crs())
        iface = Mock()
        iface.mainWindow.return_value = self.window
        iface.mapCanvas.return_value = self.canvas
        self.dock = TraverseDock(iface, manager)
        self.dock.x.setText('500000')
        self.dock.y.setText('500000')
        self.dock.load_courses(courses or self.square())
        return record

    def test_writer_to_paste_preview_create_and_build_in_meters(self):
        fabric = self.fabric()
        self.traverse(fabric)
        factor = fabric.foot_m
        self.assertEqual(self.dock.result.area, 20000)
        preview = self.dock.band.asGeometry().boundingBox()
        self.assertAlmostEqual(preview.width(), 200 * factor, places=7)
        self.assertAlmostEqual(preview.height(), 100 * factor, places=7)
        self.dock._create()
        self.assertEqual(fabric.lines.featureCount(), 4)
        self.assertEqual(fabric.points.featureCount(), 4)
        self.assertEqual(sorted(f['Distance'] for f in fabric.lines.getFeatures()), [100, 100, 200, 200])
        parcel = next(fabric.parcels.getFeatures())
        self.assertAlmostEqual(parcel.geometry().area(), 20000 * factor ** 2, places=5)
        self.assertAlmostEqual(parcel['CalculatedArea'], 20000, places=5)
        self.assertAlmostEqual(parcel.geometry().boundingBox().width(), preview.width(), places=7)
        points = [f.geometry().asPoint() for f in fabric.points.getFeatures()]
        self.assertTrue(any(p.x() == 500000 and p.y() == 500000 for p in points))
        self.assertTrue(all(abs(f['X'] - f.geometry().asPoint().x()) < 1e-8
                            and abs(f['Y'] - f.geometry().asPoint().y()) < 1e-8 for f in fabric.points.getFeatures()))

    def test_us_survey_foot_fabric_keeps_existing_geometry(self):
        from parcel_fabric.core import cogo as C
        fabric = self.fabric('EPSG:2238')
        result = C.compute((500000, 500000), self.square())
        self.assertIs(fabric.traverse_map_legs(result.legs), result.legs)
        self.traverse(fabric)
        self.dock._create()
        parcel = next(fabric.parcels.getFeatures())
        self.assertEqual(parcel.geometry().boundingBox().width(), 200)
        self.assertEqual(parcel.geometry().area(), 20000)

    def test_international_feet_are_converted_to_survey_foot_coordinates(self):
        from parcel_fabric.core import cogo as C, units as U
        fabric = self.fabric('EPSG:2238', 'international')
        result = C.compute((500000, 500000), self.square())
        legs = fabric.traverse_map_legs(result.legs)
        self.assertAlmostEqual(legs[0].end[1] - 500000, 100 * U.INTL_FOOT_M / U.US_SURVEY_FOOT_M, places=7)
        self.assertIs(legs[0].course, result.legs[0].course)

    def test_true_curve_midpoint_center_and_cogo_are_preserved(self):
        from parcel_fabric.core import cogo as C
        fabric = self.fabric()
        courses = [C.Course(C.CURVE, math.pi / 2, None, 100, math.pi * 100), C.Course(C.LINE, 3 * math.pi / 2, 200)]
        result = C.compute((500000, 500000), courses)
        converted = fabric.traverse_map_legs(result.legs)
        for field in ('end', 'mid', 'center'):
            for axis in (0, 1):
                self.assertAlmostEqual(getattr(converted[0], field)[axis],
                    500000 + (getattr(result.legs[0], field)[axis] - 500000) * fabric.foot_m, places=7)
        fabric.add_traverse('Tax', result.legs, None)
        feature = next(f for f in fabric.lines.getFeatures() if f['Radius'] == 100)
        self.assertTrue(feature.geometry().constGet().hasCurvedSegments())
        self.assertAlmostEqual(feature.geometry().length(), math.pi * 100 * fabric.foot_m, places=6)
        self.assertEqual(feature['ArcLength'], math.pi * 100)
        self.assertEqual(result.legs[0].end, (500200, 500000))

    def test_ground_to_grid_factor_is_not_replaced_by_unit_conversion(self):
        fabric = self.fabric()
        record = self.traverse(fabric)
        fabric.update_record(record, scale=1.0002, rotation=10)
        self.dock.build_after.setChecked(False)
        self.dock._create()
        self.assertEqual(fabric.lines.featureCount(), 4)
        for line in fabric.lines.getFeatures():
            self.assertAlmostEqual(line.geometry().length(), line['Distance'] * 1.0002 * fabric.foot_m, places=6)
            self.assertEqual(line['Scale'], 1.0002)
            self.assertEqual(line['Rotation'], 10)

    def test_open_endpoint_display_and_misclose_preview_use_map_coordinates(self):
        from parcel_fabric.core import cogo as C
        fabric = self.fabric()
        self.traverse(fabric, [C.Course(C.LINE, math.pi / 2, 100)])
        self.dock.closed.setChecked(False)
        self.assertIn('Ends at %.3f, 500000.000' % (500000 + 100 * fabric.foot_m), self.dock.result_lbl.text())
        self.dock.closed.setChecked(True)
        box = self.dock.gap.asGeometry().boundingBox()
        self.assertAlmostEqual(box.width(), 100 * fabric.foot_m, places=7)

    def test_geographic_fabric_is_rejected_before_any_line_is_written(self):
        from parcel_fabric.core import cogo as C
        fabric = self.fabric('EPSG:4326')
        result = C.compute((-82.5, 30), self.square())
        with self.assertRaisesRegex(ValueError, 'projected'):
            fabric.add_traverse('Tax', result.legs, None)
        self.assertEqual(fabric.lines.featureCount(), 0)
        self.assertEqual(fabric.points.featureCount(), 0)

    def test_compare_uses_map_geometry_but_reports_feet_and_acres(self):
        fabric = self.fabric()
        self.traverse(fabric)
        layer = QgsVectorLayer('Polygon?crs=EPSG:26960', 'Reference', 'memory')
        feature = QgsFeature()
        feature.setGeometry(QgsGeometry.fromRect(self.dock.band.asGeometry().boundingBox()))
        layer.dataProvider().addFeatures([feature])
        layer.selectAll()
        self.dock.iface.activeLayer.return_value = layer
        self.dock._compare()
        self.assertIn('area 0.459 ac vs 0.459 ac', self.dock.result_lbl.text())
        self.assertIn('largest offset 0.00 ft', self.dock.result_lbl.text())

    def test_compass_adjustment_preserves_record_bearings_after_unit_conversion(self):
        fabric = self.fabric()
        courses = self.square()
        courses[-1].distance = 199
        self.traverse(fabric, courses)
        self.dock.adjust.setCurrentIndex(1)
        self.dock.build_after.setChecked(False)
        self.dock._create()
        self.assertEqual(sorted(f['Direction'] for f in fabric.lines.getFeatures()), [0, 90, 180, 270])
        self.assertEqual(fabric.points.featureCount(), 4)
        self.assertEqual(sorted(f['Distance'] for f in fabric.lines.getFeatures()), [100, 100, 199, 200])


if __name__ == '__main__':
    unittest.main()
