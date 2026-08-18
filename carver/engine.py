"""
The Phase 1 carving engine.

Pipeline:
    1. Scan the image for every known header.
    2. For each header, work out where the file really ends -- each format needs
       its own method (footer search, PNG chunk walk, ZIP End-Of-Central-
       Directory parsing, last %%EOF for PDF).
    3. Classify a carved ZIP as a plain archive or an Office document (DOCX etc.)
       by looking inside it.

Each recovered file is reported with its byte offset and size. Confidence
scoring, fragmentation reassembly and de-duplication are later-phase concerns
and are deliberately absent here.
"""

import io
import zipfile
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .signatures import SIGNATURE_BY_KEY, OOXML_SUBTYPES
# Phase 2 post-processing passes. Each is implemented by a separate task and is
# a no-op until then (see carver/validation.py, dedup.py, containment.py).
from .validation import assign_confidence
from .dedup import deduplicate
from .containment import resolve_containment


@dataclass
class Candidate:
    fmt: str                 # jpg, png, pdf, zip, docx, ...
    start: int               # byte offset of the header in the image
    end: int                 # exclusive byte offset where the file ends
    note: str = ""           # optional remark (e.g. truncated)

    # ---- Phase 2 fields, filled in by the post-processing passes ----
    confidence: str = "unknown"           # Task 1: "high" / "medium" / "low"
    sha256: str = ""                      # Task 2: hex content hash
    duplicate_of: Optional[int] = None    # Task 2: start offset of the first copy
    embedded_in: Optional[int] = None     # Task 3: start offset of the container

    @property
    def size(self) -> int:
        return self.end - self.start

    @property
    def ext(self) -> str:
        if self.fmt in ("docx", "xlsx", "pptx"):
            return self.fmt
        return SIGNATURE_BY_KEY[self.fmt].ext


@dataclass
class ScanResult:
    image_path: str
    image_size: int
    candidates: List[Candidate]

    @property
    def recovered(self) -> List[Candidate]:
        """Files we actually report: not a duplicate, not embedded in another."""
        return [c for c in self.candidates
                if c.duplicate_of is None and c.embedded_in is None]

    @property
    def duplicates(self) -> List[Candidate]:
        return [c for c in self.candidates if c.duplicate_of is not None]

    @property
    def embedded(self) -> List[Candidate]:
        return [c for c in self.candidates if c.embedded_in is not None]


# --------------------------------------------------------------------------
# low level helpers
# --------------------------------------------------------------------------
def _find_all(buf, pattern: bytes, start: int = 0, end: Optional[int] = None):
    """Yield every offset of ``pattern`` in buf[start:end]."""
    n = len(buf) if end is None else end
    pos = start
    while True:
        idx = buf.find(pattern, pos, n)
        if idx == -1:
            return
        yield idx
        pos = idx + 1


# --------------------------------------------------------------------------
# per-format length determination
# --------------------------------------------------------------------------
def _carve_jpg(buf, start: int) -> Tuple[int, int, str]:
    sig = SIGNATURE_BY_KEY["jpg"]
    limit = min(len(buf), start + sig.max_size)
    # Header to the first End-Of-Image marker (FF D9), as foremost/scalpel do.
    footer = buf.find(sig.footer, start + 2, limit)
    if footer == -1:
        nxt = buf.find(sig.header, start + 3, limit)
        end = nxt if nxt != -1 else min(limit, start + 64 * 1024)
        return start, end, "no End-Of-Image marker (truncated or fragmented)"
    return start, footer + len(sig.footer), ""


def _carve_png(buf, start: int) -> Tuple[int, int, str]:
    sig = SIGNATURE_BY_KEY["png"]
    limit = min(len(buf), start + sig.max_size)
    # Walk the PNG chunk structure [len(4)][type(4)][data(len)][crc(4)] to IEND.
    pos = start + 8
    last_good = start + 8
    while pos + 12 <= limit:
        length = int.from_bytes(bytes(buf[pos:pos + 4]), "big")
        ctype = bytes(buf[pos + 4:pos + 8])
        if not all(65 <= b <= 90 or 97 <= b <= 122 for b in ctype):
            break
        nxt = pos + 12 + length
        if nxt > limit:
            break
        if ctype == b"IEND":
            return start, nxt, ""
        pos = nxt
        last_good = nxt
    return start, last_good, "no IEND chunk (truncated or fragmented)"


