# -*- coding: utf-8 -*-
"""Map tool that picks a point, snapping to fabric points first."""

from qgis.core import Qgis, QgsCoordinateTransform, QgsPointLocator, QgsPointXY, QgsProject
from qgis.gui import QgsMapTool, QgsSnapIndicator
from qgis.PyQt.QtCore import Qt, pyqtSignal


class PickPointTool(QgsMapTool):
    """Emits ``picked(QgsPointXY in fabric CRS, point_name)``."""

    picked = pyqtSignal(QgsPointXY, str)

    def __init__(self, canvas, fabric_getter):
        super().__init__(canvas)
        self._fabric = fabric_getter
        self.indicator = QgsSnapIndicator(canvas)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def _to_fabric(self, pt):
        fab = self._fabric()
        xf = QgsCoordinateTransform(self.canvas().mapSettings().destinationCrs(),
                                    fab.points.crs(), QgsProject.instance())
        return xf.transform(pt)

    def _resolve(self, e):
        """Snap to a fabric point within 10 px, else the user's snapping."""
        fab = self._fabric()
        if fab is None:
            return None, ""
        tol_px = 10 * self.canvas().mapUnitsPerPixel()
        p = self._to_fabric(e.mapPoint())
        hit = fab.find_point(p.x(), p.y(), tol=max(tol_px, fab.tolerance))
        if hit is not None:
            g = hit.geometry().asPoint()
            return QgsPointXY(g.x(), g.y()), str(hit["Name"] or "fabric point")
        m = self.canvas().snappingUtils().snapToMap(e.mapPoint())
        self.indicator.setMatch(m)
        if m.isValid():
            return self._to_fabric(m.point()), "snapped"
        return p, ""

    def canvasMoveEvent(self, e):
        m = self.canvas().snappingUtils().snapToMap(e.mapPoint())
        self.indicator.setMatch(m)

    def canvasReleaseEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        pt, name = self._resolve(e)
        if pt is not None:
            self.picked.emit(pt, name)

    def deactivate(self):
        self.indicator.setMatch(QgsPointLocator.Match())
        super().deactivate()


class DrawLineTool(QgsMapTool):
    """Click points to draw a line (snaps to fabric points, then to the
    target geometry's own boundary, then to your snapping settings).
    Right-click or double-click finishes, Backspace removes the last point,
    Esc cancels.  Emits ``finished(list of QgsPointXY in fabric CRS)``."""

    finished = pyqtSignal(list)

    def __init__(self, canvas, fabric_getter, geom_getter=None):
        super().__init__(canvas)
        from qgis.gui import QgsRubberBand
        from qgis.PyQt.QtGui import QColor
        self._fabric = fabric_getter
        # Optional: the geometry (e.g. the selected parcel) a cut must start
        # and end on. A click near its edge, not just an existing corner,
        # then lands exactly on the boundary instead of a hair's-width off -
        # which a "from edge to edge" check would otherwise always reject.
        self._geom = geom_getter
        self.indicator = QgsSnapIndicator(canvas)
        self.band = QgsRubberBand(canvas, Qgis.GeometryType.Line)
        self.band.setColor(QColor(200, 0, 120))
        self.band.setWidth(2)
        self.pts = []           # canvas CRS
        self.setCursor(Qt.CursorShape.CrossCursor)

    def _snap(self, e):
        fab = self._fabric()
        mp = e.mapPoint()
        if fab is not None:
            xf = QgsCoordinateTransform(self.canvas().mapSettings().destinationCrs(),
                                        fab.points.crs(), QgsProject.instance())
            p = xf.transform(mp)
            tol = 10 * self.canvas().mapUnitsPerPixel()
            hit = fab.find_point(p.x(), p.y(), tol=max(tol, fab.tolerance))
            if hit is not None:
                return xf.transform(hit.geometry().asPoint(), Qgis.TransformDirection.Reverse)
            geom = self._geom() if self._geom is not None else None
            if geom is not None and not geom.isNull():
                sq_dist, pt, *_ = geom.closestSegmentWithContext(p)
                if sq_dist is not None and sq_dist >= 0 and sq_dist ** 0.5 <= tol:
                    return xf.transform(pt, Qgis.TransformDirection.Reverse)
        m = self.canvas().snappingUtils().snapToMap(mp)
        self.indicator.setMatch(m)
        return m.point() if m.isValid() else mp

    def _redraw(self, extra=None):
        self.band.reset(Qgis.GeometryType.Line)
        for p in self.pts + ([extra] if extra is not None else []):
            self.band.addPoint(p, False)
        self.band.show()
        self.band.updatePosition()
        self.band.update()

    def canvasMoveEvent(self, e):
        m = self.canvas().snappingUtils().snapToMap(e.mapPoint())
        self.indicator.setMatch(m)
        if self.pts:
            self._redraw(m.point() if m.isValid() else e.mapPoint())

    def canvasReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.RightButton:
            self._finish()
            return
        if e.button() == Qt.MouseButton.LeftButton:
            self.pts.append(self._snap(e))
            self._redraw()

    def canvasDoubleClickEvent(self, e):
        self._finish()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.pts = []
            self._redraw()
        elif e.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete) and self.pts:
            self.pts.pop()
            self._redraw()
        elif e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._finish()

    def _finish(self):
        # a double-click also delivers a release: drop the duplicate point
        pts = []
        for p in self.pts:
            if not pts or p.distance(pts[-1]) > 1e-9:
                pts.append(p)
        self.pts = []
        self._redraw()
        if len(pts) < 2:
            return
        fab = self._fabric()
        if fab is None:
            return
        xf = QgsCoordinateTransform(self.canvas().mapSettings().destinationCrs(),
                                    fab.points.crs(), QgsProject.instance())
        self.finished.emit([xf.transform(p) for p in pts])

    def deactivate(self):
        self.pts = []
        self._redraw()
        self.indicator.setMatch(QgsPointLocator.Match())
        super().deactivate()

    def cleanup(self):
        self.canvas().scene().removeItem(self.band)


class PickLineTool(QgsMapTool):
    """Click a fabric line; emits ``picked(QgsFeature)``."""

    picked = pyqtSignal(object)

    def __init__(self, canvas, fabric_getter):
        super().__init__(canvas)
        self._fabric = fabric_getter
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def canvasReleaseEvent(self, e):
        from qgis.core import QgsFeatureRequest, QgsGeometry, QgsRectangle
        fab = self._fabric()
        if fab is None or e.button() != Qt.MouseButton.LeftButton:
            return
        xf = QgsCoordinateTransform(self.canvas().mapSettings().destinationCrs(),
                                    fab.points.crs(), QgsProject.instance())
        p = xf.transform(e.mapPoint())
        tol = 8 * self.canvas().mapUnitsPerPixel()
        rect = QgsRectangle(p.x() - tol, p.y() - tol, p.x() + tol, p.y() + tol)
        pt = QgsGeometry.fromPointXY(p)
        best = None
        for lyr in (fab.lines, fab.connections):
            for f in lyr.getFeatures(QgsFeatureRequest().setFilterRect(rect)):
                d = f.geometry().distance(pt)
                if d <= tol and (best is None or d < best[0]):
                    best = (d, f)
        if best is not None:
            self.picked.emit(best[1])
