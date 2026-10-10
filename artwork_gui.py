"""Artwork for a whole Plex library - the one-film helper's poster choice, for every film at once (plexfix.py).
1) find the candidates (Plex, TMDB, Wikipedia) and let the AI rank them  2) review them here, in this window
(change the choice, add an image address from the web, ask the AI again)  3) set the approved posters in Plex."""
import base64
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import urllib.parse
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import plexfix as pf  # noqa: E402  (plan file, thumbnails, AI ranking, Plex API)
import ui_theme as ui  # noqa: E402

FIX = HERE / "plexfix.py"
SETTINGS = HERE / "settings_artwork.json"  # last used choices of this window
LANGS = {"English (EN)": "EN", "Japanese (JP)": "JP", "Korean (KR)": "KR", "Chinese (CN)": "CN", "Russian (RU)": "RU"}


class Review(ttk.Frame):
    """The artwork plan, reviewed in this window: film list on the left, the chosen film's posters on the right
    (what Plex shows now + candidates with the AI's rank and kind). Click a poster = choose it (approved);
    every change is written to the plan file at once. PNG thumbnails come from Plex's photo transcoder."""
    W, H = 120, 180
    NORMAL, AI, SEL = ui.BG, "#2e7d32", "#e08a00"
    # owner (28 Sep): the AI picks are ~99% fine - the real work is the films with no poster at all
    FILTERS = {"To check": lambda f: not f["approved"],
               "No poster found": lambda f: not f["candidates"] and not f["approved"],
               "All films": lambda f: True, "Approved": lambda f: f["approved"]}

    def __init__(self, parent, app: "App"):
        super().__init__(parent, padding=6)
        self.app, self.plan, self.data, self.film, self.api, self.ai = app, None, None, None, None, None
        self.png = {}  # thumbnail cache: url -> PNG bytes
        self.imgs = []
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Label(bar, text="Show").pack(side="left")
        self.flt = tk.StringVar(value="To check")
        cb = ttk.Combobox(bar, textvariable=self.flt, values=list(self.FILTERS), state="readonly", width=16)
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self.fill())
        self.count = ttk.Label(bar, text="Run step 1, then '2 Review'.")
        self.count.pack(side="left", padx=10)
        ttk.Button(bar, text="✔ Set approved posters in Plex", style="Go.TButton",
                   command=app.apply_approved).pack(side="right")
        ttk.Button(bar, text="Approve all shown", command=self.approve_all).pack(side="right", padx=8)
        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, pady=(6, 0))
        lf = ttk.Frame(body)
        self.tree = ttk.Treeview(lf, columns=("film", "state"), show="headings", selectmode="browse")
        self.tree.heading("film", text="Title (film or show)")
        self.tree.heading("state", text="")
        self.tree.column("film", width=260)
        self.tree.column("state", width=90)
        ts = ttk.Scrollbar(lf, command=self.tree.yview)
        self.tree.configure(yscrollcommand=ts.set)
        ts.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.pick())
        body.add(lf, weight=1)
        d = ttk.Frame(body, padding=(8, 0))
        body.add(d, weight=3)
        self.head = ttk.Label(d, text="", font=("Segoe UI", 11, "bold"), foreground=ui.HEAD)
        self.head.pack(anchor="w")
        self.why = ttk.Label(d, text="", wraplength=760, justify="left", foreground="#555")
        self.why.pack(anchor="w", pady=(2, 4))
        st = ttk.Frame(d)
        st.pack(fill="x")
        self.state_btn = ttk.Button(st, text="Approve this choice", command=self.toggle)
        self.state_btn.pack(side="left")
        self.state_lbl = ttk.Label(st, text="")
        self.state_lbl.pack(side="left", padx=8)
        ttk.Label(st, text="click a poster = choose it · double-click = see it large", foreground="#666").pack(side="right")
        # web search / add / AI again ABOVE the posters, so a small window can't squeeze them out (owner, 28 Sep)
        tl = ttk.Frame(d)
        tl.pack(fill="x", pady=(6, 0))
        ttk.Button(tl, text="🔍 Find more on the web", style="Accent.TButton", command=self.web).pack(side="left")
        ttk.Label(tl, text="  Image address:").pack(side="left")
        self.url = tk.StringVar()
        self.url_entry = ttk.Entry(tl, textvariable=self.url, width=40)
        self.url_entry.pack(side="left", padx=2)
        ttk.Button(tl, text="Add", command=self.add).pack(side="left")
        ttk.Button(tl, text="AI pick again", command=self.rerank).pack(side="left", padx=6)
        self.busy = ttk.Label(d, text="", style="Muted.TLabel")
        self.busy.pack(anchor="w")
        self.canvas = tk.Canvas(d, height=self.H + 70, highlightthickness=0, bg=ui.BG)
        hs = ttk.Scrollbar(d, orient="horizontal", command=self.canvas.xview)
        self.row = ttk.Frame(self.canvas)
        self.row.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.row, anchor="nw")
        self.canvas.configure(xscrollcommand=hs.set)
        self.canvas.pack(fill="x", pady=(4, 0))
        hs.pack(fill="x")

    # ---------------------------------------------------------------- plan file
    def load(self, plan: Path):
        self.plan = plan
        self.data = json.loads(plan.read_text(encoding="utf-8"))
        self.fill()

    def save(self):
        self.plan.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        self.update_counts()

    def update_counts(self):
        fs = self.data["films"]
        search = sum(1 for f in fs if not f["candidates"] and not f["approved"])
        self.count.config(text=f"{sum(f['approved'] for f in fs)} of {len(fs)} approved · "
                               f"{sum(not f['approved'] for f in fs)} to check" +
                               (f" · {search} need a manual search (filter 'No poster found')" if search else ""))

    def state_text(self, f):
        if f["approved"]:
            return "✓ approved"
        return "search needed" if not f["candidates"] else "to check"

    def fill(self):
        keep = self.film["id"] if self.film else None
        self.tree.delete(*self.tree.get_children())
        self.tree.tag_configure("search", background="#fde2e0", foreground="#8a1c14")
        self.tree.tag_configure("check", background="#fff4d6")
        for f in self.data["films"]:
            if self.FILTERS[self.flt.get()](f):
                st = self.state_text(f)
                self.tree.insert("", "end", iid=str(f["id"]), values=(f"{f['title']} ({f['year']})", st),
                                 tags=("search",) if st == "search needed" else ("check",) if st == "to check" else ())
        self.update_counts()
        kids = self.tree.get_children()
        if kids:
            sel = str(keep) if keep and str(keep) in kids else kids[0]
            self.tree.selection_set(sel)
            self.tree.see(sel)
        else:
            self.film = None
            self.head.config(text="Nothing to show with this filter.")

    def pick(self):
        s = self.tree.selection()
        if s:
            self.film = next(f for f in self.data["films"] if str(f["id"]) == s[0])
            self.show()

    # ---------------------------------------------------------------- one film
    def show(self):
        f = self.film
        self.head.config(text=f"{f['title']} ({f['year']})   {f.get('orig') or ''}")
        self.why.config(text=(f.get("ai") or {}).get("reason", ""))
        self.state_lbl.config(text=self.state_text(f))
        self.state_btn.config(text="Undo approval" if f["approved"] else "Approve this choice")
        self.busy.config(text="")
        for w in self.row.winfo_children():
            w.destroy()
        # back to the start: a strip scrolled far right for a film with many posters looked empty for the next
        # film with fewer (owner, 28 Sep: "cannot click on the media name and see the images")
        self.canvas.xview_moveto(0)
        self.imgs, self.cells = [], []
        cur = dict(url="", key=f"/library/metadata/{f['id']}/thumb", current=True)
        for i, c in enumerate([cur] + f["candidates"]):
            self.cell(i, c)
        self.paint()
        if not f["candidates"] and not f["approved"]:  # no poster anywhere: straight to the manual search
            self.busy.config(text="No poster found in Plex, TMDB or Wikipedia. Click 🔍 Find more on the web, right-click "
                                  "a poster > Copy image address, paste it here and click Add.", foreground=ui.WARN)
            self.url_entry.focus_set()
        else:
            self.busy.config(foreground=ui.MUTED)

    def cell(self, i, c):
        f = self.film
        box = tk.Frame(self.row, highlightthickness=3, highlightbackground=self.NORMAL, padx=2, pady=2, bg=ui.BG)
        box.grid(row=0, column=i, sticky="n", padx=2)
        pic = tk.Label(box, text="loading…", width=15, height=11, bg=ui.PANEL)
        pic.pack()
        if c.get("current"):
            text = "now in Plex" + (" – video frame" if str(f.get("current", "")).startswith("media://") else "")
        else:
            rank = c.get("rank")
            first = ("★ AI pick" if rank == 1 else f"AI rank {rank}") + (f" · {c['kind']}" if c.get("kind") else "") + \
                    (" · cast" if c.get("cast") else "") if rank else ""
            src = " · ".join(x for x in (str(i), c.get("provider", ""), c.get("lang") if c.get("lang") not in
                                         ("none", "wiki", "") else "", c.get("size", ""),
                                         f"OCR {c['ocr']} words" if "ocr" in c else "") if x)
            text = f"{first}\n{src}".strip()
        ttk.Label(box, text=text, wraplength=self.W + 10, justify="left",
                  foreground="#2e7d32" if c.get("rank") == 1 else "#444").pack(anchor="w")
        self.cells.append((box, c))
        for w in (box, pic):
            w.bind("<Button-1>", lambda e, cc=c: self.choose(cc))
            if c.get("url", "").startswith("http"):
                w.bind("<Double-Button-1>", lambda e, u=c["url"]: webbrowser.open(u))
        u = c["key"] if c.get("current") else c["url"]
        fid = f["id"]
        if u in self.png:
            self.put(pic, self.png[u], fid)
            return
        key = c["key"]
        if key.startswith("https://image.tmdb.org/t/p/original/"):
            key = key.replace("/t/p/original/", "/t/p/w342/")  # small copy, faster

        def work():
            if self.api is None:
                self.api = pf.PlexAPI()
            try:
                return self.api.thumb(key, self.W, self.H, fmt="png")
            except Exception:
                return None  # e.g. an unmatched film without any picture - shown as '(none)', not as an error

        def done(png):
            if not png:
                if pic.winfo_exists():
                    pic.config(text="(none)")
                return
            if not c.get("current"):
                self.png[u] = png
            self.put(pic, png, fid)
        self.app.bg(work, done)

    def put(self, pic, png, fid):
        if not self.film or self.film["id"] != fid or not pic.winfo_exists():
            return
        img = tk.PhotoImage(data=base64.b64encode(png).decode())
        self.imgs.append(img)
        pic.config(image=img, text="", width=self.W, height=self.H)

    def paint(self):
        for box, c in self.cells:
            chosen = (c.get("current") and self.film["choice"] == "") or (not c.get("current") and
                                                                          c["url"] == self.film["choice"])
            box.config(highlightbackground=self.SEL if chosen else self.AI if c.get("rank") == 1 else self.NORMAL)

    def refresh_row(self):
        f = self.film
        if self.tree.exists(str(f["id"])):
            st = self.state_text(f)
            self.tree.set(str(f["id"]), "state", st)
            self.tree.item(str(f["id"]), tags=("search",) if st == "search needed" else ("check",)
                           if st == "to check" else ())
        self.state_lbl.config(text=self.state_text(f))
        self.state_btn.config(text="Undo approval" if f["approved"] else "Approve this choice")

    def choose(self, c):
        f = self.film
        if c.get("current"):
            f.update(choice="", approved=False, by="owner")  # keep what Plex shows
        else:
            f.update(choice=c["url"], approved=True, by="owner")
        self.save()
        self.paint()
        self.refresh_row()

    def toggle(self):
        if not self.film:
            return
        if not self.film["choice"]:
            messagebox.showinfo("Approve", "Click a poster first.")
            return
        self.film.update(approved=not self.film["approved"], by="owner")
        self.save()
        self.refresh_row()

    def approve_all(self):
        n = 0
        for iid in self.tree.get_children():
            f = next(x for x in self.data["films"] if str(x["id"]) == iid)
            if f["choice"] and not f["approved"]:
                f.update(approved=True, by="owner")
                n += 1
        if n and messagebox.askyesno("Approve all shown", f"Approve the current choice of {n} film(s)?"):
            self.save()
            self.fill()
        elif n:
            self.load(self.plan)  # undo the in-memory change

    def web(self):
        if self.film:
            webbrowser.open("https://www.google.com/search?tbm=isch&q=" +
                            urllib.parse.quote(self.film.get("search") or self.film["title"]))
            self.busy.config(text="Right-click a poster in the browser > 'Copy image address', paste it above, click Add.")

    def add(self):
        u = self.url.get().strip()
        if not self.film or not u.startswith("http"):
            return
        f = self.film
        self.busy.config(text="Loading the image…")

        def work():
            if self.api is None:
                self.api = pf.PlexAPI()
            c = dict(url=u, key=u, provider="web", lang="")
            pf.cache_thumb(f["id"], c, self.api)  # also checks the address really is an image Plex can read
            return c

        def done(c):
            f["candidates"].append(c)
            f.update(choice=u, approved=True, by="owner")
            self.url.set("")
            self.save()
            if self.film is f:
                self.show()
                self.refresh_row()
        self.app.bg(work, done)

    def rerank(self):
        if not self.film or not self.film["candidates"]:
            return
        f = self.film
        self.busy.config(text="The AI is looking at the posters…")

        def work():
            if self.api is None:
                self.api = pf.PlexAPI()
            if self.ai is None:
                self.ai = pf.PosterAI()
            top = pf.rank_film(f, self.ai, self.data.get("lang", "ja"))
            self.ai.save()
            return top

        def done(top):
            if top:
                f.update(choice=top["url"], approved=False, by="owner")
            self.save()
            if self.film is f:
                self.show()
                self.refresh_row()
        self.app.bg(work, done)


