"""
Phase 1 command line interface.

    python -m carver scan image.dd -o recovered/
    python -m carver scan image.E01 --formats jpg,png
"""

import argparse
import os
import sys
import time

from . import __version__
from .image_reader import DiskImage
from .engine import scan
from .signatures import SIGNATURE_BY_KEY


def _human(n: int) -> str:
    step = 1024.0
    v = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < step:
            return f"{v:.0f} {unit}" if unit == "B" else f"{v:.1f} {unit}"
        v /= step
    return f"{v:.1f} PB"


def _parse_formats(value: str):
    keys = [k.strip().lower() for k in value.split(",") if k.strip()]
    valid = set(SIGNATURE_BY_KEY.keys())  # jpg png pdf zip
    bad = [k for k in keys if k not in valid]
    if bad:
        raise argparse.ArgumentTypeError(
            f"unknown format(s): {', '.join(bad)}. choose from: "
            f"{', '.join(sorted(valid))} (docx/xlsx/pptx come from 'zip')")
    return keys


def _write_files(result, out_dir, include_embedded=False, include_duplicates=False):
    os.makedirs(out_dir, exist_ok=True)
    targets = list(result.recovered)
    if include_duplicates:
        targets += result.duplicates
    if include_embedded:
        targets += result.embedded
    written = 0
    with open(result.image_path, "rb") as img:
        for i, c in enumerate(targets, 1):
            name = f"{i:04d}_{c.fmt}_{c.start:x}.{c.ext}"
            img.seek(c.start)
            with open(os.path.join(out_dir, name), "wb") as fh:
                fh.write(img.read(c.size))
            written += 1
    return written


def _summary(result) -> str:
    L = []
    L.append("=" * 72)
    L.append("  FILE CARVING SUMMARY (Phase 2)")
    L.append("=" * 72)
    L.append(f"  Image      : {result.image_path}")
    L.append(f"  Image size : {_human(result.image_size)} "
             f"({result.image_size:,} bytes)")
    L.append(f"  Recovered  : {len(result.recovered)} file(s)")
    L.append(f"  Duplicates : {len(result.duplicates)} (identical SHA-256)")
    L.append(f"  Embedded   : {len(result.embedded)} (inside other files)")
    L.append("-" * 72)
    if result.recovered:
        L.append(f"  {'#':>3}  {'TYPE':<5} {'OFFSET':>12} {'SIZE':>10} "
                 f"{'CONF':<8} FILE")
        for i, c in enumerate(result.recovered, 1):
            L.append(f"  {i:>3}  {c.fmt:<5} {c.start:>#12x} "
                     f"{_human(c.size):>10} {c.confidence:<8} "
                     f"{i:04d}_{c.fmt}_{c.start:x}.{c.ext}"
                     + (f"  [{c.note}]" if c.note else ""))
    else:
        L.append("  No files recovered.")
    L.append("=" * 72)
    return "\n".join(L)


def cmd_scan(args):
    t0 = time.time()
    try:
        with DiskImage(args.image) as image:
            result = scan(image, formats=args.formats)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    written = 0
    if args.output:
        written = _write_files(result, args.output,
                               include_embedded=args.include_embedded,
                               include_duplicates=args.include_duplicates)
    print(_summary(result))
    if args.output:
        print(f"\n  Exported {written} file(s) to: {os.path.abspath(args.output)}")

    # Phase 3 reports (each format is rendered by its own module).
    from . import report as report_mod
    for kind, path in (("json", args.json), ("html", args.html), ("csv", args.csv)):
        if path:
            report_mod.write_report(result, kind, path)
            print(f"  {kind.upper():<5} report: {os.path.abspath(path)}")

    print(f"  Scan time  : {time.time() - t0:.2f}s")
    return 0


def cmd_gui(args):
    from .gui import launch
    return launch([args.image] if args.image else None)


def build_parser():
    p = argparse.ArgumentParser(
        prog="carver",
        description="Phase 1: signature-based file carving engine "
                    "(read image, scan, carve, report offset and size).")
    p.add_argument("--version", action="version", version=f"carver {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="scan a disk image and carve files")
    s.add_argument("image", help="raw (.dd/.img/.raw) or E01 disk image")
    s.add_argument("-o", "--output", metavar="DIR",
                   help="directory to write carved files into "
                        "(omit to only print the summary)")
    s.add_argument("--formats", type=_parse_formats, metavar="LIST",
                   help="comma-separated subset: jpg,png,pdf,zip "
                        "(default: all; docx/xlsx/pptx come from zip)")
    s.add_argument("--include-embedded", action="store_true",
                   help="also export objects found inside other files")
    s.add_argument("--include-duplicates", action="store_true",
                   help="also export files whose SHA-256 already appeared")
    s.add_argument("--json", metavar="FILE", help="write a JSON report")
    s.add_argument("--html", metavar="FILE", help="write an HTML report")
    s.add_argument("--csv", metavar="FILE", help="write a CSV evidence log")
    s.set_defaults(func=cmd_scan)

    g = sub.add_parser("gui", help="launch the desktop GUI (Phase 4)")
    g.add_argument("image", nargs="?", help="optionally pre-load an image path")
    g.set_defaults(func=cmd_gui)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
