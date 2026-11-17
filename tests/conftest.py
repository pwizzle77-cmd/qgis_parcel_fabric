"""Skip the QGIS integration tests when QGIS isn't installed (plain pytest)."""
try:
    import qgis.core  # noqa: F401
    collect_ignore = []
except ImportError:
    collect_ignore = ["test_qgis.py"]
