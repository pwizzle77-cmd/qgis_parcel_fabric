"""Build a QGIS Install-from-ZIP package using only the standard library."""
from configparser import ConfigParser
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

NAME = "parcel_fabric"


def add(archive, source, name):
    """Store a file as a regular, non-executable file (mode 0644)."""
    info = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_DEFLATED
    info.create_system = 3  # Unix, so the permission bits are honoured
    info.external_attr = (0o100644 & 0xFFFF) << 16
    archive.writestr(info, Path(source).read_bytes())


root = Path(__file__).resolve().parent
plugin = root / NAME
metadata = ConfigParser()
metadata.read(plugin / "metadata.txt", encoding="utf-8")
version = metadata["general"]["version"]
target = root / "dist" / f"{NAME}-{version}.zip"
target.parent.mkdir(exist_ok=True)
with ZipFile(target, "w", ZIP_DEFLATED) as archive:
    for path in sorted(plugin.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and (path.suffix in (".py", ".txt", ".png", ".svg") or path.name == ".flake8"):
            add(archive, path, f"{NAME}/{path.relative_to(plugin).as_posix()}")
    add(archive, root / "README.md", f"{NAME}/README.md")
    add(archive, root / "LICENSE", f"{NAME}/LICENSE")
    for path in sorted((root / "examples").iterdir()):
        if path.is_file() and path.suffix in (".csv", ".txt"):
            add(archive, path, f"{NAME}/examples/{path.name}")
with ZipFile(target) as archive:
    assert archive.testzip() is None
    assert all((i.external_attr >> 16) & 0o111 == 0 for i in archive.infolist())
    names = set(archive.namelist())
    assert {f"{NAME}/__init__.py", f"{NAME}/metadata.txt", f"{NAME}/plugin.py", f"{NAME}/icon.png",
            f"{NAME}/core/build.py", f"{NAME}/ui/traverse_dock.py", f"{NAME}/LICENSE"} <= names, names
print(target)
