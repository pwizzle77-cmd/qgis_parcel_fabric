# Parcel Fabric for QGIS

A records-driven parcel fabric for QGIS, modelled on the ArcGIS Pro parcel fabric.
Every line, point and parcel belongs to the legal record (plat, deed, survey) that
created it. Field names match Pro's parcel fabric so data moves between the two.
Independent of Esri. Pure Python: QGIS, its bundled Qt and the standard library only.

**Status: 0.3.0, experimental (Phases 1 and 2).** Test it on a copy of real data before
using it for production work.

## Install

1. Run `python build_plugin.py` to make `dist/parcel_fabric-0.3.0.zip`.
2. In QGIS: **Plugins → Manage and Install Plugins → Install from ZIP**, pick the zip.
3. Enable **Parcel Fabric**. It adds a **Parcel Fabric** toolbar and menu.

Targets QGIS 3.40+ and Qt 5/6 (QGIS 4.x).

## Phase 1 workflow

1. **New Fabric** – choose a GeoPackage file or a saved database connection
   (PostgreSQL/PostGIS, SQL Server, Oracle), a projected CRS in feet, and the
   parcel types (default `Tax, Lot`). All types share one `Parcels` and one
   `Parcel_Lines` table; the `ParcelType` field says which is which. Choose US
   survey or international feet. **Add Fabric to Map** opens an existing one.
2. **Fabric Settings** – set the document folder and how files are named
   (tokens `{type} {book} {page} {name} {year} {date} {id}`, e.g. folder
   `{type}/Book {book}`, file `{type}_B{book}_P{page}`), and pick the PLSS sections
   layer and its section, township and range fields for parcel numbers.
3. **Records** panel – **New Record** with type, book and page (the name fills in
   if left blank) and **Attach...** a scan: it is copied into the document folder,
   sub-folders are created and the file is named from the record. **Edit...**
   corrects a record; if the book or page changes, the stored file can be renamed
   to match. **Document** opens the scan. New lines, points and parcels are
   stamped with the active record; hand-digitized features get it too.
3. **Traverse** – pick or type the point of beginning (snaps to fabric points),
   then type courses (`N 45-30-15 E`, `S45°30'15"W`; distances in feet, or add
   `m`, `ch`, `rd`, `lk`, `vr`). Curves take a radius, a side (R/L), an arc,
   chord, delta or tangent length, and say whether the bearing is the chord,
   tangent, radial, or tangent to the previous course. **Paste description...**
   uses COGO Workbench's parser and review step. The panel shows misclose, ratio
   and area live; Compass rule adjustment is optional. **Create lines** writes
   COGO-attributed lines (true arcs) and corner points, and can build the parcel.
4. **Build** – closed loops of current lines become parcels with true curves.
   A seed polygon (`IsSeed = 1`) inside a loop passes its name on. Misclose and
   `CalculatedArea` come from the record dimensions. The record's footprint and
   `ParcelCount` update. Building again rebuilds; it never duplicates.
   New parcels are numbered as they are built (if the PLSS layer is set).
5. **Assign Parcel Numbers** – numbers look like `12-4S-16-02942-000`. Select
   parcels, then either give them the next unused ID in their
   section-township-range (split `000`), or number them as splits of a parent:
   `-001`, `-002`... continuing from the highest split already used, north-west
   to south-east. The parent can be retired by the active record at the same
   time. Retired numbers are never reused.
6. **Check Fabric** – open loops, lines without COGO, line ends without points,
   poor misclose, stated vs calculated area over 1%, unbuilt seeds, features with
   no record. Double-click a row to zoom.

Ground to grid: set `QPF_ScaleFactor` and `QPF_Rotation` (degrees) on the record.
Traverse applies them to the geometry; lines keep the record dimensions and store
`Scale` / `Rotation`, as in Pro.

## Phase 2: editing parcels

All of these need an **active record**; whatever they change is retired by that
record (never deleted) and the new features belong to it. They work on the
features selected in **Parcels** (or **Parcel Lines**) and stay open for the next edit.

- **Split Parcel** – select one parcel, **Draw Cut Line** across it (snaps to fabric
  points; right-click to finish), **Apply**. Boundary lines the cut crosses are split
  in two (old line retired; halves keep the record distance in proportion and point
  back with `ParentLineID`). The pieces are built and numbered as splits of the
  parent (`-001`, `-002`...).
- **Divide Parcel** – equal areas, equal widths, or set-area lots with the rest as a
  remainder, with cut lines parallel to a bearing (or **Pick a Line**). **Preview**
  shows the cuts and acreages first.
- **Remainder** (Split and Divide) – every piece becomes new and the parent is
  retired, or the largest piece (for set-area lots, the leftover) stays the parent
  with its number and record.
- **Merge Parcels** – select touching parcels of one type; the lines between them
  and the parents are retired and one parcel is built. Keep one parent's number
  or take a new one.
- **Copy Parallel** – offset copies of selected lines (straight or curved) to the
  right or left, into a parcel type or Connections; corner points are added.
- **Parcel Lineage** – parents and children of a parcel, current or retired, by
  record. Export CSV, a GraphViz DOT chart, or a printable page.

Shared boundaries are never duplicated: there's no "copy lines to record" step,
because neighbours keep using the same lines. Pro's Align Parcels isn't included;
your Align Edge plugin covers that edit.

## Tables

| Table | Geometry | Notes |
| --- | --- | --- |
| `Records` | MultiPolygon | One per plat, deed or survey |
| `Points` | Point | Parcel corners |
| `Parcels` | CurvePolygon | All parcel types; `ParcelType`, `ParcelNumber` and its parts |
| `Parcel_Lines` | CompoundCurve | Boundary lines with COGO dimensions and `ParcelType` |
| `Connections` | CompoundCurve | Ties that carry COGO but bound no parcel |
| `Parcel_Lineage` | none | Parent → child rows with the record and operation |
| `QPF_Settings` | none | Parcel types, foot, tolerance, documents, numbering |

Features are retired (`RetiredByRecord`), never deleted. Pro keeps one table per
parcel type; here they share one, so an export to Pro splits on `ParcelType`.
Plugin fields are `ParcelType`, `ParcelNumber`, `Section`, `Township`, `Range`,
`ParcelID`, `SplitNo`, `ParentNumber`, `Book`, `Page` and the `QPF_` fields. **To verify against a fabric exported from Pro before 1.0:** the
Records date and type field names and the coded values of `COGOType`,
`StatedAreaUnit` and `AdjustmentConstraint`.

## Tests

    python -m pytest tests          # COGO maths, no QGIS needed

QGIS integration tests (Windows example):

    set QT_QPA_PLATFORM=offscreen
    "C:\Program Files\QGIS 4.2.3\bin\python-qgis.bat" -m unittest tests.test_qgis -v

## Roadmap

Phase 3: least-squares adjustment. Phase 4:
multi-user editing, history, subdivision layout, reports, imports.

## License

GPL-2.0-or-later. `core/wb_engine.py` and `core/wb_legal.py` are copied from
COGO Workbench (same author, same licence).
