"""
Tkinter desktop GUI for the carver.

A single-window application over the same engine used by the CLI:

  * pick a raw/E01 image and an output folder, choose which signatures to hunt;
  * scan on a background thread with a live progress bar, a found counter and a
    Cancel button;
  * browse results in a colour-coded, sortable table (Recovered / Duplicates /
    Embedded / All) with a text filter;
  * inspect the selected file: full metadata, an image thumbnail (PNG/JPEG),
    a hex dump, or -- for ZIP/DOCX -- the archive's contents;
  * save a single carved file, open it in the OS default app, copy its SHA-256,
    or export the whole run as JSON / HTML / CSV;
  * image a live drive read-only ("From drive...") and scan it.

Only the standard library is required. Pillow is used for richer image previews
if installed, otherwise Tk's own PNG decoder is used, with a hex fallback.

Launch with:  python -m carver gui     (or:  python gui.py)
"""

import base64
import io
import os
import queue
import threading
import time
import tkinter as tk
import webbrowser
import zipfile
from tkinter import filedialog, messagebox, ttk

from . import __version__
from . import acquire
from .engine import scan
from .image_reader import DiskImage
from .signatures import SIGNATURE_BY_KEY
from . import report as report_mod

HIGH, MEDIUM, LOW = "high", "medium", "low"
CONF_COLORS = {HIGH: "#1a7f37", MEDIUM: "#9a6700", LOW: "#b3261e"}
VIEWS = ("Recovered", "Duplicates", "Embedded", "All")


class _Cancelled(Exception):
    """Raised from the progress callback to abort a running scan."""


def _human(n) -> str:
    step = 1024.0
    v = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < step:
            return f"{v:.0f} {unit}" if unit == "B" else f"{v:.1f} {unit}"
        v /= step
    return f"{v:.1f} PB"


def _hexdump(data: bytes, length: int = 1024) -> str:
    lines = []
    for i in range(0, min(len(data), length), 16):
        chunk = data[i:i + 16]
        hexpart = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{i:08x}  {hexpart:<47}  {ascii_part}")
    if len(data) > length:
        lines.append(f"... ({_human(len(data))} total, first {length} bytes shown)")
    return "\n".join(lines)


