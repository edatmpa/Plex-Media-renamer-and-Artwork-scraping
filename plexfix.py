"""plexfix - step 3: fix what Plex got wrong after scanning a renamed library. See CLAUDE.md.

Posters (first job):
    posters-plan   --folder "V:\\Japanese\\Samurai" --lang JP [--all] [--only TEXT] [--limit N] [--no-ai]
                   -> plans\\<name>_posters.json   candidates from Plex + TMDB, AI (Z.ai GLM-4.6V) ranks them
    posters-review --folder ...                   -> web page on this PC to check / change the choices
    posters-apply  --folder ... [--go]            -> set the approved posters in Plex and lock them (log in logs\\)
    posters-undo   logs\\<log>.csv [--go]          -> put the previous posters back

Plex is changed only through its own web API (never the database, which is read read-only), and only for
films the owner approved on the review page. The owner's poster rules (agreed 28 Sep 2026):
    1) the original release poster, in the style of the film's own year and country;
    2) among those, prefer one showing cast / credits;  3) otherwise the clearest, uncropped image.
"""
import argparse
import base64
import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import config  # this PC's settings (config.json): Plex address, API keys, Tesseract
import plexname as pn  # paths, plan helpers, TMDB (also forces IPv4 for requests)

PLEX_URL = config.PLEX_URL
ZAI_URL = "https://api.z.ai/api/paas/v4/chat/completions"
ZAI_MODEL = "glm-4.6v"
POSTER_CACHE = pn.CACHE_DIR / "posters"  # small thumbnails per Plex item, shown on the review page
AI_CACHE = pn.CACHE_DIR / "zai_posters.json"
MAX_CANDIDATES = 10
LANG_NAMES = {"ja": "Japanese", "ko": "Korean", "zh": "Chinese", "ru": "Russian", "en": "English"}


# ---------------------------------------------------------------- Plex (read-only DB + HTTP API)

def plex_token() -> str:
    """The Plex account token (never printed): config.json, else registry of the Plex user, else Preferences.xml."""
    if config.PLEX_TOKEN:
        return config.PLEX_TOKEN
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Plex, Inc.\Plex Media Server") as k:
            return winreg.QueryValueEx(k, "PlexOnlineToken")[0]
    except OSError:
        pass
    import xml.etree.ElementTree as ET
    prefs = Path(pn.PLEX_DB).parents[2] / "Preferences.xml"
    return ET.parse(prefs).getroot().attrib["PlexOnlineToken"]


class PlexAPI:
    def __init__(self):
        import requests
        self.s = requests.Session()
        self.s.headers.update({"X-Plex-Token": plex_token(), "Accept": "application/json"})

    def get(self, path, **params) -> dict:
        r = self.s.get(PLEX_URL + path, params=params, timeout=60)
        r.raise_for_status()
        return r.json().get("MediaContainer", {}) if r.content else {}

    def put(self, path, **params):
        r = self.s.put(PLEX_URL + path, params=params, timeout=60)
        r.raise_for_status()

    def thumb(self, url, w=300, h=450, fmt="") -> bytes:
        """Any poster (Plex-local key or web URL) as a small JPEG (or PNG, which tkinter can show without
        Pillow), resized by Plex's own photo transcoder."""
        params = dict(width=w, height=h, minSize=1, upscale=1, url=url)
        if fmt:
            params["format"] = fmt
        r = self.s.get(PLEX_URL + "/photo/:/transcode", timeout=90, params=params)
        r.raise_for_status()
        return r.content


def plex_form(path: Path):
    """(section id, the path as Plex stores it) for a file/folder inside any Plex library, else (None, None).
    Plex keeps the form the library was added with: drive letter (V:\\...) or NAS address (\\\\<NAS IP>\\...)."""
    con = sqlite3.connect(f"file:{pn.PLEX_DB}?mode=ro", uri=True)
    forms = [str(path), pn.unc_path(path)]
    for sid, root in con.execute("SELECT library_section_id, root_path FROM section_locations"):
        r = root.rstrip("\\")
        for f in forms:
            if f.casefold() == r.casefold() or f.casefold().startswith(r.casefold() + "\\"):
                return sid, r + f[len(r):]
    return None, None


def item_in_folder(folder: Path):
    """The Plex movie whose file is in this folder: dict(id, title, year, guid, section) or None."""
    sid, pf_ = plex_form(folder)
    if not sid:
        return None
    con = sqlite3.connect(f"file:{pn.PLEX_DB}?mode=ro", uri=True)
    r = con.execute("""SELECT m.id, m.title, m.year, m.guid FROM media_parts mp
                       JOIN media_items mi ON mi.id = mp.media_item_id JOIN metadata_items m ON m.id = mi.metadata_item_id
                       WHERE mp.file LIKE ? AND m.metadata_type = 1 AND m.deleted_at IS NULL
                       ORDER BY m.id DESC LIMIT 1""", (pf_ + "\\%",)).fetchone()
    return dict(id=r[0], title=r[1], year=r[2], guid=r[3] or "", section=sid) if r else None


def scan_folder(api: "PlexAPI", folder: Path):
    """Ask Plex to scan just this folder (after a rename) - like 'Scan library files', but one film."""
    sid, pf_ = plex_form(folder)
    if sid:
        api.get(f"/library/sections/{sid}/refresh", path=pf_)
    return sid


def plex_find(api: "PlexAPI", item_id: int, key: str, lang="en-US", tv=False) -> dict:
    """What Plex's own matching service knows for 'tmdb-<id>' or 'imdb-tt…' (name, year, guid), or {}.
    Needs a film (or, tv=True, a show) of the server as the item to ask about. Plex knows many IMDb titles TMDB lacks."""
    res = api.get(f"/library/metadata/{item_id}/matches", manual=1, title=key,
                  agent="tv.plex.agents.series" if tv else "tv.plex.agents.movie", language=lang).get("SearchResult", [])
    return res[0] if res else {}


def show_in_folder(folder: Path):
    """The Plex TV show whose episodes are in this folder: dict(id, title, year, guid, section, tv) or None."""
    sid, pf_ = plex_form(folder)
    if not sid:
        return None
    con = sqlite3.connect(f"file:{pn.PLEX_DB}?mode=ro", uri=True)
    r = con.execute("""SELECT sh.id, sh.title, sh.year, sh.guid FROM media_parts mp
                       JOIN media_items mi ON mi.id = mp.media_item_id
                       JOIN metadata_items ep ON ep.id = mi.metadata_item_id AND ep.metadata_type = 4
                       JOIN metadata_items se ON se.id = ep.parent_id JOIN metadata_items sh ON sh.id = se.parent_id
                       WHERE mp.file LIKE ? AND sh.deleted_at IS NULL ORDER BY sh.id DESC LIMIT 1""",
                    (pf_ + "\\%",)).fetchone()
    return dict(id=r[0], title=r[1], year=r[2], guid=r[3] or "", section=sid, tv=True) if r else None


def any_movie_id():
    """Some film of the server: Plex's match search is asked 'for' an item, any movie works."""
    con = sqlite3.connect(f"file:{pn.PLEX_DB}?mode=ro", uri=True)
    r = con.execute("SELECT id FROM metadata_items WHERE metadata_type = 1 LIMIT 1").fetchone()
    return r[0] if r else None


def any_show_id():
    """Some TV show of the server (Plex's TV match search is asked 'for' a show item)."""
    con = sqlite3.connect(f"file:{pn.PLEX_DB}?mode=ro", uri=True)
    r = con.execute("SELECT id FROM metadata_items WHERE metadata_type = 2 LIMIT 1").fetchone()
    return r[0] if r else None


