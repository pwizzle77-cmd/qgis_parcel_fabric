# Parcel Fabric for QGIS

A records-driven parcel fabric for QGIS, modelled on the ArcGIS Pro parcel fabric.
Every line, point and parcel belongs to the legal record (plat, deed, survey) that
created it. Field names match Pro's parcel fabric so data moves between the two.
Independent of Esri. Pure Python: QGIS, its bundled Qt and the standard library only.

**Status: 0.9.0, experimental.** Test it on a copy of real data before
using it for production work.

## Install

1. Run `python build_plugin.py` to make `dist/parcel_fabric-0.9.0.zip`.
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
   `{type}/Book {book}`, file `{type}_B{book}_P{page}`), and, if you
   don't want the default, choose your own PLSS layer(s) for parcel numbers (see below).
3. **Records** panel – **New Record** with type, book and page (the name fills in
   if left blank) and **Attach...** a scan: it is copied into the document folder,
   sub-folders are created and the file is named from the record. **Edit...**
   corrects a record; if the book or page changes, the stored file can be renamed
   to match. **Document** opens the scan. **Remove Mistake...** permanently deletes
   the lines, points and parcels the selected record created and brings back what it
   retired; the record stays. It refuses if a later record builds on it, and keeps
   boundaries other parcels share. New lines, points and parcels are
   stamped with the active record; hand-digitized features get it too.
3. **Traverse** – pick or type the point of beginning (snaps to fabric points),
   then type courses (`N 45-30-15 E`, `S45°30'15"W`; distances in feet, or add
   `m`, `ch`, `rd`, `lk`, `vr`). Curves take a radius, a side (R/L), an arc,
   chord, delta or tangent length, and say whether the bearing is the chord,
   tangent, radial, or tangent to the previous course. **Paste description...**
   uses COGO Workbench's parser and review step. The panel shows misclose, ratio
   and area live; Compass rule adjustment is optional. Nothing is written until
   **Save to Record**, which stays off until there is an active record and the
   traverse is good: misclose within the Fabric Settings ratio, ends meeting within
   the snap tolerance (use Compass rule for a small gap) and no crossing courses.
   An open traverse must start and end on fabric points, or go to Connections.
   Saving writes COGO-attributed lines (true arcs) and corner points and builds the
   parcel; if the parcel can't be built, nothing is saved.
   Start X/Y are in the fabric CRS. Traverse converts course offsets from the
   selected foot definition to the fabric's linear map units, so meter-based
   fabrics draw at the correct size too. Record COGO dimensions, closure and
   calculated area remain in feet/square feet. Geographic (degree) fabric CRSs
   are rejected. This conversion affects newly created traverses; existing lines
   are not resized.
4. **Build** – closed loops of current lines become parcels with true curves.
   A seed polygon (`IsSeed = 1`) inside a loop passes its name on. Misclose and
   `CalculatedArea` come from the record dimensions. The record's footprint and
   `ParcelCount` update. Building again rebuilds; it never duplicates.
   New parcels are numbered as they are built (PLSS comes from the BLM service unless you chose layers).
   **PLSS source.** Online PLSS data is split into layers (townships/ranges and
   sections), so the plugin looks the parcel's point up in each layer: the section
   from the sections layer, township + N/S + range from the township layer. Out of
   the box it uses the **BLM National PLSS CadNSDI** REST service
   (`gis.blm.gov/arcgis/rest/services/Cadastral/BLM_Natl_PLSS_CadNSDI/MapServer`,
   layer 2 sections `FRSTDIVNO`; layer 1 townships `TWNSHPNO`, `TWNSHPDIR`, `RANGENO`).
   Those two layers are added (unchecked) to the fabric's layer group. Only the
   features at each parcel are requested, so nothing national is downloaded.
   Fabric Settings > Parcel numbers: untick *Use the BLM National PLSS service* to
   pick your own sections layer and (optionally) a township layer with field
   mappings. Leave the township layer empty to use one combined layer with section,
   township, range (and optional N/S) fields - existing setups keep working.
   Needs internet for the default; if it's unreachable, parcels are built un-numbered
   with a message and can be numbered later.
5. **Assign Parcel Numbers** – numbers look like `12-4S-16-02942-000`. Select
   parcels, then either give them the next unused ID in their
   section-township-range (split `000`), or number them as splits of a parent:
   `-001`, `-002`... continuing from the highest split already used, north-west
   to south-east. The parent can be retired by the active record at the same
   time. Retired numbers are never reused. Each parcel also gets its parts:
   `Section` 12, `Township` 4S, `Range` 16, `ParcelID` `02942-000` (the number
   without section-township-range), `ParcelSeq` 2942 and `SplitNo` 0.
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
- **Split by a deed** (in **Traverse**) – set the deed (e.g. a quitclaim) as the active
  record, paste or type the land it conveys, and pick the point of beginning. If the
  description commences elsewhere, pick that point instead and the tie calls are run to
  the POB. Select the parent parcel and press **Split Selected Parcel**. Deed corners
  within *Fit to parent within* are held to the parent's corners and lines; a corner
  further outside is refused. Courses inside the parent become new lines with the deed's
  bearings and distances; courses along the boundary reuse the parent's lines. Check the
  acreages and largest adjustment, then choose whether the remainder keeps the number.
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