class CarverGUI(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=8)
        self.master = master
        self.grid(sticky="nsew")
        master.rowconfigure(0, weight=1)
        master.columnconfigure(0, weight=1)

        self.result = None
        self.image_path = None
        self._queue = queue.Queue()
        self._cancel = threading.Event()
        self._preview_img = None            # keep a ref so Tk doesn't GC it
        self._row_to_cand = {}
        self._sort_state = {}

        self.fmt_vars = {k: tk.BooleanVar(value=True) for k in SIGNATURE_BY_KEY}
        self.write_files = tk.BooleanVar(value=True)
        self.include_embedded = tk.BooleanVar(value=False)
        self.include_duplicates = tk.BooleanVar(value=False)
        self.view_var = tk.StringVar(value="Recovered")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._refresh_table())

        self._build()
        self.after(80, self._poll_queue)

    # ------------------------------------------------------------------ UI
    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self._build_inputs()
        self._build_scan_bar()
        self._build_body()
        self._build_footer()

    def _build_inputs(self):
        f = ttk.LabelFrame(self, text="Evidence", padding=8)
        f.grid(row=0, column=0, sticky="ew")
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Disk image:").grid(row=0, column=0, sticky="w")
        self.image_entry = ttk.Entry(f)
        self.image_entry.grid(row=0, column=1, sticky="ew", padx=6, pady=2)
        picker = ttk.Frame(f)
        picker.grid(row=0, column=2, pady=2)
        ttk.Button(picker, text="Browse…", command=self._browse_image).pack(side="left")
        ttk.Button(picker, text="From drive…", command=self._open_acquire)\
            .pack(side="left", padx=(4, 0))

        ttk.Label(f, text="Output folder:").grid(row=1, column=0, sticky="w")
        self.output_entry = ttk.Entry(f)
        self.output_entry.grid(row=1, column=1, sticky="ew", padx=6, pady=2)
        ttk.Button(f, text="Browse…", command=self._browse_output).grid(row=1, column=2, pady=2)

        opts = ttk.Frame(f)
        opts.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(opts, text="Formats:").pack(side="left")
        for k in SIGNATURE_BY_KEY:
            ttk.Checkbutton(opts, text=k.upper(), variable=self.fmt_vars[k])\
                .pack(side="left", padx=2)
        ttk.Label(opts, text="  (DOCX/XLSX/PPTX come from ZIP)",
                  foreground="#888").pack(side="left")

        opts2 = ttk.Frame(f)
        opts2.grid(row=3, column=0, columnspan=3, sticky="w", pady=(2, 0))
        ttk.Checkbutton(opts2, text="Write recovered files to output folder",
                        variable=self.write_files).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(opts2, text="Include embedded",
                        variable=self.include_embedded).pack(side="left", padx=4)
        ttk.Checkbutton(opts2, text="Include duplicates",
                        variable=self.include_duplicates).pack(side="left", padx=4)

    def _build_scan_bar(self):
        f = ttk.Frame(self, padding=(0, 6))
        f.grid(row=1, column=0, sticky="ew")
        f.columnconfigure(2, weight=1)
        self.scan_btn = ttk.Button(f, text="▶  Scan", command=self._start_scan)
        self.scan_btn.grid(row=0, column=0)
        self.cancel_btn = ttk.Button(f, text="■  Cancel", command=self._cancel_scan,
                                     state="disabled")
        self.cancel_btn.grid(row=0, column=1, padx=6)
        self.progress = ttk.Progressbar(f, mode="determinate")
        self.progress.grid(row=0, column=2, sticky="ew", padx=6)
        self.status = ttk.Label(f, text="Ready.", foreground="#888")
        self.status.grid(row=0, column=3, padx=6)

    def _build_body(self):
        paned = ttk.Panedwindow(self, orient="horizontal")
        paned.grid(row=2, column=0, sticky="nsew", pady=4)

        left = ttk.Frame(paned)
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)

        bar = ttk.Frame(left)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(bar, text="View:").pack(side="left")
        vc = ttk.Combobox(bar, textvariable=self.view_var, values=VIEWS,
                          width=12, state="readonly")
        vc.pack(side="left", padx=4)
        vc.bind("<<ComboboxSelected>>", lambda *_: self._refresh_table())
        ttk.Label(bar, text="  Filter:").pack(side="left")
        ttk.Entry(bar, textvariable=self.filter_var, width=22).pack(side="left", padx=4)

        cols = ("idx", "type", "offset", "size", "conf", "name", "sha", "notes")
        headings = ("#", "Type", "Offset", "Size", "Confidence", "Filename",
                    "SHA-256", "Notes")
        widths = (36, 52, 96, 74, 84, 150, 130, 240)
        tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse")
        for c, h, w in zip(cols, headings, widths):
            tree.heading(c, text=h, command=lambda cc=c: self._sort_by(cc))
            tree.column(c, width=w, anchor="w", stretch=(c in ("name", "notes")))
        for level, color in CONF_COLORS.items():
            tree.tag_configure(level, foreground=color)
        tree.grid(row=1, column=0, sticky="nsew")
        vs = ttk.Scrollbar(left, orient="vertical", command=tree.yview)
        vs.grid(row=1, column=1, sticky="ns")
        tree.configure(yscrollcommand=vs.set)
        tree.bind("<<TreeviewSelect>>", self._on_select)
        tree.bind("<Double-1>", lambda e: self._open_selected())
        self.tree = tree
        paned.add(left, weight=3)

        right = ttk.Frame(paned)
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)
        ttk.Label(right, text="File details", font=("", 10, "bold"))\
            .grid(row=0, column=0, sticky="w")
        self.preview_img_label = ttk.Label(right, anchor="center")
        self.preview_img_label.grid(row=1, column=0, sticky="nsew")
        self.detail_text = tk.Text(right, height=12, wrap="none", font=("Consolas", 9))
        self.detail_text.grid(row=2, column=0, sticky="nsew")
        right.rowconfigure(2, weight=1)
        dts = ttk.Scrollbar(right, orient="vertical", command=self.detail_text.yview)
        dts.grid(row=2, column=1, sticky="ns")
        self.detail_text.configure(yscrollcommand=dts.set)

        btns = ttk.Frame(right)
        btns.grid(row=3, column=0, sticky="ew", pady=4)
        ttk.Button(btns, text="Save file…", command=self._save_selected).pack(side="left")
        ttk.Button(btns, text="Open externally", command=self._open_selected)\
            .pack(side="left", padx=4)
        ttk.Button(btns, text="Copy SHA-256", command=self._copy_hash).pack(side="left")
        paned.add(right, weight=2)

    def _build_footer(self):
        f = ttk.Frame(self)
        f.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        f.columnconfigure(0, weight=1)
        self.summary = ttk.Label(f, text="No scan yet.", foreground="#555")
        self.summary.grid(row=0, column=0, sticky="w")
        ttk.Button(f, text="Open output folder", command=self._open_output)\
            .grid(row=0, column=1, padx=3)
        ttk.Button(f, text="Export JSON…", command=lambda: self._export("json"))\
            .grid(row=0, column=2, padx=3)
        ttk.Button(f, text="Export HTML…", command=lambda: self._export("html"))\
            .grid(row=0, column=3, padx=3)
        ttk.Button(f, text="Export CSV…", command=lambda: self._export("csv"))\
            .grid(row=0, column=4, padx=3)

    # -------------------------------------------------------------- actions
    def _browse_image(self):
        path = filedialog.askopenfilename(
            title="Select disk image",
            filetypes=[("Disk images", "*.dd *.img *.raw *.bin *.001 *.E01"),
                       ("All files", "*.*")])
        if path:
            self.image_entry.delete(0, "end")
            self.image_entry.insert(0, path)
            if not self.output_entry.get():
                self.output_entry.insert(0, os.path.splitext(path)[0] + "_recovered")

    def _browse_output(self):
        path = filedialog.askdirectory(title="Select output folder")
        if path:
            self.output_entry.delete(0, "end")
            self.output_entry.insert(0, path)

    def _open_acquire(self):
        AcquireDialog(self.master, on_created=self._use_acquired_image)

    def _use_acquired_image(self, path):
        self.image_entry.delete(0, "end")
        self.image_entry.insert(0, path)
        if not self.output_entry.get():
            self.output_entry.insert(0, os.path.splitext(path)[0] + "_recovered")
        self.status.configure(text=f"Loaded image {os.path.basename(path)} — ready to scan.")

    def _start_scan(self):
        image = self.image_entry.get().strip()
        if not image:
            messagebox.showwarning("No image", "Choose a disk image first.")
            return
        if not os.path.isfile(image):
            messagebox.showerror("Not found", f"Image not found:\n{image}")
            return
        fmts = [k for k, v in self.fmt_vars.items() if v.get()]
        if not fmts:
            messagebox.showwarning("No formats", "Select at least one format.")
            return

        self._cancel.clear()
        self.result = None
        self.image_path = image
        self.tree.delete(*self.tree.get_children())
        self._row_to_cand.clear()
        self.scan_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.progress.configure(value=0)
        self.status.configure(text="Scanning…")
        threading.Thread(target=self._scan_worker, args=(image, fmts), daemon=True).start()

    def _scan_worker(self, image_path, fmts):
        def progress(fmt, pos, total, found):
            if self._cancel.is_set():
                raise _Cancelled()
            self._queue.put(("progress", fmt, pos, total, found))
        try:
            with DiskImage(image_path) as image:
                result = scan(image, formats=fmts, progress=progress)
            self._queue.put(("done", result))
        except _Cancelled:
            self._queue.put(("cancelled", None))
        except Exception as exc:
            self._queue.put(("error", str(exc)))

    def _cancel_scan(self):
        self._cancel.set()
        self.status.configure(text="Cancelling…")

    def _poll_queue(self):
        try:
            while True:
                msg = self._queue.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    _, fmt, pos, total, found = msg
                    self.progress.configure(value=(100.0 * pos / total) if total else 0)
                    self.status.configure(text=f"Scanning [{fmt}] …  {found} found")
                elif kind == "done":
                    self._on_scan_done(msg[1])
                elif kind == "cancelled":
                    self._finish("Scan cancelled.")
                elif kind == "error":
                    self._finish("Error.")
                    messagebox.showerror("Scan failed", msg[1])
        except queue.Empty:
            pass
        self.after(80, self._poll_queue)

    def _finish(self, status_text):
        self.scan_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.progress.configure(value=0)
        self.status.configure(text=status_text)

    def _on_scan_done(self, result):
        self.result = result
        written = 0
        if self.write_files.get() and self.output_entry.get().strip():
            try:
                written = self._export_files(self.output_entry.get().strip())
            except Exception as exc:
                messagebox.showerror("Export failed", str(exc))
        self._finish("Done.")
        self._refresh_table()
        r = result
        extra = f"  ·  {written} file(s) written" if written else ""
        self.summary.configure(
            text=f"Recovered {len(r.recovered)}  ·  Duplicates {len(r.duplicates)}  ·  "
                 f"Embedded {len(r.embedded)}  ·  image {_human(r.image_size)}{extra}")

    # ---------------------------------------------------------------- table
    def _current_view_list(self):
        if not self.result:
            return []
        v = self.view_var.get()
        if v == "Recovered":
            return self.result.recovered
        if v == "Duplicates":
            return self.result.duplicates
        if v == "Embedded":
            return self.result.embedded
        return sorted(self.result.candidates, key=lambda c: c.start)

    def _refresh_table(self):
        self.tree.delete(*self.tree.get_children())
        self._row_to_cand.clear()
        needle = self.filter_var.get().lower().strip()
        for i, c in enumerate(self._current_view_list(), 1):
            name = f"{i:04d}_{c.fmt}_{c.start:x}.{c.ext}"
            row = (i, c.fmt, f"{c.start:#x}", _human(c.size), c.confidence,
                   name, c.sha256[:16] + "…" if c.sha256 else "", c.note)
            if needle and needle not in " ".join(str(x) for x in row).lower():
                continue
            iid = self.tree.insert("", "end", values=row, tags=(c.confidence,))
            self._row_to_cand[iid] = c

    def _sort_by(self, col):
        rev = self._sort_state.get(col, False)
        items = [(self.tree.set(iid, col), iid) for iid in self.tree.get_children()]

        def key(pair):
            val = pair[0]
            try:
                if val.startswith("0x"):
                    return int(val, 16)
                return float(val.split()[0])
            except (ValueError, IndexError):
                return val.lower()
        items.sort(key=key, reverse=rev)
        for pos, (_, iid) in enumerate(items):
            self.tree.move(iid, "", pos)
        self._sort_state[col] = not rev

    def _selected_cand(self):
        sel = self.tree.selection()
        return self._row_to_cand.get(sel[0]) if sel else None

    # -------------------------------------------------------------- preview
    def _read_bytes(self, cand, cap=4 * 1024 * 1024):
        with open(self.image_path, "rb") as fh:
            fh.seek(cand.start)
            return fh.read(min(cand.size, cap))

    def _on_select(self, _event=None):
        cand = self._selected_cand()
        if not cand:
            return
        data = self._read_bytes(cand)
        self._show_details(cand, data)
        self._show_preview(cand, data)

    def _show_details(self, cand, data):
        t = self.detail_text
        t.delete("1.0", "end")
        lines = [
            f"Type        : {cand.fmt}",
            f"Offset      : {cand.start:#x}  ({cand.start:,})",
            f"End         : {cand.end:#x}",
            f"Size        : {_human(cand.size)}  ({cand.size:,} bytes)",
            f"Confidence  : {cand.confidence}",
            f"SHA-256     : {cand.sha256}",
            f"Notes       : {cand.note}",
        ]
        if cand.embedded_in is not None:
            lines.append(f"Embedded in : file at {cand.embedded_in:#x}")
        if cand.duplicate_of is not None:
            lines.append(f"Duplicate of: file at {cand.duplicate_of:#x}")
        lines.append("")
        if cand.fmt in ("zip", "docx", "xlsx", "pptx"):
            lines.append("Archive contents:")
            try:
                zf = zipfile.ZipFile(io.BytesIO(data))
                for info in zf.infolist():
                    lines.append(f"  {info.file_size:>9,}  {info.filename}")
            except Exception as exc:
                lines.append(f"  (could not open archive: {exc})")
        else:
            lines.append("Hex dump (first 1 KB):")
            lines.append(_hexdump(data))
        t.insert("1.0", "\n".join(lines))

    def _show_preview(self, cand, data):
        self._preview_img = None
        self.preview_img_label.configure(image="", text="")
        if cand.fmt not in ("png", "jpg"):
            self.preview_img_label.configure(
                text="(no image preview for this type)", foreground="#888")
            return
        photo = self._make_photo(data)
        if photo is not None:
            self._preview_img = photo
            self.preview_img_label.configure(image=photo, text="")
        else:
            self.preview_img_label.configure(
                text="(install Pillow for JPEG preview)", foreground="#888")

    def _make_photo(self, data, max_side=260):
        try:
            from PIL import Image, ImageTk  # type: ignore
            im = Image.open(io.BytesIO(data))
            im.thumbnail((max_side, max_side))
            return ImageTk.PhotoImage(im)
        except Exception:
            pass
        try:
            return tk.PhotoImage(data=base64.b64encode(data).decode("ascii"))
        except Exception:
            return None

    # --------------------------------------------------------------- export
    def _export_files(self, out_dir):
        os.makedirs(out_dir, exist_ok=True)
        written = 0
        with open(self.image_path, "rb") as img:
            def dump(cands):
                nonlocal written
                for i, c in enumerate(cands, 1):
                    name = f"{i:04d}_{c.fmt}_{c.start:x}.{c.ext}"
                    img.seek(c.start)
                    with open(os.path.join(out_dir, name), "wb") as fh:
                        fh.write(img.read(c.size))
                    written += 1
            dump(self.result.recovered)
            if self.include_duplicates.get():
                dump(self.result.duplicates)
            if self.include_embedded.get():
                dump(self.result.embedded)
        return written

    def _save_selected(self):
        cand = self._selected_cand()
        if not cand:
            messagebox.showinfo("No selection", "Select a file in the table first.")
            return
        default = f"recovered_{cand.fmt}_{cand.start:x}.{cand.ext}"
        path = filedialog.asksaveasfilename(
            title="Save carved file", initialfile=default,
            defaultextension="." + cand.ext)
        if not path:
            return
        with open(self.image_path, "rb") as img:
            img.seek(cand.start)
            data = img.read(cand.size)
        with open(path, "wb") as fh:
            fh.write(data)
        self.status.configure(text=f"Saved {os.path.basename(path)}")

    def _open_selected(self):
        cand = self._selected_cand()
        if not cand:
            return
        import tempfile
        fd, path = tempfile.mkstemp(suffix="." + cand.ext, prefix="carved_")
        with os.fdopen(fd, "wb") as fh:
            with open(self.image_path, "rb") as img:
                img.seek(cand.start)
                fh.write(img.read(cand.size))
        try:
            if hasattr(os, "startfile"):
                os.startfile(path)
            else:
                webbrowser.open("file://" + os.path.abspath(path))
        except Exception as exc:
            messagebox.showerror("Open failed", str(exc))

    def _copy_hash(self):
        cand = self._selected_cand()
        if not cand:
            return
        self.master.clipboard_clear()
        self.master.clipboard_append(cand.sha256)
        self.status.configure(text="SHA-256 copied to clipboard.")

    def _open_output(self):
        out = self.output_entry.get().strip()
        if out and os.path.isdir(out):
            if hasattr(os, "startfile"):
                os.startfile(out)
            else:
                webbrowser.open("file://" + os.path.abspath(out))
        else:
            messagebox.showinfo("No folder", "Output folder does not exist yet.")

    def _export(self, kind):
        if not self.result:
            messagebox.showinfo("No scan", "Run a scan first.")
            return
        ext = {"json": ".json", "html": ".html", "csv": ".csv"}[kind]
        path = filedialog.asksaveasfilename(
            title=f"Export {kind.upper()} report", defaultextension=ext,
            initialfile="report" + ext,
            filetypes=[(kind.upper(), "*" + ext), ("All files", "*.*")])
        if not path:
            return
        report_mod.write_report(self.result, kind, path)
        self.status.configure(text=f"Wrote {os.path.basename(path)}")
        if kind == "html" and messagebox.askyesno(
                "Report written", "HTML report saved. Open it in your browser?"):
            webbrowser.open("file://" + os.path.abspath(path))