def plex_search(api: "PlexAPI", item_id, title: str, year: str = "", lang="en-US", tv=False) -> list:
    """Title search in Plex's own matching service (what Fix Match shows). It also knows films that exist only on
    IMDb, and transliterated show names. tv=True searches TV shows. Returns [{name, year, guid, summary}]."""
    params = dict(manual=1, title=title, agent="tv.plex.agents.series" if tv else "tv.plex.agents.movie",
                  language=lang)
    if year:
        params["year"] = year
    item = item_id or (any_show_id() if tv else any_movie_id())
    res = api.get(f"/library/metadata/{item}/matches", **params).get("SearchResult", [])
    return [dict(name=r.get("name", ""), year=str(r.get("year", "") or ""), guid=r.get("guid", ""),
                 summary=r.get("summary", "")) for r in res if r.get("guid")]


def plex_details(api: "PlexAPI", guid: str) -> dict:
    """Full details of a film from Plex's metadata service (the same data Plex fills in after a match):
    title, original title, year, description, studio, director, cast, IMDb/TMDB ids, poster."""
    r = api.s.get("https://metadata.provider.plex.tv/library/metadata/" + guid.rsplit("/", 1)[-1], timeout=30)
    r.raise_for_status()
    m = r.json()["MediaContainer"]["Metadata"][0]
    ids = {g["id"].split("://")[0]: g["id"].split("://")[1] for g in m.get("Guid", []) if "://" in g.get("id", "")}
    return dict(title=m.get("title", ""), orig=m.get("originalTitle", ""), year=str(m.get("year", "") or ""),
                summary=m.get("summary", ""), studio=m.get("studio", ""), imdb=ids.get("imdb", ""),
                tmdb=ids.get("tmdb", ""), guid=guid, thumb=m.get("thumb", ""),
                directors=[d.get("tag") for d in m.get("Director", [])][:2],
                cast=[d.get("tag") for d in m.get("Role", [])][:4])


def match_tmdb(api: "PlexAPI", item_id: int, tmdb_id: str, lang="en-US", tv=False) -> str:
    """Fix Match to exactly this film (or show, tv=True), then refresh it. tmdb_id is a TMDB number, or a full key
    'tmdb-<id>' / 'imdb-tt…' (Plex's match search understands both)."""
    key = tmdb_id if "-" in str(tmdb_id) else f"tmdb-{tmdb_id}"
    m = plex_find(api, item_id, key, lang, tv=tv)
    if not m:
        raise RuntimeError(f"Plex does not know {key}")
    api.put(f"/library/metadata/{item_id}/match", guid=m["guid"], name=m.get("name", ""), year=m.get("year", ""))
    api.put(f"/library/metadata/{item_id}/refresh")
    return f"{m.get('name')} ({m.get('year')})"


FIELD_NAMES = {"title": "title", "native": "originalTitle", "year": "year", "summary": "summary",
               "sort": "titleSort"}


def write_fields(api: "PlexAPI", section: int, item_id: int, values: dict, tv=False):
    """Write fields into Plex and lock them (a later refresh won't overwrite them). values: keys of FIELD_NAMES.
    tv=True: the item is a TV show (type 2)."""
    params = dict(type=2 if tv else 1, id=item_id)
    for k, v in values.items():
        f = FIELD_NAMES[k]
        params[f"{f}.value"] = v
        params[f"{f}.locked"] = 1
    api.put(f"/library/sections/{section}/all", **params)


def set_poster(api: "PlexAPI", section: int, item_id: int, url: str = "", data: bytes = b"", tv=False):
    """Show this poster in Plex and lock it: an image address from the web, a Plex candidate, or file bytes."""
    if data:
        api.s.post(PLEX_URL + f"/library/metadata/{item_id}/posters", data=data, timeout=120).raise_for_status()
    elif url.startswith("http"):
        api.s.post(PLEX_URL + f"/library/metadata/{item_id}/posters", params={"url": url}, timeout=120).raise_for_status()
        api.put(f"/library/metadata/{item_id}/poster", url=url)
    else:
        api.put(f"/library/metadata/{item_id}/poster", url=url)
    api.put(f"/library/sections/{section}/all", type=2 if tv else 1, id=item_id, **{"thumb.locked": 1})


def find_section(folder: Path):
    """(section id, name, root path) of the Plex library that holds the renamed copy of this folder."""
    want = {s.casefold() for s in (str(folder), str(folder) + " (Plex)",
                                   pn.unc_path(folder), pn.unc_path(folder) + " (Plex)")}
    con = sqlite3.connect(f"file:{pn.PLEX_DB}?mode=ro", uri=True)
    for sid, name, path in con.execute("SELECT s.id, s.name, l.root_path FROM library_sections s "
                                       "JOIN section_locations l ON l.library_section_id = s.id"):
        if path.rstrip("\\").casefold() in want:
            return sid, name, path
    sys.exit(f"No Plex library points at {folder} or {folder} (Plex).")


def section_is_tv(sid: int) -> bool:
    """True for a TV-series library (Plex section type 2) - tool 3 then works on the shows' posters."""
    con = sqlite3.connect(f"file:{pn.PLEX_DB}?mode=ro", uri=True)
    r = con.execute("SELECT section_type FROM library_sections WHERE id = ?", (sid,)).fetchone()
    return bool(r and r[0] == 2)


def show_items(sid: int) -> list:
    """The shows of a TV library, in the same form as library_items (films). The show folder comes from the path of
    one of its episodes (the part right under the library folder)."""
    con = sqlite3.connect(f"file:{pn.PLEX_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    roots = [r[0].rstrip("\\") for r in con.execute("SELECT root_path FROM section_locations WHERE library_section_id=?",
                                                    (sid,))]
    q = """SELECT sh.id, sh.title, sh.original_title, sh.year, sh.user_thumb_url, sh.user_fields, sh.guid,
                  (SELECT group_concat(t.tag, ' ') FROM taggings tg JOIN tags t ON t.id = tg.tag_id
                    WHERE tg.metadata_item_id = sh.id AND (t.tag LIKE 'tmdb://%' OR t.tag LIKE 'imdb://%')) AS ids,
                  (SELECT mp.file FROM metadata_items se JOIN metadata_items ep ON ep.parent_id = se.id
                     JOIN media_items mi ON mi.metadata_item_id = ep.id JOIN media_parts mp ON mp.media_item_id = mi.id
                    WHERE se.parent_id = sh.id LIMIT 1) AS file
           FROM metadata_items sh WHERE sh.library_section_id = ? AND sh.metadata_type = 2 AND sh.deleted_at IS NULL
           ORDER BY sh.title_sort"""
    out = []
    for r in con.execute(q, (sid,)):
        f = r["file"] or ""
        root = next((x for x in roots if f.casefold().startswith(x.casefold() + "\\")), "")
        folder = f[len(root) + 1:].split("\\")[0] if root else Path(f.replace("\\", "/")).parent.name
        tm = re.search(r"tmdb://(\d+)", r["ids"] or "") or re.search(r"\{tmdb-(\d+)\}", folder)
        out.append(dict(id=r["id"], title=r["title"] or "", orig=r["original_title"] or "", year=str(r["year"] or ""),
                        current=r["user_thumb_url"] or "", tmdb=tm.group(1) if tm else "",
                        imdb=(re.search(r"imdb://(tt\d+)", r["ids"] or "") or [None, ""])[1],
                        locked="9" in (r["user_fields"] or "").replace("lockedFields=", "").split("|"),
                        matched=not (r["guid"] or "local://").startswith("local://"), folder=folder, tv=True))
    return out