METHODS = (
    ("A", "A   Full search + AI",
     "Plex + all TMDB posters + Wikipedia (up to 24 per film). The AI picks the original release poster of the film's "
     "own year and country, preferring cast names. Best for Japanese, Korean, Chinese, Russian… libraries."),
    ("B", "B   Plex's posters, OCR + AI",
     "Only the posters Plex already offers. Text reading (OCR) picks the one showing cast names or a tagline; the AI "
     "decides when that is unclear. Best for English libraries."),
    ("C", "C   Plex's first poster",
     "No AI: the first real poster Plex offers (your own poster file if the folder has one). Fastest."),
)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        ui.apply(self)
        self.title("Artwork for a whole library")
        self.geometry("1180x960")
        self.busy = False
        ui.header(self, "Artwork for a whole library",
                  "Gives every film - or, in a TV-series library, every show - a proper poster. The kind of library is "
                  "recognised automatically. Nothing changes in Plex until step 3 - and you confirm it first.")
        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="Library folder").grid(row=0, column=0, sticky="w")
        self.folder = tk.StringVar()
        ttk.Entry(top, textvariable=self.folder, width=70).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(top, text="Browse…", command=self.pick).grid(row=0, column=2)
        ttk.Label(top, text="the original library folder (e.g. V:\\Japanese\\Samurai) or its '(Plex)' copy",
                  foreground="#666").grid(row=1, column=1, sticky="w", padx=4)
        ttk.Label(top, text="Original language").grid(row=2, column=0, sticky="w", pady=4)
        self.lang = tk.StringVar(value="Japanese (JP)")
        ttk.Combobox(top, textvariable=self.lang, values=list(LANGS), state="readonly", width=18).grid(
            row=2, column=1, sticky="w", padx=4)
        ttk.Label(top, text="Films").grid(row=3, column=0, sticky="nw")
        self.scope = tk.StringVar(value="frames")
        sf = ttk.Frame(top)
        sf.grid(row=3, column=1, sticky="w", padx=4)
        ttk.Radiobutton(sf, text="only those showing a video frame or no poster", variable=self.scope,
                        value="frames").pack(anchor="w")
        ttk.Radiobutton(sf, text="all films (takes longer)", variable=self.scope, value="all").pack(anchor="w")
        ttk.Label(top, text="Only titles containing").grid(row=4, column=0, sticky="w", pady=4)
        self.only = tk.StringVar()  # deliberately not remembered: a forgotten filter would make a partial plan
        ttk.Entry(top, textvariable=self.only, width=30).grid(row=4, column=1, sticky="w", padx=4)
        ttk.Label(top, text="Number of films").grid(row=5, column=0, sticky="w")
        nf = ttk.Frame(top)
        nf.grid(row=5, column=1, sticky="w", padx=4)
        self.limit = tk.StringVar()  # not remembered either
        ttk.Entry(nf, textvariable=self.limit, width=8).pack(side="left")
        ttk.Label(nf, text="for a test run, e.g. 20 - leave blank for all", style="Muted.TLabel").pack(side="left", padx=6)
        top.columnconfigure(1, weight=1)

        mf = ttk.LabelFrame(self, text="Method", padding=(10, 6))
        mf.pack(fill="x", padx=10)
        self.method = tk.StringVar(value="A")
        for r, (val, title, desc) in enumerate(METHODS):
            ttk.Radiobutton(mf, text=title, value=val, variable=self.method, width=30).grid(row=r, column=0,
                                                                                           sticky="nw", pady=2)
            ttk.Label(mf, text=desc, style="Muted.TLabel", wraplength=820, justify="left").grid(row=r, column=1,
                                                                                                sticky="w", pady=2)
        self.lang.trace_add("write", lambda *_: self.suggest_method())

        steps = ttk.LabelFrame(self, text="Steps", padding=(10, 6))
        steps.pack(fill="x", padx=10, pady=(6, 0))
        ttk.Button(steps, text="1   Find posters", width=20, style="Accent.TButton", command=self.plan).grid(
            row=0, column=0, padx=3, pady=3)
        ttk.Button(steps, text="2   Review", width=20, style="Accent.TButton", command=self.review).grid(
            row=0, column=1, padx=3, pady=3)
        ttk.Button(steps, text="3   Set in Plex", width=20, style="Go.TButton", command=self.apply_approved).grid(
            row=0, column=2, padx=3, pady=3)
        ttk.Button(steps, text="Undo a run…", width=14, command=self.undo).grid(row=0, column=3, padx=(18, 3))
        self.stop_btn = ttk.Button(steps, text="■ Stop", width=10, style="Stop.TButton", command=self.stop,
                                   state="disabled")
        self.stop_btn.grid(row=0, column=4, padx=3)
        self.status = ttk.Label(steps, text="Pick the library folder, check the method, then step 1.")
        self.status.grid(row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))
        self.proc = None

        self.events = queue.Queue()  # background work hands results to the window here (no Tk calls from threads)
        self.after(100, self.poll)
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=8, pady=8)
        self.rev = Review(self.tabs, self)
        self.tabs.add(self.rev, text="  Review  ")
        lf = ttk.Frame(self.tabs)
        self.tabs.add(lf, text="  Log  ")
        self.out = tk.Text(lf, wrap="none", font=("Consolas", 9), bg=ui.PANEL, relief="flat")
        sb = ttk.Scrollbar(lf, command=self.out.yview)
        self.out.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.out.pack(fill="both", expand=True)
        self.tabs.select(1)
        self.log("1 Find posters runs the chosen method in the background (■ Stop keeps what is finished).\n"
                 "2 Review: click a film on the left, then the poster you want (green frame = AI pick, orange = your\n"
                 "  choice). Clear picks are approved already; 'Approve all shown' accepts the rest.\n"
                 "3 Set in Plex shows how many films will change, asks, then sets and locks the posters.\n"
                 "  'Undo a run…' puts the old posters back.\n")
        self.load_settings()
        self.protocol("WM_DELETE_WINDOW", self.close)

    # ---------------------------------------------------------------- remembered choices
    # Language and method are remembered PER LIBRARY FOLDER (owner, 28 Sep: a remembered English setting from the
    # Action library was used for J.Samurai). An unknown folder gets a language guessed from its path.
    GUESS = (("japan", "Japanese (JP)"), ("korea", "Korean (KR)"), ("chin", "Chinese (CN)"), ("hong kong", "Chinese (CN)"),
             ("russia", "Russian (RU)"), ("soviet", "Russian (RU)"))

    def lib_key(self) -> str:
        f = os.path.normpath(self.folder.get().strip()) if self.folder.get().strip() else ""
        return (f[: -len(" (Plex)")] if f.endswith(" (Plex)") else f).casefold()

    def folder_changed(self):
        key = self.lib_key()
        if not key:
            return
        known = self.per_folder.get(key)
        if known:
            if known.get("lang") in LANGS:
                self.lang.set(known["lang"])
            if known.get("method") in ("A", "B", "C"):
                self.method.set(known["method"])
            self.status.config(text=f"Settings of this library: {self.lang.get()}, method {self.method.get()}.")
        else:
            guess = next((lang for word, lang in self.GUESS if word in key), "English (EN)")
            self.lang.set(guess)  # also suggests the method
            self.status.config(text=f"New library: language guessed from the folder name as {guess} - check it.")

    def load_settings(self):
        self.per_folder = {}
        try:
            s = json.loads(SETTINGS.read_text(encoding="utf-8"))
            self.per_folder = s.get("libraries", {})
            self.scope.set(s.get("scope", "frames"))
            self.folder.set(s.get("folder", ""))  # language + method of that library, or a guess (folder_changed)
        except (OSError, ValueError):
            pass
        self.folder.trace_add("write", lambda *_: self.folder_changed())
        self.folder_changed()

    def suggest_method(self):
        """English films: Plex's own posters are usually fine -> B; other languages need the full search -> A."""
        self.method.set("B" if LANGS.get(self.lang.get()) == "EN" else "A")

    def save_settings(self):
        if self.lib_key():
            self.per_folder[self.lib_key()] = dict(lang=self.lang.get(), method=self.method.get())
        try:
            SETTINGS.write_text(json.dumps(dict(folder=self.folder.get(), scope=self.scope.get(),
                                                libraries=self.per_folder), ensure_ascii=False, indent=1),
                                encoding="utf-8")
        except OSError:
            pass

    def close(self):
        if self.busy:
            if not messagebox.askyesno("A step is running", "A step is still running. Stop it and close?"):
                return
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        self.save_settings()
        self.destroy()

    # ---------------------------------------------------------------- helpers
    def pick(self):
        d = filedialog.askdirectory(title="Library folder", initialdir=self.folder.get() or "V:\\")
        if d:
            self.folder.set(os.path.normpath(d))

    def library(self):
        """plexfix works with the original library folder name; '(Plex)' is added when looking up the library."""
        f = self.folder.get().strip()
        if not f:
            messagebox.showwarning("Folder", "Pick the library folder first.")
            return None
        f = os.path.normpath(f)
        if f.endswith(" (Plex)"):
            f = f[: -len(" (Plex)")]
        return f

    def log(self, text):
        self.out.insert("end", text)
        self.out.see("end")

    def run(self, args, on_done=None):
        if self.busy:
            messagebox.showinfo("Busy", "A step is still running.")
            return
        self.busy = True
        self.save_settings()
        self.status.config(text="Running: " + args[0] + " …")
        self.log("\n> plexfix " + " ".join(f'"{a}"' if " " in a else a for a in args) + "\n")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", PYTHONNOUSERSITE="1")

        self.proc = subprocess.Popen([sys.executable, str(FIX), *args], stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=env,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.stop_btn.config(state="normal")

        def work():
            p = self.proc
            lines = []
            for line in p.stdout:
                lines.append(line)
                self.after(0, self.log, line)
            p.wait()
            self.after(0, self.finish, p.returncode, "".join(lines), on_done)

        threading.Thread(target=work, daemon=True).start()

    def finish(self, code, output, on_done):
        self.busy = False
        self.stop_btn.config(state="disabled")
        stopped = getattr(self, "stopping", False)
        self.stopping = False
        if stopped:
            self.status.config(text="Stopped. Films finished so far are saved (every 10) - '2 Review the choices' "
                                    "shows them; a new step 1 reuses the finished work quickly.")
            self.log("\n(stopped by you)\n")
        else:
            self.status.config(text="Finished." if code == 0 else f"Stopped with an error (code {code}).")
        if on_done and code == 0:
            on_done(output)

    def stop(self):
        if self.proc and self.proc.poll() is None and messagebox.askyesno(
                "Stop?", "Stop the running step?\n\nFilms already finished are kept (saved every 10 films)."):
            self.stopping = True
            self.proc.terminate()

    # ---------------------------------------------------------------- steps
    def plan(self):
        """Step 1 with the chosen method. All three write a poster plan that step 2 (Review) and 3 use."""
        lib = self.library()
        if not lib:
            return
        code = LANGS[self.lang.get()]
        guess = next((lang for word, lang in self.GUESS if word in lib.casefold()), None)
        if guess and guess != self.lang.get() and not messagebox.askyesno(
                "Check the language", f"The folder name suggests {guess}, but the language is set to "
                                      f"{self.lang.get()}.\n\nThe AI uses the language to find the ORIGINAL poster. "
                                      f"Continue with {self.lang.get()}?"):
            return
        m = self.method.get()
        if m == "A":
            args = ["posters-plan", "--folder", lib, "--lang", code]
            note = "Full search + AI: about 10-20 seconds per film."
        else:
            args = ["posters-quickplan", "--folder", lib, "--lang", pf.pn.LANGS[code]]
            if m == "C":
                args.append("--first-only")
                note = "Plex's first poster: a few seconds per film."
            else:
                note = "OCR + AI: about 5-15 seconds per film."
        if self.scope.get() == "all":
            args.append("--all")
        if self.only.get().strip():
            args += ["--only", self.only.get().strip()]
        n = self.limit.get().strip()
        if n:
            if not n.isdigit() or int(n) < 1:
                messagebox.showwarning("Number of films", "Type a number (e.g. 20), or leave it blank for all films.")
                return
            args += ["--limit", n]
        self.log(f"({note} The Review tab opens by itself when this finishes.)\n")
        self.tabs.select(1)
        self.run(args, lambda out: self.review() if "Plan written" in out else None)

    def review(self):
        lib = self.library()
        if not lib:
            return
        plan = pf.plan_path(Path(lib))
        if not plan.exists():
            messagebox.showinfo("Review", "No artwork plan yet for this library - run step 1 first.")
            return
        self.rev.load(plan)
        self.tabs.select(0)
        self.status.config(text=f"Reviewing {plan.name} - every click is saved at once. Then step 3.")

    def bg(self, work, done):
        """Run work() in a thread; done(result) runs on the window's thread. Errors go to the status line."""
        def go():
            try:
                self.events.put((done, work()))
            except Exception as e:
                self.events.put((lambda m: self.status.config(text=m), f"Error: {e}"))
        threading.Thread(target=go, daemon=True).start()

    def poll(self):
        while not self.events.empty():
            fn, arg = self.events.get_nowait()
            fn(arg)
        self.after(100, self.poll)

    def apply_approved(self):
        """One button in the Review tab: dry run first (shows how many), then ask, then set them in Plex."""
        lib = self.library()
        if not lib:
            return

        def ask(output):
            import re
            m = re.search(r"Approved changes\s*:\s*(\d+)", output)
            n = int(m.group(1)) if m else 0
            if not n:
                messagebox.showinfo("Set in Plex", "Nothing to change: no approved poster differs from what Plex "
                                                   "shows now.")
                return
            if messagebox.askyesno("Set approved posters in Plex?",
                                   f"{n} film(s) get their approved poster in Plex, locked so a refresh can't undo "
                                   "it (list in the Log tab).\nA log is written; 'Undo a run…' puts the old posters "
                                   "back.\n\nContinue?"):
                self.tabs.select(1)
                self.run(["posters-apply", "--folder", lib, "--go"],
                         lambda _o: self.status.config(text="Done - the approved posters are set in Plex. "
                                                            "Check a few films in Plex."))
        self.run(["posters-apply", "--folder", lib], ask)


    def undo(self):
        p = filedialog.askopenfilename(title="Choose the log of the poster run to undo",
                                       initialdir=str(HERE / "logs"), filetypes=[("Poster logs", "*_posters_applied_*.csv")])
        if not p:
            return

        def ask(_output):
            if messagebox.askyesno("Undo", "The dry run of the undo is shown below.\nPut the old posters back now?"):
                self.run(["posters-undo", p, "--go"])
        self.run(["posters-undo", p], ask)


if __name__ == "__main__":
    App().mainloop()