## Parcel History (map timeline)

Every parcel carries `QPF_StartDate` (the recorded date of the record that created
it) and `QPF_EndDate` (the recorded date of the record that retired it). They are
kept up to date after every build, split, divide, merge and record edit.

- The **Parcel History** layer shows every parcel ever recorded, current and
  retired, and is time-enabled: QGIS's **Temporal Controller** shows the parcel map
  as it stood on any date, and can export an animation.
- The **Parcel History** window: pick a parcel (current or retired, or type its
  number). It finds the original parcel(s) and every split, divide and merge that
  came from them, colours the family by generation, and lists each step with its
  date and record. Click a step, drag the slider or press **Play** to watch the
  parcel change on the map. **Today** shows the present.
- **History Handout** writes a printable page: for every step, the record (book,
  page, document link), what became what with acreages, and a map of that date.
- Check Fabric warns about records with no recorded date, because history can't
  place them in time.

## Descriptions, comparisons and reports

- **Write Legal Description** – works on the polygon selected in ANY layer (fabric
  parcels, the county's parcels, a sketch). Choose the coordinate system to measure
  in, the point of beginning (most northwesterly corner or one you click), direction,
  and an optional commencement tie from a point you click (e.g. a section corner).
  True curves are described with radius, central angle, arc and chord, tangent or
  non-tangent; curves that were densified into many vertices are recognised again;
  near-straight vertices are joined. Holes become LESS AND EXCEPT parts. `{str}` in
  the opening line fills in the section, township and range. A **self-check** reads
  the description back and closes it, so a wrong call can't slip through.
- **Compare with Existing Parcels** – pairs fabric parcels (selected, in view, or all)
  with an existing parcel layer, by parcel number or by overlap, and lists area
  difference, largest offset, shift and overlap. **Show Differences on Map** adds
  the slivers as a scratch layer.
- **Traverse > Compare to Selected Polygon** – how a traverse fits an existing polygon
  before you create anything.
- **Records > Closure Report** – every course, misclose, precision and area (record
  vs. coordinates vs. stated) for a record's parcels, as a printable page.

## Read Deed (AI) – optional, configurable services

**Traverse > Read Deed (AI)...** turns a deed file into Traverse courses:

1. **Read Document** – a text PDF is read directly; scanned PDFs and images (PNG, JPG,
   TIF, multi-page TIF) are turned into page images and transcribed by a vision model
   using your configured AI service. The default is [Ollama](https://ollama.com)
   on your own computer (`deepseek-ocr`; `qwen2.5vl` and `gemma4` also work).
   With Ollama, use a downloaded local model.
   The text is editable so you can fix misread characters.
2. **Find Description** – picks out the legal description (from "Commence/Begin at"
   to the last "Point of Beginning"); **Ask AI to Find It** is the fallback.
3. **Extract Calls** – COGO Workbench's parser reads the calls first. **Ask AI About
   Unread Calls** sends only the clauses it couldn't read to the model, which must
   quote the deed; an answer is rejected if its quote isn't in the deed or its numbers
   aren't in the quote. AI-read calls are shown in orange.
4. Closure and area show as you go. Nothing loads into Traverse until you tick "I
   checked every call against the deed". The deed can be attached to the active record.

PDF pages are rendered with Qt's PDF module, GDAL's PDF driver, or Poppler's
`pdftoppm`, whichever this QGIS has.

Open **Fabric Settings > AI Services**, or **AI Settings...** in Read Deed, to
select Ollama, OpenAI, LM Studio, or a custom OpenAI-compatible service. Each
service remembers its API base URL, page-reading model and description/call
model. Use **Find Models / Test Connection**, or type model IDs directly. The
page-reading model must support images. Custom services must implement
`GET /models` (optional if you type model IDs) and `POST /chat/completions`
under the base URL, with OpenAI text/image message formats. See the
[OpenAI API reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
and [LM Studio compatibility documentation](https://lmstudio.ai/docs/developer/openai-compat).

API keys use QGIS's encrypted Authentication Manager: create/select a **Basic**
configuration with Username `api-key` and the API key in Password. Keys are sent
as Bearer tokens; the username is not sent. The plugin stores only the credential
ID and service preferences in your QGIS profile, never in the shared fabric.
Switching services retains each service's configuration; Cancel discards edits.

Hosted AI requests send deed text or page images to the configured service and
may incur charges. The selected endpoint is shown in Read Deed. Opening the
dialog makes no hosted API request. Text PDFs, deterministic parsing and manual
entry need no AI key. Quote/number checks and the review checkbox apply to every
service. HTTPS is required outside loopback; proxies and redirects are disabled.

The Ollama URL must use `localhost`, `127.0.0.1`, or `::1`; HTTP proxies and
redirects are disabled, and cloud-tagged models are rejected. For server-wide
local-only operation, Ollama also supports `OLLAMA_NO_CLOUD=1` (restart Ollama
after setting it; see the [Ollama FAQ](https://docs.ollama.com/faq)).

## Least-squares adjustment (Phase 3)

**Least-Squares Adjustment** treats each fabric line's record bearing and distance
(curves by their chord) as observations between the points at its ends.

- **Consistency check** – no control: one point is held and the rest float, so record
  dimensions that disagree with their neighbours stand out (normalized residual over 3).
- **Weighted adjustment** – fixed control (`AdjustmentConstraint` = XY constrained, or
  **Mark Selected Points as Control**) and weighted control (XY weighted, with
  `QPF_StdDevXY`). Measured dimensions weigh twice a record one; computed and estimated
  ones much less.
- Results: standard error of unit weight and a chi-square test, the worst residuals
  (double-click to zoom), and **Show Results on Map** – line residuals (red = disagree),
  point movements and error ellipses (drawn enlarged).
- **Apply to Fabric** moves the points, reshapes their lines (curves keep their shape)
  and rebuilds the parcels. COGO dimensions are never changed.

The solver is built in (numpy). DynAdjust isn't wired in yet; very large networks
(thousands of points) may be slow.

## Subdivision, addressing and data exchange (0.8.0)

**Subdivide Block** lays out lots on one selected block parcel:

- **Frontage runs** – type the street bearing or **Pick Frontage Line**; lot lines run
  square to it.
- **Lots N ft wide** (the leftover is the last lot) or **N lots of equal frontage**;
  **Lots on both sides** splits the block front-to-back first (double-loaded block);
  **Start from the other end** flips where the first lot starts.
- **Zoning** – minimum area and frontage; Preview lists every lot's acres and frontage
  and flags the ones that fail. Apply cuts the block under the active record
  (operation "Subdivide"), with lineage, like Divide.

**Address Lots (NENA)** asks the *NENA Addressing and RCL Tools* plugin for each selected
lot's house number (its own ranging, parity and spacing; nearest centerline, a point
inside the lot as the rooftop). Review and untick or edit numbers, then **Write
Addresses** adds them to the NENA address layer's edits for you to save. Needs that
plugin installed with its centerline and address layers chosen; otherwise the window
says so.

**Import / Export...**

- **ArcGIS Pro fabric** – a File Geodatabase or GeoPackage export of a Pro fabric
  (`Records`, `Points`, `Connections`, `<Type>` and `<Type>_Lines`). Values copy by
  field name (GlobalIDs and record links kept); each Pro type lands in `Parcels` /
  `Parcel_Lines` with its `ParcelType` (rename it in the table first if you like); Z/M
  dropped; reprojected to the fabric's CRS. Import into an empty fabric.
- **LandXML** – Parcels from Civil 3D, Carlson, TBC... Shared lines added once, built
  under the active record, LandXML names and areas carried over. Coordinates must be in
  the fabric's CRS.
- **DXF export** – current lines, connections, points and parcels with symbols and labels,
  whole fabric or the current view.

**Shared fabrics.** Fabric Settings → **Multi-user** installs database rules (PostgreSQL,
SQL Server or GeoPackage triggers): once a feature is retired its `RetiredByRecord` can't
change, and fabric features can't be deleted. Split, Divide, Merge and Subdivide also
re-read the parcel from the database before retiring it, so two editors can't split the
same parcel. Installing triggers needs a login that may create them.

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

Features are retired (`RetiredByRecord`), never deleted, except by Remove Mistake. Pro keeps one table per
parcel type; here they share one, so an export to Pro splits on `ParcelType`.
Plugin fields are `ParcelType`, `ParcelNumber`, `Section`, `Township`, `Range`,
`ParcelID`, `ParcelSeq`, `SplitNo`, `ParentNumber`, `Book`, `Page` and the `QPF_` fields. **To verify against a fabric exported from Pro before 1.0:** the
Records date and type field names and the coded values of `COGOType`,
`StatedAreaUnit` and `AdjustmentConstraint`.

## Tests

    python -m pytest tests          # COGO maths, no QGIS needed

QGIS integration tests (Windows example):

    set QT_QPA_PLATFORM=offscreen
    "C:\Program Files\QGIS 4.2.3\bin\python-qgis.bat" -m unittest tests.test_qgis -v

## Roadmap

Before 1.0: verify field names and coded values against a real Pro export, export *to*
Pro (split on `ParcelType`), DynAdjust for very large adjustments, and branch versioning
style edit sessions for PostgreSQL.

## License

GPL-2.0-or-later. `core/wb_engine.py` and `core/wb_legal.py` are copied from
COGO Workbench (same author, same licence).
