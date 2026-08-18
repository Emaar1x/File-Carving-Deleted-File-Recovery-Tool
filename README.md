# Carver — File Carving & Deleted-File Recovery Tool

A Python tool that recovers files from a raw or E01 disk image by scanning for
their **signatures** (headers/footers), without relying on the file system.
When a file is deleted, the file-table entry is unlinked but the data usually
survives on disk until it is overwritten — file carving reconstructs those files
straight from the raw bytes.

**Forensic domain:** disk forensics · **Deliverable:** command-line tool / utility

## Project status

| Phase | Scope | Status |
|-------|-------|--------|
| 1 | Read raw/E01 image, scan signatures (JPG/PNG/PDF/ZIP/DOCX), carve + report offset & size | ✅ done |
| 2 | Validation & confidence, fragmentation handling, de-duplication, containment | ✅ done |
| 3 | Reporting — console + JSON + HTML + CSV | ✅ done |
| 4 | Desktop GUI + live drive acquisition | ✅ done |

**All four phases complete.** Phases 2–4 were built collaboratively as
independent parallel tasks — see [`TASKS.md`](TASKS.md).

## Features

**Phase 1 — carving engine**
- Reads **raw** (`.dd` / `.img` / `.raw`) and **E01** (EnCase) images, independent
  of any file system (raw images are memory-mapped so large images stream).
- Scans for the header signatures of **JPG, PNG, PDF, ZIP** and **DOCX**.
- Determines each file's true end (format-aware) and **carves + exports** it,
  reporting its **byte offset and size**.

**Phase 2 — accuracy & resilience**
- **Confidence scoring** — every carved file is rated `high` / `medium` / `low`
  by validating its bytes against the format structure. This also handles
  **fragmentation/truncation gracefully**: incomplete files come out `low`.
- **De-duplication** — identical files are detected by **SHA-256** and the
  duplicates are dropped from the results.
- **Containment resolution** — objects stored **inside** another file (e.g. a
  JPEG inside a PDF/DOCX) are flagged as embedded and not reported as standalone.

**Phase 3 — reporting**
- A single canonical summary (`carver/report.py`) is rendered to three formats:
  a **JSON** report (machine-readable), a self-contained **HTML** report with a
  colour-coded confidence table, and a **CSV** evidence log (one row per file).

**Phase 4 — desktop GUI & live acquisition**
- A **Tkinter GUI** (`python -m carver gui`) with three tabs: **Scan** (pick an
  image, threaded scan, colour-coded results table), **Reports** (export carved
  files + JSON/HTML/CSV), and **Acquire**.
- **Read-only drive acquisition** (`carver/acquire.py`) — image a live drive or
  volume to a `.dd` file so it can be carved (the source is only read, never
  written; imaging a physical drive needs Administrator rights).

## How signature carving works

| Format | Header (hex) | How the end is found |
|--------|--------------|----------------------|
| JPG    | `FF D8 FF`   | first `FF D9` (End of Image) |
| PNG    | `89 50 4E 47 …` | chunk-walked to the `IEND` chunk |
| PDF    | `25 50 44 46` (`%PDF`) | last `%%EOF` trailer |
| ZIP    | `50 4B 03 04` | End-of-Central-Directory (`50 4B 05 06`) |
| DOCX   | `50 4B 03 04` (a ZIP) | detected by `word/document.xml` inside |

The ZIP pass runs first and records each archive's byte range so the many
internal `PK\x03\x04` headers (and any media) inside an archive are not mistaken
for separate files.

### Confidence levels (Phase 2)

| Level | Meaning |
|-------|---------|
| high | Structure fully validates — PNG walks to `IEND`, JPEG has JFIF/EXIF markers + `FF D9`, PDF has an xref + `%%EOF`, ZIP/DOCX opens and passes its integrity check. |
| medium | Header and footer present, but validation is inconclusive. |
| low | Header found but no clean end — **truncated or fragmented**. |

## Requirements

- **Python 3.8+**, standard library only. No third-party packages are needed for
  raw images or any supported format.
- **E01 images** additionally need the optional `pyewf` library; without it the
  tool tells you to convert the image to raw with `ewfexport` instead.

## Usage

```bash
# 1. (optional) build a synthetic test image with known contents
python make_test_image.py test_image.dd --size-mb 8

# 2. scan it, export the recovered files, and write reports
python -m carver scan test_image.dd -o recovered/ --json report.json --html report.html --csv report.csv
```