def library_items(sid: int) -> list:
    """Films of a film library - or, for a TV library, its shows (show_items)."""
    if section_is_tv(sid):
        return show_items(sid)
    con = sqlite3.connect(f"file:{pn.PLEX_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    q = """SELECT m.id, m.title, m.original_title, m.year, m.user_thumb_url, m.user_fields, m.guid,
                  (SELECT group_concat(t.tag, ' ') FROM taggings tg JOIN tags t ON t.id = tg.tag_id
                    WHERE tg.metadata_item_id = m.id AND (t.tag LIKE 'tmdb://%' OR t.tag LIKE 'imdb://%')) AS ids,
                  (SELECT mp.file FROM media_items mi JOIN media_parts mp ON mp.media_item_id = mi.id
                    WHERE mi.metadata_item_id = m.id LIMIT 1) AS file
           FROM metadata_items m WHERE m.library_section_id = ? AND m.metadata_type = 1 ORDER BY m.title_sort"""
    out = []
    for r in con.execute(q, (sid,)):
        tm = re.search(r"tmdb://(\d+)", r["ids"] or "") or re.search(r"\{tmdb-(\d+)\}", r["file"] or "")
        out.append(dict(id=r["id"], title=r["title"] or "", orig=r["original_title"] or "", year=str(r["year"] or ""),
                        current=r["user_thumb_url"] or "", tmdb=tm.group(1) if tm else "",
                        imdb=(re.search(r"imdb://(tt\d+)", r["ids"] or "") or [None, ""])[1],
                        locked="9" in (r["user_fields"] or "").replace("lockedFields=", "").split("|"),
                        matched=not (r["guid"] or "local://").startswith("local://"),
                        folder=Path((r["file"] or "").replace("\\", "/")).parent.name))
    return out


def is_frame(url: str) -> bool:
    """A still from the video (what Plex shows when no poster was chosen), or nothing at all."""
    return not url or (url.startswith("media://") and "/Thumbnails/" in url)


# ---------------------------------------------------------------- AI (Z.ai GLM-4.6V, looks at the images)

RULES = """You choose the poster that Plex should show for a {kind}.
{Kind}: {title} ({year}); original title: {orig}; original language: {lang}.
Rules, in this order:
1. Prefer the ORIGINAL release poster from the {kind}'s own country and year ({year}): title in the
   original language, artwork in the style of that era. Avoid later re-release posters, DVD/Blu-ray covers,
   posters made for other countries, modern or fan-made designs, screenshots/video frames and textless art.
2. Among equally original posters, prefer the one that shows more information: cast names, credits, studio.
3. Otherwise prefer the clearest, least cropped, best-quality image.
The images are numbered 1 to {n}. Reply with JSON only:
{{"best": <number>, "ranking": [<numbers, best first>], "confidence": "high"|"medium"|"low",
  "images": [{{"n": 1, "kind": "original poster"|"re-release"|"dvd cover"|"foreign poster"|"fan art"|"screenshot"|"textless"|"other",
              "era_matches_year": true|false, "title_text": "<text you can read>", "cast_info": true|false,
              "duplicate_of": <number or null>}}],
  "reason": "<one short sentence for the owner>"}}"""


class PosterAI:
    def __init__(self):
        import requests
        self.requests = requests
        self.key = config.key("zai")
        self.cache = json.loads(AI_CACHE.read_text(encoding="utf-8")) if AI_CACHE.exists() else {}
        self.lock = threading.Lock()
        self.tokens = dict(prompt=0, completion=0, calls=0)

    def rank(self, film: dict, images: list, lang: str) -> dict:
        # the language is part of the question ('original language: …'), so it is part of the cache key too
        key = f"{lang}|{film['id']}|" + "|".join(c["url"] for c in film["candidates"])
        with self.lock:
            if key in self.cache:
                return self.cache[key]
        kind = "TV series" if film.get("tv") else "film"
        content = [{"type": "text", "text": RULES.format(title=film["title"], year=film["year"] or "?",
                                                          orig=film["orig"] or "-", lang=LANG_NAMES.get(lang, lang),
                                                          n=len(images), kind=kind, Kind=kind[0].upper() + kind[1:])}]
        for i, b in enumerate(images, 1):
            content += [{"type": "text", "text": f"Image {i}:"},
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(b).decode()}}]
        body = dict(model=ZAI_MODEL, temperature=0, thinking={"type": "disabled"},
                    messages=[{"role": "user", "content": content}])
        r = self.requests.post(ZAI_URL, json=body, timeout=180, headers={"Authorization": f"Bearer {self.key}"})
        r.raise_for_status()
        j = r.json()
        text = j["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", text, re.S)
        ans = json.loads(m.group(0)) if m else {}
        u = j.get("usage") or {}
        with self.lock:
            self.tokens["prompt"] += u.get("prompt_tokens", 0)
            self.tokens["completion"] += u.get("completion_tokens", 0)
            self.tokens["calls"] += 1
            self.cache[key] = ans
        return ans

    def save(self):
        with self.lock:
            AI_CACHE.write_text(json.dumps(self.cache, ensure_ascii=False, indent=0), encoding="utf-8")


# ---------------------------------------------------------------- Wikipedia / Wikimedia Commons (official APIs)

WIKI_UA = {"User-Agent": "PlexMediaRenamer/1.0 (Plex library tool; poster lookup; "
                         "https://github.com/edatmpa/Plex-Media-renamer-and-Artwork-scraping)"}  # Wikimedia asks for one
WIKI_CACHE = pn.CACHE_DIR / "wiki_posters.json"
_wiki_lock = threading.Lock()


def _wget(url: str, **params) -> dict:
    """GET JSON from Wikimedia. It sometimes answers an automated client with an empty page (rate limiting):
    wait and try again, politely, up to 4 times."""
    import time
    import requests
    for n in range(4):
        r = requests.get(url, headers=WIKI_UA, timeout=30, params=params)
        if r.status_code == 200 and r.content[:1] in (b"{", b"["):
            return r.json()
        time.sleep(float(r.headers.get("retry-after") or 0) or 2 * (n + 1))
    raise RuntimeError(f"Wikimedia did not answer: {url}")


def _wiki_file(wiki: str, name: str):
    """(url, width, height) of an image file used on a wiki (Commons files resolve through any wiki)."""
    j = _wget(f"https://{wiki}.wikipedia.org/w/api.php", action="query", titles="File:" + name, prop="imageinfo",
              iiprop="url|size", format="json")
    info = (next(iter(j["query"]["pages"].values())).get("imageinfo") or [{}])[0]
    return (info.get("url") or "").split("?")[0], info.get("width") or 0, info.get("height") or 0


def _infobox_image(wiki: str, title: str) -> str:
    """File name of the picture in the article's info box (where Wikipedia shows a film's poster)."""
    j = _wget(f"https://{wiki}.wikipedia.org/w/api.php", action="parse", page=title, prop="wikitext", section=0,
              format="json", redirects=1)
    wt = j.get("parse", {}).get("wikitext", {}).get("*", "")
    m = re.search(r"\|\s*(?:image|画像|poster)\s*=\s*(?:\[\[)?(?:File:|ファイル:|画像:)?([^|\]\n]+\.(?:jpe?g|png|gif|webp))",
                  wt, re.I)
    return m.group(1).strip() if m else ""


def wiki_posters(tmdb_id: str = "", imdb_id: str = "", tv=False) -> list:
    """Posters from Wikipedia for a film found in Wikidata by its TMDB (P4947; TV shows: P4983) or IMDb (P345) id:
    Wikidata's 'film poster' (P3383) and the info-box picture of the Japanese and English articles. Portrait images
    only (film stills are landscape). English Wikipedia keeps copyrighted posters small (~266x375)."""
    key = f"{'tv:' if tv else ''}{tmdb_id}|{imdb_id}"
    with _wiki_lock:
        cache = json.loads(WIKI_CACHE.read_text(encoding="utf-8")) if WIKI_CACHE.exists() else {}
    if key in cache:
        return cache[key]
    out = []
    try:
        qid = ""
        for prop, val in (("P4983" if tv else "P4947", tmdb_id), ("P345", imdb_id)):
            if val and not qid:
                hits = _wget("https://www.wikidata.org/w/api.php", action="query", list="search",
                             srsearch=f"haswbstatement:{prop}={val}", format="json")
                qid = next((h["title"] for h in hits["query"]["search"]), "")
        if qid:
            e = _wget(f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json")["entities"][qid]
            files = [("en", c["mainsnak"]["datavalue"]["value"], "wikidata")
                     for p in ("P3383", "P18") for c in e.get("claims", {}).get(p, []) if "datavalue" in c["mainsnak"]]
            for wiki in ("ja", "en"):
                t = e.get("sitelinks", {}).get(f"{wiki}wiki", {}).get("title")
                f = _infobox_image(wiki, t) if t else ""
                if f:
                    files.append((wiki, f, f"wikipedia-{wiki}"))
            seen = set()
            for wiki, f, prov in files:
                url, w, h = _wiki_file(wiki, f)
                if url and url not in seen and h >= w:
                    seen.add(url)
                    out.append(dict(url=url, key=url, provider=prov, lang="wiki", size=f"{w}x{h}"))
    except Exception:
        return []  # not cached: try again next time
    with _wiki_lock:
        cache = json.loads(WIKI_CACHE.read_text(encoding="utf-8")) if WIKI_CACHE.exists() else {}
        cache[key] = out
        WIKI_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
    return out


# ---------------------------------------------------------------- DeepL (plot translation)

def translate(text: str, target: str = "EN-GB") -> str:
    """Translate a plot into English with DeepL (free-plan keys end in ':fx' and use the free endpoint)."""
    import requests
    key = config.key("deepl")
    url = "https://api-free.deepl.com/v2/translate" if key.endswith(":fx") else "https://api.deepl.com/v2/translate"
    r = requests.post(url, headers={"Authorization": f"DeepL-Auth-Key {key}"},
                      data={"text": text, "target_lang": target}, timeout=60)
    r.raise_for_status()
    return r.json()["translations"][0]["text"]


# ---------------------------------------------------------------- posters-plan

def plan_path(folder: Path) -> Path:
    return pn.PLAN_DIR / f"{pn.plan_tag(folder)}_posters.json"


def tv_posters(tmdb, tmdb_id: str, lang_code: str) -> list:
    """All TMDB posters of a TV show, sorted like TMDB.posters (original language, English, no text, others)."""
    j = tmdb._get(f"tv/{tmdb_id}/images")
    order = {lang_code: 0, "en": 1, None: 2}
    out = [dict(path=p["file_path"], lang=p.get("iso_639_1"), w=p.get("width"), h=p.get("height"),
                votes=p.get("vote_count", 0), score=p.get("vote_average", 0)) for p in j.get("posters", [])]
    out.sort(key=lambda p: (order.get(p["lang"], 3), -p["score"], -p["votes"]))
    return out


def gather(film: dict, api: PlexAPI, tmdb, tmdb_lock, lang: str, limit=MAX_CANDIDATES, all_langs=False, tv=False):
    """Candidates for one film (or TV show, tv=True): Plex's own list (no video frames) + TMDB posters of the
    original language (all TMDB posters with all_langs, for the manual helper) + Wikipedia."""
    cands, names = [], set()
    for m in api.get(f"/library/metadata/{film['id']}/posters").get("Metadata", []):
        url = m.get("ratingKey") or ""
        if is_frame(url):
            continue
        name = url.rsplit("/", 1)[-1].split("?")[0]
        if name in names:  # the same file offered by TMDB and TVDB
            continue
        names.add(name)
        cands.append(dict(url=url, key=m.get("key") or url, provider=m.get("provider") or "plex", lang=""))
    if film["tmdb"]:
        try:
            with tmdb_lock:
                ck = f"{'tvimages' if tv else 'images'}:{film['tmdb']}"
                tposters = tmdb.cache.get(ck)
                if tposters is None:
                    tposters = tv_posters(tmdb, film["tmdb"], lang) if tv else tmdb.posters(film["tmdb"], lang)
                    tmdb.cache[ck] = tposters
        except Exception:
            tposters = []
        by_name = {p["path"].lstrip("/"): p for p in tposters}
        for c in cands:
            p = by_name.get(c["url"].rsplit("/", 1)[-1])
            if p:
                c["lang"] = p["lang"] or "none"
        for p in tposters:  # original-language posters Plex did not offer
            if (all_langs or p["lang"] == lang) and p["path"].lstrip("/") not in names:
                u = "https://image.tmdb.org/t/p/original" + p["path"]
                cands.append(dict(url=u, key=u, provider="tmdb", lang=p["lang"] or "none"))
                names.add(p["path"].lstrip("/"))
    wiki = [w for w in wiki_posters(film.get("tmdb", ""), film.get("imdb", ""), tv=tv)
            if w["url"].rsplit("/", 1)[-1] not in names]
    order = {lang: 0, "none": 2, "en": 3}
    cands.sort(key=lambda c: order.get(c["lang"], 4))
    # Wikipedia often has the original release poster: always keep its places, even for much-postered films
    return cands[: max(limit - len(wiki), 0)] + wiki


# The whole-library run works like the one-film helper (owner, 28 Sep: "this should be the model"):
# up to 24 candidates, the AI ranks up to 16 (Wikipedia and added web images always among them).
CANDIDATES_SHOWN = 24
AI_SHOWN = 16
SEARCH_WORD = {"ja": "ポスター", "ko": "포스터", "zh": "海报", "ru": "постер"}


def cache_thumb(film_id, c: dict, api: PlexAPI) -> bytes:
    """Small JPEG of a candidate in cache\\posters\\<id>\\ (sets c['thumb']). TMDB images are fetched at w342
    directly; everything else through Plex's photo transcoder."""
    d = POSTER_CACHE / str(film_id)
    d.mkdir(parents=True, exist_ok=True)
    f = d / (hashlib.sha1(c["url"].encode()).hexdigest()[:16] + ".jpg")
    if not f.exists():
        if c["url"].startswith("https://image.tmdb.org/t/p/original/"):
            import requests  # plain request: the Plex token must not go to TMDB
            r = requests.get(c["url"].replace("/t/p/original/", "/t/p/w342/"), timeout=60)
            r.raise_for_status()
            f.write_bytes(r.content)
        else:
            f.write_bytes(api.thumb(c["key"]))
    c["thumb"] = f.name
    return f.read_bytes()


