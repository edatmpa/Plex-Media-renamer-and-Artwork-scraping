"""plexname - prepare media folders on the NAS for Plex, one top-level folder at a time.
See CLAUDE.md in this folder for the full workflow and rules.

Safe workflow (original folder never changed):
    a) split-plan  ROOT --lang JP --type movie        -> plans\\<name>_split_<date>.xlsx  (review Apply column)
       split-copy  PLAN [--go]                        -> "<ROOT> (split temp)"  one folder per film
    b) rename-plan ROOT --lang JP --type movie --split-plan PLAN   -> plans\\<name>_rename_<date>.xlsx
       rename-copy PLAN [--go]                        -> "<ROOT> (Plex)"  final names

Every step without --go is a dry run. Facts come from the live Plex database (read-only) and TMDB;
DeepSeek only chooses among TMDB results.
"""
import argparse
import csv
import ctypes
import datetime as dt
import os
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import config  # this PC's settings (config.json): Plex database, API keys, NAS names

PLEX_DB = config.PLEX_DB
HERE = Path(__file__).resolve().parent  # project folder; plans, logs and cache live here
PLAN_DIR = HERE / "plans"

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".ogm", ".wmv", ".mpg", ".mpeg", ".ts", ".m2ts",
             ".divx", ".mov", ".flv", ".webm", ".vob"}
SUB_EXT = {".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt", ".smi"}
VCD_MIN = 50_000_000  # a .DAT this big is VCD video (MPEG-1); Plex can't play .dat, so it is copied as .mpg


def is_video(p: Path) -> bool:
    ext = p.suffix.lower()
    return ext in VIDEO_EXT or (ext == ".dat" and p.stat().st_size > VCD_MIN)


def out_ext(p: Path) -> str:
    return ".mpg" if p.suffix.lower() == ".dat" else p.suffix.lower()
# Everything else (.nfo, .jpg, .png, .txt, Thumbs.db, desktop.ini, ...) is a leftover -> _to_delete.

PART_RE = re.compile(r"(?i)[\s._\-\[(]*(?:cd|dis[ck]|part|pt)[\s._\-]*([1-9])(?![0-9])[\])]?")
YEAR_RE = re.compile(r"(?<![0-9])(19[0-9]{2}|20[0-9]{2})(?![0-9])")
EXTRA_RE = re.compile(r"(?i)\b(sample|trailer|teaser)\b")
DISC_DIRS = {"VIDEO_TS", "AUDIO_TS", "BDMV", "CERTIFICATE"}
LANGS = {"JP": "ja", "KR": "ko", "CN": "zh", "RU": "ru", "EN": "en"}


# ---------------------------------------------------------------- helpers

NAS_HOSTS = config.NAS_HOSTS  # NAS name -> IP (config.json "nas_hosts")


def unc_path(p: Path) -> str:
    """V:\\x -> \\\\<NAS IP>\\<share>\\x (the form stored in the Plex database).
    Drives mapped by NAS name (\\\\MyNAS\\...) are converted to the IP form too (config.json "nas_hosts")."""
    s = str(p)
    if len(s) >= 2 and s[1] == ":":
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.c_ulong(1024)
        if ctypes.windll.mpr.WNetGetConnectionW(s[:2], buf, ctypes.byref(n)) == 0:
            s = buf.value.rstrip("\\") + s[2:]
    m = re.match(r"^\\\\([^\\]+)(\\.*)$", s)
    if m and m.group(1).casefold() in NAS_HOSTS:
        s = "\\\\" + NAS_HOSTS[m.group(1).casefold()] + m.group(2)
    return s


def res_label(w, h):
    if not w or not h:
        return ""
    eff = max(int(h), int(int(w) * 9 / 16))
    if eff >= 1900:
        return "2160p"
    if eff >= 950:
        return "1080p"
    if eff >= 650:
        return "720p"
    return "SD"


