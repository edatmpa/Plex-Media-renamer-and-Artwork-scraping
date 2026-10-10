"""Manual helper for ONE media folder: find the film on TMDB, see its posters, and get the proposed
Plex folder and file names to copy & paste (same naming rules as plexname.py).
The original media folder is never changed. A chosen poster is saved only into the NEW '(Plex)' folder."""
import base64
import json
import os
import queue
import re
import sys
import threading
import tkinter as tk
import urllib.parse
import webbrowser
from collections import defaultdict
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

HERE = Path(__file__).resolve().parent
SETTINGS = HERE / "settings_lookup.json"  # last used language and folder of this window
sys.path.insert(0, str(HERE))
import plexname as pn  # noqa: E402  (naming rules, Plex DB, TMDB)
import plexfix as pf  # noqa: E402  (Plex web API: scan, match, fields, posters; AI poster pick; DeepL)
import plextv as tv  # noqa: E402  (series: TMDB TV search, episode numbers and names)
import ui_theme as ui  # noqa: E402

try:  # optional: poster thumbnails need Pillow (TMDB images are JPEG)
    from PIL import Image, ImageTk
    import io
except ImportError:
    Image = None

LANG_NAMES = {"Japanese (JP)": "JP", "Korean (KR)": "KR", "Chinese (CN)": "CN", "Russian (RU)": "RU",
              "English (EN)": "EN"}
# Film sites for manual look-up, per language (opened in the browser; nothing is scraped). The owner edits them with
# 'Edit sites…' (saved in search_sites.json). An entry has either "search" = the site's own search address with {q}
# where the title goes, or "site" = a site without a usable search, searched through Google (site:…).
SITES_FILE = HERE / "search_sites.json"
DEFAULT_SITES = {
    "JP": [{"name": "JMDb", "site": "jmdb.ne.jp"}, {"name": "Japanese Cinema DB", "site": "japanese-cinema-db.jp"},
           {"name": "allcinema", "site": "allcinema.net"}, {"name": "eiga.com", "site": "eiga.com"}],
    "KR": [{"name": "KMDb", "site": "kmdb.or.kr"}, {"name": "KOBIS", "site": "kobis.or.kr"}],
    "CN": [{"name": "Douban", "search": "https://search.douban.com/movie/subject_search?search_text={q}"},
           {"name": "HKMDB", "site": "hkmdb.com"}],
    "RU": [{"name": "Kinopoisk", "search": "https://www.kinopoisk.ru/index.php?kp_query={q}"},
           {"name": "kino-teatr.ru", "site": "kino-teatr.ru"}, {"name": "ruskino.ru", "site": "ruskino.ru"},
           {"name": "Russian Film Hub", "site": "russianfilmhub.com"}],
    "EN": [{"name": "IMDb", "search": "https://www.imdb.com/find/?q={q}"},
           {"name": "Letterboxd", "search": "https://letterboxd.com/search/films/{q}/"},
           {"name": "TMDB website", "search": "https://www.themoviedb.org/search?query={q}"}],
}


def load_sites() -> dict:
    try:
        saved = json.loads(SITES_FILE.read_text(encoding="utf-8"))
        return {k: saved.get(k, v) for k, v in DEFAULT_SITES.items()} | {k: v for k, v in saved.items()
                                                                          if k not in DEFAULT_SITES}
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULT_SITES))


def site_url(entry: dict, q: str) -> str:
    """The address to open for a site entry and a search text (title + year)."""
    enc = urllib.parse.quote(q)
    if entry.get("search"):
        return entry["search"].replace("{q}", enc)
    return "https://www.google.com/search?q=" + urllib.parse.quote(f"site:{entry.get('site', '')} ") + enc