def rank_film(film: dict, ai: PosterAI, lang: str):
    """The AI ranks the candidates by the owner's rules; every ranked one gets 'rank' and 'kind'.
    Returns the best candidate (or None)."""
    pool = film["candidates"]
    always = [c for c in pool if c.get("lang") == "wiki" or c.get("provider") == "web"]
    shown = [c for c in pool if c not in always][: max(AI_SHOWN - len(always), 0)] + always
    for c in pool:
        for k in ("rank", "kind", "cast"):
            c.pop(k, None)
    if not shown:
        return None
    imgs = [(POSTER_CACHE / str(film["id"]) / c["thumb"]).read_bytes() for c in shown]
    ans = ai.rank(dict(film, candidates=shown), imgs, lang)
    info = {i.get("n"): i for i in ans.get("images", []) if isinstance(i, dict)}
    ranking = [n for n in ans.get("ranking", []) if isinstance(n, int) and 1 <= n <= len(shown)]
    best = ans.get("best")
    if isinstance(best, int) and 1 <= best <= len(shown):
        ranking = [best] + [n for n in ranking if n != best]
    for r, n in enumerate(dict.fromkeys(ranking), 1):
        c = shown[n - 1]
        c.update(rank=r, kind=info.get(n, {}).get("kind", ""), cast=bool(info.get(n, {}).get("cast_info")))
    film["ai"] = dict(reason=ans.get("reason", ""), confidence=ans.get("confidence", ""))
    top = shown[ranking[0] - 1] if ranking else None
    film["ai_best"] = top["url"] if top else ""
    return top


