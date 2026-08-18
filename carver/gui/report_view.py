"""
Phase 4 - Task B: Reports & Export panel.

Owner:  <assign a teammate>
Tests:  tests/test_gui_report.py   (currently RED -- make them pass)

--------------------------------------------------------------------------
WHAT TO DO
--------------------------------------------------------------------------
Build the panel that turns the current scan result into files on disk:

  * an "Export carved files..." button that asks for a folder and writes the
    recovered files there (see the export logic in carver/cli.py);
  * "Save JSON / Save HTML / Save CSV" buttons, each asking for a path
    (tkinter.filedialog.asksaveasfilename) and writing that report;
  * an "Open HTML report" convenience that opens the saved HTML in the browser
    (webbrowser.open);
  * sensible disabling when there is no scan result yet
    (self.app.result is None), and a small status label.

Use the Phase 3 reporting code -- ``carver.report.write_report(result, kind,
path)`` already renders 'json' / 'html' / 'csv'. You only edit THIS file and
tests/test_gui_report.py. Do not touch app.py or the other panels.

--------------------------------------------------------------------------
CONTRACT (checked by tests/test_gui_report.py)
--------------------------------------------------------------------------
  * save_report(kind, path) -- write a report of `kind` ('json'|'html'|'csv')
    for self.app.result to `path`. (Delegate to carver.report.write_report;
    this is the function your Save buttons should call.)
"""

"""
Phase 4 - Task B: Reports & Export panel.
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import webbrowser

# Phase 3 ka reporting code import kar rahe hain jaisa comments mein bataya gaya hai
from carver.report import write_report 

class ReportView(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=10)
        self.app = app

        ttk.Label(self, text="Reports & Export", font=("", 11, "bold")).pack(anchor="w", pady=(0, 10))

        # Buttons add kar rahe hain report save karne ke liye
        ttk.Button(self, text="Save JSON Report", command=lambda: self.export_dialog("json")).pack(anchor="w", pady=2)
        ttk.Button(self, text="Save HTML Report", command=lambda: self.export_dialog("html")).pack(anchor="w", pady=2)
        ttk.Button(self, text="Save CSV Report", command=lambda: self.export_dialog("csv")).pack(anchor="w", pady=2)

    def export_dialog(self, kind):
        """Ask user for a path and save the selected report."""
        if not self.app.result:
            messagebox.showerror("Error", "No scan result available to export.")
            return
            
        path = filedialog.asksaveasfilename(
            defaultextension=f".{kind}",
            filetypes=[(f"{kind.upper()} Files", f"*.{kind}"), ("All Files", "*.*")]
        )
        if path:
            self.save_report(kind, path)
            messagebox.showinfo("Success", f"Report saved successfully to:\n{path}")
            
            # Agar HTML save ki hai to browser mein open karne ki option
            if kind == "html":
                webbrowser.open(path)

    def save_report(self, kind, path):
        """Write a `kind` report for self.app.result to `path`."""
        if self.app.result:
            # write_report() function auto handle kar lega json/html/csv ko
            write_report(self.app.result, kind, path)