class Lookup(tk.Tk):
    def __init__(self):
        super().__init__()
        ui.apply(self)
        self.title("Fix one film")
        self.geometry("1100x990")
        ui.header(self, "Fix one film",
                  "Analyse the folder, pick the film on TMDB, check description and poster, then '✔ Accept & update "
                  "Plex'. The original library folder is never changed.")
        self.tmdb = pn.TMDB()
        self.facts = {}
        self.folder = None
        self.videos, self.files = [], []
        self.res = {}
        self.plex = None
        self.thumbs = []

        # --- folder and language
        f = ttk.Frame(self, padding=6)
        f.pack(fill="x")
        ttk.Label(f, text="Media folder").pack(side="left")
        self.path = tk.StringVar()
        ttk.Entry(f, textvariable=self.path, width=80).pack(side="left", padx=4, fill="x", expand=True)
        ttk.Button(f, text="Browse…", command=self.pick).pack(side="left")
        self.lang = tk.StringVar(value="Japanese (JP)")
        ttk.Combobox(f, textvariable=self.lang, values=list(LANG_NAMES), state="readonly", width=15).pack(
            side="left", padx=6)
        # owner (29 Sep): one window for both - the switch decides the logic (film: one film per folder;
        # series: one show per folder, every episode gets s01e03 via the series engine plextv.py)
        self.mtype = tk.StringVar(value="movie")
        ttk.Radiobutton(f, text="Movie", value="movie", variable=self.mtype).pack(side="left")
        ttk.Radiobutton(f, text="Series", value="series", variable=self.mtype).pack(side="left", padx=(2, 6))
        self.mtype.trace_add("write", lambda *_: self.mode_changed())
        ttk.Button(f, text="Analyse", style="Accent.TButton", command=self.analyse).pack(side="left")
        self.info = ttk.Label(self, text="Pick a media folder (one film).", padding=(8, 0), justify="left")
        self.info.pack(fill="x")
        try:  # last used language and folder (the folder is shown, not analysed until asked)
            saved = json.loads(SETTINGS.read_text(encoding="utf-8"))
            if saved.get("lang") in LANG_NAMES:
                self.lang.set(saved["lang"])
            if saved.get("mtype") in ("movie", "series"):
                self.mtype.set(saved["mtype"])
            self.path.set(saved.get("path", ""))
            if self.path.get():
                self.info.config(text="Last used folder shown - click Analyse, or Browse… for another film.")
        except (OSError, ValueError):
            pass
        self.protocol("WM_DELETE_WINDOW", self.close)

        # --- search
        s = ttk.LabelFrame(self, text="Find the film (TMDB or IMDb)", padding=6)
        s.pack(fill="x", padx=6, pady=4)
        ttk.Label(s, text="Title").grid(row=0, column=0)
        self.q = tk.StringVar()
        ttk.Entry(s, textvariable=self.q, width=40).grid(row=0, column=1, padx=4, sticky="we")
        ttk.Label(s, text="Year").grid(row=0, column=2)
        self.qy = tk.StringVar()
        ttk.Entry(s, textvariable=self.qy, width=6).grid(row=0, column=3, padx=4)
        # owner (29 Sep): IMDb must be usable too - many older films are on IMDb but not on TMDB
        self.src = tk.StringVar(value="tmdb")
        sf = ttk.Frame(s)
        sf.grid(row=0, column=4, padx=2)
        ttk.Radiobutton(sf, text="TMDB", value="tmdb", variable=self.src).pack(side="left")
        ttk.Radiobutton(sf, text="IMDb (via Plex)", value="imdb", variable=self.src).pack(side="left", padx=(4, 0))
        ttk.Button(s, text="Search", command=self.search).grid(row=0, column=5, padx=4)
        ttk.Label(s, text="or id / link").grid(row=0, column=6)
        self.qid = tk.StringVar()
        ttk.Entry(s, textvariable=self.qid, width=26).grid(row=0, column=7, padx=4)
        ttk.Button(s, text="Load", command=lambda: self.load_any(self.qid.get())).grid(row=0, column=8)
        ttk.Label(s, style="Muted.TLabel", text="IMDb (via Plex) searches Plex's own film service, which also knows "
                                               "IMDb-only films. id / link: a TMDB number or link, or an IMDb id "
                                               "(tt0199360) or link (https://www.imdb.com/title/tt0199360/)").grid(
            row=3, column=0, columnspan=9, sticky="w")
        s.columnconfigure(1, weight=1)
        self.tree = ttk.Treeview(s, columns=("title", "orig", "year", "lang", "id"), show="headings", height=5)
        for c, w in (("title", 330), ("orig", 280), ("year", 60), ("lang", 50), ("id", 80)):
            self.tree.heading(c, text={"title": "English title", "orig": "Original title"}.get(c, c.title()))
            self.tree.column(c, width=w, anchor="w")
        self.tree.grid(row=1, column=0, columnspan=8, sticky="we", pady=4)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.pick_result())
        self.results = {}  # tree row id -> Plex search result (rows 'plex:…'); TMDB rows use the TMDB id
        web = ttk.Frame(s)
        web.grid(row=2, column=0, columnspan=8, sticky="w")
        ttk.Label(web, text="Not on TMDB? Search:").pack(side="left")
        self.web = web
        self.lang.trace_add("write", lambda *_: self.build_sites())  # the site buttons follow the language
        self.after(50, self.build_sites)

        # --- details of the chosen film (editable - they feed the proposed names)
        d = ttk.LabelFrame(self, text="Chosen film (edit freely for films TMDB does not have)", padding=6)
        d.pack(fill="x", padx=6)
        self.v = {k: tk.StringVar() for k in ("prefix", "title", "year", "idtag", "edition", "native", "res")}
        labels = [("Series prefix", "prefix", 22), ("English title", "title", 45), ("Year", "year", 6),
                  ("Id tag", "idtag", 16), ("Edition", "edition", 14), ("Native title", "native", 30),
                  ("Resolution", "res", 7)]
        for i, (lab, key, w) in enumerate(labels):
            ttk.Label(d, text=lab).grid(row=i // 4 * 2, column=i % 4, sticky="w", padx=3)
            ttk.Entry(d, textvariable=self.v[key], width=w).grid(row=i // 4 * 2 + 1, column=i % 4, sticky="we", padx=3)
            self.v[key].trace_add("write", lambda *_: self.propose())
        b = ttk.Frame(d)
        b.grid(row=4, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Button(b, text="Open TMDB page", command=self.open_tmdb).pack(side="left", padx=2)
        ttk.Button(b, text="Open IMDb page", command=self.open_imdb).pack(side="left", padx=2)
        ttk.Button(b, text="Posters…", command=self.show_posters).pack(side="left", padx=2)
        ttk.Button(b, text="Use in rename plan…", command=self.use_in_plan).pack(side="left", padx=2)
        self.overview = ttk.Label(d, text="", wraplength=1000, justify="left", foreground="#555")
        self.overview.grid(row=5, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.chosen = {}

        self.status = ttk.Label(self, text="", padding=4)
        self.status.pack(side="bottom", fill="x")
        tabs = ttk.Notebook(self)
        tabs.pack(fill="both", expand=True, padx=6, pady=4)

        # --- tab 1 - Plex: fix this film (description + artwork) - only in '(Plex)' / temp, never the original
        x = ttk.Frame(tabs, padding=6)
        tabs.add(x, text="  Plex: description & artwork  ")
        self.plex_info = ttk.Label(x, text="Analyse a folder first.")
        self.plex_info.grid(row=0, column=0, columnspan=3, sticky="w")
        bx = ttk.Frame(x)
        bx.grid(row=1, column=0, columnspan=3, sticky="w", pady=3)
        ttk.Button(bx, text="✔ Accept & update Plex", style="Go.TButton", command=self.accept_all).pack(
            side="left", padx=(2, 12))
        ttk.Label(bx, text="or step by step:", style="Muted.TLabel").pack(side="left")
        ttk.Button(bx, text="1 Rename folder now", command=self.rename_now).pack(side="left", padx=2)
        ttk.Button(bx, text="2 Match in Plex", command=self.match_plex).pack(side="left", padx=2)
        ttk.Label(x, text="Description").grid(row=2, column=0, sticky="nw", pady=2)
        self.plot = tk.Text(x, height=4, wrap="word", font=("Segoe UI", 9), bg=ui.PANEL, relief="flat")
        self.plot.grid(row=2, column=1, sticky="we", pady=2)
        pb = ttk.Frame(x)
        pb.grid(row=2, column=2, sticky="n", padx=4)
        ttk.Button(pb, text="Plot from TMDB", command=self.tmdb_plot).pack(fill="x")
        ttk.Button(pb, text="Translate to English", command=self.translate_plot).pack(fill="x", pady=2)
        ttk.Button(pb, text="Save title + description to Plex", command=self.write_plex).pack(fill="x")
        ttk.Label(x, text="Artwork").grid(row=3, column=0, sticky="nw", pady=(6, 0))
        self.art = ArtworkStrip(x, self)
        self.art.grid(row=3, column=1, columnspan=2, sticky="nsew", pady=(6, 0))
        x.columnconfigure(1, weight=1)
        x.rowconfigure(3, weight=1)
        self.item = None  # the Plex movie in this folder

        # --- tab 2 - proposed names to copy & paste
        p = ttk.Frame(tabs, padding=6)
        tabs.add(p, text="  Proposed names  ")
        self.new_folder = tk.StringVar()
        self.new_video = tk.StringVar()
        for r, (lab, var) in enumerate((("Folder", self.new_folder), ("Video file", self.new_video))):
            ttk.Label(p, text=lab).grid(row=r, column=0, sticky="w")
            ttk.Entry(p, textvariable=var).grid(row=r, column=1, sticky="we", padx=4, pady=2)
            ttk.Button(p, text="Copy", command=lambda v=var: self.copy(v.get())).grid(row=r, column=2)
        p.columnconfigure(1, weight=1)
        self.all = tk.Text(p, height=6, wrap="none", font=("Consolas", 9), bg=ui.PANEL, relief="flat")
        self.all.grid(row=2, column=0, columnspan=3, sticky="nsew", pady=4)
        p.rowconfigure(2, weight=1)
        ttk.Button(p, text="Copy all (old -> new)", command=lambda: self.copy(self.all.get("1.0", "end"))).grid(
            row=3, column=2, sticky="e")
        self.mapping = []
        self.events = queue.Queue()
        self.after(100, self.poll)

    # ------------------------------------------------------------ helpers
    def bg(self, work, done):
        """Run work() in a thread; done(result) runs back on the window's thread (via a queue)."""
        def run():
            try:
                self.events.put((done, work()))
            except Exception as e:
                self.events.put((lambda msg: self.status.config(text=msg), f"Error: {e}"))
        threading.Thread(target=run, daemon=True).start()

    def poll(self):
        while not self.events.empty():
            fn, arg = self.events.get_nowait()
            fn(arg)
        self.after(100, self.poll)

    def copy(self, text):
        self.clipboard_clear()
        self.clipboard_append(text.strip())
        self.status.config(text="Copied to the clipboard.")

    # ------------------------------------------------------------ film sites for the chosen language
    def build_sites(self):
        for w in self.web.winfo_children()[1:]:
            w.destroy()
        code = LANG_NAMES.get(self.lang.get(), "EN")
        query = lambda: f"{self.q.get()} {self.qy.get()}".strip() or (self.folder.name if self.folder else "")
        for e in load_sites().get(code, []):
            ttk.Button(self.web, text=e.get("name", "?"),
                       command=lambda e=e: webbrowser.open(site_url(e, query()))).pack(side="left", padx=2)
        ttk.Button(self.web, text="Google images", command=lambda: webbrowser.open(
            "https://www.google.com/search?tbm=isch&q=" + urllib.parse.quote(query() + " poster"))).pack(
            side="left", padx=2)
        ttk.Button(self.web, text="✎ Edit sites…", command=lambda: SitesEditor(self, code)).pack(side="left", padx=(10, 2))

    def lang_code(self):
        return pn.LANGS[LANG_NAMES[self.lang.get()]]

    def series(self) -> bool:
        return self.mtype.get() == "series"

    def mode_changed(self):
        """Movie <-> Series: the same window, other logic. Re-read the folder with the new logic."""
        self.chosen = {}
        self.tree.delete(*self.tree.get_children()) if hasattr(self, "tree") else None
        if getattr(self, "status", None):
            self.status.config(text=("Series: one show per folder - every episode gets s01e03. " if self.series() else
                                     "Movie: one film per folder. ") + "Click Analyse.")

    def save_settings(self):
        try:
            SETTINGS.write_text(json.dumps(dict(lang=self.lang.get(), path=self.path.get().strip(),
                                                mtype=self.mtype.get()), ensure_ascii=False, indent=1),
                                encoding="utf-8")
        except OSError:
            pass

    def close(self):
        self.save_settings()
        self.destroy()

    def pick(self):
        last = Path(self.path.get().strip()) if self.path.get().strip() else None
        start = str(last.parent) if last and last.parent.is_dir() else "V:\\"  # next film is usually a sibling
        d = filedialog.askdirectory(title="One media folder (one film)", initialdir=start)
        if d:
            self.path.set(os.path.normpath(d))
            self.analyse()

    # ------------------------------------------------------------ analyse the folder
    def analyse(self):
        folder = Path(self.path.get().strip())
        if not folder.is_dir():
            messagebox.showwarning("Folder", "Pick a media folder first.")
            return
        self.folder = folder
        series = self.series()
        self.status.config(text="Reading the folder and the Plex database…")

        def work():
            files = sorted(p for p in folder.rglob("*") if p.is_file())
            videos = [p for p in files if p.suffix.lower() in pn.VIDEO_EXT and not pn.EXTRA_RE.search(p.stem)]
            if not self.facts:
                self.facts = pn.PlexDB().parts_under(folder.parent)
            plex = next((pn.lookup(self.facts, v) for v in videos if pn.lookup(self.facts, v)), None)
            res = {v: (pn.lookup(self.facts, v) or {}).get("res") or pn.mediainfo_res(v) for v in videos}
            if series:  # a show: Plex's facts per file are episodes, not the show - the show comes from its episodes
                return files, videos, None, res, pf.show_in_folder(folder)
            return files, videos, plex, res, pf.item_in_folder(folder)

        def done(out):
            self.files, self.videos, self.plex, self.res, self.item = out
            self.show_plex_info()
            t, y = pn.clean_from_filename(self.videos[0].stem if self.videos else self.folder.name)
            ft, fy = pn.clean_from_filename(self.folder.name)
            self.q.set(re.sub(r"[\[(].*?[\])]", " ", ft or t).strip(" -"))
            self.qy.set(fy or y)
            cur = re.split(r"\s*\((?:19|20)\d{2}\)", self.folder.name)[0]
            self.v["prefix"].set(cur.split(" - ", 1)[0].strip() if " - " in cur else "")
            ed = pn.EDITION_RE.search(self.folder.name) or next(
                (pn.EDITION_RE.search(v.stem) for v in self.videos if pn.EDITION_RE.search(v.stem)), None)
            self.v["edition"].set(ed.group(1).title().replace("'S", "'s") if ed else "")
            best = max(self.res.values(), key=lambda r: pn.RES_ORDER.get(r, 0)) if self.res else ""
            self.v["res"].set(best)
            # starting point before a TMDB film is chosen: Plex's title/year, else the cleaned name
            self.v["title"].set(pn.safe_name(self.plex["title"]) if self.plex else self.q.get())
            self.v["year"].set(self.plex["year"] if self.plex else self.qy.get())
            self.v["idtag"].set(f"{{imdb-{self.plex['imdb']}}}" if self.plex and self.plex["imdb"] else "")
            self.v["native"].set(pn.safe_name(self.plex["orig"]) if self.plex and self.plex["orig"] else "")
            vids = ", ".join(f"{v.name} [{self.res.get(v) or '?'}]" for v in self.videos) or "no video file!"
            plex = (f"Plex: {self.plex['title']} ({self.plex['year']})  tmdb {self.plex['tmdb'] or '-'}  "
                    f"imdb {self.plex['imdb'] or '-'}") if self.plex else "Plex: not matched"
            if series:
                vids = f"{len(self.videos)} episode files (e.g. {self.videos[0].name})" if self.videos else "no video!"
                plex = (f"Plex show: {self.item['title']} ({self.item['year']})" if self.item
                        else "Plex: show not matched / not scanned")
            self.info.config(text=f"{len(self.files)} files. {'' if series else 'Video: '}{vids}\n{plex}")
            self.build_sites()
            self.status.config(text="Ready. Search TMDB, or pick the Plex match below.")
            self.chosen = {}
            self.plot.delete("1.0", "end")
            if series:
                self.v["prefix"].set("")
                self.v["edition"].set("")
                tid = re.search(r"tmdb-(\d+)", self.folder.name)
                if tid:
                    self.select_tv(tid.group(1))  # the show's id is already in the folder name
                else:
                    self.search()
                    self.load_art()
            elif self.plex and self.plex["tmdb"]:
                self.select_id(self.plex["tmdb"])  # also loads the artwork
            else:
                self.search()
                self.load_art()
        self.bg(work, done)

    # ------------------------------------------------------------ TMDB
    def search(self):
        q, y, lang = self.q.get().strip(), self.qy.get().strip(), self.lang_code()
        if not q:
            return
        imdb_mode = self.src.get() == "imdb"
        self.status.config(text="Searching Plex's film service (IMDb films too)…" if imdb_mode else "Searching TMDB…")
        item = self.item

        series = self.series()
        folder_name = self.folder.name if self.folder else ""

        def work():
            res = []
            if not imdb_mode and series:  # TMDB TV search; shows of the chosen language first
                # like the library run: also the folder's Cyrillic / transliterated names ('Poyas Oriona' -> Пояс Ориона)
                texts = [q]
                if folder_name:
                    ft, _ = tv.name_parts(folder_name)
                    texts += ft
                    if lang == "ru":
                        texts += [tv.to_cyrillic(t) for t in ft if not re.search("[А-Яа-я]", t)]
                seen = {}
                for t in dict.fromkeys(x for x in texts if x):
                    for r in tv.tv_search(self.tmdb, t, y, lang):
                        seen.setdefault(r["id"], r)
                res = [dict(id=f"tv:{r['id']}", title=r["name"], orig=r["orig"], year=r["year"], lang=r["lang"])
                       for r in seen.values()]
                near = lambda r: bool(y and r["year"]) and abs(int(r["year"]) - int(y)) <= 1
                res.sort(key=lambda r: (r["lang"] != lang, not near(r)))
            elif not imdb_mode:
                res = self.tmdb.search(q, y, lang)
                if y:
                    ids = {r["id"] for r in res}
                    res += [r for r in self.tmdb.search(q, "", lang) if r["id"] not in ids]
            plex = []
            if imdb_mode or not res:  # IMDb mode, or TMDB found nothing: Plex's service (knows IMDb-only titles)
                plex = pf.plex_search(pf.PlexAPI(), item["id"] if item and item.get("tv") == series else None, q, y,
                                      tv=series)
            return res, plex

        def done(out):
            res, plex = out
            self.tree.delete(*self.tree.get_children())
            self.results = {}
            for r in res:
                self.tree.insert("", "end", iid=r["id"], values=(r["title"], r["orig"], r["year"], r["lang"], r["id"]))
            for i, r in enumerate(plex):
                iid = f"plex:{i}"
                self.results[iid] = r
                self.tree.insert("", "end", iid=iid, values=(r["name"], "", r["year"], "Plex", "click for details"))
            if res:
                self.status.config(text=f"{len(res)} TMDB results ({lang} films first). Click one.")
            elif plex:
                self.status.config(text=f"{'' if imdb_mode else 'TMDB found nothing - '}{len(plex)} results from "
                                        "Plex's film service (IMDb films too). Click one to load its details.")
            else:
                self.status.config(text="Nothing found. Try another title/year, or paste an IMDb/TMDB link into "
                                        "'id / link'.")
        self.bg(work, done)

    def pick_result(self):
        s = self.tree.selection()
        if not s:
            return
        if s[0].startswith("plex:"):
            self.select_plex(self.results[s[0]]["guid"])
        elif s[0].startswith("tv:"):
            self.select_tv(s[0][3:])
        else:
            self.select_id(s[0])

    def select_plex(self, guid: str):
        """Load a film from Plex's film service: all its details go into the fields, ready for 'Accept & update Plex'.
        A film that is also on TMDB is loaded from TMDB (same film, fuller naming data)."""
        self.status.config(text="Loading the film's details from Plex's film service…")

        def done(d):
            if d["tmdb"] and self.series():
                self.select_tv(d["tmdb"])
                return
            if d["tmdb"]:
                self.select_id(d["tmdb"])  # TMDB knows it: names from TMDB as everywhere else
                self.status.config(text=f"Found on TMDB as well (tmdb-{d['tmdb']}, {d['imdb'] or 'no IMDb'}) - "
                                        "loaded from TMDB.")
                return
            self.fill_film(d, source="Plex's film service")
        self.bg(lambda: pf.plex_details(pf.PlexAPI(), guid), done)

    def fill_film(self, d: dict, source: str):
        """Fill the fields from a film that is not on TMDB (id tag {imdb-tt…})."""
        self.chosen = dict(id="", imdb=d.get("imdb", ""), title=d.get("title", ""), orig=d.get("orig", ""),
                           year=d.get("year", ""), overview=d.get("summary", ""), lang="", collection="")
        self.v["title"].set(pn.safe_name(d.get("title", "")))
        self.v["year"].set(d.get("year", ""))
        self.v["idtag"].set(f"{{imdb-{d['imdb']}}}" if d.get("imdb") else "")
        native = d.get("orig", "")
        self.v["native"].set(pn.safe_name(native) if native and pn.words_of(native) != pn.words_of(d.get("title", ""))
                             else "")
        cur = self.plot.get("1.0", "end").strip()
        if d.get("summary") and (not cur or cur == getattr(self, "auto_plot", "")):
            self.plot.delete("1.0", "end")
            self.plot.insert("1.0", d["summary"])
            self.auto_plot = d["summary"].strip()
        extra = " · ".join(x for x in (d.get("studio", ""), ", ".join(d.get("directors") or []),
                                       ", ".join(d.get("cast") or [])) if x)
        self.overview.config(text=f"{d.get('title', '?')} / {native or '-'} ({d.get('year', '?')})  "
                                  f"imdb {d.get('imdb') or '-'}  - from {source}{'   ' + extra if extra else ''}")
        self.status.config(text=f"Loaded from {source}. Id tag {self.v['idtag'].get() or '(none)'} - check the "
                                "fields, then '✔ Accept & update Plex'.")
        if self.item:
            self.load_art()

    def select_id(self, tid):
        if not tid or not tid.isdigit():
            return
        self.status.config(text=f"Loading TMDB {tid}…")

        def done(m):
            self.chosen = m
            self.v["title"].set(pn.safe_name(m["title"]))
            self.v["year"].set(m["year"])
            self.v["idtag"].set(f"{{tmdb-{m['id']}}}")
            self.v["native"].set(pn.safe_name(m["orig"]) if pn.words_of(m["orig"]) != pn.words_of(m["title"]) else "")
            cur = self.plot.get("1.0", "end").strip()
            if not cur or cur == getattr(self, "auto_plot", ""):  # don't overwrite a plot the owner typed
                self.plot.delete("1.0", "end")
                self.plot.insert("1.0", m["overview"])
                self.auto_plot = m["overview"].strip()
            warn = f"   ⚠ original language '{m['lang']}'" if m["lang"] != self.lang_code() else ""
            coll = f"   Collection: {m['collection']}" if m["collection"] else ""
            self.overview.config(text=f"{m['title']} / {m['orig']} ({m['year']})  imdb {m['imdb'] or '-'}"
                                      f"{coll}{warn}")  # the plot itself is in the Description box below
            if self.item:  # new film chosen: its posters
                self.load_art()
            self.status.config(text="Proposed names below. Edit any field above to adjust them.")
        self.bg(lambda: self.tmdb.details(tid), done)

    def select_tv(self, tid: str):
        """Series mode: a TMDB TV show - names (owner's rule: English name from the folder when TMDB has none),
        id tag, native title, description; the episode names follow in 'Proposed names'."""
        self.status.config(text=f"Loading TMDB show {tid}…")
        folder_name = self.folder.name if self.folder else ""

        def work():
            s = tv.tv_details(self.tmdb, tid)
            if not s:
                raise RuntimeError(f"TMDB has no TV show {tid}")
            j = self.tmdb._get(f"tv/{tid}", language=self.tmdb.lang)
            ov = j.get("overview") or self.tmdb._get(f"tv/{tid}", language=self.lang_code()).get("overview") or ""
            self.tmdb.save()
            return s, ov

        def done(out):
            s, ov = out
            title = tv.english_title(s, folder_name)
            self.chosen = dict(id=s["id"], imdb=s["imdb"], title=title, orig=s["orig"], year=s["year"],
                               overview=ov, lang=s["lang"], collection="", tv=True, seasons=s.get("seasons", {}))
            self.v["title"].set(pn.safe_name(title))
            self.v["year"].set(s["year"])
            self.v["idtag"].set(f"{{tmdb-{s['id']}}}")
            self.v["native"].set(pn.safe_name(s["orig"]) if s["orig"] and tv.tw(s["orig"]) != tv.tw(title) else "")
            cur = self.plot.get("1.0", "end").strip()
            if not cur or cur == getattr(self, "auto_plot", ""):
                self.plot.delete("1.0", "end")
                self.plot.insert("1.0", ov)
                self.auto_plot = ov.strip()
            seasons = ", ".join(f"S{int(k):02d}: {n} ep." for k, n in sorted(s.get("seasons", {}).items(),
                                                                                key=lambda kv: int(kv[0])))
            warn = f"   ⚠ original language '{s['lang']}'" if s["lang"] != self.lang_code() else ""
            self.overview.config(text=f"{title} / {s['orig']} ({s['year']})  imdb {s['imdb'] or '-'}   "
                                      f"TMDB seasons: {seasons or '-'}{warn}")
            self.propose()
            if self.item:
                self.load_art()
            self.status.config(text="Show loaded. Episode names are in 'Proposed names' - check them, then "
                                    "'✔ Accept & update Plex'.")
        self.bg(work, done)

    def load_any(self, text: str):
        """Load by id or link: TMDB number / themoviedb.org link, or IMDb tt-id / imdb.com link."""
        text = text.strip()
        im = re.search(r"(tt\d{5,10})", text)
        if self.series():  # a TMDB TV id / link, or an IMDb series id
            tm = re.search(r"themoviedb\.org/tv/(\d+)", text) or re.fullmatch(r"(\d+)", text)
            if tm and not im:
                self.select_tv(tm.group(1))
            elif im:
                def find():
                    r = self.tmdb._get(f"find/{im.group(1)}", external_source="imdb_id").get("tv_results") or []
                    if r:
                        return ("tmdb", str(r[0]["id"]))
                    api = pf.PlexAPI()
                    m = pf.plex_find(api, pf.any_show_id(), f"imdb-{im.group(1)}", tv=True)
                    return ("plex", m["guid"]) if m else ("none", "")

                def found(out):
                    kind, val = out
                    if kind == "tmdb":
                        self.select_tv(val)
                    elif kind == "plex":
                        self.select_plex(val)
                    else:
                        self.status.config(text=f"{im.group(1)}: not found on TMDB or in Plex's TV service.")
                self.bg(find, found)
            else:
                messagebox.showinfo("Load", "Type a TMDB TV number or link, or an IMDb id (tt…) or link.")
            return
        tm = re.search(r"themoviedb\.org/movie/(\d+)", text) or re.fullmatch(r"(\d+)", text)
        if tm and not im:
            self.select_id(tm.group(1))
        elif im:
            self.select_imdb(im.group(1))
        else:
            messagebox.showinfo("Load", "Type a TMDB number or link, or an IMDb id (tt…) or link.")

    def select_imdb(self, imdb: str):
        """An IMDb film, resolved through official channels only (IMDb's own pages are not read):
        1) TMDB (full details), 2) Plex's own matching service (knows many IMDb films TMDB lacks), 3) Wikidata."""
        self.status.config(text=f"Looking up {imdb} (TMDB, Plex, Wikidata)…")
        item = self.item

        def work():
            tid = self.tmdb.find_imdb(imdb)
            if tid:
                return ("tmdb", tid)
            try:
                api = pf.PlexAPI()
                m = pf.plex_find(api, item["id"] if item else pf.any_movie_id(), f"imdb-{imdb}")
                if m:
                    d = pf.plex_details(api, m["guid"])
                    d["imdb"] = d.get("imdb") or imdb
                    return ("plex", d)
            except Exception:
                pass
            hits = pf._wget("https://www.wikidata.org/w/api.php", action="query", list="search",
                            srsearch=f"haswbstatement:P345={imdb}", format="json")["query"]["search"]
            if hits:
                q = hits[0]["title"]
                e = pf._wget(f"https://www.wikidata.org/wiki/Special:EntityData/{q}.json")["entities"][q]
                lab = e.get("labels", {})
                date = next((c["mainsnak"]["datavalue"]["value"]["time"] for c in e.get("claims", {}).get("P577", [])
                             if "datavalue" in c["mainsnak"]), "")
                return ("wikidata", dict(title=(lab.get("en") or {}).get("value", ""),
                                         orig=(lab.get(self.lang_code()) or {}).get("value", ""), year=date[1:5]))
            return ("none", {})

        def done(out):
            kind, info = out
            if kind == "tmdb":
                self.select_id(info)
                return
            if kind == "plex":  # full details; the film also appears in the results list
                self.tree.delete(*self.tree.get_children())
                self.results = {"plex:0": dict(name=info["title"], year=info["year"], guid=info["guid"])}
                self.tree.insert("", "end", iid="plex:0", values=(info["title"], info["orig"], info["year"], "Plex",
                                                                   imdb))
                self.fill_film(info, source="Plex's film service")
                return
            if kind == "none":
                self.status.config(text=f"{imdb}: not on TMDB, unknown to Plex and Wikidata. The id tag is set - "
                                        "type the title, year and native title yourself.")
                info = {}
            self.chosen = dict(id="", imdb=imdb, title=info.get("title", ""), orig=info.get("orig", ""),
                               year=info.get("year", ""), overview="", lang="", collection="")
            if info.get("title"):
                self.v["title"].set(pn.safe_name(info["title"]))
            if info.get("year"):
                self.v["year"].set(info["year"])
            self.v["idtag"].set(f"{{imdb-{imdb}}}")
            if info.get("orig"):
                self.v["native"].set(pn.safe_name(info["orig"]))
            src = {"plex": "Plex's matching service", "wikidata": "Wikidata"}.get(kind, "")
            self.overview.config(text=f"IMDb {imdb}: {info.get('title', '?')} ({info.get('year', '?')})"
                                      f"{' - from ' + src if src else ''}. Not on TMDB: check the fields, type the "
                                      f"native title and a description yourself.")
            if kind != "none":
                self.status.config(text=f"Loaded {imdb} from {src}. Id tag {{imdb-{imdb}}} - Plex will match by IMDb.")
            if self.item:
                self.load_art()
        self.bg(work, done)

    def open_tmdb(self):
        if self.chosen.get("id"):
            webbrowser.open(f"https://www.themoviedb.org/movie/{self.chosen['id']}")
        else:
            messagebox.showinfo("TMDB", "This film is not on TMDB (loaded from IMDb).")

    def open_imdb(self):
        if self.chosen.get("imdb"):
            webbrowser.open(f"https://www.imdb.com/title/{self.chosen['imdb']}/")
        else:
            messagebox.showinfo("IMDb", "TMDB has no IMDb id for this film.")

    # ------------------------------------------------------------ proposed names
    def propose_series(self):
        """Series: the show folder name and, for every file, 'Season 01\\Show (Year) - s01e03 - Title [RES].ext'."""
        g = {k: v.get().strip() for k, v in self.v.items()}
        title = pn.safe_name(g["title"]) or pn.safe_name(self.folder.name)
        folder = pn.safe_name(" ".join(x for x in (f"{title} ({g['year']})" if g["year"] else title, g["idtag"],
                                                   g["native"]) if x))
        tid = self.tmdb_id()
        files = self.files
        res = {p: self.res.get(p, "") for p in files}
        mapping = tv.episode_mapping(self.folder, files, title, g["year"], tid, self.tmdb, res)
        self.mapping = mapping
        self.new_folder.set(folder)
        first = next((n for _, n in mapping if not n.startswith("(") and n.startswith("Season")), "")
        self.new_video.set(first)
        self.all.delete("1.0", "end")
        self.all.insert("end", f"Show folder:\n  {self.folder.name}\n  -> {folder}\n\nEpisodes and other files:\n")
        for old, new in mapping:
            self.all.insert("end", f"  {old}\n  -> {new}\n")
        missing = sum(1 for _, n in mapping if n.startswith("(no episode"))
        if missing:
            self.status.config(text=f"⚠ {missing} file(s) without a recognisable episode number - see Proposed names.")

    def propose(self):
        if not self.folder:
            return
        if self.series():
            self.propose_series()
            return
        g = {k: v.get().strip() for k, v in self.v.items()}
        name = pn.safe_name(g["title"]) or pn.safe_name(self.folder.name)
        if g["prefix"] and not pn.has_series(name, g["prefix"]):
            name = f"{g['prefix']} - {name}"
        edition = "{edition-" + g["edition"] + "}" if g["edition"] and not g["edition"].startswith("{") else g["edition"]

        def compose(res, extra=""):
            parts = [f"{name} ({g['year']})" if g["year"] else name, g["idtag"], edition, g["native"],
                     f"[{res}]" if res else ""]
            return pn.safe_name(" ".join(p for p in parts if p) + extra)

        folder = compose(g["res"])
        groups = defaultdict(list)
        for v in self.videos:
            groups[pn.part_key(v.stem)].append(v)
        stem, seen = {}, defaultdict(int)
        for grp in groups.values():
            grp.sort()
            for i, v in enumerate(grp, 1):
                hs = " [EngHardSub]" if pn.HARDSUB_RE.search(v.stem) or pn.HARDSUB_RE.search(self.folder.name) else ""
                pt = f" - pt{i}" if len(grp) > 1 else ""
                r = self.res.get(v) or g["res"]
                seen[(r, hs, pt)] += 1
                ver = f" - version {seen[(r, hs, pt)]}" if seen[(r, hs, pt)] > 1 else ""
                stem[v] = compose(r, hs + pt + ver)
        mapping, used = [], set()
        stems = sorted(((pn.norm(v.stem), v) for v in self.videos), key=lambda t: -len(t[0]))
        for p in self.files:
            rel = str(p.relative_to(self.folder))
            if p in stem:
                new = stem[p] + p.suffix.lower()
            elif p.suffix.lower() in pn.SUB_EXT and self.videos:
                owner = next((v for s, v in stems if s and pn.norm(p.stem).startswith(s)), self.videos[0])
                code, flags = pn.sub_language(p)
                new, k = stem[owner] + (f".{code}" if code else "") + flags + p.suffix.lower(), 2
                while new.casefold() in used:
                    new = stem[owner] + (f".{code}" if code else "") + flags + f".{k}" + p.suffix.lower()
                    k += 1
            elif p.suffix.lower() in pn.VIDEO_EXT:
                new = "(sample/trailer - not copied)"
            else:
                new = "(leftover - not copied)"
            used.add(new.casefold())
            mapping.append((rel, new))
        self.mapping = mapping
        self.new_folder.set(folder)
        self.new_video.set(stem[self.videos[0]] + self.videos[0].suffix.lower() if self.videos else "")
        self.all.delete("1.0", "end")
        self.all.insert("end", f"Folder:\n  {self.folder.name}\n  -> {folder}\n\nFiles:\n")
        for old, new in mapping:
            self.all.insert("end", f"  {old}\n  -> {new}\n")
        longest = max((len(str(self.new_root() / folder / n)) for _, n in mapping), default=0)
        if longest > pn.MAX_PATH:
            self.status.config(text=f"⚠ Longest new path is {longest} characters (limit {pn.MAX_PATH}) - "
                                    "shorten the native title.")

    def new_root(self) -> Path:
        """'<library> (Plex)' next to the library folder that holds this media folder."""
        lib = self.folder.parent
        if lib.name.endswith(" (split temp)"):
            lib = lib.with_name(lib.name[: -len(" (split temp)")])
        return lib.with_name(lib.name + " (Plex)")

    # ------------------------------------------------------------ Plex: rename, match, details, poster
    def editable(self) -> bool:
        """Renaming is allowed only in the new copies - never in the original library folder."""
        return bool(self.folder) and self.folder.parent.name.endswith((" (Plex)", " (split temp)"))

    def show_plex_info(self):
        where = "" if self.editable() else "   (original folder: renaming here is not allowed)"
        if self.item:
            tm = re.search(r"tmdb-(\d+)", self.folder.name)
            self.plex_info.config(text=f"In Plex: {self.item['title']} ({self.item['year']}) - item {self.item['id']}"
                                       f"{'  · folder id tmdb-' + tm.group(1) if tm else ''}{where}")
        else:
            self.plex_info.config(text="Not in Plex yet (or not scanned since a rename) - 'Match in Plex' will scan "
                                       "this folder first." + where)

    def tmdb_id(self) -> str:
        m = re.search(r"tmdb-(\d+)", self.v["idtag"].get())
        return m.group(1) if m else ""

    def match_key(self) -> str:
        """What Plex should match: 'tmdb-<id>', else 'imdb-tt…' (IMDb films TMDB lacks), else ''."""
        t = self.tmdb_id()
        if t:
            return f"tmdb-{t}"
        m = re.search(r"imdb-(tt\d+)", self.v["idtag"].get())
        return f"imdb-{m.group(1)}" if m else ""

    # --- the pieces (planned on the window's thread, carried out in a worker thread) ---
    def plan_rename(self):
        """(file moves, new folder) for the proposed names. Raises ValueError with a reason for the owner."""
        if not self.editable():
            raise ValueError("Only a film in a '(Plex)' or '(split temp)' folder can be renamed here.\n"
                             "The original library folder is never changed.")
        new_dir = self.folder.parent / self.new_folder.get().strip()
        if not new_dir.name or pn.safe_name(new_dir.name) != new_dir.name:
            raise ValueError("The new folder name is empty or has characters Windows can't use.")
        moves = []
        for old, new in self.mapping:
            src = self.folder / old
            dst = self.folder / new  # series: 'Season 01\\…' inside the show folder
            if new.startswith("(") or dst == src:
                continue  # leftovers stay; unchanged names need nothing
            if dst.exists() and dst != src:
                raise ValueError(f"A file with the new name already exists:\n{dst.name}")
            moves.append((src, dst))
        if new_dir != self.folder and new_dir.exists():
            raise ValueError(f"A folder with the new name already exists:\n{new_dir}")
        return moves, new_dir

    @staticmethod
    def do_rename(moves, old_dir: Path, new_dir: Path) -> str:
        """Files first (inside the old folder), then the folder; every step logged for 'Undo a run…'."""
        import csv
        log = pn.LOG_DIR / f"manual_rename_{re.sub(r'[^0-9A-Za-z]+', '_', old_dir.name)[:40]}_" \
                           f"{pn.dt.datetime.now():%Y-%m-%d_%H%M%S}.csv"
        pn.LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(log, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=pn.LOG_FIELDS)
            w.writeheader()
            for src, dst in moves:
                if not dst.parent.exists():  # series: new 'Season 01' folder (Undo removes it again when empty)
                    dst.parent.mkdir(parents=True)
                    w.writerow(dict(op="MKDIR", src="", dst=str(dst.parent), result="ok"))
                os.rename(src, dst)
                w.writerow(dict(op="MOVE", src=str(src), dst=str(dst), result="ok"))
                fh.flush()
            if new_dir != old_dir:
                os.rename(old_dir, new_dir)
                w.writerow(dict(op="MOVE", src=str(old_dir), dst=str(new_dir), result="ok"))
        return log.name

    @staticmethod
    def ensure_matched(api, folder: Path, key: str, series=False):
        """(Plex item, message): find the film's (series: the show's) Plex item (scanning the library after a rename -
        Plex then finds the moved files and keeps their watch history), match it to key ('tmdb-<id>' or 'imdb-tt…'),
        and wait until Plex's refresh is done, so the refresh can't overwrite what is written afterwards."""
        guid = key.replace("-", "://", 1) if key else ""  # 'imdb-tt123' -> 'imdb://tt123' as in Plex's Guid list
        import time
        find = pf.show_in_folder if series else pf.item_in_folder
        it = find(folder)
        if not it:
            sid, _ = pf.plex_form(folder)
            if not sid:
                raise RuntimeError("this folder is not inside a Plex library")
            api.get(f"/library/sections/{sid}/refresh")
            for _ in range(100):  # up to ~5 minutes
                time.sleep(3)
                it = find(folder)
                if it:
                    break
            if not it:
                raise RuntimeError("Plex has not picked up the folder yet - try again in a few minutes")
        if not key:
            return it, "no TMDB/IMDb id - match left as it is"
        before = api.get(f"/library/metadata/{it['id']}").get("Metadata", [{}])[0]
        if guid in [g["id"] for g in before.get("Guid", [])]:
            return it, f"already matched to {key}"
        msg = "matched to " + pf.match_tmdb(api, it["id"], key, tv=series)
        for _ in range(40):  # wait (max ~2 min) until Plex has refreshed the item after the match
            time.sleep(3)
            now = api.get(f"/library/metadata/{it['id']}").get("Metadata", [{}])[0]
            if guid in [g["id"] for g in now.get("Guid", [])] and now.get("updatedAt") != before.get("updatedAt"):
                break
        return find(folder) or it, msg

    def detail_values(self) -> dict:
        g = {k: v.get().strip() for k, v in self.v.items()}
        values = dict(title=g["title"], native=g["native"], year=g["year"], summary=self.plot.get("1.0", "end").strip())
        if g["prefix"]:  # sorts a series together in Plex: 'Bored Hatamoto 1958 Idle Vassal Tribute'
            values["sort"] = f"{g['prefix']} {g['year']} {g['title']}".strip()
        return {k: v for k, v in values.items() if v}

    # --- one button: accept everything shown and update Plex ---
    def accept_all(self):
        if not self.folder:
            return
        tid = self.match_key()  # 'tmdb-…' or 'imdb-tt…'
        old_dir, moves, new_dir = self.folder, [], self.folder
        steps = []
        if self.editable():
            try:
                moves, new_dir = self.plan_rename()
            except ValueError as e:
                messagebox.showerror("Accept & update Plex", str(e))
                return
            if moves or new_dir != old_dir:
                steps.append(f"Rename the folder (and {len(moves)} file(s)) to:\n     {new_dir.name}")
        elif not self.item:
            messagebox.showwarning("Accept & update Plex", "This folder is not in a Plex library.")
            return
        steps.append(f"Match in Plex to {tid}" if tid else "No TMDB/IMDb id: keep Plex's match, write the details below")
        values = self.detail_values()
        steps.append("Write and lock: " + ", ".join(pf.FIELD_NAMES[k] for k in values) +
                     f"\n     ('{values.get('title', '')}' / {values.get('native', '')} / {values.get('year', '')})")
        poster = self.art.selected()
        if poster:
            steps.append(f"Set and lock poster number {self.art.sel} ({poster['provider']})")
        if not messagebox.askyesno("Accept & update Plex?", "\n\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)) +
                                   "\n\nThe rename is logged ('Undo a run…' can reverse it). Continue?"):
            return
        self.status.config(text="Updating Plex… (a rename makes Plex scan the library - this can take a minute or two)")
        series = self.series()

        def work():
            report = []
            api = pf.PlexAPI()
            if moves or new_dir != old_dir:
                report.append("renamed (log " + self.do_rename(moves, old_dir, new_dir) + ")")
            item, msg = self.ensure_matched(api, new_dir, tid, series)
            report.append(msg)
            pf.write_fields(api, item["section"], item["id"], values, tv=series)
            report.append("details written + locked")
            if poster:
                pf.set_poster(api, item["section"], item["id"], url="" if poster.get("data") else poster["url"],
                              data=poster.get("data", b""), tv=series)
                report.append("poster set + locked")
            return new_dir, item, report

        def done(out):
            d, self.item, report = out
            self.path.set(str(d))
            self.folder = d
            self.show_plex_info()
            self.status.config(text="Plex updated: " + "; ".join(report) + ". Check the film in Plex.")
            self.art.refresh_current()
        self.bg(work, done)

    # --- single steps ---
    def rename_now(self):
        if not self.folder or not self.mapping:
            return
        try:
            moves, new_dir = self.plan_rename()
        except ValueError as e:
            messagebox.showwarning("Rename", str(e))
            return
        if not moves and new_dir == self.folder:
            messagebox.showinfo("Rename", "The names are already correct - nothing to rename.")
            return
        if not messagebox.askyesno("Rename now?", f"In {self.folder.parent}:\n\n{self.folder.name}\n  ->  {new_dir.name}\n\n"
                                                  f"{len(moves)} file(s) renamed as listed below. A log is written; "
                                                  "'Undo a run…' in the renamer window can reverse it.\n\nContinue?"):
            return
        old_dir = self.folder

        def done(log):
            self.path.set(str(new_dir))
            self.status.config(text=f"Renamed. Log: {log}. Next: '2 Match in Plex' so Plex picks up the new name.")
            self.analyse()
        self.bg(lambda: self.do_rename(moves, old_dir, new_dir), done)

    def match_plex(self):
        tid = self.match_key()
        if not self.folder:
            return
        if not tid:
            messagebox.showinfo("Match in Plex", "Choose the film first: the Id tag must be {tmdb-…} or {imdb-tt…}.\n"
                                                 "For a film neither has, skip this and use "
                                                 "'Save title + description to Plex'.")
            return
        folder = self.folder
        self.status.config(text="Asking Plex to scan this folder and match it - this can take a minute…")

        def done(out):
            self.item, msg = out
            self.show_plex_info()
            self.load_art()
            self.status.config(text=f"Plex: {msg}. Check it in Plex; then check the description and artwork below.")
        series = self.series()
        self.bg(lambda: self.ensure_matched(pf.PlexAPI(), folder, tid, series), done)

    def need_item(self) -> bool:
        if not self.item:
            messagebox.showinfo("Plex", "This film is not in Plex yet - click '2 Match in Plex' first.")
        return bool(self.item)

    def write_plex(self):
        if not self.need_item():
            return
        values = self.detail_values()
        shown = "\n".join(f"{pf.FIELD_NAMES[k]}: {v[:120]}{'…' if len(v) > 120 else ''}" for k, v in values.items())
        if not messagebox.askyesno("Write details to Plex?", f"{self.item['title']} (item {self.item['id']})\n\n{shown}\n\n"
                                                             "These fields are written and LOCKED (a Plex refresh "
                                                             "won't overwrite them). Continue?"):
            return
        item = self.item
        self.bg(lambda: pf.write_fields(pf.PlexAPI(), item["section"], item["id"], values, tv=bool(item.get("tv"))),
                lambda _: self.status.config(text=f"Written to Plex and locked: {', '.join(pf.FIELD_NAMES[k] for k in values)}."))

    def tmdb_plot(self):
        tid = self.tmdb_id()
        if not tid:
            return

        kind = "tv" if self.series() else "movie"

        def work():  # English first, else the original-language plot (to translate)
            en = self.tmdb._get(f"{kind}/{tid}", language=self.tmdb.lang).get("overview") or ""
            return en or self.tmdb._get(f"{kind}/{tid}", language=self.lang_code()).get("overview") or ""

        def done(text):
            self.plot.delete("1.0", "end")
            self.plot.insert("1.0", text or "")
            self.status.config(text="Plot loaded from TMDB." if text else "TMDB has no plot for this film.")
        self.bg(work, done)

    def translate_plot(self):
        text = self.plot.get("1.0", "end").strip()
        if not text:
            return
        self.status.config(text="Translating with DeepL…")

        def done(t):
            self.plot.delete("1.0", "end")
            self.plot.insert("1.0", t)
            self.status.config(text="Translated to English (DeepL). Check it, then 'Save title + description to Plex'.")
        self.bg(lambda: pf.translate(text), done)

    def load_art(self):
        if self.item:
            self.art.load(self.item, dict(title=self.v["title"].get(), year=self.v["year"].get(),
                                          orig=self.v["native"].get()),
                          self.tmdb_id(), self.chosen.get("imdb", ""), self.lang_code())
        else:
            self.art.clear("Not in Plex yet - use '2 Match in Plex' first, then the artwork appears here.")

    # ------------------------------------------------------------ posters
    def show_posters(self):
        if not self.chosen:
            messagebox.showinfo("Posters", "Choose a TMDB film first.")
            return
        win = tk.Toplevel(self)
        win.title(f"Posters - {self.chosen['title']} ({self.chosen['year']})")
        win.geometry("900x620")
        canvas = tk.Canvas(win)
        sb = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        if Image is None:
            ttk.Label(inner, text="(Install Pillow to see thumbnails here: python -m pip install pillow. "
                                  "Until then use Preview to see a poster in the browser.)").grid(
                row=0, column=0, columnspan=5, sticky="w", pady=4)

        def done(posters):
            if not posters:
                ttk.Label(inner, text="TMDB has no posters for this film.").grid(row=1, column=0)
            for i, p in enumerate(posters[:40]):
                cell = ttk.Frame(inner, padding=4)
                cell.grid(row=1 + i // 5, column=i % 5, sticky="n")
                if Image is not None:
                    try:
                        img = Image.open(io.BytesIO(self.tmdb.image_bytes(p["path"], "w154")))
                        tkimg = ImageTk.PhotoImage(img)
                        self.thumbs.append(tkimg)
                        ttk.Label(cell, image=tkimg).pack()
                    except Exception:
                        pass
                ttk.Label(cell, text=f"{p['lang'] or 'no text'}  {p['w']}x{p['h']}  ★{p['score']:.1f}").pack()
                ttk.Button(cell, text="Preview", command=lambda q=p: webbrowser.open(
                    f"https://image.tmdb.org/t/p/original{q['path']}")).pack(fill="x")
                ttk.Button(cell, text="Save as poster.jpg", command=lambda q=p: self.save_poster(q)).pack(fill="x")
        self.bg(lambda: self.tmdb.posters(self.chosen["id"], self.lang_code()), done)

    def save_poster(self, p):
        target = self.new_root() / self.new_folder.get() / "poster.jpg"
        if not messagebox.askyesno("Save poster", f"Save this poster to\n{target}\n\n(the new renamed folder; "
                                                  f"the original folder is not touched)?"):
            alt = filedialog.asksaveasfilename(title="Save poster as", initialfile="poster.jpg",
                                               defaultextension=".jpg", filetypes=[("JPEG", "*.jpg")])
            if not alt:
                return
            target = Path(alt)
        try:
            data = self.tmdb.image_bytes(p["path"], "original")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            self.status.config(text=f"Saved {len(data) // 1024} KB -> {target}")
        except Exception as e:
            messagebox.showerror("Save poster", str(e))

    # ------------------------------------------------------------ write into a rename plan
    def use_in_plan(self):
        import csv
        if not self.mapping:
            return
        series = self.series()
        plan = ""
        if series:  # the series plan of this library, if there is one
            lib = self.folder.parent
            p = pn.latest_plan(lib, "tvrename")
            if p and messagebox.askyesno("Series plan", f"Write this show into\n{p.name}?"):
                plan = str(p)
        if not plan:
            plan = filedialog.askopenfilename(title="Rename plan to update", initialdir=str(HERE / "plans"),
                                              filetypes=[("Plans", "*.xlsx *.csv")])
        if not plan:
            return
        while pn.is_locked(Path(plan)):  # an open plan can't be written, and unsaved edits would be lost
            if not messagebox.askretrycancel("Close the plan in Excel",
                                             f"{Path(plan).name} is open in Excel.\n\n"
                                             "Save it (Ctrl+S) and close Excel, then click Retry."):
                return
        fields, rows = pn.read_plan(plan)
        new = dict(self.mapping)
        n = 0
        for r in rows:
            if Path(r.get("Root", "")) / r.get("Folder", "") != self.folder:
                continue
            if series and r.get("Kind") in ("VIDEO", "SUB", "REVIEW", "EXTRA"):
                nf = new.get(r.get("File", ""), "")
                if not nf or nf.startswith("("):
                    continue
                sub, _, fname = nf.rpartition("\\")  # 'Season 01\\Show (2019) - s01e03 [720p].mkv'
                m = re.search(r"(?i)s(\d{2})e(\d{2,3}(?:-e\d{2,3})?)", fname)
                r.update(NewFolder=f"{self.new_folder.get()}\\{sub}" if sub else self.new_folder.get(), NewFile=fname,
                         TMDB=self.chosen.get("id", ""), IMDb=self.chosen.get("imdb", ""), Title=self.v["title"].get(),
                         Year=self.v["year"].get(), OriginalTitle=self.chosen.get("orig", ""), Source="manual",
                         Season=str(int(m.group(1))) if m else r.get("Season", ""),
                         Episode=m.group(2) if m else r.get("Episode", ""), Apply="Y",
                         Note="set with 'Fix one film' (Series)")
                n += 1
                continue
            if r.get("Kind") not in ("VIDEO", "SUB"):
                continue
            nf = new.get(r["File"], "")
            if not nf or nf.startswith("("):
                continue
            r.update(NewFolder=self.new_folder.get(), NewFile=nf, TMDB=self.chosen.get("id", ""),
                     IMDb=self.chosen.get("imdb", ""), Title=self.v["title"].get(), Year=self.v["year"].get(),
                     OriginalTitle=self.chosen.get("orig", ""), Source="manual", Confidence="manual", Apply="Y")
            n += 1
        if not n:
            messagebox.showinfo("Rename plan", "This folder is not in that plan (make the plan for its library first).")
            return
        try:
            if plan.lower().endswith(".xlsx"):
                pn.write_plan(Path(plan), rows, fields, new_if_locked=False)
            else:
                with open(plan, "w", encoding="utf-8-sig", newline="") as fh:
                    w = csv.DictWriter(fh, fieldnames=fields)
                    w.writeheader()
                    w.writerows(rows)
        except PermissionError:
            messagebox.showerror("Rename plan", "The plan is open in Excel - close it and try again.")
            return
        self.status.config(text=f"Updated {n} rows in {Path(plan).name} (Source = manual, Apply = Y).")


class SitesEditor(tk.Toplevel):
    """Edit the film sites shown for one language - no code needed (saved in search_sites.json)."""

    def __init__(self, app: "Lookup", code: str):
        super().__init__(app)
        self.app, self.code = app, code
        name = next((k for k, v in LANG_NAMES.items() if v == code), code)
        self.title(f"Film sites - {name}")
        self.geometry("760x440")
        self.configure(bg=ui.BG)
        ttk.Label(self, text=f"Sites shown in the 'Not on TMDB? Search:' row when the language is {name}.",
                  font=("Segoe UI", 10, "bold"), foreground=ui.HEAD).pack(anchor="w", padx=10, pady=(10, 2))
        ttk.Label(self, style="Muted.TLabel", wraplength=730, justify="left", text=(
            "Address: either just the site (e.g. russianfilmhub.com) - it is then searched through Google - or the "
            "site's own search address with {q} where the film title goes (e.g. https://www.imdb.com/find/?q={q}). "
            "Tip: search for any film on the site, copy the address from the browser and replace the film title "
            "with {q}.")).pack(anchor="w", padx=10)
        self.tree = ttk.Treeview(self, columns=("name", "addr"), show="headings", height=8)
        self.tree.heading("name", text="Button name")
        self.tree.heading("addr", text="Site or search address")
        self.tree.column("name", width=180)
        self.tree.column("addr", width=540)
        self.tree.pack(fill="both", expand=True, padx=10, pady=8)
        for e in load_sites().get(code, []):
            self.tree.insert("", "end", values=(e.get("name", ""), e.get("search") or e.get("site", "")))
        add = ttk.Frame(self)
        add.pack(fill="x", padx=10)
        self.nv, self.av = tk.StringVar(), tk.StringVar()
        ttk.Label(add, text="Name").pack(side="left")
        ttk.Entry(add, textvariable=self.nv, width=20).pack(side="left", padx=4)
        ttk.Label(add, text="Address").pack(side="left")
        ttk.Entry(add, textvariable=self.av, width=44).pack(side="left", padx=4)
        ttk.Button(add, text="Add", command=self.add).pack(side="left")
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=10)
        ttk.Button(bar, text="Remove selected", command=self.remove).pack(side="left")
        ttk.Button(bar, text="Test selected", command=self.test).pack(side="left", padx=6)
        ttk.Button(bar, text="Save", style="Go.TButton", command=self.save).pack(side="right")
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right", padx=6)

    @staticmethod
    def entry(name: str, addr: str) -> dict:
        addr = addr.strip()
        if "{q}" in addr:
            return {"name": name.strip(), "search": addr}
        site = re.sub(r"^https?://", "", addr).strip("/").split("/")[0]  # 'https://x.com/page/' -> 'x.com'
        return {"name": name.strip() or site, "site": site}

    def add(self):
        if self.av.get().strip():
            e = self.entry(self.nv.get(), self.av.get())
            self.tree.insert("", "end", values=(e["name"], e.get("search") or e.get("site")))
            self.nv.set("")
            self.av.set("")

    def remove(self):
        for s in self.tree.selection():
            self.tree.delete(s)

    def test(self):
        s = self.tree.selection()
        if s:
            n, a = self.tree.item(s[0])["values"]
            webbrowser.open(site_url(self.entry(str(n), str(a)), self.app.q.get() or "Seven Samurai 1954"))

    def save(self):
        allsites = load_sites()
        allsites[self.code] = [self.entry(str(n), str(a)) for n, a in (self.tree.item(i)["values"]
                                                                        for i in self.tree.get_children())]
        try:
            SITES_FILE.write_text(json.dumps(allsites, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as e:
            messagebox.showerror("Film sites", f"Could not save: {e}", parent=self)
            return
        self.app.build_sites()
        self.destroy()


class ArtworkStrip(ttk.Frame):
    """Poster choice inside the main window: what Plex shows now, then the candidates (Plex, TMDB, Wikipedia,
    or an image address / file the owner adds). The AI pick is highlighted and pre-selected; click another to
    choose it, double-click to preview, then 'Set selected poster in Plex' (set + lock via the Plex API).
    Thumbnails are PNGs from Plex's photo transcoder, which tkinter shows without Pillow."""
    W, H = 110, 165
    NORMAL, AI, SEL = ui.BG, "#2e7d32", "#e08a00"

    def __init__(self, parent, app: "Lookup"):
        super().__init__(parent)
        self.app, self.api, self.item, self.gen = app, None, None, 0
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Button(bar, text="Set selected poster in Plex", style="Go.TButton",
                   command=self.set_selected).pack(side="left")
        ttk.Button(bar, text="AI pick", command=self.ai_pick).pack(side="left", padx=4)
        ttk.Label(bar, text="   Add image address:").pack(side="left")
        self.url = tk.StringVar()
        ttk.Entry(bar, textvariable=self.url, width=36).pack(side="left", padx=2)
        ttk.Button(bar, text="Add", command=self.add_url).pack(side="left")
        ttk.Button(bar, text="From file…", command=self.add_file).pack(side="left", padx=4)
        ttk.Button(bar, text="Find more on the web", command=self.web_search).pack(side="left")
        self.msg = ttk.Label(self, text="Analyse a film that is in Plex to see its artwork.", wraplength=950,
                             justify="left", foreground="#555")
        self.msg.pack(fill="x", pady=2)
        self.canvas = tk.Canvas(self, height=self.H + 44, highlightthickness=0, bg=ui.BG)
        sb = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.row = ttk.Frame(self.canvas)
        self.row.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.row, anchor="nw")
        self.canvas.configure(xscrollcommand=sb.set)
        self.canvas.pack(fill="both", expand=True)
        sb.pack(fill="x")
        self.cands, self.cells, self.caps, self.imgs, self.sel, self.ai = [], [], [], [], None, None

    def clear(self, text=""):
        self.gen += 1  # results of an older load are ignored
        for w in self.row.winfo_children():
            w.destroy()
        self.canvas.xview_moveto(0)  # a strip left scrolled to the right would look empty for the next film
        self.cands, self.cells, self.caps, self.imgs, self.sel, self.ai = [], [], [], [], None, None
        self.msg.config(text=text)

    def load(self, item: dict, film: dict, tmdb_id: str, imdb: str, lang: str):
        self.clear("Loading artwork from Plex, TMDB and Wikipedia…")
        self.item, self.film, self.lang, g = item, film, lang, self.gen

        def work():
            if self.api is None:
                self.api = pf.PlexAPI()
            return g, pf.gather(dict(id=item["id"], tmdb=tmdb_id, imdb=imdb), self.api, self.app.tmdb,
                                threading.Lock(), lang, limit=24, all_langs=True, tv=bool(item.get("tv")))

        def done(out):
            if out[0] != self.gen:
                return
            self.add(dict(url="", key=f"/library/metadata/{item['id']}/thumb", provider="now in Plex", lang="",
                          current=True))
            for c in out[1]:
                self.add(c)
            self.msg.config(text=f"{len(out[1])} candidates. AI is picking by your rules (original release poster of "
                                 "its year, then cast info)…")
            self.ai_pick()
        self.app.bg(work, done)

    def add(self, c):
        i = len(self.cands)
        self.cands.append(c)
        cell = tk.Frame(self.row, highlightthickness=3, highlightbackground=self.NORMAL, padx=2, pady=2, bg=ui.BG)
        cell.grid(row=0, column=i, sticky="n", padx=2)
        pic = tk.Label(cell, text="loading…", width=14, height=10, bg=ui.PANEL)
        pic.pack()
        extra = f" {c['size']}" if c.get("size") else ""
        cap = ttk.Label(cell, text=(c["provider"] if c.get("current") else
                                    f"{i} · {c['provider']} {c.get('lang') if c.get('lang') not in ('none', 'wiki') else ''}"
                                    f"{extra}").strip(), wraplength=self.W + 10, foreground="#444")
        cap.pack()
        self.cells.append(cell)
        self.caps.append(cap)
        if not c.get("current"):
            for w in (cell, pic):
                w.bind("<Button-1>", lambda e, n=i: self.select(n))
                if c["url"].startswith("http"):
                    w.bind("<Double-Button-1>", lambda e, u=c["url"]: webbrowser.open(u))
        g = self.gen

        def work():
            return None if c.get("data") else self.api.thumb(c["key"], self.W, self.H, fmt="png")

        def done(png):
            if g != self.gen or not pic.winfo_exists():
                return
            if png:
                img = tk.PhotoImage(data=base64.b64encode(png).decode())
                self.imgs.append(img)
                pic.config(image=img, text="", width=self.W, height=self.H)
            else:
                pic.config(text="(your file)" if c.get("data") else "(none)")
        self.app.bg(work, done)

    def web_search(self):
        """Google Images in the browser (nothing is scraped): native title + year + 'poster' in Japanese finds the
        Japanese poster sites; the owner copies an image address into 'Add image address'."""
        a = self.app
        native, title, year = a.v["native"].get().strip(), a.v["title"].get().strip(), a.v["year"].get().strip()
        word = {"ja": "ポスター", "ko": "포스터", "zh": "海报", "ru": "постер"}.get(a.lang_code(), "poster")
        q = f"{native or title} {year} {word}".strip()
        webbrowser.open("https://www.google.com/search?tbm=isch&q=" + urllib.parse.quote(q))
        self.msg.config(text=f"Searching the web for: {q}  -  right-click a poster > 'Copy image address', "
                             "paste it into 'Add image address', click Add, then 'AI pick' again.")

    def selected(self):
        """The chosen poster (not 'now in Plex'), or None."""
        if self.sel is None or self.sel >= len(self.cands) or self.cands[self.sel].get("current"):
            return None
        return self.cands[self.sel]

    def refresh_current(self):
        """Reload the 'now in Plex' picture after Plex was changed."""
        if not self.item or not self.cells:
            return
        item, g = self.item, self.gen

        def done(png):
            if g == self.gen and png:
                img = tk.PhotoImage(data=base64.b64encode(png).decode())
                self.imgs.append(img)
                self.cells[0].winfo_children()[0].config(image=img, text="")
        self.app.bg(lambda: self.api.thumb(f"/library/metadata/{item['id']}/thumb", self.W, self.H, fmt="png"), done)

    def paint(self):
        for n, cell in enumerate(self.cells):
            cell.config(highlightbackground=self.SEL if n == self.sel else self.AI if n == self.ai else self.NORMAL)

    def select(self, n):
        self.sel = n
        self.paint()

    def add_url(self):
        u = self.url.get().strip()
        if u.startswith("http") and self.item:
            self.add(dict(url=u, key=u, provider="web", lang=""))
            self.url.set("")
            self.select(len(self.cands) - 1)

    def add_file(self):
        p = filedialog.askopenfilename(title="Poster image", filetypes=[("Images", "*.jpg *.jpeg *.png *.webp")])
        if p and self.item:
            self.add(dict(url=f"file:{Path(p).name}", key="", provider="your file", lang="", data=Path(p).read_bytes()))
            self.select(len(self.cands) - 1)

    def ai_pick(self):
        pool = [c for c in self.cands if not c.get("data") and not c.get("current")]
        wiki = [c for c in pool if c.get("lang") == "wiki" or c["provider"] in ("web",)]  # always shown to the AI
        web = [c for c in pool if c not in wiki][: 16 - len(wiki)] + wiki
        if not web or not self.item:
            self.msg.config(text="No poster candidates - add an image address or a file.")
            return
        film = dict(id=self.item["id"], title=self.film["title"], year=self.film["year"], orig=self.film["orig"],
                    candidates=web)
        g = self.gen

        def work():
            imgs = [self.api.thumb(c["key"]) for c in web]
            ai = pf.PosterAI()
            ans = ai.rank(film, imgs, self.lang)
            ai.save()
            return ans

        def done(ans):
            if g != self.gen:
                return
            best = ans.get("best")
            if isinstance(best, int) and 1 <= best <= len(web):
                self.ai = self.cands.index(web[best - 1])
                ranking = [n for n in ans.get("ranking", []) if isinstance(n, int) and 1 <= n <= len(web)] or [best]
                kinds = {i.get("n"): i.get("kind", "") for i in ans.get("images", []) if isinstance(i, dict)}
                for rank, n in enumerate(ranking, 1):  # every ranked poster shows its place and what the AI saw
                    k = self.cands.index(web[n - 1])
                    base = self.caps[k].cget("text").split("\n")[-1]
                    self.caps[k].config(text=f"{'★ AI pick' if rank == 1 else f'AI rank {rank}'}"
                                             f"{' · ' + kinds[n] if kinds.get(n) else ''}\n{base}")
                if self.sel is None:
                    self.sel = self.ai
                self.paint()
                self.msg.config(text=f"AI pick (green, pre-selected in orange): number {self.ai} - "
                                     f"{ans.get('reason', '')}  Click another poster to choose it; double-click to "
                                     "see it large.")
            else:
                self.msg.config(text=f"AI gave no clear pick: {ans.get('reason', '')}")
        self.app.bg(work, done)

    def set_selected(self):
        if not self.item or self.sel is None:
            messagebox.showinfo("Poster", "Click a poster first.")
            return
        c, n = self.cands[self.sel], self.sel
        if not messagebox.askyesno("Set poster in Plex?", f"Show poster number {n} for '{self.item['title']}' "
                                                          "in Plex and lock it (a refresh won't change it)?"):
            return
        item, g = self.item, self.gen

        def work():
            pf.set_poster(self.api, item["section"], item["id"], tv=bool(item.get("tv")),
                          url="" if c.get("data") else c["url"],
                          data=c.get("data", b""))
            return self.api.thumb(f"/library/metadata/{item['id']}/thumb", self.W, self.H, fmt="png")

        def done(png):
            self.app.status.config(text=f"Poster {n} is now shown in Plex and locked.")
            if g == self.gen and png:  # refresh the 'now in Plex' picture
                img = tk.PhotoImage(data=base64.b64encode(png).decode())
                self.imgs.append(img)
                self.cells[0].winfo_children()[0].config(image=img, text="")
        self.app.bg(work, done)


if __name__ == "__main__":
    Lookup().mainloop()