def plan_one(film, api, tmdb, tmdb_lock, ai, lang):
    cands = gather(film, api, tmdb, tmdb_lock, lang, limit=CANDIDATES_SHOWN, all_langs=True, tv=bool(film.get("tv")))
    seen, keep = set(), []
    for c in cands:
        try:
            b = cache_thumb(film["id"], c, api)
        except Exception:
            continue  # image no longer available
        h = hashlib.md5(b).hexdigest()
        if h not in seen:  # identical picture from two sources
            seen.add(h)
            keep.append(c)
    film["candidates"] = keep
    cur = POSTER_CACHE / str(film["id"]) / "current.jpg"
    cur.parent.mkdir(parents=True, exist_ok=True)
    if film["current"] and not cur.exists():
        try:
            cur.write_bytes(api.thumb(f"/library/metadata/{film['id']}/thumb"))
        except Exception:
            pass
    film["current_thumb"] = "current.jpg" if cur.exists() else ""
    film.update(choice="", approved=False, by="", ai={}, ai_best="",
                search=f"{film['orig'] or film['title']} {film['year']} {SEARCH_WORD.get(lang, 'poster')}".strip())
    if not keep:
        film["ai"] = dict(reason="NOT MATCHED in Plex (no online entry, so no posters). Match it first with the "
                                 "one-film tool (2 in the start window), then make the artwork plan again."
                          if not film["matched"] else "No poster found in Plex, TMDB or Wikipedia - use 'Find more "
                                                      "on the web' and add an image address.")
        return film
    if ai:
        try:
            top = rank_film(film, ai, lang)
        except Exception as e:
            film["ai"], top = dict(reason=f"AI error: {e}"[:200]), None
        if top:
            film["choice"] = top["url"]
            film["approved"] = film["ai"].get("confidence") == "high" and top.get("kind") == "original poster"
    else:
        film["choice"] = keep[0]["url"]  # original language first
    return film


def cmd_posters_plan(a):
    folder = Path(a.folder)
    lang = pn.LANGS[a.lang]
    sid, name, root = find_section(folder)
    tv = section_is_tv(sid)
    print(f"Plex library     : {name} (section {sid})  {root}  - {'TV series: show posters' if tv else 'films'}")
    items = library_items(sid)
    todo = [f for f in items if a.all or is_frame(f["current"])]
    if a.only:
        todo = [f for f in todo if a.only.casefold() in (f["title"] + " " + f["folder"]).casefold()]
    if a.limit:
        todo = todo[: a.limit]
    print(f"{'Shows' if tv else 'Films'}            : {len(items)}; showing no poster (or a video frame): "
          f"{sum(is_frame(f['current']) for f in items)}; planned now: {len(todo)}")
    out = plan_path(folder)
    if pn.is_locked(out):
        sys.exit(f"The poster plan is open - close it and run again.\n   {out}")
    prev = {}
    if out.exists():  # rerun: keep what the owner chose on the review page
        prev = {f["id"]: f for f in json.loads(out.read_text(encoding="utf-8"))["films"] if f.get("by") == "owner"}
    api, tmdb, tmdb_lock = PlexAPI(), pn.TMDB(), threading.Lock()
    ai = None if a.no_ai else PosterAI()
    done, films = 0, []
    pn.PLAN_DIR.mkdir(parents=True, exist_ok=True)
    pn.backup_plan(out)

    def write(complete):  # also every 10 films: a stopped run keeps the films already done (cache makes reruns fast)
        out.write_text(json.dumps(dict(library=name, section=sid, folder=str(folder), lang=lang, complete=complete,
                                       tv=tv, made=dt.datetime.now().isoformat(timespec="minutes"), films=films),
                                  ensure_ascii=False, indent=1), encoding="utf-8")

    with ThreadPoolExecutor(max_workers=4) as ex:
        for film in ex.map(lambda f: plan_one(f, api, tmdb, tmdb_lock, ai, lang), todo):
            p = prev.get(film["id"])
            if p and (not p["choice"] or p["choice"] in {c["url"] for c in film["candidates"]} | {film["current"]}):
                film.update(choice=p["choice"], approved=p["approved"], by="owner")
            films.append(film)
            done += 1
            if done % 10 == 0:
                print(f"   ... {done}/{len(todo)} films (saved)", flush=True)
                write(False)
                with tmdb_lock:  # other threads may be adding to the TMDB cache
                    tmdb.save()
                if ai:
                    ai.save()
    tmdb.save()
    if ai:
        ai.save()
    write(True)
    print(f"Not matched in Plex (match first, no posters possible): {sum(not f['matched'] for f in films)}")
    print(f"AI picks approved automatically (clear original poster): {sum(f['approved'] for f in films)}")
    print(f"To check on the review page: {sum(not f['approved'] for f in films)}")
    if ai and ai.tokens["calls"]:
        t = ai.tokens
        print(f"AI calls         : {t['calls']}  (tokens: {t['prompt']:,} in, {t['completion']:,} out)")
    print(f"Plan written     : {out}")


