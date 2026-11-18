# -*- coding: utf-8 -*-
"""Tie-in to the "NENA Addressing and RCL Tools" plugin.

Parcel Fabric doesn't copy that plugin's numbering rules; it asks the plugin
itself (when installed and set up) for each new lot's house number, so the
numbers follow the county's ranging, parity and spacing.
"""

import importlib

from qgis.core import QgsCoordinateTransform, QgsProject


class NenaUnavailable(Exception):
    pass


def find_plugin():
    """(plugin instance, engine module) or raise NenaUnavailable."""
    try:
        from qgis import utils
    except ImportError:
        raise NenaUnavailable("QGIS plugin registry not available.")
    for _key, inst in getattr(utils, "plugins", {}).items():
        if hasattr(inst, "_address_context") and hasattr(inst, "activate_address_tool"):
            pkg = type(inst).__module__.rsplit(".", 1)[0]
            try:
                engine = importlib.import_module(pkg + ".core.addressing.engine")
            except ImportError as e:
                raise NenaUnavailable("NENA plugin found but its addressing engine didn't load: %s" % e)
            return inst, engine
    raise NenaUnavailable("The NENA Addressing and RCL Tools plugin isn't installed or enabled.")


def context():
    """(engine, AddressContext) using the centerline and address layers chosen in the NENA plugin."""
    inst, engine = find_plugin()
    try:
        ctx = inst._address_context()        # noqa: SLF001 - the plugin's own entry point
    except Exception as e:
        raise NenaUnavailable("NENA plugin isn't set up: %s" % e)
    return engine, ctx


def plan_lots(engine, ctx, lots, src_crs):
    """Plan an address for each lot. ``lots`` [(label, QgsGeometry)] in ``src_crs``.
    The access point is the nearest centerline point to the lot; the rooftop
    is a point inside the lot. Returns [(label, plan or None, message)]."""
    xf = QgsCoordinateTransform(src_crs, ctx.rcl.crs(), QgsProject.instance())
    rows = []
    for label, geom in lots:
        try:
            roof = xf.transform(geom.pointOnSurface().asPoint())
            snapped = engine.snap_access(ctx, roof)
            if snapped is None:
                rows.append((label, None, "no centerline nearby"))
                continue
            fid, access = snapped
            plan = engine.plan_address(ctx, access, roof, segment_fid=fid)
            msg = "; ".join(plan.messages)
            if plan.result.number is None:
                msg = plan.result.message or msg or "no number"
            rows.append((label, plan, msg))
        except Exception as e:
            rows.append((label, None, str(e)))
    return rows


def commit(engine, ctx, rows):
    """Write the planned addresses into the NENA layers' edit buffers (the
    user saves them). ``rows`` [(plan, number, suffix)]. Returns the count."""
    for lyr in (ctx.addr, getattr(ctx, "access", None), getattr(ctx, "dw", None)):
        if lyr is not None and not lyr.isEditable():
            lyr.startEditing()
    n = 0
    for plan, number, suffix in rows:
        engine.commit_address(ctx, plan, number, suffix, create_driveway=False)
        n += 1
    return n
