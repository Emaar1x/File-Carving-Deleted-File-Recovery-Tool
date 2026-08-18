"""
GUI test: the results table is populated from a scan.

Needs a display; skips automatically where Tk cannot open (e.g. headless CI).
Run:  python tests/test_gui_scan.py
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk

import make_test_image as mti
from carver import DiskImage, scan
from carver.gui import CarverGUI


def _image():
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "img.dd")
    mti.build(p, size_mb=8, quiet=True)
    return p


def _root():
    try:
        r = tk.Tk()
        r.withdraw()
        return r
    except tk.TclError as exc:
        raise unittest.SkipTest(f"no display: {exc}")


def test_results_table_has_one_row_per_recovered_file():
    root = _root()
    try:
        path = _image()
        with DiskImage(path) as img:
            result = scan(img)
        gui = CarverGUI(root)
        gui.image_path = path
        gui.result = result
        gui.view_var.set("Recovered")
        gui._refresh_table()
        assert len(gui.tree.get_children()) == len(result.recovered)
        # the Duplicates view shows the duplicate JPEG
        gui.view_var.set("Duplicates")
        gui._refresh_table()
        assert len(gui.tree.get_children()) == len(result.duplicates)
    finally:
        root.destroy()


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"PASS  {name}")
            except unittest.SkipTest as e:
                print(f"SKIP  {name}: {e}")
            except Exception as e:
                failures += 1; print(f"FAIL  {name}: {e!r}")
    sys.exit(1 if failures else 0)
