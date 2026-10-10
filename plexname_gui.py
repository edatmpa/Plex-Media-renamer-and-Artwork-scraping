"""Small window around plexname.py: pick a top-level folder and language, then run
   1) split-plan -> review -> split-apply   and   2) rename-plan -> review -> rename-apply.
Every apply is a dry run first; the real run (--go) asks for confirmation."""
import json
import os
import re
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from types import SimpleNamespace

import plexname as pn  # plan names (newest plan of a library)
import ui_theme as ui

HERE = Path(__file__).resolve().parent
TOOL = HERE / "plexname.py"
FIX = HERE / "plexfix.py"  # step 3: posters in Plex
TV = HERE / "plextv.py"  # TV series ('Contains: TV series')
SETTINGS = HERE / "settings.json"  # last used choices of this window
LANGS = {"Japanese (JP)": "JP", "Korean (KR)": "KR", "Chinese (CN)": "CN", "Russian (RU)": "RU", "English (EN)": "EN"}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        ui.apply(self)
        self.title("Rename a library folder")
        self.geometry("1000x720")
        self.plans = {"split": "", "rename": ""}
        self.busy = False
        ui.header(self, "Rename a library folder",
                  "Plan -> review in Excel -> dry run -> DO IT. The original folder is never changed; results go into "
                  "'<folder> (Plex)'.")

        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="Top-level folder").grid(row=0, column=0, sticky="w")
        self.folder = tk.StringVar()
        ttk.Entry(top, textvariable=self.folder, width=70).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(top, text="Browse…", command=self.pick).grid(row=0, column=2)
        ttk.Label(top, text="Original language").grid(row=1, column=0, sticky="w", pady=4)
        self.lang = tk.StringVar(value="Japanese (JP)")
        ttk.Combobox(top, textvariable=self.lang, values=list(LANGS), state="readonly", width=18).grid(
            row=1, column=1, sticky="w", padx=4)
        ttk.Label(top, text="Contains").grid(row=2, column=0, sticky="w")
        self.kind = tk.StringVar(value="movie")
        f = ttk.Frame(top)
        f.grid(row=2, column=1, sticky="w", padx=4)
        ttk.Radiobutton(f, text="Movies (one subfolder per movie)", variable=self.kind, value="movie").pack(side="left")
        ttk.Radiobutton(f, text="TV series", variable=self.kind, value="tv").pack(side="left", padx=12)
        self.ai = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Use DeepSeek for films TMDB can't match directly", variable=self.ai).grid(
            row=3, column=1, sticky="w", padx=4)
        ttk.Label(top, text="a) Split temp folder").grid(row=4, column=0, sticky="w", pady=2)
        self.temp = tk.StringVar()
        ttk.Entry(top, textvariable=self.temp, width=70).grid(row=4, column=1, sticky="we", padx=4)
        self.mark = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="mark split-from folders", variable=self.mark).grid(row=4, column=2, sticky="w")
        ttk.Label(top, text="b) New renamed folder").grid(row=5, column=0, sticky="w", pady=2)
        self.target = tk.StringVar()
        ttk.Entry(top, textvariable=self.target, width=70).grid(row=5, column=1, sticky="we", padx=4)
        ttk.Label(top, text="original stays untouched").grid(row=5, column=2, sticky="w")
        ttk.Label(top, text="Only media folders containing").grid(row=6, column=0, sticky="w", pady=2)
        self.only = tk.StringVar()
        ttk.Entry(top, textvariable=self.only, width=40).grid(row=6, column=1, sticky="w", padx=4)
        ttk.Label(top, text="optional - empty = whole library folder").grid(row=6, column=2, sticky="w")
        top.columnconfigure(1, weight=1)

        steps = ttk.LabelFrame(self, text="Steps (in order)", padding=8)
        steps.pack(fill="x", padx=10)
        rows = [
            ("1  Split: plan", lambda: self.plan("split-plan", "split")),
            ("Open split plan", lambda: self.open_plan("split")),
            ("Copy to temp: dry run", lambda: self.apply("split-copy", "split", go=False)),
            ("Copy to temp: DO IT", lambda: self.apply("split-copy", "split", go=True)),
        ]
        rows2 = [
            ("2  Rename: plan", lambda: self.plan("rename-plan", "rename")),
            ("Open rename plan", lambda: self.open_plan("rename")),
            ("Copy renamed: dry run", lambda: self.apply("rename-copy", "rename", go=False)),
            ("Copy renamed: DO IT", lambda: self.apply("rename-copy", "rename", go=True)),
        ]
        for r, items in enumerate((rows, rows2)):  # artwork: its own window (start window, tool 3)
            for c, (label, cmd) in enumerate(items):
                style = "Go.TButton" if "DO IT" in label else "Accent.TButton" if c == 0 else "TButton"
                ttk.Button(steps, text=label, command=cmd, width=20, style=style).grid(row=r, column=c, padx=3, pady=3)
        ttk.Button(steps, text="Undo a run…", command=self.undo, width=20).grid(row=0, column=4, padx=12)
        self.status = ttk.Label(steps, text="Pick a folder, then start with step 1.")
        self.status.grid(row=3, column=0, columnspan=5, sticky="w", pady=(6, 0))

        self.out = tk.Text(self, wrap="none", font=("Consolas", 9), bg=ui.PANEL, relief="flat")
        sb = ttk.Scrollbar(self, command=self.out.yview)
        self.out.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.out.pack(fill="both", expand=True, padx=(8, 0), pady=8)
        self.load_settings()
        self.protocol("WM_DELETE_WINDOW", self.close)

    # -------------------------------------------------------------- remembered choices
    # 'Only media folders containing' is deliberately NOT remembered: a forgotten filter would make a partial plan.
    def settings_vars(self):
        return dict(folder=self.folder, lang=self.lang, kind=self.kind, ai=self.ai, temp=self.temp,
                    target=self.target, mark=self.mark)

    def load_settings(self):
        try:
            saved = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for k, var in self.settings_vars().items():
            if k in saved:
                try:
                    var.set(saved[k])
                except tk.TclError:
                    pass
        if self.folder.get():
            self.status.config(text=f"Last used: {self.folder.get()}  -  check the settings, then start.")

    def save_settings(self):
        try:
            SETTINGS.write_text(json.dumps({k: v.get() for k, v in self.settings_vars().items()},
                                           ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    def close(self):
        if self.busy and not messagebox.askyesno("A step is running", "A step is still running. Close anyway?\n"
                                                 "(The running copy/plan may stop.)"):
            return
        self.save_settings()
        self.destroy()

    # -------------------------------------------------------------- helpers
    def pick(self):
        d = filedialog.askdirectory(title="Top-level folder (holds one subfolder per movie or show)",
                                    initialdir=self.folder.get() or "V:\\")
        if d:
            self.folder.set(os.path.normpath(d))
            self.target.set(os.path.normpath(d) + " (Plex)")
            self.temp.set(os.path.normpath(d) + " (split temp)")
            self.plans = {"split": "", "rename": ""}

    def log(self, text):
        self.out.insert("end", text)
        self.out.see("end")

    def run(self, args, on_done=None, tool=None):
        if self.busy:
            messagebox.showinfo("Busy", "A step is still running.")
            return
        self.busy = True
        self.save_settings()
        self.status.config(text="Running: " + " ".join(args[:1]) + " …")
        self.log("\n> plexname " + " ".join(f'"{a}"' if " " in a else a for a in args) + "\n")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")

        def work():
            p = subprocess.Popen([sys.executable, str(tool or TOOL), *args], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                 env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            lines = []
            for line in p.stdout:
                lines.append(line)
                self.after(0, self.log, line)
            p.wait()
            self.after(0, self.finish, p.returncode, "".join(lines), on_done)

        threading.Thread(target=work, daemon=True).start()

    def finish(self, code, output, on_done):
        self.busy = False
        self.status.config(text="Finished." if code == 0 else f"Stopped with an error (code {code}).")
        if on_done:
            on_done(output)

    def need_folder(self):
        root = self.folder.get().strip()
        if not root or not Path(root).is_dir():
            messagebox.showwarning("Folder", "Pick the top-level folder first.")
            return None
        root = os.path.normpath(root)
        # typed or remembered folder: fill the temp / new folder boxes too (Browse used to be the only way)
        for var, suffix in ((self.temp, " (split temp)"), (self.target, " (Plex)")):
            if not var.get().strip() or not var.get().strip().casefold().startswith(root.casefold()):
                var.set(root + suffix)
        return root

    def plans_closed(self, root):
        """Before every step: a plan open in Excel would be overwritten/locked or read without unsaved edits."""
        while True:
            still = pn.open_plans(Path(root))
            if not still:
                return True
            if not messagebox.askretrycancel(
                    "Close the plan in Excel",
                    "This plan is open in Excel:\n\n" + "\n".join(p.name for p in still) +
                    "\n\nSave it (Ctrl+S) and close Excel, then click Retry."):
                self.status.config(text="Cancelled - the plan was still open in Excel.")
                return False

    def only_tag(self):
        return pn.only_tag(SimpleNamespace(only=self.only.get().strip()))

    # -------------------------------------------------------------- steps
    def tv(self) -> bool:
        return self.kind.get() == "tv"

    def plan(self, cmd, which):
        root = self.need_folder()
        if not root or not self.plans_closed(root):
            return
        if self.tv():  # TV series: separate engine (plextv.py); the film tools stay unchanged
            if cmd == "split-plan":
                messagebox.showinfo("TV series", "TV series need no split step - every folder is one show.\n"
                                                 "Start with '2 Rename: plan'.")
                return
            args = ["tv-plan", root, "--lang", LANGS[self.lang.get()]]
            if self.only.get().strip():
                args += ["--only", self.only.get().strip()]

            def opened(output):
                m = re.search(r"Plan written\s*:\s*(.+)", output)
                if m and Path(m.group(1).strip()).exists():
                    os.startfile(m.group(1).strip())
            self.run(args, opened, tool=TV)
            return
        args = [cmd, root, "--lang", LANGS[self.lang.get()], "--type", self.kind.get()]
        if self.only.get().strip():
            args += ["--only", self.only.get().strip()]
        if cmd == "rename-plan" and not self.ai.get():
            args.append("--no-ai")
        if cmd == "rename-plan":
            # the tool finds the split plan itself (newest in plans\) - no dialog that could be cancelled
            args += ["--temp", self.temp.get().strip()]
            prev = pn.latest_plan(Path(root), "rename", self.only_tag())
            if prev and messagebox.askyesno(  # rerun after fixing things: keep the owner's Y and name changes
                    "Keep your review work?",
                    f"Keep your decisions from the current rename plan?\n\n{prev.name}\n\n"
                    "Your Y marks and name changes are carried into the new plan; only what is still "
                    "unsolved stays red/yellow. Save and close that plan in Excel first.\n\n"
                    "No = start a fresh plan."):
                args += ["--previous", str(prev)]

        def done(output):
            m = re.search(r"Plan written\s*:\s*(.+)", output)
            if m and Path(m.group(1).strip()).exists():
                p = m.group(1).strip()
                self.status.config(text=f"Plan ready and opened in Excel: check the red/yellow rows, type Y in Apply, "
                                        f"Ctrl+S, close Excel.  ({Path(p).name})")
                os.startfile(p)  # owner: open the newest plan for me
        self.run(args, done)

    def plan_file(self, which):
        """Always the newest plan of the chosen library folder; a dialog only when none is found."""
        root = self.folder.get().strip()
        if self.tv() and which == "rename":
            which = "tvrename"  # TV plans: <tag>_tvrename.xlsx
        p = pn.latest_plan(Path(root), which, self.only_tag()) if root and Path(root).is_dir() else None
        if p:
            self.status.config(text=f"Using the newest {which} plan: {p.name}")
            return str(p)
        return filedialog.askopenfilename(title=f"No {which} plan found for this folder - choose one",
                                          initialdir=str(HERE / "plans"), filetypes=[("Plans", "*.xlsx *.csv")])

    def open_plan(self, which):
        p = self.plan_file(which)
        if p:
            os.startfile(p)

    def apply(self, cmd, which, go):
        root = self.need_folder()  # also fills the temp / new folder boxes
        if not root or not self.plans_closed(root):
            return
        if self.tv() and cmd == "split-copy":
            messagebox.showinfo("TV series", "TV series need no split step - use step 2.")
            return
        p = self.plan_file(which)
        if not p:
            return
        if self.tv():  # copy the renamed shows into '<folder> (Plex)'
            if go and not messagebox.askyesno("Copy renamed series?", f"This COPIES every show into\n{root} (Plex)\n"
                                              "under its new names (rows marked Y; others copied unchanged; films and "
                                              "leftovers left out). The original folder is not touched. Continue?"):
                return
            self.run(["tv-copy", p] + (["--go"] if go else []), tool=TV)
            return
        extra = []
        if cmd == "rename-copy" and self.target.get().strip():
            extra = ["--copy-to", self.target.get().strip()]
        if cmd == "split-copy":
            extra = (["--temp", self.temp.get().strip()] if self.temp.get().strip() else []) + \
                    ["--mark", "file" if self.mark.get() else "none"]
        msg = (f"This COPIES every movie into\n{self.target.get()}\nunder its new name (rows marked Y; others "
               f"copied unchanged). The original folder is not touched. It can take hours and can be re-run "
               f"to resume. Continue?" if cmd == "rename-copy" else
               f"This COPIES the films of multi-movie folders (rows marked Y) into\n{self.temp.get()}\n"
               f"one folder each. The original is not touched"
               f"{' (except a small _SPLIT_INTO_TEMP.txt note in each split-from folder)' if self.mark.get() else ''}. "
               f"Continue?")
        if go and not messagebox.askyesno("Change files on the NAS?", msg):
            return
        self.run([cmd, p, *extra] + (["--go"] if go else []))

    def undo(self):
        p = filedialog.askopenfilename(title="Choose the log of the run to undo",
                                       initialdir=str(HERE / "logs"), filetypes=[("Undo log", "*.csv")])
        if not p:
            return
        posters = "_posters_applied_" in Path(p).name  # poster logs are undone by plexfix (in Plex, not on the NAS)
        cmd, tool = ("posters-undo", FIX) if posters else ("undo", TOOL)

        def ask(_output):
            if messagebox.askyesno("Undo", "The dry run of the undo is shown below.\nReverse these changes now?"):
                self.run([cmd, p, "--go"], tool=tool)
        self.run([cmd, p], ask, tool=tool)


if __name__ == "__main__":
    App().mainloop()