class AcquireDialog(tk.Toplevel):
    """Modal dialog: pick a live drive and image it (read-only) to a .dd file."""

    def __init__(self, master, on_created=None):
        super().__init__(master)
        self.title("Create disk image from a drive")
        self.transient(master)
        self.geometry("720x460")
        self.on_created = on_created
        self._queue = queue.Queue()
        self._cancel = threading.Event()
        self._drives = []
        self._start_time = time.monotonic()

        self.limit_on = tk.BooleanVar(value=True)
        self.limit_mb = tk.StringVar(value="200")

        self._build()
        self._refresh()
        self.after(80, self._poll)
        self.grab_set()

    def _build(self):
        pad = {"padx": 8, "pady": 4}
        warn = ("Read-only acquisition. The source drive is only read, never "
                "written.\nImaging a physical drive or raw volume normally "
                "requires running as Administrator. Removable USB media is the "
                "easiest target for a live test.")
        ttk.Label(self, text=warn, foreground="#9a6700", wraplength=690,
                  justify="left").pack(anchor="w", **pad)

        top = ttk.Frame(self)
        top.pack(fill="both", expand=True, **pad)
        cols = ("kind", "device", "size", "removable", "name")
        heads = ("Kind", "Device", "Size", "Removable", "Name")
        widths = (90, 160, 80, 80, 280)
        tree = ttk.Treeview(top, columns=cols, show="headings", selectmode="browse")
        for c, h, w in zip(cols, heads, widths):
            tree.heading(c, text=h)
            tree.column(c, width=w, anchor="w", stretch=(c == "name"))
        tree.pack(side="left", fill="both", expand=True)
        vs = ttk.Scrollbar(top, orient="vertical", command=tree.yview)
        vs.pack(side="left", fill="y")
        tree.configure(yscrollcommand=vs.set)
        self.tree = tree

        row = ttk.Frame(self)
        row.pack(fill="x", **pad)
        ttk.Button(row, text="Refresh drives", command=self._refresh).pack(side="left")
        ttk.Button(row, text="Test read speed", command=self._test_speed)\
            .pack(side="left", padx=(6, 0))
        ttk.Checkbutton(row, text="Limit to", variable=self.limit_on)\
            .pack(side="left", padx=(16, 2))
        ttk.Entry(row, textvariable=self.limit_mb, width=7).pack(side="left")
        ttk.Label(row, text="MB  (recommended for a quick test)").pack(side="left", padx=2)

        out = ttk.Frame(self)
        out.pack(fill="x", **pad)
        ttk.Label(out, text="Save image to:").pack(side="left")
        self.out_entry = ttk.Entry(out)
        self.out_entry.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(out, text="Browse…", command=self._browse_out).pack(side="left")

        prog = ttk.Frame(self)
        prog.pack(fill="x", **pad)
        self.progress = ttk.Progressbar(prog, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True)
        self.status = ttk.Label(prog, text="", foreground="#888")
        self.status.pack(side="left", padx=8)

        btns = ttk.Frame(self)
        btns.pack(fill="x", **pad)
        self.create_btn = ttk.Button(btns, text="Create image", command=self._start)
        self.create_btn.pack(side="right")
        self.cancel_btn = ttk.Button(btns, text="Cancel imaging",
                                     command=lambda: self._cancel.set(), state="disabled")
        self.cancel_btn.pack(side="right", padx=6)
        ttk.Button(btns, text="Close", command=self.destroy).pack(side="left")

    def _refresh(self):
        self.tree.delete(*self.tree.get_children())
        self.status.configure(text="Enumerating drives…")
        self.update_idletasks()
        self._drives = acquire.list_drives()
        for i, d in enumerate(self._drives):
            self.tree.insert("", "end", iid=str(i),
                             values=(getattr(d, "kind", "") or "disk",
                                     d.device, _human(d.size or 0),
                                     "yes" if getattr(d, "removable", False) else "no",
                                     d.name))
        self.status.configure(text=f"{len(self._drives)} drive(s).")

    def _selected_drive(self):
        sel = self.tree.selection()
        return self._drives[int(sel[0])] if sel else None

    def _browse_out(self):
        path = filedialog.asksaveasfilename(
            parent=self, title="Save disk image", defaultextension=".dd",
            initialfile="drive_image.dd",
            filetypes=[("Raw disk image", "*.dd *.img *.raw"), ("All files", "*.*")])
        if path:
            self.out_entry.delete(0, "end")
            self.out_entry.insert(0, path)

    def _start(self):
        drive = self._selected_drive()
        if not drive:
            messagebox.showwarning("No drive", "Select a drive first.", parent=self)
            return
        out = self.out_entry.get().strip()
        if not out:
            messagebox.showwarning("No output", "Choose where to save the image.", parent=self)
            return
        max_bytes = None
        if self.limit_on.get():
            try:
                max_bytes = int(float(self.limit_mb.get()) * 1024 * 1024)
            except ValueError:
                messagebox.showwarning("Bad limit", "Enter a number of MB.", parent=self)
                return
        elif not messagebox.askyesno(
                "Image entire drive?",
                f"You are about to image the ENTIRE drive ({_human(drive.size or 0)}). "
                "This can be very large and slow.\n\nContinue?", parent=self):
            return

        self._cancel.clear()
        self._start_time = time.monotonic()
        self.create_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.progress.configure(value=0)
        self.status.configure(text="Imaging…")
        threading.Thread(target=self._worker, args=(drive, out, max_bytes), daemon=True).start()

    def _test_speed(self):
        drive = self._selected_drive()
        if not drive:
            messagebox.showwarning("No drive", "Select a drive first.", parent=self)
            return
        nbytes = min(drive.size or (512 << 20), 512 << 20)
        self._cancel.clear()
        self._start_time = time.monotonic()
        self.create_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.progress.configure(value=0)
        self.status.configure(text="Testing read speed…")
        threading.Thread(target=self._bench_worker, args=(drive, nbytes), daemon=True).start()

    def _bench_worker(self, drive, nbytes):
        def progress(read, target):
            self._queue.put(("p", read, target))
        try:
            read, elapsed = acquire.read_benchmark(
                drive.device, nbytes, progress=progress, cancel=self._cancel)
            self._queue.put(("bench", read, elapsed))
        except Exception as exc:
            self._queue.put(("error", str(exc)))

    def _worker(self, drive, out, max_bytes):
        def progress(written, target):
            self._queue.put(("p", written, target))
        try:
            n = acquire.image_source(drive.device, out, max_bytes=max_bytes,
                                     progress=progress, cancel=self._cancel)
            self._queue.put(("cancelled" if self._cancel.is_set() else "done", out, n))
        except Exception as exc:
            self._queue.put(("error", str(exc)))

    def _poll(self):
        try:
            while True:
                msg = self._queue.get_nowait()
                if msg[0] == "p":
                    _, written, target = msg
                    if target:
                        self.progress.configure(value=100.0 * written / target)
                    elapsed = max(time.monotonic() - self._start_time, 1e-6)
                    speed = written / elapsed
                    txt = f"{_human(written)} @ {_human(speed)}/s"
                    if target and speed > 0:
                        eta = (target - written) / speed
                        txt += f"  ·  ETA {int(eta // 60)}m{int(eta % 60):02d}s"
                    self.status.configure(text=txt)
                elif msg[0] == "bench":
                    _, read, elapsed = msg
                    self.create_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                    speed = read / elapsed if elapsed else 0
                    self.status.configure(
                        text=f"Read {_human(read)} in {elapsed:.1f}s = {_human(speed)}/s")
                    messagebox.showinfo(
                        "Read speed test",
                        f"Pure read speed (no writing / antivirus):\n\n"
                        f"{_human(read)} in {elapsed:.1f} s = {_human(speed)}/s\n\n"
                        "If this is also ~40 MB/s, the drive's connection (e.g. a "
                        "USB 2.0 port or cable) is the limit.", parent=self)
                elif msg[0] in ("done", "cancelled"):
                    self._finish(msg[1], msg[2], cancelled=(msg[0] == "cancelled"))
                elif msg[0] == "error":
                    self.create_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                    self.status.configure(text="Failed.")
                    messagebox.showerror("Imaging failed", msg[1], parent=self)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(80, self._poll)

    def _finish(self, out, n, cancelled):
        self.create_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.status.configure(
            text=("Cancelled — " if cancelled else "Done — ") + f"{_human(n)} written")
        verb = "Partial image" if cancelled else "Image"
        if messagebox.askyesno(
                "Image created",
                f"{verb} saved ({_human(n)}):\n{out}\n\nLoad it into the carver now?",
                parent=self):
            if self.on_created:
                self.on_created(out)
            self.destroy()


def launch(argv=None):
    root = tk.Tk()
    root.title(f"Carver — File Carving & Deleted-File Recovery  v{__version__}")
    root.geometry("1120x700")
    root.minsize(900, 580)
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    app = CarverGUI(root)
    if argv and argv[0]:
        app.image_entry.insert(0, argv[0])
    root.mainloop()
    return 0


if __name__ == "__main__":
    launch()
