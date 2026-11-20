# -*- coding: utf-8 -*-
"""Deed files -> page images (base64 PNG) for a vision model.

Images (PNG, JPG, TIF including multi-page TIF) are read with Qt.  PDFs are
rendered with Qt's PDF module when QGIS has it, otherwise with GDAL's PDF
driver, otherwise with Poppler's pdftoppm if it's installed.  PDFs that
already contain text give that text directly (no OCR needed).
"""

import base64
import importlib
import os
import shutil
import subprocess  # nosec B404 - only runs pdftoppm, found on PATH, with a fixed argument list
import tempfile

from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt, QT_VERSION
from qgis.PyQt.QtGui import QImage, QImageReader

MAX_SIDE = 1800       # long side in pixels sent to the model


class DeedReadError(RuntimeError):
    pass


def _to_b64(img):
    if img.isNull():
        raise DeedReadError("A document page could not be rendered.")
    if max(img.width(), img.height()) > MAX_SIDE:
        img = img.scaled(MAX_SIDE, MAX_SIDE, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    saved = img.save(buf, "PNG")
    buf.close()
    if not saved:
        raise DeedReadError("A document page could not be encoded as PNG.")
    return base64.b64encode(bytes(ba)).decode("ascii")


def image_pages(path):
    reader = QImageReader(path)
    count = max(1, reader.imageCount())
    pages = []
    for i in range(count):
        # Some TIFF handlers implement absolute page seeking but not jumpToNextImage.
        if i:
            reader = QImageReader(path)
            if not reader.jumpToImage(i):
                raise DeedReadError("Couldn't seek to image page %d." % (i + 1))
        img = reader.read()
        if img.isNull():
            raise DeedReadError("Couldn't read image page %d (%s)." % (i + 1, reader.errorString()))
        pages.append(_to_b64(img))
    return pages


def _pdf_document():
    """QGIS does not expose QtPdf in its PyQt shim on every supported build."""
    try:
        from qgis.PyQt.QtPdf import QPdfDocument
    except ImportError:
        try:
            QPdfDocument = importlib.import_module("PyQt%d.QtPdf" % (QT_VERSION >> 16)).QPdfDocument
        except ImportError:
            return None
    return QPdfDocument(None)


def pdf_text(path):
    """Text layer of a PDF, page by page, or [] if there isn't one/we can't read it."""
    doc = _pdf_document()
    if doc is None:
        return []
    doc.load(path)
    out = []
    for i in range(doc.pageCount()):
        try:
            sel = doc.getAllText(i)
            out.append(sel.text())
        except AttributeError:
            return []
    return out if any(t.strip() for t in out) else []


def _pdf_qt(path, dpi):
    doc = _pdf_document()
    if doc is None:
        return None
    doc.load(path)
    if doc.pageCount() <= 0:
        return None
    pages = []
    for i in range(doc.pageCount()):
        pts = doc.pagePointSize(i)
        size = QSize(int(pts.width() / 72.0 * dpi), int(pts.height() / 72.0 * dpi))
        pages.append(_to_b64(doc.render(i, size)))
    return pages


def _pdf_gdal(path, dpi):
    try:
        from osgeo import gdal
    except ImportError:
        return None
    gdal.SetConfigOption("GDAL_PDF_DPI", str(dpi))
    ds = gdal.Open(path)
    if ds is None:
        return None
    subs = [s[0] for s in (ds.GetSubDatasets() or [])] or [path]
    pages = []
    for src in subs:
        mem = "/vsimem/deed_page.png"
        out = gdal.Translate(mem, src, format="PNG")
        if out is None:
            continue
        out = None
        f = gdal.VSIFOpenL(mem, "rb")
        gdal.VSIFSeekL(f, 0, 2)
        n = gdal.VSIFTellL(f)
        gdal.VSIFSeekL(f, 0, 0)
        data = gdal.VSIFReadL(1, n, f)
        gdal.VSIFCloseL(f)
        gdal.Unlink(mem)
        img = QImage.fromData(data)
        if not img.isNull():
            pages.append(_to_b64(img))
    return pages or None


def _pdf_poppler(path, dpi):
    exe = shutil.which("pdftoppm")
    if not exe:
        return None
    tmp = tempfile.mkdtemp(prefix="deed_")
    try:
        subprocess.run([exe, "-r", str(int(dpi)), "-png", path, os.path.join(tmp, "p")],  # nosec B603
                       check=True, capture_output=True, timeout=300, shell=False)
        pages = []
        for name in sorted(os.listdir(tmp)):
            img = QImage(os.path.join(tmp, name))
            if not img.isNull():
                pages.append(_to_b64(img))
        return pages or None
    except (subprocess.SubprocessError, OSError):
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def pdf_pages(path, dpi=200):
    for fn in (_pdf_qt, _pdf_gdal, _pdf_poppler):
        try:
            pages = fn(path, dpi)
        except Exception:
            pages = None
        if pages:
            return pages
    raise DeedReadError("This copy of QGIS can't turn PDF pages into images. Save the deed as an image "
                        "(PNG/JPG/TIF) or install Poppler (pdftoppm) and try again.")


def load(path, dpi=200):
    """(page images as base64 PNG, text layer pages or [])."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        text = pdf_text(path)
        if text:
            return [], text
        return pdf_pages(path, dpi), []
    return image_pages(path), []