def safe_name(s: str) -> str:
    s = s.replace(":", " -").replace("/", "-").replace("\\", "-")
    s = re.sub(r'[<>"|?*]', "", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s


def clean_from_filename(stem: str):
    """Best-effort 'Title (Year)' from a messy file name, used only when Plex has no match."""
    s = PART_RE.sub(" ", stem)
    s = re.sub(r"[._]+", " ", s)
    m = YEAR_RE.search(s)
    year = m.group(1) if m else ""
    title = s[: m.start()] if m else s
    title = re.sub(r"[\[(]\s*$", "", title)
    title = re.sub(r"\s+", " ", title).strip(" -([")
    return title, year


def only_tag(a) -> str:
    """'_only-Throne_Of_Blood' for plans limited with --only, so they never overwrite a full-library plan."""
    if getattr(a, "only", None):
        return "_only-" + (re.sub(r"[^0-9A-Za-z]+", "_", a.only).strip("_")[:40] or "x")
    return ""


def part_key(stem: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", PART_RE.sub("", stem).casefold())


def norm(s: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", s.casefold())


# ---------------------------------------------------------------- TMDB (live titles; Plex's stored titles can be stale)

CACHE_DIR = HERE / "cache"


class TMDB:
    def __init__(self, lang="en-US"):
        import json
        import socket
        import requests
        import urllib3.util.connection as uc
        uc.allowed_gai_family = lambda: socket.AF_INET  # IPv6 on this network is broken
        self.json, self.requests, self.lang = json, requests, lang
        self.key = config.key("tmdb")
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.cache_file = CACHE_DIR / "tmdb_movie.json"
        self.cache = json.loads(self.cache_file.read_text(encoding="utf-8")) if self.cache_file.exists() else {}

    def movie(self, tmdb_id):
        if not tmdb_id:
            return None
        if tmdb_id not in self.cache:
            r = self.requests.get(f"https://api.themoviedb.org/3/movie/{tmdb_id}",
                                  params={"api_key": self.key, "language": self.lang}, timeout=20)
            if r.status_code == 404:
                self.cache[tmdb_id] = None
            else:
                r.raise_for_status()
                j = r.json()
                coll = (j.get("belongs_to_collection") or {}).get("name") or ""
                self.cache[tmdb_id] = dict(title=j.get("title") or "", orig=j.get("original_title") or "",
                                           year=(j.get("release_date") or "")[:4], lang=j.get("original_language"),
                                           collection=coll)
        return self.cache[tmdb_id]

    def _get(self, path, **params):
        r = self.requests.get(f"https://api.themoviedb.org/3/{path}",
                              params={"api_key": self.key, **params}, timeout=20)
        r.raise_for_status()
        return r.json()

    def find_imdb(self, imdb_id):
        """IMDb tt-id -> TMDB movie id (or '')."""
        k = f"imdb:{imdb_id}"
        if k not in self.cache:
            res = self._get(f"find/{imdb_id}", external_source="imdb_id").get("movie_results") or []
            self.cache[k] = str(res[0]["id"]) if res else ""
        return self.cache[k]

    def search(self, query, year="", lang_code=""):
        """TMDB movie search; results of the wanted original language first. Returns list of dicts."""
        k = f"search:{query}|{year}|{lang_code}"
        if k not in self.cache:
            params = dict(query=query, include_adult="true", language=self.lang)
            if year:
                params["year"] = year
            out = []
            for m in self._get("search/movie", **params).get("results", [])[:10]:
                out.append(dict(id=str(m["id"]), title=m.get("title") or "", orig=m.get("original_title") or "",
                                year=(m.get("release_date") or "")[:4], lang=m.get("original_language") or "",
                                overview=(m.get("overview") or "")[:200]))
            out.sort(key=lambda m: m["lang"] != lang_code)
            self.cache[k] = out
        return self.cache[k]

    def details(self, tmdb_id):
        """Full details for the manual helper: overview, IMDb id, poster path (not cached)."""
        j = self._get(f"movie/{tmdb_id}", language=self.lang, append_to_response="external_ids")
        return dict(id=str(j["id"]), title=j.get("title") or "", orig=j.get("original_title") or "",
                    year=(j.get("release_date") or "")[:4], lang=j.get("original_language") or "",
                    overview=j.get("overview") or "", poster=j.get("poster_path") or "",
                    imdb=(j.get("external_ids") or {}).get("imdb_id") or "",
                    collection=(j.get("belongs_to_collection") or {}).get("name") or "")

    def posters(self, tmdb_id, lang_code=""):
        """All posters TMDB has: original-language ones first, then English, then no-text, then others."""
        j = self._get(f"movie/{tmdb_id}/images")
        order = {lang_code: 0, "en": 1, None: 2}
        out = [dict(path=p["file_path"], lang=p.get("iso_639_1"), w=p.get("width"), h=p.get("height"),
                    votes=p.get("vote_count", 0), score=p.get("vote_average", 0)) for p in j.get("posters", [])]
        out.sort(key=lambda p: (order.get(p["lang"], 3), -p["score"], -p["votes"]))
        return out

    def image_bytes(self, path, size="w185"):
        r = self.requests.get(f"https://image.tmdb.org/t/p/{size}{path}", timeout=30)
        r.raise_for_status()
        return r.content

    def save(self):
        self.cache_file.write_text(self.json.dumps(self.cache, ensure_ascii=False, indent=0), encoding="utf-8")


SERIES_WORDS = re.compile(r"(?i)\b(series|collection|trilogy|quadrilogy|complete|saga|box ?set|films|movies)\b")


def series_from_folder(name: str) -> str:
    """'Bored Hatamoto series' -> 'Bored Hatamoto'; 'Zatoichi 1962-1989 Complete Series' -> 'Zatoichi'."""
    s = re.sub(r"[\[(][^\])]*[\])]", " ", name)
    s = re.sub(r"(?i)\b(19|20)\d{2}(\s*[-–]\s*(19|20)?\d{2})?\b|\b\d{3,4}p\b", " ", s)
    s = SERIES_WORDS.sub(" ", s)
    s = re.sub(r"[._]+", " ", s)
    s = re.split(r"\s+[-–]\s+", s.strip())[0]  # 'Nemuri Kyoshiro - Ichikawa Raizo' -> 'Nemuri Kyoshiro'
    return re.sub(r"\s+", " ", s).strip(" -–,&")


SMALL_WORDS = {"a", "an", "the", "of", "and"}


def has_series(title: str, series: str) -> bool:
    """True when the title already STARTS with the series name, ignoring small words and punctuation
    ('Case of a Young Lord 8' starts with 'Case of Young Lord'; 'Lone Wolf and Cub' with 'Lone Wolf & Cub';
    'The Tale of Zatoichi' does not start with 'Zatoichi', so it gets the prefix and sorts with the series)."""
    import unicodedata
    fold = lambda s: "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    words = lambda s: [w for w in re.findall(r"[0-9a-z]+", fold(s).casefold()) if w not in SMALL_WORDS]
    sw, tw = words(series), words(title)
    return bool(sw) and tw[:len(sw)] == sw


# ---------------------------------------------------------------- Plex database

class PlexDB:
    def __init__(self, path=PLEX_DB):
        self.con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    def parts_under(self, root) -> dict:
        """lower-case file path -> dict of Plex facts for every file under root. Plex stores a library path the way
        it was added: NAS address (\\\\<NAS IP>\\...) or drive letter (V:\\...), so both forms are searched."""
        forms = {unc_path(Path(root)), str(root)}
        out = {}
        for f in forms:
            out.update(self._parts_under(f))
        return out

    def _parts_under(self, unc_root: str) -> dict:
        q = """
        SELECT mp.file, mp.size, m.id, m.title, m.original_title, m.year, mi.width, mi.height, mi.id,
               (SELECT group_concat(t.tag, ' ') FROM taggings tg JOIN tags t ON t.id = tg.tag_id
                 WHERE tg.metadata_item_id = m.id AND (t.tag LIKE 'tmdb://%' OR t.tag LIKE 'imdb://tt%'))
        FROM media_parts mp
        JOIN media_items mi ON mi.id = mp.media_item_id
        JOIN metadata_items m ON m.id = mi.metadata_item_id
        WHERE mp.file LIKE ? AND m.metadata_type = 1"""
        out = {}
        for f, size, mid, title, orig, year, w, h, miid, ids in self.con.execute(q, (unc_root.rstrip("\\") + "\\%",)):
            ids = ids or ""
            tm = re.search(r"tmdb://(\d+)", ids)
            im = re.search(r"imdb://(tt\d+)", ids)
            fact = dict(mid=mid, miid=miid, title=title or "", orig=orig or "", year=str(year or ""),
                        tmdb=tm.group(1) if tm else "", imdb=im.group(1) if im else "", res=res_label(w, h))
            out[f.casefold()] = fact
            # files moved since Plex's last scan (e.g. by split) are still known by name + size
            out[("name", Path(f.replace("\\", "/")).name.casefold(), size)] = fact
        return out


def lookup(facts: dict, p: Path):
    """Plex facts for a file: by its path, else by file name + size (moved since Plex's last scan)."""
    f = facts.get(unc_path(p).casefold()) or facts.get(str(p).casefold())  # NAS-address or drive-letter form
    if f is None:
        try:
            f = facts.get(("name", p.name.casefold(), p.stat().st_size))
        except OSError:
            f = None
    return f


# ---------------------------------------------------------------- plan files (.xlsx for review, .csv still read)

# Check column: why a blank-Apply row needs the owner's eye. CHECK (red) = the match itself is doubtful,
# review (yellow) = probably right but not confirmed. Colours are conditional, so a row turns white once Apply = Y.
CHECK_WORDS = ("differs", "not identified", "not found", "not matched", "duplicate", "different movies",
               "error", "no video", "dvd/blu-ray", "not '", "possible duplicate", "fill newfile", "not split")
RED, YELLOW, GREY = "FFC7CE", "FFEB9C", "EDEDED"
WIDTHS = {"Apply": 7, "Check": 9, "Note": 50, "What to do": 60, "Kind": 12, "Action": 12, "Folder": 45, "File": 55,
          "NewFolder": 60, "NewFile": 60, "TargetFolder": 45, "Title": 30, "TMDBTitle": 30, "OriginalTitle": 22,
          "PlexTitle": 30, "Dropped": 30, "Root": 30}
FRONT = ["Apply", "Check", "Note", "What to do"]  # the review columns come first, next to Apply

# (words in Note, advice) - first match wins; plain language for the owner
ADVICE = [
    ("not a film folder", "Nothing to do - this folder is not copied."),
    ("no video file", "The video is missing. Put the video back into this folder in the original and make the "
                      "rename plan again - or leave blank (the files are copied with their old names)."),
    ("not split", "Films left in a multi-movie folder. In the SPLIT plan set their rows to Y (or fix TargetFolder), "
                  "run 'Copy to temp: DO IT' again (copies only the new ones), then make the rename plan again. "
                  "Or leave blank: copied with old names."),
    ("dvd/blu-ray", "Leave blank - copied as it is. Plex plays DVD folders poorly; consider converting to .mkv later."),
    ("subtitle not matched", "Copy NewFile of the film's video row, change .mkv/.avi to .en.srt (or .ja.srt), "
                             "put it in NewFile, then Y."),
    ("subtitle language unknown", "Optional: open the .srt, add .en or .ja before .srt in NewFile. Then Y."),
    ("subtitle - goes with", "Type Y here together with the film's video row."),
    ("subtitle, goes with", "Type Y here together with the film's video row."),
    ("sample/trailer", "Leave blank to keep it (old name), or ignore it."),
    ("differs", "Probably the wrong film. Check with the manual helper (Open TMDB page). Right: Y. "
                "Wrong: pick the right film there and click 'Use in rename plan...'."),
    ("original language", "May be a foreign remake with the same title. Check with the manual helper."),
    ("not identified", "Find the film with the manual helper (TMDB or the specialist sites), then 'Use in rename plan...'."),
    ("not found", "Find the film with the manual helper (TMDB or the specialist sites), then 'Use in rename plan...'."),
    ("not matched in plex", "Name came from the file name. Check it, or use the manual helper."),
    ("no id found", "Find the film with the manual helper (TMDB or the specialist sites), then 'Use in rename plan...'."),
    ("duplicate", "Two files seem to be the same film. Keep both as versions (Y) or leave one blank."),
    ("already exists", "A folder with this name already exists - possible duplicate. Compare them first."),
    ("error", "Something went wrong reading this folder - copy the Note and tell Claude."),
    ("no tmdb", "Name comes from Plex without a TMDB id. Check it, or find it with the manual helper."),
]


def advice(r: dict) -> str:
    if r.get("Apply", "").strip():
        return ""
    note = r.get("Note", "").casefold()
    for words, text in ADVICE:
        if words in note:
            return text
    if r.get("Source") in ("tmdb-search", "deepseek") or "match" in note:
        return "Compare NewFolder (title, year, native title) with the old name in Folder/File. " \
               "Same film: type Y (also on its subtitle rows)."
    return "Check this row; if the new name is right, type Y."


def review_note(r: dict) -> str:
    """Reason text for a blank-Apply row whose Note is empty (older plans left these unexplained)."""
    kind = r.get("Kind") or r.get("Action", "")
    if kind == "SUB" or r.get("Note", "").startswith("subtitle, goes"):
        return "subtitle - goes with its video above; set Y together with that video"
    src = r.get("Source", "")
    if src == "tmdb-search":
        return "match found by TMDB search (not by Plex) - check the new name, then Y"
    if src == "deepseek":
        return "match chosen by DeepSeek among TMDB results - check the new name, then Y"
    if not r.get("TMDB") and not r.get("IMDb") and kind == "VIDEO":
        return "no id found - check the name, or use the manual helper"
    return "check this row, then Y"


def check_level(r: dict) -> str:
    if r.get("Apply", "").strip():
        return ""
    if (r.get("Kind") or r.get("Action")) == "REVIEW":
        return "CHECK"
    note = r.get("Note", "").casefold()
    return "CHECK" if any(w in note for w in CHECK_WORDS) else "review"


def with_check(rows: list, fields: list) -> list:
    """Fill empty review notes, the Check and What to do columns; returns the field list with FRONT first."""
    for r in rows:
        if not r.get("Apply", "").strip() and not r.get("Note", "").strip():
            r["Note"] = review_note(r)
        r["Check"] = check_level(r)
        r["What to do"] = advice(r)
    return FRONT + [f for f in fields if f not in FRONT]


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))  # Excel may turn a typed id into a number
    return str(v)


def read_plan(path) -> tuple:
    """(fields, rows) from a plan - .xlsx (sheet 'Plan') or the older CSV UTF-8."""
    p = Path(path)
    if p.suffix.lower() == ".xlsx":
        from openpyxl import load_workbook
        wb = load_workbook(p, read_only=True, data_only=True)
        ws = wb["Plan"] if "Plan" in wb.sheetnames else wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        head = [_cell(h).strip() for h in next(it)]
        rows = []
        for vals in it:
            vals = [_cell(v) for v in vals]
            if not any(vals):
                continue
            vals += [""] * (len(head) - len(vals))
            rows.append({h: v for h, v in zip(head, vals) if h})
        wb.close()
        return [h for h in head if h], rows
    with open(p, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), list(reader)


def write_plan(out: Path, rows: list, fields: list, new_if_locked=True) -> Path:
    """Write a review plan as .xlsx: Check column, colours for rows needing review, frozen header, filters.
    If the file is open in Excel, a new file with the time appended is written instead (or PermissionError
    is raised with new_if_locked=False). Returns the path used."""
    from openpyxl import Workbook
    from openpyxl.formatting.rule import FormulaRule
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    out = out.with_suffix(".xlsx")
    fields = with_check(rows, fields)
    wb = Workbook()
    ws = wb.active
    ws.title = "Plan"
    ws.append(fields)
    for r in rows:
        ws.append([str(r.get(k, "") or "") for k in fields])
    for c in ws[1]:
        c.font = Font(bold=True)
    n = len(rows) + 1
    last = get_column_letter(len(fields))
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:{last}{n}"
    for i, f in enumerate(fields, 1):
        ws.column_dimensions[get_column_letter(i)].width = WIDTHS.get(f, 12)
    # owner: columns from OriginalTitle on are not needed for the review - hidden as one group ('+' shows them)
    if "OriginalTitle" in fields:
        first = fields.index("OriginalTitle") + 1
        ws.column_dimensions.group(get_column_letter(first), last, hidden=True, outline_level=1)
    rng = f"A2:{last}{max(n, 2)}"
    fill = lambda rgb: PatternFill(start_color=rgb, end_color=rgb, fill_type="solid")
    ws.conditional_formatting.add(rng, FormulaRule(formula=['AND($A2="",$B2="CHECK")'], fill=fill(RED)))
    ws.conditional_formatting.add(rng, FormulaRule(formula=['AND($A2="",$B2="review")'], fill=fill(YELLOW)))
    ws.conditional_formatting.add(rng, FormulaRule(formula=['$A2="n/a"'], fill=fill(GREY)))
    lg = wb.create_sheet("How to review")
    for line in (["Colour / Check", "Meaning"],
                 ["red / CHECK", "The match or the file itself is doubtful (year differs, not identified, "
                                 "duplicate, DVD folder, unmatched subtitle...). Read Note."],
                 ["yellow / review", "Probably right but not confirmed (TMDB search, DeepSeek, subtitle of such a "
                                     "film). Compare the new name with the old one, then type Y."],
                 ["grey / n/a", "Information only - nothing to do."],
                 ["white / Y", "Will be done. A coloured row turns white as soon as you type Y in Apply."],
                 ["Note / What to do", "Note = what the tool found. What to do = your choices for that row."],
                 ["Hidden columns", "Columns from OriginalTitle on (years, ids, source, ...) are hidden. "
                                    "Click the + above the column letters to show them."],
                 ["", ""],
                 ["Tip", "Use the filter button on 'Check' to show only CHECK or review rows. "
                         "Save with Ctrl+S (keep the .xlsx format) and close Excel before the next step."]):
        lg.append(line)
    lg["A1"].font = lg["B1"].font = Font(bold=True)
    lg.column_dimensions["A"].width, lg.column_dimensions["B"].width = 18, 110
    for cell, rgb in (("A2", RED), ("A3", YELLOW), ("A4", GREY)):
        lg[cell].fill = fill(rgb)
    if new_if_locked and out.exists():  # never overwrite a plan the owner may already be reviewing
        out = out.with_name(f"{out.stem}_{dt.datetime.now():%H%M}.xlsx")
        print(f"(a plan with this name already exists - writing a new file instead)")
    try:
        wb.save(out)
    except PermissionError:  # usually: the previous plan is open in Excel
        if not new_if_locked:
            raise
        out = out.with_name(f"{out.stem}_{dt.datetime.now():%H%M}.xlsx")
        print(f"(previous plan is open/locked - writing a new file instead)")
        wb.save(out)
    return out


# ---------------------------------------------------------------- stage 1: split plan

FIELDS = ["Apply", "Action", "Folder", "File", "Unit", "TargetFolder", "Series", "TMDBTitle", "OriginalTitle",
          "TMDBYear", "FileYear", "TMDB", "IMDb", "Res", "Collection", "PlexTitle", "PlexYear", "Note", "Root"]


def plan_folder(root: Path, folder: Path, facts: dict, existing: set, tmdb: "TMDB"):
    files = [p for p in folder.rglob("*") if p.is_file()]
    disc = sorted({str(p.relative_to(folder).parent) for p in files
                   if {s.upper() for s in p.relative_to(folder).parts[:-1]} & DISC_DIRS})
    if disc:  # DVD / Blu-ray structure is one movie; never split it
        return [dict(Apply="n/a", Action="KEEP-DISC", Folder=folder.name, File="; ".join(disc),
                     TargetFolder=folder.name,
                     Note="DVD/Blu-ray folder structure - not split (Plex plays these poorly; consider remuxing to .mkv)")]
    videos = [p for p in files if p.suffix.lower() in VIDEO_EXT]
    main = [v for v in videos if not EXTRA_RE.search(v.stem)]
    if len(main) <= 1:
        return []

    # group videos into movies: same Plex item = same movie (parts or versions)
    units = defaultdict(list)
    info = {}
    for v in main:
        f = lookup(facts, v)
        info[v] = f
        key = ("plex", f["mid"]) if f else ("name", part_key(v.stem))
        units[key].append(v)

    rel = lambda p: str(p.relative_to(folder))
    rows = []

    if len(units) == 1:  # one movie in parts / versions -> stays together
        for v in sorted(main):
            f = info[v] or {}
            rows.append(dict(Apply="n/a", Action="KEEP-PARTS", Folder=folder.name, File=rel(v), Unit="1",
                             TargetFolder=folder.name, PlexTitle=f.get("title", ""),
                             OriginalTitle=f.get("orig", ""), PlexYear=f.get("year", ""),
                             FileYear=(YEAR_RE.search(v.stem).group(1) if YEAR_RE.search(v.stem) else ""),
                             TMDB=f.get("tmdb", ""), IMDb=f.get("imdb", ""), Res=f.get("res", ""),
                             Note=f"{len(main)} files = one movie (parts or versions); not split"))
        return rows

    # series prefix: the folder being split, when it is clearly a series or the file names carry its name
    series = series_from_folder(folder.name)
    series_folder = bool(SERIES_WORDS.search(folder.name))

    # one target folder per unit
    unit_ids, unit_target, unit_note, unit_ok, unit_meta = {}, {}, {}, {}, {}
    used = defaultdict(int)
    for n, (key, vids) in enumerate(sorted(units.items(), key=lambda kv: str(sorted(kv[1])[0])), 1):
        vids.sort()
        f = info[vids[0]]
        notes, ok = [], True
        fyear_m = YEAR_RE.search(vids[0].stem)
        fyear = fyear_m.group(1) if fyear_m else ""
        t = tmdb.movie(f["tmdb"]) if f and f["tmdb"] else None
        if t:
            title, year, orig, coll = t["title"], t["year"], t["orig"], t["collection"]
            if norm(t["title"]) != norm(f["title"]):
                notes.append(f"Plex's stored title is out of date ('{f['title']}')")
        elif f:
            title, year, orig, coll = f["title"], f["year"], f["orig"], ""
            if not f["tmdb"]:
                notes.append("no TMDB id in Plex" + (f" (IMDb {f['imdb']})" if f["imdb"] else ""))
        else:
            (title, year), orig, coll = clean_from_filename(vids[0].stem), "", ""
            notes.append("not matched in Plex - name taken from file name")
            ok = False
        if f and fyear and year and fyear != year:
            notes.append(f"matched year {year} differs from file name year {fyear} - check the match")
            ok = False
        name = safe_name(title) or safe_name(vids[0].stem)
        prefix = ""
        if series and (series_folder or norm(series) in norm(vids[0].stem)):
            if not has_series(name, series):
                prefix = series
                name = f"{series} - {name}"
        target = safe_name(f"{name} ({year})" if year else name)
        unit_meta[n] = dict(Series=prefix or (series if series and has_series(name, series) else ""),
                            TMDBTitle=t["title"] if t else "", OriginalTitle=orig,
                            TMDBYear=t["year"] if t else "", Collection=coll)
        used[target.casefold()] += 1
        if used[target.casefold()] > 1:
            notes.append("same target as another movie in this folder - duplicate or wrong match?")
            ok = False
            target = f"{target} [{used[target.casefold()]}]"
        if target.casefold() == folder.name.casefold():
            notes.append("already in a folder with this name - stays where it is")
        elif target.casefold() in existing:
            notes.append("a folder with this name already exists in the top-level folder - possible duplicate")
            ok = False
        if len(vids) > 1:
            notes.append(f"{len(vids)} files = one movie (parts or versions), moved together")
        for v in vids:
            unit_ids[v] = n
        unit_target[n], unit_note[n], unit_ok[n] = target, notes, ok

    stays = {n for n, t in unit_target.items() if t.casefold() == folder.name.casefold()}
    act = lambda n: "STAY" if n in stays else "MOVE"
    apply = lambda n: "n/a" if n in stays else ("Y" if unit_ok[n] else "")

    # attach subtitles to the video whose name they start with
    stems = sorted(((norm(v.stem), v) for v in main), key=lambda t: -len(t[0]))
    for v in sorted(main):
        n = unit_ids[v]
        f = info[v] or {}
        rows.append(dict(Apply=apply(n), Action=act(n), Folder=folder.name, File=rel(v),
                         Unit=str(n), TargetFolder=unit_target[n], PlexTitle=f.get("title", ""),
                         PlexYear=f.get("year", ""),
                         FileYear=(YEAR_RE.search(v.stem).group(1) if YEAR_RE.search(v.stem) else ""),
                         TMDB=f.get("tmdb", ""), IMDb=f.get("imdb", ""), Res=f.get("res", ""),
                         Note="; ".join(unit_note[n]), **unit_meta[n]))
    for p in sorted(files):
        if p in unit_ids:
            continue
        ext = p.suffix.lower()
        if ext in VIDEO_EXT:  # sample / trailer
            rows.append(dict(Apply="", Action="REVIEW", Folder=folder.name, File=rel(p),
                             Note="sample/trailer video - delete or keep as extra?"))
            continue
        if ext in SUB_EXT:
            ns = norm(p.stem)
            owner = next((v for s, v in stems if s and ns.startswith(s)), None)
            if owner:
                n = unit_ids[owner]
                rows.append(dict(Apply="n/a" if n in stays else "auto", Action=act(n), Folder=folder.name,
                                 File=rel(p), Unit=str(n), TargetFolder=unit_target[n],
                                 Note="subtitle - goes with its film automatically (type n to leave it out)"))
            else:
                rows.append(dict(Apply="", Action="REVIEW", Folder=folder.name, File=rel(p),
                                 Note="subtitle not matched to a video - pick its movie (Unit)"))
            continue
        rows.append(dict(Apply="Y", Action="TO_DELETE", Folder=folder.name, File=rel(p),
                         TargetFolder="_to_delete", Note="leftover (.nfo/.jpg/.txt/Thumbs.db...)"))
    return rows


def cmd_split_plan(a):
    root = Path(a.root)
    if not root.is_dir():
        sys.exit(f"Not a folder: {root}")
    if a.type != "movie":
        sys.exit("split-plan is for movie folders; TV folders are handled separately.")
    unc = unc_path(root)
    print(f"Top-level folder : {root}  ({unc})")
    facts = PlexDB().parts_under(root)
    print(f"Plex knows       : {sum(1 for k in facts if isinstance(k, str))} video files under it")
    tmdb = TMDB()
    subdirs = sorted(d for d in root.iterdir() if d.is_dir() and not d.name.startswith(("_", "#", "@")))
    existing = {d.name.casefold() for d in subdirs}
    if a.only:
        subdirs = [d for d in subdirs if a.only.casefold() in d.name.casefold()]
        print(f"Only folders containing '{a.only}': {len(subdirs)}")
    rows, split, kept = [], 0, 0
    for d in subdirs:
        r = plan_folder(root, d, facts, existing, tmdb)
        if r:
            if r[0]["Action"] in ("KEEP-PARTS", "KEEP-DISC"):
                kept += 1
            else:
                split += 1
            rows += r
    tmdb.save()
    PLAN_DIR.mkdir(parents=True, exist_ok=True)
    tag = re.sub(r"[^0-9A-Za-z]+", "_", str(root.relative_to(root.anchor))).strip("_")
    for r in rows:
        r["Root"] = str(root)
    out = write_plan(PLAN_DIR / f"{tag}_split{only_tag(a)}_{dt.date.today():%Y-%m-%d}.xlsx", rows, FIELDS)
    retire_old_plans(out, f"{tag}_split{only_tag(a)}_")
    moves = [r for r in rows if r["Action"] == "MOVE" and not r["Note"].startswith("subtitle")]
    print(f"Folders scanned  : {len(subdirs)}")
    print(f"To split         : {split} folders -> {len({(r['Folder'], r['Unit']) for r in moves})} movie folders")
    print(f"Kept together    : {kept} folders (one movie in parts/versions)")
    print(f"Need your review : {sum(1 for r in rows if r['Apply'] == '' )} rows (Apply left blank)")
    print(f"Plan written     : {out}")


# ---------------------------------------------------------------- stage 2: final Plex names

HARDSUB_RE = re.compile(r"(?i)\b(eng(lish)?[\s._-]*hard[\s._-]*subs?|hard[\s._-]*subs?)\b")
EDITION_RE = re.compile(r"(?i)\b(criterion(?: collection)?|director'?s[\s._-]*cut|extended(?: cut| edition)?|"
                        r"uncut|unrated|theatrical(?: cut)?|remastered|restored)\b")
RES_ORDER = {"2160p": 4, "1080p": 3, "720p": 2, "SD": 1, "": 0}
SUB_LANG_RE = re.compile(r"(?i)\.(en|eng|english|ja|jp|jpn|japanese|zh|chi|chs|cht|zho|chinese|ko|kor|korean|"
                         r"ru|rus|russian)((?:\.(?:forced|sdh|cc))*)$")
LANG_MAP = {"en": "en", "eng": "en", "english": "en", "ja": "ja", "jp": "ja", "jpn": "ja", "japanese": "ja",
            "zh": "zh", "chi": "zh", "chs": "zh", "cht": "zh", "zho": "zh", "chinese": "zh",
            "ko": "ko", "kor": "ko", "korean": "ko", "ru": "ru", "rus": "ru", "russian": "ru"}
MAX_PATH = 240


def fold(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def words_of(s: str) -> list:
    return [w for w in re.findall(r"[0-9a-z]+", fold(s).casefold()) if w not in SMALL_WORDS]


def mediainfo_res(path: Path) -> str:
    import glob
    import json
    import shutil
    import subprocess
    exe = shutil.which("mediainfo") or next(iter(glob.glob(
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\MediaArea.MediaInfo*\MediaInfo.exe"))), None)
    if not exe:
        return ""
    try:
        # CREATE_NO_WINDOW: MediaInfo is a console program - without it a black window flashes for every file
        out = subprocess.run([exe, "--Output=JSON", str(path)], capture_output=True, timeout=120,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        tracks = json.loads(out)["media"]["track"]
        v = next(t for t in tracks if t.get("@type") == "Video")
        return res_label(v.get("Width"), v.get("Height"))
    except Exception:
        return ""


def sub_language(p: Path) -> tuple:
    """(language code or '', flags like '.forced') from the name, else from the text."""
    m = SUB_LANG_RE.search(p.stem)
    if m:
        return LANG_MAP[m.group(1).lower()], m.group(2).lower()
    if p.suffix.lower() in (".sub", ".sup"):  # binary image subtitles: language unknown from content
        return "", ""
    try:
        raw = p.read_bytes()[:200_000]
    except OSError:
        return "", ""
    if p.suffix.lower() == ".idx":
        m = re.search(rb"(?m)^id:\s*([a-z]{2})", raw)
        return (m.group(1).decode(), "") if m else ("", "")
    for enc in ("utf-8-sig", "utf-16", "cp932", "gb18030", "cp949", "cp1251"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("latin-1", "ignore")
    kana = len(re.findall(r"[\u3040-\u30ff]", text))
    hangul = len(re.findall(r"[\uac00-\ud7af]", text))
    han = len(re.findall(r"[\u4e00-\u9fff]", text))
    cyr = len(re.findall(r"[\u0400-\u04ff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    best = max([("ja", kana * 3 + (han if kana else 0)), ("ko", hangul), ("zh", han if not kana else 0),
                ("ru", cyr), ("en", latin // 3)], key=lambda t: t[1])
    return (best[0], "") if best[1] > 50 else ("", "")


class DeepSeek:
    """Chooses among TMDB search results or suggests better search words. Never supplies an id itself."""

    def __init__(self):
        from openai import OpenAI
        key = config.key("deepseek")
        self.client = OpenAI(api_key=key, base_url="https://api.deepseek.com")

    def choose(self, lang, folder, files, candidates):
        import json
        import hashlib
        cache_file = CACHE_DIR / "deepseek.json"
        cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else {}
        key = hashlib.sha1(json.dumps([lang, folder, files, [c["id"] for c in candidates]],
                                      ensure_ascii=False).encode()).hexdigest()
        if key in cache:
            return cache[key]
        out = self._ask(lang, folder, files, candidates)
        if not str(out.get("reason", "")).startswith("DeepSeek error"):
            cache[key] = out
            cache_file.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
        return out

    def _ask(self, lang, folder, files, candidates):
        import json
        system = ("You identify films for a media library. Answer ONLY with JSON: "
                  '{"tmdb_id": "<id from the candidate list or null>", "queries": ["<better TMDB search '
                  'titles, English or original, max 3>"], "confidence": <0..1>, "reason": "<short>"}. '
                  "Choose tmdb_id ONLY from the given candidates; never invent one. If none fits, "
                  "return null and suggest queries (e.g. the original-language title in romaji or native script).")
        user = json.dumps(dict(original_language=lang, folder=folder, files=files, candidates=candidates),
                          ensure_ascii=False)
        try:
            r = self.client.chat.completions.create(
                model="deepseek-chat", temperature=0, response_format={"type": "json_object"},
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
            out = json.loads(r.choices[0].message.content)
        except Exception as e:
            return dict(tmdb_id=None, queries=[], confidence=0, reason=f"DeepSeek error: {e}")
        ids = {c["id"] for c in candidates}
        if out.get("tmdb_id") is not None and str(out["tmdb_id"]) not in ids:
            out["reason"] = f"rejected id {out['tmdb_id']} (not a candidate); " + str(out.get("reason", ""))
            out["tmdb_id"] = None
        return out


def identify(folder: Path, vids, f, tmdb: TMDB, ds, lang):
    """-> (tmdb_id, imdb_id, source, confidence, note). Facts only from Plex/TMDB; DeepSeek only picks."""
    if f and f["tmdb"]:
        return f["tmdb"], f["imdb"], "plex", "1.0", ""
    if f and f["imdb"]:
        tid = tmdb.find_imdb(f["imdb"])
        if tid:
            return tid, f["imdb"], "plex-imdb", "0.95", "TMDB id found from Plex's IMDb id"
    queries = []
    for s in (folder.name, vids[0].stem):
        t, y = clean_from_filename(s)
        t = EDITION_RE.sub("", HARDSUB_RE.sub("", re.sub(r"(?i)\b(aka|dvdrip|vhsrip|bluray|web-?dl|hdtv|"
                                                         r"x26[45]|h\.?26[45]|xvid|\d{3,4}p)\b.*", "", t)))
        t = re.sub(r"[\[(].*?[\])]", " ", t).strip(" -")
        if t and (t, y) not in queries:
            queries.append((t, y))
    cands = {}
    for q, y in queries:
        for c in tmdb.search(q, y, lang) + (tmdb.search(q, "", lang) if y else []):
            cands[c["id"]] = c
    years = {y for _, y in queries if y}
    good = [c for c in cands.values() if c["lang"] == lang and
            (not years or any(c["year"] and abs(int(c["year"]) - int(y)) <= 1 for y in years))]
    if len(good) == 1:
        return good[0]["id"], "", "tmdb-search", "0.8", f"only {lang} match for '{queries[0][0]}'"
    if ds is None:
        return "", "", "", "0", "not found in Plex/TMDB (DeepSeek off)"
    files = [v.name for v in vids]
    top = sorted(cands.values(), key=lambda c: (c["lang"] != lang, c["title"]))[:10]
    ans = ds.choose(lang, folder.name, files, top)
    if not ans.get("tmdb_id") and ans.get("queries"):
        for q in ans["queries"][:3]:
            for c in tmdb.search(q, "", lang):
                cands[c["id"]] = c
        top = sorted(cands.values(), key=lambda c: (c["lang"] != lang, c["title"]))[:12]
        ans = ds.choose(lang, folder.name, files, top)
    if ans.get("tmdb_id"):
        return str(ans["tmdb_id"]), "", "deepseek", str(ans.get("confidence", "")), str(ans.get("reason", ""))
    return "", "", "", "0", "not found - " + str(ans.get("reason", ""))


RFIELDS = ["Apply", "Kind", "Folder", "File", "NewFolder", "NewFile", "TMDB", "IMDb", "Title", "OriginalTitle",
           "Year", "FileYear", "Res", "Source", "Confidence", "Dropped", "Note", "Root", "ToolFolder", "ToolFile"]
# ToolFolder/ToolFile (hidden in Excel) = the tool's own proposal, so a rerun can tell the owner's edits apart
OWNER_FIELDS = ["Apply", "NewFolder", "NewFile", "TMDB", "IMDb", "Title", "OriginalTitle", "Year", "Source",
                "Confidence"]


def carry_over(rows: list, prev_plan) -> dict:
    """Rerun: keep the owner's work from the previous plan of the same folders.
    - rows the owner edited (names changed, manual helper) -> the owner's values are kept
    - rows the owner set to Y with the same proposed name -> stay Y
    Returns counts, including folders whose problems are gone."""
    _, prev = read_plan(prev_plan)
    key = lambda r: (r.get("Root", "").casefold(), r.get("Folder", "").casefold(), r.get("File", "").casefold())
    old = {key(r): r for r in prev if r.get("Kind") != "SPLIT_TO_TEMP" and r.get("Folder")}
    n = dict(kept=0, approved=0, changed=0)
    for r in rows:
        r["ToolFolder"], r["ToolFile"] = r.get("NewFolder", ""), r.get("NewFile", "")
        p = old.get(key(r))
        if not p or r.get("Kind") == "TO_DELETE":
            continue
        p_names = (p.get("NewFolder", ""), p.get("NewFile", ""))
        tool = (p["ToolFolder"], p["ToolFile"]) if p.get("ToolFolder") or p.get("ToolFile") else None
        yes = p.get("Apply", "").strip().upper() == "Y"
        edited = p.get("Source") == "manual" or (p_names != tool if tool else (yes and p_names != (r["NewFolder"], r.get("NewFile", ""))))
        if edited and p.get("NewFile", "").strip():
            r.update({k: p.get(k, "") for k in OWNER_FIELDS})
            r["Note"] = "kept your change from the previous plan" + (f" (tool now proposes: {r['ToolFile']})"
                                                                      if r["ToolFile"] and r["ToolFile"] != r["NewFile"] else "")
            n["kept"] += 1
        elif yes and r.get("Apply", "") != "Y":
            if p_names == (r.get("NewFolder", ""), r.get("NewFile", "")):
                r["Apply"] = "Y"
                n["approved"] += 1
            else:
                r["Note"] = (r.get("Note", "") + "; " if r.get("Note") else "") + \
                            f"you approved a different name before: {p_names[1] or p_names[0]}"
                n["changed"] += 1
    was = {(r.get("Root", "").casefold(), r.get("Folder", "").casefold())
           for r in prev if r.get("Folder") and not r.get("Apply", "").strip()}
    now = {(r.get("Root", "").casefold(), r.get("Folder", "").casefold())
           for r in rows if r.get("Folder") and not r.get("Apply", "").strip()}
    n["solved"], n["new"] = len(was - now), len(now - was)
    return n


def rename_folder(root: Path, folder: Path, facts: dict, tmdb: TMDB, ds, lang: str, exclude=frozenset()):
    """exclude: files (casefold paths) already split out into the temp folder - planned from there instead."""
    all_files = sorted(p for p in folder.rglob("*") if p.is_file())
    files = [p for p in all_files if str(p).casefold() not in exclude]
    rel = lambda p: str(p.relative_to(folder))
    base = dict(Folder=folder.name)
    split_from = len(files) < len(all_files)
    if split_from and not any(is_video(p) for p in files):
        return []  # every movie in this folder was split out; only leftovers remain
    if any({s.upper() for s in p.relative_to(folder).parts[:-1]} & DISC_DIRS for p in files):
        return [dict(base, Apply="", Kind="REVIEW", Note="DVD/Blu-ray folder structure - rename by hand or remux")]
    videos = [p for p in files if is_video(p)]
    main = [v for v in videos if not EXTRA_RE.search(v.stem)]
    if not main:
        subs = [p.name for p in files if p.suffix.lower() in SUB_EXT or is_video(p)]
        if not subs:  # e.g. 'External Subs' with a .txt, 'Thumbs' - a note folder, not a film
            return [dict(base, Apply="n/a", Kind="REVIEW",
                         Note="not a film folder (only " + (", ".join(p.name for p in files[:3]) or "nothing") +
                              ") - nothing is copied")]
        return [dict(base, Apply="", Kind="REVIEW",
                     Note="no video file in this folder - only " + ", ".join(subs[:3]) +
                          (f" (+{len(subs) - 3} more)" if len(subs) > 3 else ""))]
    info = {v: lookup(facts, v) for v in main}
    keys = {("plex", info[v]["mid"]) if info[v] else ("name", part_key(v.stem)) for v in main}
    if len(keys) > 1:
        names = "; ".join(str(v.relative_to(folder)) for v in main)
        why = "their rows in the split plan are not Y" if split_from else "folder was not in the split plan"
        return [dict(base, Apply="", Kind="REVIEW", Note=f"{len(keys)} films in one folder, not split ({why}): {names}")]

    f = next((info[v] for v in main if info[v]), None)
    tid, imdb, source, conf, why = identify(folder, main, f, tmdb, ds, lang)
    if not imdb and f and f["imdb"]:
        imdb = f["imdb"]  # no TMDB entry: fall back to {imdb-tt...}, which Plex also understands
    meta = tmdb.movie(tid) if tid else None
    notes = [why] if why else []
    if meta:
        title, year, orig = meta["title"], meta["year"], meta["orig"]
        if meta.get("lang") and meta["lang"] != lang:
            notes.append(f"TMDB original language is '{meta['lang']}', not '{lang}'")
    elif f:
        title, year, orig = f["title"], f["year"], f["orig"]
        notes.append("no TMDB entry - name from Plex without id")
    else:
        (title, year), orig = clean_from_filename(main[0].stem), ""
        notes.append("not identified - name from file name, check it")
    fy = YEAR_RE.search(main[0].stem) or YEAR_RE.search(folder.name)
    fyear = fy.group(1) if fy else ""
    ok = source in ("plex", "plex-imdb")
    if fyear and year and fyear != year:
        if abs(int(fyear) - int(year)) > 1:
            ok = False
            notes.append(f"year {year} differs from the old name's {fyear} - check the match")
        else:
            notes.append(f"year {year} vs old name {fyear} (one year apart)")

    # keep a series prefix from stage 1 ('Bored Hatamoto - Ninja Assassins (1960)')
    name = safe_name(title)
    cur = re.split(r"\s*\((?:19|20)\d{2}\)", folder.name)[0]
    if " - " in cur:
        pre, rest = cur.split(" - ", 1)
        if not has_series(name, pre) and words_of(rest)[:3] == words_of(name)[:3]:
            name = f"{pre.strip()} - {name}"
    idtag = f"{{tmdb-{tid}}}" if tid else (f"{{imdb-{imdb}}}" if imdb else "")
    ed = EDITION_RE.search(folder.name) or next((EDITION_RE.search(v.stem) for v in main if EDITION_RE.search(v.stem)), None)
    edition = "{edition-" + ed.group(1).title().replace("'S", "'s") + "}" if ed else ""
    native = safe_name(orig) if orig and words_of(orig) != words_of(title) else ""

    res = {}
    for v in main:
        res[v] = (info[v] or {}).get("res") or mediainfo_res(v)
    best = max((res[v] for v in main), key=lambda r: RES_ORDER.get(r, 0))

    def compose(native_part, r, extra=""):
        parts = [f"{name} ({year})" if year else name, idtag, edition, native_part, f"[{r}]" if r else ""]
        return safe_name(" ".join(p for p in parts if p) + extra)

    # file names: versions (different Plex media items) keep their own resolution; parts get ' - ptN'
    groups = defaultdict(list)
    for v in main:
        groups[(info[v] or {}).get("miid") or part_key(v.stem)].append(v)
    new_video, seen = {}, defaultdict(int)
    for g in groups.values():
        g.sort()
        for i, v in enumerate(g, 1):
            hs = " [EngHardSub]" if HARDSUB_RE.search(v.stem) or HARDSUB_RE.search(folder.name) else ""
            pt = f" - pt{i}" if len(g) > 1 else ""
            key = (res[v], hs, pt)
            seen[key] += 1
            ver = f" - version {seen[key]}" if seen[key] > 1 else ""
            new_video[v] = (res[v], hs + pt + ver)

    # shorten the native title if a path would get too long
    nat = native
    longest_ext = max(len(p.suffix) + 12 for p in files)
    while True:
        new_folder = compose(nat, best)
        worst = max(len(str(root / new_folder / (compose(nat, r, x) + ".en.forced.srt")))
                    for r, x in new_video.values())
        if worst <= MAX_PATH or not nat:
            break
        nat = nat[:-1].rstrip()
    if nat != native:
        notes.append("native title shortened to keep paths under 240 characters" if nat else
                     "native title left out to keep paths under 240 characters")

    old_words = set(words_of(folder.name)) | {w for v in main for w in words_of(v.stem)}
    dropped = sorted(old_words - set(words_of(new_folder)) - {w for _, x in new_video.values() for w in words_of(x)}
                     - {year, fyear} - {"pt1", "pt2", "pt3", "pt4"})
    common = dict(base, NewFolder=new_folder, TMDB=tid, IMDb=imdb, Title=title, OriginalTitle=orig, Year=year,
                  FileYear=fyear, Source=source, Confidence=conf)
    apply_v = "Y" if ok and tid else ""
    rows = []
    names_used = set()
    new_stem = {}
    for v in main:
        r, x = new_video[v]
        stem = compose(nat, r, x)
        new_stem[v] = stem
        names_used.add((stem + out_ext(v)).casefold())
        rows.append(dict(common, Apply=apply_v, Kind="VIDEO", File=rel(v), NewFile=stem + out_ext(v), Res=r,
                         Dropped=" ".join(dropped), Note="; ".join(notes)))
    stems = sorted(((norm(v.stem), v) for v in main), key=lambda t: -len(t[0]))
    for p in files:
        if p in new_video:
            continue
        ext = p.suffix.lower()
        if is_video(p):
            rows.append(dict(common, Apply="", Kind="REVIEW", File=rel(p), NewFile="",
                             Note="sample/trailer video - keep as extra or delete?"))
        elif ext in SUB_EXT:
            ns = norm(p.stem)
            owner = next((v for s, v in stems if s and ns.startswith(s)), None) or (main[0] if len(main) == 1 else None)
            if not owner:
                rows.append(dict(common, Apply="", Kind="REVIEW", File=rel(p), NewFile="",
                                 Note="subtitle not matched to a video - fill NewFile yourself"))
                continue
            idx = p.with_suffix(".idx")  # VobSub .idx/.sub pair must keep one name: take the .idx language
            code, flags = sub_language(idx if ext == ".sub" and idx.exists() else p)
            nf = new_stem[owner] + (f".{code}" if code else "") + flags + ext
            k = 2
            while nf.casefold() in names_used:
                nf = new_stem[owner] + (f".{code}" if code else "") + flags + f".{k}" + ext
                k += 1
            names_used.add(nf.casefold())
            rows.append(dict(common, Apply="auto", Kind="SUB", File=rel(p), NewFile=nf,  # follows its film's VIDEO row
                             Note="subtitle - renamed with its film automatically (type n to leave it out)"
                             if code else "subtitle language unknown - renamed with its film automatically"))
        else:
            rows.append(dict(common, Apply=apply_v or "Y", Kind="TO_DELETE", File=rel(p),
                             NewFolder="_to_delete", NewFile="", Note="leftover (.nfo/.jpg/.txt/Thumbs.db...)"))
    return rows


def cmd_rename_plan(a):
    root = Path(a.root)
    if not root.is_dir():
        sys.exit(f"Not a folder: {root}")
    if a.type != "movie":
        sys.exit("rename-plan handles movie folders; TV comes next.")
    lang = LANGS[a.lang]
    unc = unc_path(root)
    print(f"Top-level folder : {root}  ({unc})")
    facts = PlexDB().parts_under(root)
    tmdb = TMDB()
    ds = None if a.no_ai else DeepSeek()
    subdirs = sorted(d for d in root.iterdir() if d.is_dir() and not d.name.startswith(("_", "#", "@")))
    if a.only:
        subdirs = [d for d in subdirs if a.only.casefold() in d.name.casefold()]
    if a.limit:
        subdirs = subdirs[: a.limit]
    # films split out into the temp folder are planned from there, not from the original
    # the split plan is found automatically (newest full split plan of this library): a forgotten or cancelled
    # choice must never produce a plan without the split films
    out = PLAN_DIR / f"{plan_tag(root)}_rename{only_tag(a)}.xlsx"  # one fixed name, overwritten on every run
    if is_locked(out):
        sys.exit(f"The rename plan is open in Excel - save it, close Excel, then run again.\n   {out}")
    exclude, temp = set(), None
    split_plan = Path(a.split_plan) if a.split_plan else latest_plan(root, "split")
    t = Path(a.temp) if a.temp else temp_folder(root)
    if split_plan and not a.no_split:
        if not t.is_dir():
            sys.exit(f"Split plan {split_plan.name} found, but the temp folder is missing:\n   {t}\n"
                     f"Run step 1 'Copy to temp' first.")
        temp = t
        for r in split_moves(read_plan(split_plan)[1]):  # same rows split-copy copied (subtitles follow films)
            exclude.add(str(root / r["Folder"] / r["File"]).casefold())
        print(f"Split plan used  : {split_plan.name}")
        print(f"Split films from : {temp}  ({len(exclude)} files taken from there instead of the original)")
    elif t.is_dir() and not a.no_split:
        sys.exit(f"The temp folder exists but no split plan was found in plans\\:\n   {t}\n"
                 f"Put the split plan back into plans\\ (or run with --no-split if the temp folder is not used).")
    else:
        print("Split plan used  : none (no multi-movie folders split for this library)")

    jobs = [(root, d) for d in subdirs]
    if temp:
        tdirs = sorted(d for d in temp.iterdir() if d.is_dir())
        if a.only:
            tdirs = [d for d in tdirs if a.only.casefold() in d.name.casefold()]
        jobs += [(temp, d) for d in tdirs]
    rows = []
    for i, (r_root, d) in enumerate(jobs, 1):
        try:
            got = rename_folder(r_root, d, facts, tmdb, ds, lang, exclude if r_root == root else frozenset())
        except Exception as e:  # one bad folder must not stop the run
            got = [dict(Folder=d.name, Apply="", Kind="REVIEW", Note=f"error: {e}")]
        for r in got:
            r["Root"] = str(r_root)
        rows += got
        if i % 25 == 0:
            print(f"   ... {i}/{len(jobs)} folders")
            tmdb.save()
    tmdb.save()
    for x in sorted(exclude):  # record them so rename-copy does not copy them twice
        p = Path(x)
        rows.append(dict(Apply="n/a", Kind="SPLIT_TO_TEMP", Folder="", File=str(p), Root=str(root),
                         Note="split out - copied from the temp folder instead"))
    PLAN_DIR.mkdir(parents=True, exist_ok=True)
    for r in rows:
        r.setdefault("Root", str(root))
        r["ToolFolder"], r["ToolFile"] = r.get("NewFolder", ""), r.get("NewFile", "")
    carried = carry_over(rows, a.previous) if a.previous else None
    if is_locked(out):
        sys.exit(f"The rename plan was opened in Excel during the run - close Excel and run again.\n   {out}")
    backup_plan(out)  # after carry_over has read it
    out = write_plan(out, rows, RFIELDS, new_if_locked=False)
    retire_old_plans(out, f"{plan_tag(root)}_rename{only_tag(a)}_")  # older dated plans from before the fixed name
    vids = [r for r in rows if r.get("Kind") == "VIDEO"]
    by_src = defaultdict(int)
    for r in vids:
        by_src[r.get("Source") or "not found"] += 1
    print(f"Folders          : {len(jobs)}")
    print(f"Videos           : {len(vids)}  (identified by: " +
          ", ".join(f"{k} {v}" for k, v in sorted(by_src.items())) + ")")
    print(f"Ready (Apply=Y)  : {sum(1 for r in vids if r.get('Apply') == 'Y')} videos")
    print(f"Need your review : {sum(1 for r in rows if r.get('Check') == 'CHECK')} red, "
          f"{sum(1 for r in rows if r.get('Check') == 'review')} yellow rows")
    if carried:
        print(f"From last plan   : {Path(a.previous).name}")
        print(f"   kept your edits  : {carried['kept']} rows")
        print(f"   kept your Y      : {carried['approved']} rows")
        print(f"   name changed since you approved it (left blank, see Note): {carried['changed']} rows")
        print(f"   folders no longer needing review: {carried['solved']}   new folders needing review: {carried['new']}")
    print(f"Plan written     : {out}")


def cmd_rename_apply(a):
    plan = Path(a.plan)
    rows = read_plan(plan)[1]
    if not rows or not rows[0].get("Root"):
        sys.exit("Plan has no Root column.")
    root = Path(rows[0]["Root"])
    by_folder = defaultdict(list)
    for r in rows:
        if r["Apply"].strip().upper() == "Y" and r["Kind"] in ("VIDEO", "SUB", "TO_DELETE"):
            by_folder[r["Folder"]].append(r)
    ops, problems = [], []
    for folder, rs in sorted(by_folder.items()):
        src_dir = root / folder
        vids = [r for r in rs if r["Kind"] == "VIDEO"]
        new_folder = next((r["NewFolder"] for r in vids), "")
        if not src_dir.is_dir():
            problems.append(f"folder missing: {src_dir}")
            continue
        if new_folder and safe_name(new_folder) != new_folder:
            problems.append(f"bad NewFolder '{new_folder}'")
            continue
        planned = set()
        folder_ops = []
        for r in rs:
            src = src_dir / r["File"]
            if r["Kind"] == "TO_DELETE":
                dst = root / "_to_delete" / folder / r["File"]
            else:
                nf = r["NewFile"]
                if not nf or safe_name(nf) != nf:
                    problems.append(f"bad NewFile '{nf}' for {src}")
                    continue
                dst = src_dir / nf
            if not src.exists():
                problems.append(f"missing: {src}")
            elif str(src).casefold() == str(dst).casefold() and src.name == dst.name:
                continue  # already right
            elif (dst.exists() and str(src).casefold() != str(dst).casefold()) or str(dst).casefold() in planned:
                problems.append(f"target exists, not overwriting: {dst}")
            else:
                planned.add(str(dst).casefold())
                folder_ops.append(("MOVE", src, dst))
        ops += folder_ops
        if vids and new_folder and new_folder != folder:
            dst_dir = root / new_folder
            if dst_dir.exists() and str(dst_dir).casefold() != str(src_dir).casefold():
                problems.append(f"folder exists, files renamed but folder kept: {dst_dir}")
            else:
                ops.append(("RENAMEDIR", src_dir, dst_dir))

    print(f"Plan       : {plan}\nRoot       : {root}")
    print(f"Folders    : {sum(1 for o in ops if o[0] == 'RENAMEDIR')} renamed")
    print(f"Files      : {sum(1 for o in ops if o[0] == 'MOVE' and '_to_delete' not in str(o[2]))} renamed, "
          f"{sum(1 for o in ops if '_to_delete' in str(o[2]))} leftovers to _to_delete")
    print(f"Skipped    : {len(problems)}")
    for p in problems[:30]:
        print("   !", p)
    if not a.go:
        print("\nDRY RUN - nothing changed. Examples:")
        for o, s, d in [o for o in ops if o[0] == "RENAMEDIR"][:5] + [o for o in ops if o[0] == "MOVE"][:8]:
            print(f"   {o:9} {s.relative_to(root)}\n             -> {d.relative_to(root)}")
        print("\nRun again with --go to do it.")
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = LOG_DIR / f"{plan.stem}_applied_{dt.datetime.now():%Y-%m-%d_%H%M%S}.csv"
    done = err = 0
    with open(log, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=LOG_FIELDS)
        w.writeheader()
        for o, s, d in ops:  # file renames come before their folder rename
            try:
                for parent in reversed([p for p in [d.parent, *d.parent.parents] if not p.exists()]):
                    parent.mkdir()
                    w.writerow(dict(op="MKDIR", src="", dst=str(parent), result="ok"))
                os.rename(s, d)
                w.writerow(dict(op="MOVE", src=str(s), dst=str(d), result="ok"))
                done += 1
                if o == "MOVE" and s.parent != d.parent and s.parent.exists() and not any(s.parent.iterdir()):
                    s.parent.rmdir()  # emptied sub-folder inside the movie folder
                    w.writerow(dict(op="RMDIR", src=str(s.parent), dst="", result="ok"))
            except OSError as e:
                w.writerow(dict(op=o, src=str(s), dst=str(d), result=f"ERROR {e}"))
                err += 1
            fh.flush()
    print(f"\nDone: {done} renamed/moved, {err} errors. Undo log: {log}")


# ---------------------------------------------------------------- apply / undo

LOG_DIR = HERE / "logs"
LOG_FIELDS = ["op", "src", "dst", "result"]


def cmd_split_apply(a):
    plan = Path(a.plan)
    rows = read_plan(plan)[1]
    if not rows or not rows[0].get("Root"):
        sys.exit("Plan has no Root column - re-create it with split-plan.")
    root = Path(rows[0]["Root"])
    todo = [r for r in rows if r["Apply"].strip().upper() == "Y" and r["Action"] in ("MOVE", "TO_DELETE")]

    ops, problems, planned = [], [], set()
    for r in todo:
        src = root / r["Folder"] / r["File"]
        if r["Action"] == "MOVE":
            tgt = safe_name(r["TargetFolder"])
            if not tgt or tgt != r["TargetFolder"].strip():
                problems.append(f"bad TargetFolder '{r['TargetFolder']}' for {src}")
                continue
            dst = root / tgt / Path(r["File"]).name
        else:
            dst = root / "_to_delete" / r["Folder"] / r["File"]
        if not src.exists():
            problems.append(f"source missing: {src}")
        elif dst.exists() or str(dst).casefold() in planned:
            problems.append(f"target already exists, not overwriting: {dst}")
        else:
            planned.add(str(dst).casefold())
            ops.append((r["Action"], src, dst))

    moving = {str(s).casefold() for _, s, _ in ops}
    emptied = []
    for folder in sorted({root / r["Folder"] for r in todo}):
        left = [p for p in folder.rglob("*") if p.is_file() and str(p).casefold() not in moving]
        if folder.exists() and not left:
            emptied.append(folder)

    new_dirs = sorted({d.parent for _, _, d in ops if not d.parent.exists()})
    print(f"Plan         : {plan}")
    print(f"Root         : {root}")
    print(f"Moves        : {sum(1 for o in ops if o[0] == 'MOVE')} files into "
          f"{len({d.parent for o, _, d in ops if o == 'MOVE'})} movie folders "
          f"({len([d for d in new_dirs if d.parent == root])} new)")
    print(f"To _to_delete: {sum(1 for o in ops if o[0] == 'TO_DELETE')} leftover files")
    print(f"Emptied      : {len(emptied)} old folders (removed once empty)")
    print(f"Skipped      : {len(problems)} rows with problems")
    for p in problems[:30]:
        print("   !", p)

    if not a.go:
        print("\nDRY RUN - nothing changed. First moves:")
        for o, s, d in ops[:15]:
            print(f"   {o:9} {s.relative_to(root)}\n             -> {d.relative_to(root)}")
        print("\nRun again with --go to do it.")
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = LOG_DIR / f"{plan.stem}_applied_{dt.datetime.now():%Y-%m-%d_%H%M%S}.csv"
    done = err = 0
    with open(log, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=LOG_FIELDS)
        w.writeheader()
        for o, s, d in ops:
            try:
                for parent in reversed([p for p in [d.parent, *d.parent.parents] if not p.exists()]):
                    parent.mkdir()
                    w.writerow(dict(op="MKDIR", src="", dst=str(parent), result="ok"))
                os.rename(s, d)
                w.writerow(dict(op="MOVE", src=str(s), dst=str(d), result="ok"))
                done += 1
            except OSError as e:
                w.writerow(dict(op="MOVE", src=str(s), dst=str(d), result=f"ERROR {e}"))
                err += 1
            fh.flush()
        for folder in emptied:  # remove only directories that are really empty now
            for dpath in sorted([p for p in folder.rglob("*") if p.is_dir()] + [folder],
                                key=lambda p: -len(p.parts)):
                try:
                    if not any(dpath.iterdir()):
                        dpath.rmdir()
                        w.writerow(dict(op="RMDIR", src=str(dpath), dst="", result="ok"))
                except OSError as e:
                    w.writerow(dict(op="RMDIR", src=str(dpath), dst="", result=f"ERROR {e}"))
    print(f"\nDone: {done} moved, {err} errors. Undo log: {log}")


LEFTOVER_EXT = {".nfo", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".txt", ".url", ".db", ".ini", ".lnk", ".xml"}


def temp_folder(root: Path) -> Path:
    return root.with_name(root.name + " (split temp)")


def follows_film(r: dict) -> bool:
    """Owner (28 Sep): subtitles go with their film by default. A subtitle row with Apply blank or 'auto' follows
    its film's decision; only an explicit n / no / x leaves it out."""
    return r.get("Apply", "").strip().upper() in ("", "AUTO")


def split_moves(rows: list) -> list:
    """Split-plan rows to copy: MOVE rows marked Y, plus subtitle rows that follow a film (same Folder + Unit)
    whose video is marked Y."""
    is_sub = lambda r: Path(r.get("File", "")).suffix.lower() in SUB_EXT
    yes = {(r["Folder"], r.get("Unit", "")) for r in rows
           if r.get("Action") == "MOVE" and r["Apply"].strip().upper() == "Y" and not is_sub(r)}
    return [r for r in rows if r.get("Action") == "MOVE" and (
        r["Apply"].strip().upper() == "Y" or (is_sub(r) and follows_film(r) and (r["Folder"], r.get("Unit", "")) in yes))]


def cmd_split_copy(a):
    """Step a): copy each film of a multi-movie folder into its own folder in a TEMP folder.
    The original is untouched (unless --mark file: a small note is written into each split-from folder)."""
    plan = Path(a.plan)
    rows = read_plan(plan)[1]
    root = Path(rows[0]["Root"])
    temp = Path(a.temp) if a.temp else temp_folder(root)
    if str(temp).casefold() == str(root).casefold() or str(temp).casefold().startswith(str(root).casefold() + "\\"):
        sys.exit("The temp folder must be outside the original folder.")
    ops, problems, used, sources = [], [], set(), defaultdict(list)
    for r in split_moves(rows):
        tgt = safe_name(r["TargetFolder"])
        src = root / r["Folder"] / r["File"]
        if not tgt or tgt != r["TargetFolder"].strip():
            problems.append(f"bad TargetFolder '{r['TargetFolder']}' - skipped")
            continue
        if not src.exists():
            problems.append(f"missing in original: {src}")
            continue
        dst = temp / tgt / src.name
        if str(dst).casefold() in used:
            problems.append(f"two files want {dst} - second skipped")
            continue
        used.add(str(dst).casefold())
        ops.append((src, dst))
        sources[r["Folder"]].append(dst)
    print(f"Split plan   : {plan}")
    print(f"Split from   : {len(sources)} multi-movie folders -> {len({d.parent for _, d in ops})} film folders")
    for p in problems[:20]:
        print("   !", p)
    log = copy_files(ops, root, temp, plan.stem + "_temp", a.go)
    if not a.go:
        print("\nDRY RUN - nothing copied. Examples:")
        for s, d in ops[:6]:
            print(f"   {s.relative_to(root)}\n      -> {d.relative_to(temp)}")
        print("\nRun again with --go to copy (it can be stopped and re-run; finished files are skipped).")
        return
    if a.mark == "file":
        for folder, dsts in sources.items():
            note = root / folder / "_SPLIT_INTO_TEMP.txt"
            note.write_text("The films in this folder were copied, one folder each, to:\n" +
                            "\n".join(sorted({str(d.parent) for d in dsts})) + "\n", encoding="utf-8")
        print(f"Marked {len(sources)} split-from folders with _SPLIT_INTO_TEMP.txt")
    print(f"Next: rename-plan \"{root}\" --split-plan \"{plan}\"")


def copy_files(ops, root, target, label, go):
    """Copy (src, dst) pairs; resumable (same-size targets are skipped); .partial until complete."""
    import shutil
    total = sum(s.stat().st_size for s, _ in ops)
    todo = [(s, d) for s, d in ops if not (d.exists() and d.stat().st_size == s.stat().st_size)]
    todo_bytes = sum(s.stat().st_size for s, _ in todo)
    free = shutil.disk_usage(str(target.anchor) if target.drive else str(root)).free
    print(f"Original     : {root}  (left untouched)")
    print(f"New folder   : {target}")
    print(f"Files        : {len(ops)} ({total / 1e9:,.0f} GB)")
    print(f"Still to copy: {len(todo)} files, {todo_bytes / 1e9:,.1f} GB (files already there with the same size are skipped)")
    print(f"Free space   : {free / 1e9:,.0f} GB")
    if todo_bytes > free * 0.95:
        sys.exit("Not enough free space for the copy.")
    if not go:
        return None
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = LOG_DIR / f"{label}_copied_{dt.datetime.now():%Y-%m-%d_%H%M%S}.csv"
    done_bytes = err = 0
    start = dt.datetime.now()
    with open(log, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=LOG_FIELDS)
        w.writeheader()
        for i, (s, d) in enumerate(todo, 1):
            try:
                d.parent.mkdir(parents=True, exist_ok=True)
                tmp = d.with_name(d.name + ".partial")
                shutil.copy2(s, tmp)  # Windows CopyFile: server-side copy on the same NAS share when possible
                if tmp.stat().st_size != s.stat().st_size:
                    raise OSError("size mismatch after copy")
                os.replace(tmp, d)
                w.writerow(dict(op="COPY", src=str(s), dst=str(d), result="ok"))
            except OSError as e:
                err += 1
                w.writerow(dict(op="COPY", src=str(s), dst=str(d), result=f"ERROR {e}"))
            fh.flush()
            done_bytes += s.stat().st_size
            rate = done_bytes / max((dt.datetime.now() - start).total_seconds(), 1)
            eta = (todo_bytes - done_bytes) / rate if rate else 0
            print(f"   {i}/{len(todo)}  {done_bytes / 1e9:,.1f}/{todo_bytes / 1e9:,.1f} GB  {rate / 1e6:,.0f} MB/s  "
                  f"~{eta / 3600:,.1f} h left  {d.name[:70]}", flush=True)
    print(f"\nDone: {len(todo) - err} copied, {err} errors. Log: {log}")
    return log


def cmd_rename_copy(a):
    """SAFE stage 2: copy every movie into a new top-level folder under its final name; original untouched."""
    plan = Path(a.plan)
    rows = read_plan(plan)[1]
    roots = list(dict.fromkeys(Path(r["Root"]) for r in rows if r.get("Root")))
    root = roots[0]  # the original; a second root is the split temp folder
    target = Path(a.copy_to) if a.copy_to else root.with_name(root.name + " (Plex)")
    for rt in roots:
        if str(target).casefold() == str(rt).casefold() or str(target).casefold().startswith(str(rt).casefold() + "\\"):
            sys.exit("The new folder must be outside the original and temp folders.")

    renamed, skip, problems = {}, set(), []
    # subtitles follow their film: a SUB row with Apply blank/auto is renamed when a VIDEO row of its folder is Y,
    # into that video's (possibly edited) NewFolder
    film_folder = {}
    for r in rows:
        if r.get("Kind") == "VIDEO" and r["Apply"].strip().upper() == "Y":
            film_folder.setdefault((r["Root"].casefold(), r["Folder"].casefold()), r["NewFolder"])
    for r in rows:
        if r["Kind"] == "SPLIT_TO_TEMP":
            skip.add(r["File"].casefold())  # full path of the original; its copy in temp is used instead
            continue
        src = str(Path(r["Root"]) / r["Folder"] / r["File"]).casefold() if r.get("File") else ""
        if not src:
            continue
        follow = r["Kind"] == "SUB" and follows_film(r) and (r["Root"].casefold(), r["Folder"].casefold()) in film_folder
        if r["Kind"] == "TO_DELETE":
            skip.add(src)
        elif follow or (r["Apply"].strip().upper() == "Y" and (r["Kind"] in ("VIDEO", "SUB") or r.get("NewFile"))):
            # REVIEW rows count too once the owner filled NewFile (e.g. an unmatched subtitle)
            nfold, nfile = r["NewFolder"], r["NewFile"]
            if follow:
                nfold = film_folder[(r["Root"].casefold(), r["Folder"].casefold())]
            if not nfold or not nfile or safe_name(nfold) != nfold or safe_name(nfile) != nfile:
                problems.append(f"bad new name for {r['Folder']}\\{r['File']} - copied unchanged")
                continue
            renamed[src] = target / nfold / nfile

    # only the folders that are in the plan are copied (a plan made with --only covers just those films)
    in_plan = {(str(Path(r["Root"])).casefold(), r["Folder"].casefold())
               for r in rows if r.get("Folder") and r["Kind"] != "SPLIT_TO_TEMP"}
    ops, used = [], set()
    for rt in roots:
        for p in sorted(rt.rglob("*")):
            if not p.is_file() or p.relative_to(rt).parts[0].startswith(("_", "#", "@")):
                continue
            if (str(rt).casefold(), p.relative_to(rt).parts[0].casefold()) not in in_plan:
                continue
            k = str(p).casefold()
            in_disc = bool({s.upper() for s in p.relative_to(rt).parts[:-1]} & DISC_DIRS)
            if k in skip or (not in_disc and k not in renamed and
                             (p.suffix.lower() in LEFTOVER_EXT or p.name.lower() == "thumbs.db")):
                continue
            dst = renamed.get(k) or target / p.relative_to(rt)
            if str(dst).casefold() in used:
                problems.append(f"two files want {dst.name} - second copied unchanged")
                dst = target / p.relative_to(rt)
                if str(dst).casefold() in used:
                    problems.append(f"skipped, name taken twice: {dst}")
                    continue
            used.add(str(dst).casefold())
            ops.append((p, dst))

    print(f"Plan         : {plan}")
    print(f"Folders      : {len(in_plan)} media folders in this plan (only these are copied)")
    print(f"Renamed      : {sum(1 for s, _ in ops if str(s).casefold() in renamed)} files get their new name; "
          f"{sum(1 for s, _ in ops if str(s).casefold() not in renamed)} copied unchanged (not marked Y)")
    for p in problems[:20]:
        print("   !", p)
    log = copy_files(ops, root, target, plan.stem, a.go)
    if not a.go:
        print("\nDRY RUN - nothing copied. Examples:")
        for s, d in [o for o in ops if str(o[0]).casefold() in renamed][:6]:
            print(f"   {s}\n      -> {d.relative_to(target)}")
        print("\nRun again with --go to copy (it can be stopped and re-run; finished files are skipped).")


def cmd_undo(a):
    log = Path(a.log)
    with open(log, encoding="utf-8-sig", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r["result"] == "ok"]
    print(f"Undo {log.name}: {len(rows)} operations to reverse")
    if not a.go:
        print("DRY RUN - run again with --go to reverse them.")
        return
    bad = 0
    for r in reversed(rows):
        try:
            if r["op"] == "MOVE":
                src, dst = Path(r["src"]), Path(r["dst"])
                src.parent.mkdir(parents=True, exist_ok=True)
                if src.exists():
                    raise OSError(f"original path is occupied: {src}")
                os.rename(dst, src)
            elif r["op"] == "RMDIR":
                Path(r["src"]).mkdir(parents=True, exist_ok=True)
            elif r["op"] == "MKDIR":
                p = Path(r["dst"])
                if p.exists() and not any(p.iterdir()):
                    p.rmdir()
        except OSError as e:
            bad += 1
            print("   !", r["op"], r["src"] or r["dst"], e)
    print(f"Undo finished, {bad} problems.")


def plan_tag(root: Path) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", str(Path(root).relative_to(Path(root).anchor))).strip("_")


def latest_plan(root: Path, which: str, only: str = ""):
    """Newest plan of a library folder: '<tag>_rename.xlsx' (fixed name) or dated '<tag>_split_<date>[_HHMM].xlsx/.csv'.
    only = only_tag() text; single-film plans are never picked for a full run."""
    pat = re.compile(re.escape(f"{plan_tag(root)}_{which}{only}") + r"(_\d{4}-\d{2}-\d{2})?(_\d{4})?\.(xlsx|csv)$", re.I)
    found = [p for p in PLAN_DIR.glob("*.*") if p.is_file() and pat.match(p.name)] if PLAN_DIR.is_dir() else []
    return max(found, key=lambda p: p.stat().st_mtime, default=None)


def is_locked(p: Path) -> bool:
    """True when Excel (or anything) has the file open for writing."""
    try:
        with open(p, "r+b"):
            return False
    except PermissionError:
        return True
    except FileNotFoundError:
        return False


def open_plans(root: Path) -> list:
    """Plans of this library that are open in Excel (locked, or Excel's '~$' owner file next to them)."""
    if not PLAN_DIR.is_dir():
        return []
    pre = plan_tag(root).casefold() + "_"
    return [p for p in sorted(PLAN_DIR.iterdir())
            if p.is_file() and p.name.casefold().startswith(pre) and p.suffix.lower() in (".xlsx", ".csv")
            and (is_locked(p) or (PLAN_DIR / ("~$" + p.name[2:])).exists() or (PLAN_DIR / ("~$" + p.name)).exists())]


def backup_plan(p: Path):
    """Move the current fixed-name plan to plans\\_obsolete (dated) before it is overwritten."""
    if not p.exists():
        return
    dest = PLAN_DIR / "_obsolete"
    dest.mkdir(exist_ok=True)
    d = dest / f"{p.stem}_{dt.datetime.fromtimestamp(p.stat().st_mtime):%Y-%m-%d_%H%M%S}{p.suffix}"
    p.rename(d)
    print(f"Previous version : moved to plans\\_obsolete\\{d.name}")


def retire_old_plans(out: Path, prefix: str):
    """Owner's rule: after a new plan is written, older plans of the same kind (same library, split or rename,
    same --only) leave plans\\ - moved to plans\\_obsolete, never deleted. A plan open in Excel stays put."""
    pat = re.compile(re.escape(prefix) + r"\d{4}-\d{2}-\d{2}(_\d{4})?\.(xlsx|csv)$", re.I)
    dest = PLAN_DIR / "_obsolete"
    moved = []
    for p in PLAN_DIR.iterdir():
        if p.is_file() and pat.match(p.name) and p.resolve() != out.resolve():
            dest.mkdir(exist_ok=True)
            d = dest / p.name
            if d.exists():
                d = dest / f"{p.stem}_old{dt.datetime.now():%H%M%S}{p.suffix}"
            try:
                p.rename(d)
                moved.append(p.name)
            except OSError:
                print(f"   (could not move {p.name} - open in Excel? It stays in plans\\)")
    if moved:
        print(f"Older plans      : {len(moved)} moved to plans\\_obsolete ({', '.join(moved)})")


def cmd_to_xlsx(a):
    fields, rows = read_plan(a.plan)
    out = write_plan(Path(a.plan), rows, fields)
    print(f"Rows             : {len(rows)}")
    print(f"Need your review : {sum(1 for r in rows if r['Check'] == 'CHECK')} red (CHECK), "
          f"{sum(1 for r in rows if r['Check'] == 'review')} yellow (review)")
    print(f"Plan written     : {out}")


def main():
    ap =argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("split-plan", help="plan splitting multi-movie folders (changes nothing)")
    p.add_argument("root")
    p.add_argument("--lang", choices=sorted(LANGS), required=True)
    p.add_argument("--type", choices=["movie", "tv"], required=True)
    p.add_argument("--only", help="only media folders whose name contains this text")
    p.set_defaults(func=cmd_split_plan)
    p = sub.add_parser("split-apply", help="carry out the Y rows of a reviewed split plan (dry run unless --go)")
    p.add_argument("plan")
    p.add_argument("--go", action="store_true")
    p.set_defaults(func=cmd_split_apply)
    p = sub.add_parser("split-copy", help="step a: copy the films of multi-movie folders into a TEMP folder, "
                                          "one folder each (original untouched; dry run unless --go)")
    p.add_argument("plan")
    p.add_argument("--temp", help='temp folder (default: "<original> (split temp)")')
    p.add_argument("--mark", choices=["none", "file"], default="none",
                   help="file = write _SPLIT_INTO_TEMP.txt into each split-from folder of the original")
    p.add_argument("--go", action="store_true")
    p.set_defaults(func=cmd_split_copy)
    p = sub.add_parser("rename-plan", help="stage 2: plan final Plex names for every movie folder (changes nothing)")
    p.add_argument("root")
    p.add_argument("--lang", choices=sorted(LANGS), required=True)
    p.add_argument("--type", choices=["movie", "tv"], required=True)
    p.add_argument("--only", help="only folders whose name contains this text")
    p.add_argument("--limit", type=int, help="only the first N folders (for a test run)")
    p.add_argument("--no-ai", action="store_true", help="do not call DeepSeek")
    p.add_argument("--split-plan", help="the split plan used for split-copy: split films are planned from the temp folder")
    p.add_argument("--temp", help='temp folder of split-copy (default: "<original> (split temp)")')
    p.add_argument("--previous", help="earlier rename plan of the same folder: keep the owner's Y and edits")
    p.add_argument("--no-split", action="store_true", help="ignore split plan and temp folder (not normally needed)")
    p.set_defaults(func=cmd_rename_plan)
    p = sub.add_parser("rename-apply", help="stage 2: carry out the Y rows of a reviewed rename plan (dry run unless --go)")
    p.add_argument("plan")
    p.add_argument("--go", action="store_true")
    p.set_defaults(func=cmd_rename_apply)
    p = sub.add_parser("rename-copy", help="SAFE stage 2: copy every movie into a new folder under its final "
                                           "name; original untouched (dry run unless --go)")
    p.add_argument("plan")
    p.add_argument("--copy-to", help='new top-level folder (default: "<original> (Plex)")')
    p.add_argument("--go", action="store_true")
    p.set_defaults(func=cmd_rename_copy)
    p = sub.add_parser("undo", help="reverse an applied plan from its log (dry run unless --go)")
    p.add_argument("log")
    p.add_argument("--go", action="store_true")
    p.set_defaults(func=cmd_undo)
    p = sub.add_parser("to-xlsx", help="turn a CSV plan into a coloured .xlsx plan (same name; the CSV is kept)")
    p.add_argument("plan")
    p.set_defaults(func=cmd_to_xlsx)
    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