Options:

```
python -m carver scan IMAGE [-o DIR] [--formats jpg,png,pdf,zip]
                            [--include-embedded] [--include-duplicates]
                            [--json FILE] [--html FILE] [--csv FILE]
```

- `-o DIR`               write recovered files here (omit to only print the summary)
- `--formats`            restrict the search; `docx` comes from `zip`
- `--include-embedded`   also export objects found inside other files
- `--include-duplicates` also export files whose SHA-256 already appeared
- `--json` / `--html` / `--csv`  write a report in that format (Phase 3)

Carved files are named `NNNN_<type>_<offset-hex>.<ext>` (e.g.
`0003_pdf_120000.pdf`) so the filename records where the data was found.

### Desktop GUI (Phase 4)

```bash
python -m carver gui        # or:  python gui.py   (pythonw gui.py for no console)
```

Three tabs: **Scan** (pick an image, run a threaded scan, browse the colour-coded
results table), **Reports** (export the carved files and save JSON/HTML/CSV), and
**Acquire** (image a live drive read-only to a `.dd` file, then scan it).

### Example output

```
========================================================================
  FILE CARVING SUMMARY (Phase 2)
========================================================================
  Image      : test_image.dd
  Image size : 8.0 MB (8,388,608 bytes)
  Recovered  : 6 file(s)
  Duplicates : 1 (identical SHA-256)
  Embedded   : 0 (inside other files)
------------------------------------------------------------------------
    #  TYPE        OFFSET       SIZE CONF     FILE
    1  png         0x1000       69 B high     0001_png_1000.png
    2  jpg        0x80000      160 B high     0002_jpg_80000.jpg
    3  pdf       0x120000      456 B high     0003_pdf_120000.pdf
    4  zip       0x1c0000      312 B high     0004_zip_1c0000.zip
    5  docx      0x260000      912 B high     0005_docx_260000.docx
    6  png       0x3c0000       33 B low      0006_png_3c0000.png  [no IEND chunk]
========================================================================
```

## Project layout

```
carver/
  __init__.py       package exports
  signatures.py     the signature catalogue (headers/footers/limits)
  image_reader.py   raw (mmap) and E01 (pyewf) image access
  engine.py         header scanning, carving, and the Phase 2 hook calls
  validation.py     Phase 2 · confidence scoring
  dedup.py          Phase 2 · SHA-256 de-duplication
  containment.py    Phase 2 · embedded-object resolution
  report.py         Phase 3 · canonical report data + dispatcher
  report_json.py    Phase 3 · JSON report
  report_html.py    Phase 3 · HTML report
  report_csv.py     Phase 3 · CSV evidence log
  acquire.py        Phase 4 · read-only drive imaging backend
  gui/
    app.py          Phase 4 · GUI shell (main window + shared AppState)
    scan_view.py    Phase 4 · Scan & Results tab
    report_view.py  Phase 4 · Reports & Export tab
    acquire_view.py Phase 4 · Drive Acquisition tab
  cli.py            the `python -m carver scan` / `gui` command line
  __main__.py       entry point
make_test_image.py  builds a synthetic image with known ground truth
gui.py              convenience GUI launcher (python gui.py)
tests/
  test_phase1.py        carving: exact offsets/sizes, DOCX classification
  test_validation.py    confidence levels
  test_dedup.py         SHA-256 duplicate detection
  test_containment.py   embedded-object flagging
  test_report_json.py   JSON report
  test_report_html.py   HTML report
  test_report_csv.py    CSV evidence log
  test_gui_scan.py      GUI Scan tab
  test_gui_report.py    GUI Reports tab
  test_acquire.py       read-only drive imaging
TASKS.md            per-phase task assignments and contribution workflow
```

## Testing

```bash
python -m pytest -q      # or run each tests/test_*.py file directly
```

The suite (17 tests) builds the synthetic image and checks the full pipeline:
files carved at their exact offset and size, clean files rated `high` and a
truncated file `low`, a duplicate detected by hash and dropped, a file embedded
inside another flagged as contained, the JSON/HTML/CSV reports generated
correctly, the GUI tabs populated, and a read-only drive image copied exactly.
(The two GUI tests need a display and skip automatically on a headless machine.)