# ---------------------------------------------------------------- posters-review (local web page)

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Poster review</title>
<style>
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1d1f;--mute:#6b6b70;--line:#e2e0da;--pick:#c47f00;--ok:#1f7a3a;--warn:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#18181a;--card:#232326;--ink:#ececef;--mute:#a0a0a8;--line:#3a3a40;--pick:#f0b43c;--ok:#5cc47a;--warn:#ff8a80}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,sans-serif}
header{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
h1{font-size:17px;margin:0}.count{color:var(--mute)}button,select{font:inherit;padding:5px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink);cursor:pointer}
main{padding:12px 16px;display:grid;gap:12px}.film{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.head{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}.head b{font-size:15px}.orig{color:var(--mute)}
.why{color:var(--mute);margin:4px 0 8px}.row{display:flex;gap:10px;overflow-x:auto;padding-bottom:4px}
figure{margin:0;flex:0 0 auto;width:132px;cursor:pointer;border:3px solid transparent;border-radius:8px;padding:3px}
figure img{width:120px;height:180px;object-fit:cover;border-radius:4px;display:block;background:var(--line)}
figcaption{font-size:11px;color:var(--mute);margin-top:3px;line-height:1.3}
.none{width:120px;height:180px;border-radius:4px;background:var(--line);color:var(--mute);display:grid;place-items:center}
figure.cur{cursor:default;opacity:.85;border-style:dashed;border-color:var(--line)}figure.ai{border-color:var(--ok)}
.rk{display:block;font-weight:600;color:var(--ink)}figure.ai .rk{color:var(--ok)}
figure.sel{border-color:var(--pick)}.state{margin-left:auto;display:flex;gap:8px;align-items:center}
.ok{color:var(--ok);font-weight:600}.warn{color:var(--warn)}.sep{width:1px;background:var(--line);flex:0 0 1px}
.tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px}.tools input{font:inherit;padding:4px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink);width:340px;max-width:100%}
.tools a{color:var(--pick)}
</style></head><body>
<header><h1>Poster review – <span id="lib"></span></h1><span class="count" id="count"></span>
<select id="filter"><option value="todo">To check</option><option value="all">All films</option><option value="ok">Approved</option></select>
<button id="allok" title="Approve every film whose AI pick is shown">Approve all shown</button>
<button id="done">Done – close review</button><span id="saved" class="count"></span></header>
<main id="list"></main>
<script>
const D = __DATA__;
const $ = s => document.querySelector(s);
$("#lib").textContent = D.library;
function save(f){ fetch("/save",{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify({id:f.id,choice:f.choice,approved:f.approved})}).then(r=>{$("#saved").textContent=r.ok?"saved":"NOT saved"}); }
function post(path,body){return fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});}
function rankText(c){return c.rank? (c.rank===1?"★ AI pick":"AI rank "+c.rank)+(c.kind?" · "+c.kind:"")+(c.cast?" · cast":""):"";}
function cap(c,i){return [String(i+1), c.provider, c.lang&&!["none","wiki"].includes(c.lang)?c.lang:"", c.size||""].filter(Boolean).join(" · ");}
function thumbSrc(f,c){return c.thumb?`/img/${f.id}/${c.thumb}`:c.url;}
function render(){
  const flt=$("#filter").value, L=$("#list"); L.innerHTML="";
  const shown=D.films.filter(f=>flt==="all"||(flt==="ok")===!!f.approved);
  $("#count").textContent=`${D.films.filter(f=>f.approved).length} of ${D.films.length} approved · showing ${shown.length}`;
  for(const f of shown){
    const el=document.createElement("section"); el.className="film";
    el.innerHTML=`<div class="head"><b></b><span class="orig"></span><span class="state"></span></div><div class="why"></div><div class="row"></div>
      <div class="tools"><a target="_blank" rel="noopener">Find more on the web</a>
      <input placeholder="Paste an image address (right-click a poster > Copy image address)"><button class="add">Add</button>
      <button class="re">AI pick again</button><span class="count busy"></span></div>`;
    const a=el.querySelector(".tools a"); a.href="https://www.google.com/search?tbm=isch&q="+encodeURIComponent(f.search||f.title);
    const inp=el.querySelector(".tools input"), busy=el.querySelector(".busy");
    el.querySelector(".add").onclick=()=>{const u=inp.value.trim(); if(!u.startsWith("http")) return; busy.textContent="adding…";
      post("/add",{id:f.id,url:u}).then(r=>r.ok?r.json():r.text().then(t=>{throw t})).then(c=>{f.candidates.push(c);f.choice=c.url;f.approved=true;f.by="owner";render();})
      .catch(e=>{busy.textContent="could not load that image: "+e;});};
    el.querySelector(".re").onclick=()=>{busy.textContent="AI is looking…";
      post("/rerank",{id:f.id}).then(r=>r.ok?r.json():r.text().then(t=>{throw t})).then(nf=>{Object.assign(f,nf);render();})
      .catch(e=>{busy.textContent="AI error: "+e;});};
    el.querySelector("b").textContent=`${f.title} (${f.year})`;
    el.querySelector(".orig").textContent=f.orig||"";
    el.querySelector(".why").textContent=(f.ai&&f.ai.reason)||"";
    const st=el.querySelector(".state");
    st.innerHTML=f.approved?`<span class="ok">approved</span> <button>undo</button>`:`<button>Approve this choice</button>`;
    st.querySelector("button").onclick=()=>{f.approved=!f.approved;f.by="owner";save(f);render();};
    const row=el.querySelector(".row");
    const cur=document.createElement("figure"); cur.className="cur"+(f.choice===""?" sel":"");
    cur.innerHTML=f.current_thumb?`<img alt="current"><figcaption>now in Plex${f.current.startsWith("media://")?" – video frame":""}</figcaption>`
      :`<div class="none">no poster</div><figcaption>now in Plex – nothing</figcaption>`;
    if(f.current_thumb) cur.querySelector("img").src=`/img/${f.id}/${f.current_thumb}`;
    cur.onclick=()=>{f.choice="";f.by="owner";save(f);render();};
    row.append(cur); const sep=document.createElement("div"); sep.className="sep"; row.append(sep);
    f.candidates.forEach((c,i)=>{const fig=document.createElement("figure");
      fig.className=(c.rank===1?"ai ":"")+(f.choice===c.url?"sel":"");
      fig.innerHTML=`<img loading="lazy" alt=""><figcaption><span class="rk"></span><span class="src"></span></figcaption>`;
      fig.querySelector("img").src=thumbSrc(f,c); fig.querySelector(".rk").textContent=rankText(c);
      fig.querySelector(".src").textContent=cap(c,i); fig.title="click = choose · double-click = see it large";
      fig.onclick=()=>{f.choice=c.url;f.approved=true;f.by="owner";save(f);render();};
      fig.ondblclick=()=>window.open(c.url.startsWith("http")?c.url:thumbSrc(f,c),"_blank");
      row.append(fig);});
    L.append(el);}
}
$("#filter").onchange=render;
$("#allok").onclick=()=>{const flt=$("#filter").value;
  for(const f of D.films) if(flt==="all"||(flt==="ok")===!!f.approved){ if(f.choice&&!f.approved){f.approved=true;f.by="owner";save(f);} } render();};
