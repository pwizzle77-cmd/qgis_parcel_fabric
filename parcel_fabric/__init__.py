"""Parcel Fabric for QGIS entry point."""


def classFactory(iface):
    from .plugin import ParcelFabricPlugin
    return ParcelFabricPlugin(iface)
