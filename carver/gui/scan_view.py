"""
Phase 4 - Task A: Scan & Results panel.

Lets the user pick a disk image, scan it on a background thread (so the window
stays responsive), and browse the recovered files in a colour-coded table. When
the scan finishes the result is published via ``app.set_result(...)`` so the
Reports tab can use it.

Contract used by tests/test_gui_scan.py:
  * self.tree -- a ttk.Treeview listing the recovered files.
  * refresh() -- (re)fills self.tree from self.app.result.recovered.
"""

import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from carver import DiskImage, scan

_CONF_COLORS = {"high": "#1a7f37", "medium": "#9a6700", "low": "#b3261e"}


def _human(n):
    step = 1024.0
    v = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < step:
            return f"{v:.0f} {unit}" if unit == "B" else f"{v:.1f} {unit}"
        v /= step
    return f"{v:.1f} PB"


class ScanView(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=10)
        self.app = app
        self._queue = queue.Queue()

        # --- image picker + scan controls ---
        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Label(top, text="Disk image:").pack(side="left")
        self.image_entry = ttk.Entry(top)
        self.image_entry.pack(side="left", fill="x", expand=True, padx=6)
        if self.app.image_path:
            self.image_entry.insert(0, self.app.image_path)
        ttk.Button(top, text="Browse…", command=self._browse).pack(side="left")
        self.scan_btn = ttk.Button(top, text="▶  Scan", command=self._start_scan)
        self.scan_btn.pack(side="left", padx=(6, 0))

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(6, 4))
        self.progress = ttk.Progressbar(bar, mode="indeterminate")
        self.progress.pack(side="left", fill="x", expand=True)
        self.status = ttk.Label(bar, text="Ready.", foreground="#666")
        self.status.pack(side="left", padx=8)

        # --- results table ---
        cols = ("idx", "type", "offset", "size", "conf", "sha")
        heads = ("#", "Type", "Offset", "Size", "Confidence", "SHA-256")
        widths = (36, 60, 96, 74, 90, 220)
        self.tree = ttk.Treeview(self, columns=cols, show="headings")
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w")
        for level, color in _CONF_COLORS.items():
            self.tree.tag_configure(level, foreground=color)
        self.tree.pack(fill="both", expand=True)
        vs = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        vs.place(relx=1.0, rely=0, relheight=1.0, anchor="ne")
        self.tree.configure(yscrollcommand=vs.set)

        self.app.subscribe(self._on_state)
        self.after(80, self._poll)

    # -- state / actions ---------------------------------------------------
    def _on_state(self):
        """React to shared-state changes (e.g. the Acquire tab loaded an image)."""
        path = self.app.image_path or ""
        if path and path != self.image_entry.get():
            self.image_entry.delete(0, "end")
            self.image_entry.insert(0, path)
        self.refresh()

    def _browse(self):
        path = filedialog.askopenfilename(
            title="Select disk image",
            filetypes=[("Disk images", "*.dd *.img *.raw *.bin *.001 *.E01"),
                       ("All files", "*.*")])
        if path:
            self.image_entry.delete(0, "end")
            self.image_entry.insert(0, path)

    def _start_scan(self):
        path = self.image_entry.get().strip()
        if not path:
            messagebox.showwarning("No image", "Choose a disk image first.")
            return
        if not os.path.isfile(path):
            messagebox.showerror("Not found", f"Image not found:\n{path}")
            return
        self.scan_btn.configure(state="disabled")
        self.status.configure(text="Scanning…")
        self.progress.start(12)
        threading.Thread(target=self._scan_worker, args=(path,),
                         daemon=True).start()

    def _scan_worker(self, path):
        try:
            with DiskImage(path) as image:
                result = scan(image)
            self._queue.put(("done", result))
        except Exception as exc:  # surface reader/scan errors in the UI
            self._queue.put(("error", str(exc)))

    def _poll(self):
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                self.progress.stop()
                self.scan_btn.configure(state="normal")
                if kind == "done":
                    self.app.set_result(payload)  # notifies -> refresh()
                    r = payload
                    self.status.configure(
                        text=f"Recovered {len(r.recovered)}  ·  "
                             f"duplicates {len(r.duplicates)}  ·  "
                             f"embedded {len(r.embedded)}")
                elif kind == "error":
                    self.status.configure(text="Error.")
                    messagebox.showerror("Scan failed", payload)
        except queue.Empty:
            pass
        self.after(80, self._poll)

    # -- results table -----------------------------------------------------
    def refresh(self):
        """Fill self.tree from self.app.result.recovered."""
        self.tree.delete(*self.tree.get_children())
        result = self.app.result
        if not result:
            return
        for i, c in enumerate(result.recovered, 1):
            self.tree.insert(
                "", "end",
                values=(i, c.fmt, f"{c.start:#x}", _human(c.size),
                        c.confidence, c.sha256[:16] + "…" if c.sha256 else ""),
                tags=(c.confidence,))