$("#done").onclick=()=>fetch("/quit",{method:"POST"}).then(()=>{document.body.innerHTML="<main><h1>Review saved – you can close this tab.</h1></main>";});
render();
</script></body></html>"""


def cmd_posters_review(a):
    import http.server
    import webbrowser
    folder = Path(a.folder)
    plan = plan_path(folder)
    if not plan.exists():
        sys.exit(f"No poster plan yet: {plan} - run 'posters-plan' first.")
    data = json.loads(plan.read_text(encoding="utf-8"))
    lock = threading.Lock()
    made = {}

    def tools():  # Plex API and the AI, created on first use (only 'Add' / 'AI pick again' need them)
        with lock:
            if not made:
                made.update(api=PlexAPI(), ai=PosterAI())
        return made["api"], made["ai"]

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                page = PAGE.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
                return self.send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            m = re.fullmatch(r"/img/(\d+)/([0-9a-f]{16}\.jpg|current\.jpg)", self.path)
            f = POSTER_CACHE / m.group(1) / m.group(2) if m else None
            if f and f.is_file():
                return self.send(200, f.read_bytes(), "image/jpeg")
            self.send(404)

        def do_POST(self):
            if self.path == "/quit":
                self.send(200)
                threading.Thread(target=srv.shutdown, daemon=True).start()
                return
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            film = next((f for f in data["films"] if f["id"] == body.get("id")), None)
            if film is None:
                return self.send(404)
            try:
                if self.path == "/save":
                    film.update(choice=body.get("choice", ""), approved=bool(body.get("approved")), by="owner")
                    reply = b""
                elif self.path == "/add":  # an image address the owner found on the web
                    u = str(body.get("url", "")).strip()
                    if not u.startswith("http"):
                        return self.send(400, b"not a web address")
                    c = dict(url=u, key=u, provider="web", lang="")
                    cache_thumb(film["id"], c, tools()[0])
                    film["candidates"].append(c)
                    film.update(choice=u, approved=True, by="owner")
                    reply = json.dumps(c, ensure_ascii=False).encode("utf-8")
                elif self.path == "/rerank":  # 'AI pick again' (e.g. after adding images)
                    api, ai = tools()
                    top = rank_film(film, ai, data.get("lang", "ja"))
                    ai.save()
                    film.update(choice=top["url"] if top else film["choice"], approved=False, by="owner")
                    reply = json.dumps(film, ensure_ascii=False).encode("utf-8")
                else:
                    return self.send(404)
            except Exception as e:
                return self.send(500, str(e)[:300].encode("utf-8"))
            with lock:
                plan.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            self.send(200, reply, "application/json; charset=utf-8")

    for port in range(8765, 8785):
        try:
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
            break
        except OSError:
            continue
    url = f"http://127.0.0.1:{port}/"
    print(f"Review page      : {url}  (choices are saved into {plan.name} as you click)")
    print("Click 'Done - close review' on the page when finished.")
    webbrowser.open(url)
    srv.serve_forever()
    print("Review closed.")


# ---------------------------------------------------------------- posters-apply / posters-undo

def cmd_posters_apply(a):
    folder = Path(a.folder)
    plan = plan_path(folder)
    data = json.loads(plan.read_text(encoding="utf-8"))
    sid = data["section"]
    tv = section_is_tv(sid)
    now = {f["id"]: f for f in library_items(sid)}  # current state, the plan may be older
    todo = [f for f in data["films"] if f["approved"] and f["choice"] and f["id"] in now
            and now[f["id"]]["current"] != f["choice"]]
    print(f"Poster plan      : {plan.name}  ({data['library']}{', TV shows' if tv else ''})")
    print(f"Approved changes : {len(todo)} {'shows' if tv else 'films'}  (of {len(data['films'])} in the plan)")
    for f in todo[:15]:
        print(f"   {f['title']} ({f['year']})  <- {f['choice'][:80]}")
    if len(todo) > 15:
        print(f"   ... and {len(todo) - 15} more")
    if not a.go:
        print("\nDry run - nothing changed. Use DO IT (--go) to set these posters in Plex and lock them.")
        return
    api = PlexAPI()
    pn.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = pn.LOG_DIR / f"{plan.stem}_applied_{dt.datetime.now():%Y-%m-%d_%H%M%S}.csv"
    err = 0
    with open(log, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["id", "title", "section", "old", "old_locked", "new", "result", "tv"])
        w.writeheader()
        for i, f in enumerate(todo, 1):
            old = now[f["id"]]
            row = dict(id=f["id"], title=f["title"], section=sid, old=old["current"], old_locked=int(old["locked"]),
                       new=f["choice"], tv=int(tv))
            try:
                set_poster(api, sid, f["id"], url=f["choice"], tv=tv)
                row["result"] = "ok"
            except Exception as e:
                err += 1
                row["result"] = f"ERROR {e}"[:300]
            w.writerow(row)
            fh.flush()
            if i % 25 == 0:
                print(f"   ... {i}/{len(todo)}", flush=True)
    print(f"\nDone: {len(todo) - err} posters set and locked, {err} errors. Log: {log}")


def cmd_posters_quick(a):
    """Quick fix without AI or review (owner, 28 Sep): every MATCHED film that shows a video frame or no poster gets
    the first real poster of Plex's own list (what Edit > Poster offers; the first is the one Plex's agent
    downloaded) - set and locked. Same log as posters-apply, so posters-undo reverses it."""
    folder = Path(a.folder)
    sid, name, root = find_section(folder)
    items = library_items(sid)
    todo = [f for f in items if f["matched"] and is_frame(f["current"])]
    if a.only:
        todo = [f for f in todo if a.only.casefold() in (f["title"] + " " + f["folder"]).casefold()]
    if a.limit:
        todo = todo[: a.limit]
    print(f"Plex library     : {name} (section {sid})")
    print(f"Films            : {len(items)}; matched but showing a video frame or no poster: "
          f"{sum(1 for f in items if f['matched'] and is_frame(f['current']))}; planned now: {len(todo)}")
    print(f"Not matched      : {sum(not f['matched'] for f in items)} (no Plex posters - fix these with 'Fix one film')")
    api = PlexAPI()
    picks, none = [], []
    for i, f in enumerate(todo, 1):
        first = next((m.get("ratingKey") for m in api.get(f"/library/metadata/{f['id']}/posters").get("Metadata", [])
                      if m.get("ratingKey") and not is_frame(m["ratingKey"])), None)
        (picks if first else none).append((f, first))
        if i % 50 == 0:
            print(f"   ... {i}/{len(todo)} checked", flush=True)
    print(f"Plex has a poster: {len(picks)} films;  Plex has none: {len(none)} (use tool 3 for those)")
    for f, u in picks[:15]:
        print(f"   {f['title']} ({f['year']})  <- {u[:70]}")
    if len(picks) > 15:
        print(f"   ... and {len(picks) - 15} more")
    for f, _ in none[:10]:
        print(f"   no Plex poster: {f['title']} ({f['year']})")
    if not a.go:
        print(f"\nDry run - nothing changed. DO IT sets these {len(picks)} posters in Plex and locks them.")
        return
    pn.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = pn.LOG_DIR / f"{re.sub(r'[^0-9A-Za-z]+', '_', name)}_posters_applied_{dt.datetime.now():%Y-%m-%d_%H%M%S}.csv"
    err = 0
    with open(log, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["id", "title", "section", "old", "old_locked", "new", "result"])
        w.writeheader()
        for i, (f, u) in enumerate(picks, 1):
            row = dict(id=f["id"], title=f["title"], section=sid, old=f["current"], old_locked=int(f["locked"]), new=u)
            try:
                set_poster(api, sid, f["id"], url=u, tv=section_is_tv(sid))
                row["result"] = "ok"
            except Exception as e:
                err += 1
                row["result"] = f"ERROR {e}"[:300]
            w.writerow(row)
            fh.flush()
            if i % 50 == 0:
                print(f"   ... {i}/{len(picks)} set", flush=True)
    print(f"\nDone: {len(picks) - err} posters set and locked, {err} errors. Log: {log}")


# ---------------------------------------------------------------- quick plan: OCR first, AI only when unclear
# Owner (28 Sep): for well-matched English libraries pick from Plex's own list; local Tesseract counts readable
# words (cast names, tagline), the AI decides only when the OCR result is not clear.

TESSERACT = config.TESSERACT
OCR_MIN_WORDS = 4      # a clear OCR winner needs at least this many believable words ...
OCR_MARGIN = 2.0       # ... and at least this many times the words of the next poster
_VOWELS = set("aeiouyAEIOUY")


def ocr_words(img: bytes) -> list:
    """Believable words Tesseract reads on a poster: confident, 3+ letters, with a vowel, sane capitalisation
    (poster artwork otherwise produces 'words' like 'Mas DEF if DA Vin')."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as t:
        t.write(img)
    try:
        out = subprocess.run([str(TESSERACT), t.name, "stdout", "--psm", "11", "tsv"], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=120,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    finally:
        os.unlink(t.name)
    words = []
    for r in csv.DictReader(io.StringIO(out), delimiter="\t", quoting=csv.QUOTE_NONE):
        w = (r.get("text") or "").strip(".,:;!?'\"“”‘’()-–")
        try:
            conf = float(r.get("conf") or -1)
        except ValueError:
            continue
        if conf >= 80 and len(w) >= 3 and w.isalpha() and _VOWELS & set(w) and (w.isupper() or w.istitle() or w.islower()):
            words.append(w)
    return words


def quick_one(film, api, ai, lang, first_only=False):
    """Candidates = Plex's own list only (no frames), max 8. OCR each; clear winner -> chosen, else the AI ranks.
    first_only (method C): no OCR, no AI - Plex's first real poster is chosen and approved."""
    lst = [m for m in api.get(f"/library/metadata/{film['id']}/posters").get("Metadata", [])
           if m.get("ratingKey") and not is_frame(m["ratingKey"])][:8]
    seen, keep = set(), []
    for m in lst:
        c = dict(url=m["ratingKey"], key=m.get("key") or m["ratingKey"], provider=m.get("provider") or "plex", lang="")
        try:
            b = cache_thumb(film["id"], c, api)
        except Exception:
            continue
        h = hashlib.md5(b).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        if not first_only:
            try:
                words = ocr_words(api.thumb(c["key"], 1000, 1500))  # larger copy: cast lines are small
            except Exception:
                words = []
            c.update(ocr=len(words), ocr_text=" ".join(words)[:120])
        keep.append(c)
    film["candidates"] = keep
    cur = POSTER_CACHE / str(film["id"]) / "current.jpg"
    if film["current"] and not cur.exists():
        try:
            cur.write_bytes(api.thumb(f"/library/metadata/{film['id']}/thumb"))
        except Exception:
            pass
    film["current_thumb"] = "current.jpg" if cur.exists() else ""
    film.update(choice="", approved=False, by="", ai={}, ai_best="", decided_by="",
                search=f"{film['title']} {film['year']} poster")
    if not keep:
        film["ai"] = dict(reason="Plex has no poster for this film - use 'Find more on the web' and add one.")
        return film
    if first_only:
        film.update(choice=keep[0]["url"], approved=True, decided_by="first")
        keep[0].update(rank=1, kind="Plex's first poster")
        film["ai"] = dict(reason=f"Plex's first poster ({keep[0]['provider']}).")
        return film
    ranked = sorted(keep, key=lambda c: -c["ocr"])
    top, second = ranked[0], (ranked[1]["ocr"] if len(ranked) > 1 else 0)
    if top["ocr"] >= OCR_MIN_WORDS and top["ocr"] >= OCR_MARGIN * max(second, 1):
        film.update(choice=top["url"], approved=True, decided_by="ocr")
        top.update(rank=1, kind="most readable text")
        film["ai"] = dict(reason=f"OCR: clear winner with {top['ocr']} readable words ({top['ocr_text'][:70]}); "
                                 f"next best {second}.")
        return film
    if len(keep) == 1:
        film.update(choice=keep[0]["url"], approved=True, decided_by="only one")
        film["ai"] = dict(reason="Only one poster in Plex's list.")
        return film
    if ai is None:
        film.update(choice=keep[0]["url"], decided_by="first (no AI)")
        film["ai"] = dict(reason="OCR not clear; AI switched off - Plex's first poster pre-selected.")
        return film
    try:
        best = rank_film(film, ai, lang)
    except Exception as e:
        best = None
        film["ai"] = dict(reason=f"AI error: {e}"[:200])
    if best:
        film.update(choice=best["url"], decided_by="ai",
                    approved=film["ai"].get("confidence") == "high" and best.get("kind") == "original poster")
        film["ai"]["reason"] = "OCR not clear -> AI: " + film["ai"].get("reason", "")
    return film


def cmd_posters_quickplan(a):
    folder = Path(a.folder)
    if not a.first_only and not TESSERACT.exists():
        sys.exit(f"Tesseract OCR is not installed ({TESSERACT}). Install it (see the Setup tab) or use method C.")
    sid, name, root = find_section(folder)
    items = library_items(sid)
    todo = [f for f in items if f["matched"] and (a.all or is_frame(f["current"]))]
    if a.only:
        todo = [f for f in todo if a.only.casefold() in (f["title"] + " " + f["folder"]).casefold()]
    if a.limit:
        todo = todo[: a.limit]
    tv = section_is_tv(sid)
    print(f"Plex library     : {name} (section {sid}) - {'TV series: show posters' if tv else 'films'}")
    print(f"Method           : {'Plex first poster (no OCR, no AI)' if a.first_only else 'OCR first, AI when unclear'}")
    print(f"{'Shows' if tv else 'Films'}            : {len(items)}; matched but showing no poster (or a video frame): "
          f"{sum(1 for f in items if f['matched'] and is_frame(f['current']))}; planned now: {len(todo)}")
    print(f"Not matched      : {sum(not f['matched'] for f in items)} (no Plex posters - match them first with tool 2)")
    out = plan_path(folder)
    prev = {}
    if out.exists():
        prev = {f["id"]: f for f in json.loads(out.read_text(encoding="utf-8"))["films"] if f.get("by") == "owner"}
    api = PlexAPI()
    ai = None if (a.no_ai or a.first_only) else PosterAI()
    films = []
    pn.PLAN_DIR.mkdir(parents=True, exist_ok=True)
    pn.backup_plan(out)

    def write(complete):
        out.write_text(json.dumps(dict(library=name, section=sid, folder=str(folder), lang=a.lang, complete=complete,
                                       kind="first" if a.first_only else "quick", tv=tv,
                                       made=dt.datetime.now().isoformat(timespec="minutes"),
                                       films=films), ensure_ascii=False, indent=1), encoding="utf-8")

    with ThreadPoolExecutor(max_workers=4) as ex:
        for film in ex.map(lambda f: quick_one(f, api, ai, a.lang, a.first_only), todo):
            p = prev.get(film["id"])
            if p and (not p["choice"] or p["choice"] in {c["url"] for c in film["candidates"]}):
                film.update(choice=p["choice"], approved=p["approved"], by="owner")
            films.append(film)
            if len(films) % 10 == 0:
                print(f"   ... {len(films)}/{len(todo)} films (saved)", flush=True)
                write(False)
                if ai:
                    ai.save()
    if ai:
        ai.save()
    write(True)
    by = defaultdict(int)
    for f in films:
        by[f["decided_by"] or "no poster"] += 1
    print(f"Decided by       : " + ", ".join(f"{k} {v}" for k, v in sorted(by.items())))
    print(f"Approved         : {sum(f['approved'] for f in films)}  (OCR clear winners, only poster, clear AI picks)")
    print(f"To check         : {sum(not f['approved'] for f in films)}")
    if ai and ai.tokens["calls"]:
        print(f"AI calls         : {ai.tokens['calls']}  (tokens: {ai.tokens['prompt']:,} in, "
              f"{ai.tokens['completion']:,} out)")
    print(f"Plan written     : {out}")


def cmd_posters_undo(a):
    with open(a.log, encoding="utf-8-sig", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r["result"] == "ok"]
    print(f"Undo {Path(a.log).name}: {len(rows)} posters to put back")
    if not a.go:
        print("Dry run - nothing changed.")
        return
    api, err = PlexAPI(), 0
    for r in rows:
        try:
            if r["old"]:
                api.put(f"/library/metadata/{r['id']}/poster", url=r["old"])
            api.put(f"/library/sections/{r['section']}/all", type=2 if r.get("tv") == "1" else 1, id=r["id"],
                    **{"thumb.locked": r["old_locked"]})
        except Exception as e:
            err += 1
            print(f"   ! {r['title']}: {e}")
    print(f"Undo finished, {err} problems.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("posters-plan", help="find poster candidates and let the AI rank them (changes nothing)")
    p.add_argument("--folder", required=True, help="the library folder, e.g. V:\\Japanese\\Samurai")
    p.add_argument("--lang", choices=sorted(pn.LANGS), required=True)
    p.add_argument("--all", action="store_true", help="all films, not only those showing a video frame")
    p.add_argument("--only", help="only films whose title or folder contains this text")
    p.add_argument("--limit", type=int)
    p.add_argument("--no-ai", action="store_true")
    p.set_defaults(func=cmd_posters_plan)
    p = sub.add_parser("posters-review", help="open the review page for the poster plan")
    p.add_argument("--folder", required=True)
    p.set_defaults(func=cmd_posters_review)
    p = sub.add_parser("posters-apply", help="set approved posters in Plex and lock them (dry run unless --go)")
    p.add_argument("--folder", required=True)
    p.add_argument("--go", action="store_true")
    p.set_defaults(func=cmd_posters_apply)
    p = sub.add_parser("posters-quick", help="matched films showing a frame/no poster get Plex's own first poster "
                                             "(no AI, no review; dry run unless --go)")
    p.add_argument("--folder", required=True)
    p.add_argument("--only")
    p.add_argument("--limit", type=int)
    p.add_argument("--go", action="store_true")
    p.set_defaults(func=cmd_posters_quick)
    p = sub.add_parser("posters-quickplan", help="matched films showing a frame/no poster: Plex's own posters, OCR "
                                                 "(Tesseract) picks the one with cast/tagline text, AI when unclear "
                                                 "-> poster plan for the Review tab (changes nothing)")
    p.add_argument("--folder", required=True)
    p.add_argument("--lang", default="en", help="language for the AI's rules (default en)")
    p.add_argument("--only")
    p.add_argument("--limit", type=int)
    p.add_argument("--no-ai", action="store_true")
    p.add_argument("--first-only", action="store_true", help="method C: Plex's first poster, no OCR, no AI")
    p.add_argument("--all", action="store_true", help="all matched films, not only those showing a frame")
    p.set_defaults(func=cmd_posters_quickplan)
    p = sub.add_parser("posters-undo", help="put the previous posters back (dry run unless --go)")
    p.add_argument("log")
    p.add_argument("--go", action="store_true")
    p.set_defaults(func=cmd_posters_undo)
    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
