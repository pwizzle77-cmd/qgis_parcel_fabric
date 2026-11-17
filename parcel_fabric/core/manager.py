# -*- coding: utf-8 -*-
"""Keeps track of the fabrics in the project and the active one."""

from qgis.core import QgsProject
from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal

from .fabric import Fabric

ACTIVE_FABRIC_KEY = ("ParcelFabric", "active_fabric")


class FabricManager(QObject):
    fabricsChanged = pyqtSignal()
    activeFabricChanged = pyqtSignal()
    activeRecordChanged = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.fabrics = []
        self._active_id = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self.refresh)
        p = QgsProject.instance()
        p.readProject.connect(lambda *_: self._timer.start())
        p.layersAdded.connect(lambda *_: self._timer.start())
        p.layersRemoved.connect(lambda *_: self._timer.start())
        p.cleared.connect(self.refresh)
        self.refresh()

    def refresh(self):
        old = self._active_id
        try:
            self.fabrics = Fabric.from_project()
        except Exception:  # never break QGIS because of a half-loaded project
            self.fabrics = []
        ids = [f.id for f in self.fabrics]
        saved, ok = QgsProject.instance().readEntry(*ACTIVE_FABRIC_KEY)
        if self._active_id not in ids:
            self._active_id = saved if ok and saved in ids else (ids[0] if ids else None)
        for f in self.fabrics:
            f._configure_layers()
        self.fabricsChanged.emit()
        if old != self._active_id:
            self.activeFabricChanged.emit()

    @property
    def active(self):
        for f in self.fabrics:
            if f.id == self._active_id:
                return f
        return None

    def set_active(self, fabric_id):
        if fabric_id == self._active_id:
            return
        self._active_id = fabric_id
        QgsProject.instance().writeEntry(ACTIVE_FABRIC_KEY[0], ACTIVE_FABRIC_KEY[1], fabric_id or "")
        self.activeFabricChanged.emit()

    @property
    def active_record_id(self):
        return Fabric.active_record_id()

    def set_active_record(self, gid):
        Fabric.set_active_record_id(gid)
        self.activeRecordChanged.emit(gid or "")

    def active_record(self):
        f = self.active
        return f.record_by_id(self.active_record_id) if f else None
