"""
Phase 4 - Task C: Drive Acquisition panel (UI half).
"""

import os
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from carver.acquire import list_drives, image_source


class AcquireView(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=10)
        self.app = app

        self.drives = []
        self.cancel_event = threading.Event()

        # Title
        ttk.Label(
            self,
            text="Drive Acquisition",
            font=("", 11, "bold")
        ).pack(anchor="w")

        ttk.Label(
            self,
            text="Select a drive and create a read-only forensic image.",
            foreground="#666"
        ).pack(anchor="w", pady=(2, 8))

        # Refresh button
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 5))

        ttk.Button(
            top,
            text="Refresh",
            command=self.refresh
        ).pack(side="left")

        # Drive list
        columns = ("kind", "device", "size", "removable", "name")

        self.tree = ttk.Treeview(
            self,
            columns=columns,
            show="headings",
            height=8
        )

        self.tree.heading("kind", text="Kind")
        self.tree.heading("device", text="Device")
        self.tree.heading("size", text="Size")
        self.tree.heading("removable", text="Removable")
        self.tree.heading("name", text="Name")

        self.tree.column("kind", width=100)
        self.tree.column("device", width=180)
        self.tree.column("size", width=120)
        self.tree.column("removable", width=100)
        self.tree.column("name", width=250)

        self.tree.pack(fill="both", expand=True, pady=(0, 10))

        # Output file
        output_frame = ttk.Frame(self)
        output_frame.pack(fill="x", pady=3)

        ttk.Label(
            output_frame,
            text="Output file:"
        ).pack(side="left")

        self.output_var = tk.StringVar()

        ttk.Entry(
            output_frame,
            textvariable=self.output_var
        ).pack(side="left", fill="x", expand=True, padx=5)

        ttk.Button(
            output_frame,
            text="Browse",
            command=self.choose_output
        ).pack(side="left")

        # Size limit
        limit_frame = ttk.Frame(self)
        limit_frame.pack(fill="x", pady=3)

        ttk.Label(
            limit_frame,
            text="Limit to N MB:"
        ).pack(side="left")

        self.limit_var = tk.StringVar()

        ttk.Entry(
            limit_frame,
            textvariable=self.limit_var,
            width=12
        ).pack(side="left", padx=5)

        ttk.Label(
            limit_frame,
            text="(optional)"
        ).pack(side="left")

        # Buttons
        button_frame = ttk.Frame(self)
        button_frame.pack(fill="x", pady=(8, 5))

        self.image_button = ttk.Button(
            button_frame,
            text="Image",
            command=self.start_image
        )
        self.image_button.pack(side="left")

        self.cancel_button = ttk.Button(
            button_frame,
            text="Cancel",
            command=self.cancel_image,
            state="disabled"
        )
        self.cancel_button.pack(side="left", padx=5)

        # Progress
        self.progress = ttk.Progressbar(
            self,
            mode="determinate"
        )
        self.progress.pack(fill="x", pady=(5, 2))

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(
            self,
            textvariable=self.status_var
        ).pack(anchor="w")

        # Automatically load drives
        self.refresh()

    def refresh(self):
        """Refresh the list of available drives."""
        self.drives = list_drives()

        for item in self.tree.get_children():
            self.tree.delete(item)

        for drive in self.drives:
            self.tree.insert(
                "",
                "end",
                values=(
                    getattr(drive, "kind", ""),
                    getattr(drive, "device", ""),
                    self.format_size(getattr(drive, "size", 0)),
                    "Yes" if getattr(drive, "removable", False) else "No",
                    getattr(drive, "name", ""),
                )
            )

        self.status_var.set(
            f"{len(self.drives)} drive(s) found"
        )

    @staticmethod
    def format_size(size):
        """Format bytes as a readable size."""
        if size is None:
            return ""

        size = float(size)

        units = ["B", "KB", "MB", "GB", "TB"]

        for unit in units:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024

        return f"{size:.1f} PB"

    def choose_output(self):
        """Choose the destination image file."""
        path = filedialog.asksaveasfilename(
            title="Save forensic image",
            defaultextension=".dd",
            filetypes=[
                ("Disk image", "*.dd"),
                ("Raw image", "*.img"),
                ("All files", "*.*"),
            ],
        )

        if path:
            self.output_var.set(path)

    def get_selected_drive(self):
        """Return the DriveInfo object selected in the tree."""
        selected = self.tree.selection()

        if not selected:
            return None

        index = self.tree.index(selected[0])

        if index < 0 or index >= len(self.drives):
            return None

        return self.drives[index]

    def start_image(self):
        """Start imaging in a background thread."""
        drive = self.get_selected_drive()

        if drive is None:
            messagebox.showwarning(
                "No drive selected",
                "Please select a drive first."
            )
            return

        out_path = self.output_var.get().strip()

        if not out_path:
            messagebox.showwarning(
                "No output file",
                "Please choose an output image file."
            )
            return

        # Prevent accidentally overwriting the source.
        if os.path.abspath(out_path) == os.path.abspath(drive.device):
            messagebox.showerror(
                "Invalid output",
                "The output file must not be the source drive."
            )
            return

        # Parse optional MB limit.
        max_bytes = None
        limit_text = self.limit_var.get().strip()

        if limit_text:
            try:
                mb = float(limit_text)

                if mb < 0:
                    raise ValueError

                max_bytes = int(mb * 1024 * 1024)

            except ValueError:
                messagebox.showerror(
                    "Invalid limit",
                    "Enter a valid number of MB."
                )
                return

        self.cancel_event.clear()

        self.image_button.config(state="disabled")
        self.cancel_button.config(state="normal")
        self.progress["value"] = 0
        self.status_var.set("Imaging...")

        thread = threading.Thread(
            target=self._image_worker,
            args=(drive.device, out_path, max_bytes),
            daemon=True
        )

        thread.start()

    def _image_worker(self, source, out_path, max_bytes):
        """Perform imaging outside the GUI thread."""

        try:
            written = image_source(
                source,
                out_path,
                max_bytes=max_bytes,
                progress=self._progress_callback,
                cancel=self.cancel_event,
            )

            self.after(
                0,
                self._image_finished,
                written,
                out_path
            )

        except Exception as exc:
            self.after(
                0,
                self._image_failed,
                str(exc)
            )

    def _progress_callback(self, written, target):
        """Update progress safely from the worker thread."""

        self.after(
            0,
            self._update_progress,
            written,
            target
        )

    def _update_progress(self, written, target):
        if target and target > 0:
            percent = (written / target) * 100
            self.progress["value"] = min(percent, 100)

            self.status_var.set(
                f"Imaging... {written:,} / {target:,} bytes"
            )
        else:
            self.status_var.set(
                f"Imaging... {written:,} bytes"
            )

    def cancel_image(self):
        """Request cancellation of the current acquisition."""
        self.cancel_event.set()
        self.status_var.set("Cancelling...")

    def _image_finished(self, written, out_path):
        """Handle successful acquisition."""

        cancelled = self.cancel_event.is_set()

        self.image_button.config(state="normal")
        self.cancel_button.config(state="disabled")

        if cancelled:
            self.status_var.set(
                f"Imaging cancelled after {written:,} bytes"
            )
            return

        self.progress["value"] = 100

        self.status_var.set(
            f"Image complete: {written:,} bytes"
        )

        # Make the new forensic image available to the Scan tab.
        self.app.set_image(out_path)

        messagebox.showinfo(
            "Acquisition complete",
            f"Forensic image created successfully.\n\n"
            f"File: {out_path}\n"
            f"Bytes written: {written:,}"
        )

    def _image_failed(self, error):
        """Handle acquisition errors."""

        self.image_button.config(state="normal")
        self.cancel_button.config(state="disabled")

        self.status_var.set("Imaging failed")

        messagebox.showerror(
            "Acquisition failed",
            error
        )