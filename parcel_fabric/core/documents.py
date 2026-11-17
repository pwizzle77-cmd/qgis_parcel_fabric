# -*- coding: utf-8 -*-
"""Record documents: where scans are kept and how they're named.

Pure Python.  Patterns use tokens in braces:
    {type} {book} {page} {name} {year} {date} {id}
Folder pattern example ``{type}/Book {book}`` and file pattern
``{type}_B{book}_P{page}`` give ``Plat/Book 5/Plat_B5_P22.pdf``.
"""

import os
import re
import shutil

TOKENS = ["type", "book", "page", "name", "year", "date", "id"]
DEFAULT_FOLDER = "{type}/Book {book}"
DEFAULT_NAME = "{type}_B{book}_P{page}"

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_TOKEN = re.compile(r"\{(\w+)\}")


def safe(part):
    """One file or folder name that Windows and Linux both accept."""
    s = _BAD.sub("_", str(part))
    s = re.sub(r"\s+", " ", s).strip().rstrip(". ")
    s = re.sub(r"_+", "_", s)
    if s.upper() in {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} | \
            {"LPT%d" % i for i in range(1, 10)}:
        s = "_" + s
    return s


def render(pattern, values):
    """Fill a pattern; unknown or empty tokens become empty."""
    def rep(m):
        v = values.get(m.group(1).lower())
        return "" if v is None else str(v).strip()
    return _TOKEN.sub(rep, pattern or "")


def relative_path(folder_pattern, name_pattern, values, ext):
    """Folder parts + file name, every part made safe; blank parts dropped."""
    folder = render(folder_pattern, values).replace("\\", "/")
    parts = [safe(p) for p in folder.split("/")]
    parts = [p for p in parts if p]
    name = safe(render(name_pattern, values)) or safe(values.get("id") or "document")
    ext = ext if not ext or ext.startswith(".") else "." + ext
    return parts + [name + (ext or "").lower()]


def unique(path):
    """``path`` or ``path (2)``, ``path (3)``... whichever doesn't exist."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 2
    while os.path.exists("%s (%d)%s" % (stem, i, ext)):
        i += 1
    return "%s (%d)%s" % (stem, i, ext)


def target_for(root, folder_pattern, name_pattern, values, ext):
    return os.path.join(root, *relative_path(folder_pattern, name_pattern, values, ext))


def is_inside(path, root):
    if not path or not root:
        return False
    try:
        p = os.path.normcase(os.path.abspath(path))
        r = os.path.normcase(os.path.abspath(root))
        return os.path.commonpath([p, r]) == r
    except ValueError:      # different drives on Windows
        return False


def store(src, root, folder_pattern, name_pattern, values, move=False):
    """Copy (or move) ``src`` into the document store. Returns the new path.

    Folders are created as needed and an existing file is never
    overwritten.  If ``src`` is already the target, nothing happens.
    """
    if not root:
        raise ValueError("No document folder is set (Fabric Settings).")
    if not os.path.isfile(src):
        raise ValueError("File not found: %s" % src)
    ext = os.path.splitext(src)[1]
    target = target_for(root, folder_pattern, name_pattern, values, ext)
    if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(target)):
        return target
    target = unique(target)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if move:
        shutil.move(src, target)
    else:
        shutil.copy2(src, target)
    return target


def record_values(name="", rtype="", book="", page="", date=None, gid=""):
    """Token values for a record. ``date`` may be a QDate, date or ISO text."""
    iso = ""
    if date is not None:
        if hasattr(date, "toString"):
            iso = date.toString("yyyy-MM-dd") if date.isValid() else ""
        elif hasattr(date, "isoformat"):
            iso = date.isoformat()[:10]
        else:
            iso = str(date)[:10]
    return {"type": rtype or "", "book": book or "", "page": page or "", "name": name or "",
            "date": iso, "year": iso[:4], "id": (gid or "").strip("{}")}