def _carve_pdf(buf, start: int) -> Tuple[int, int, str]:
    sig = SIGNATURE_BY_KEY["pdf"]
    limit = min(len(buf), start + sig.max_size)
    next_hdr = buf.find(sig.header, start + 4, limit)
    region_end = next_hdr if next_hdr != -1 else limit
    # PDFs saved incrementally have several %%EOF markers; the real end is the
    # last one in this document's region.
    last_eof = -1
    for pos in _find_all(buf, sig.footer, start, region_end):
        last_eof = pos
    if last_eof == -1:
        return start, region_end, "no %%EOF trailer (truncated or fragmented)"
    end = last_eof + len(sig.footer)
    while end < region_end and bytes(buf[end:end + 1]) in (b"\r", b"\n"):
        end += 1
    return start, end, ""


def _carve_zip(buf, start: int) -> Tuple[str, int, int, str]:
    sig = SIGNATURE_BY_KEY["zip"]
    limit = min(len(buf), start + sig.max_size)
    eocd = buf.find(sig.footer, start + 4, limit)
    if eocd == -1:
        return "zip", start, limit, "no End-Of-Central-Directory (truncated)"
    comment_len = int.from_bytes(bytes(buf[eocd + 20:eocd + 22]), "little")
    end = min(eocd + 22 + comment_len, len(buf))
    return _classify_zip(buf, start, end), start, end, ""


def _classify_zip(buf, start: int, end: int) -> str:
    """Open the carved bytes as a ZIP to tell whether it is really an Office
    document. Real disks contain false PK signatures, so any failure to open is
    treated as 'not a real archive' rather than crashing the scan."""
    data = bytes(buf[start:end])
    try:
        names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
    except Exception:  # BadZipFile, ValueError (negative seek), OSError, ...
        return "zip"
    for marker, (fmt, _ext, _friendly) in OOXML_SUBTYPES:
        if marker in names:
            return fmt
    return "zip"


_CARVERS = {"jpg": _carve_jpg, "png": _carve_png, "pdf": _carve_pdf}


# --------------------------------------------------------------------------
# main scan
# --------------------------------------------------------------------------
def scan(image, formats: Optional[List[str]] = None, progress=None) -> ScanResult:
    """Scan an open :class:`DiskImage` and return a :class:`ScanResult`.

    ``formats`` optionally restricts the search (subset of jpg/png/pdf/zip).
    DOCX/XLSX/PPTX are discovered via the ``zip`` scan, so keep ``zip`` enabled
    to recover Office documents.

    ``progress`` is an optional callback ``progress(fmt, pos, total, found)``
    called as the scan sweeps the image; a UI can use it to drive a progress bar
    and a live count, and may raise from within it to cancel the scan.
    """
    buf = image.buf
    total = len(buf)
    active = formats or ["jpg", "png", "pdf", "zip"]
    candidates: List[Candidate] = []
    zip_ranges: List[Tuple[int, int]] = []

    # ZIP first: an archive holds many internal "PK\x03\x04" headers (and often
    # JPEG/PNG media). Carving archives up front lets us skip those inner headers
    # instead of mistaking them for separate files.
    if "zip" in active:
        sig = SIGNATURE_BY_KEY["zip"]
        skip_until = -1
        for pos in _find_all(buf, sig.header):
            if pos < skip_until:
                continue
            if progress:
                progress("zip", pos, total, len(candidates))
            fmt, s, e, note = _carve_zip(buf, pos)
            candidates.append(Candidate(fmt, s, e, note))
            zip_ranges.append((s, e))
            skip_until = e

    def _inside_archive(off: int) -> bool:
        return any(a <= off < b for a, b in zip_ranges)

    for key in ("jpg", "png", "pdf"):
        if key not in active:
            continue
        sig = SIGNATURE_BY_KEY[key]
        for pos in _find_all(buf, sig.header):
            if _inside_archive(pos):
                continue
            if progress:
                progress(key, pos, total, len(candidates))
            s, e, note = _CARVERS[key](buf, pos)
            candidates.append(Candidate(key, s, e, note))

    candidates.sort(key=lambda c: c.start)

    # ---- Phase 2 post-processing passes ---------------------------------
    # Each is owned by a separate task (see TASKS.md) and is a no-op until
    # implemented. Order: score confidence, resolve containment, then dedup.
    assign_confidence(candidates, buf)     # Task 1  -> c.confidence
    resolve_containment(candidates)        # Task 3  -> c.embedded_in
    deduplicate(candidates, buf)           # Task 2  -> c.sha256, c.duplicate_of

    return ScanResult(image.path, len(buf), candidates)
