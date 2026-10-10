"""plextv - rename TV-series folders for Plex (tool 1 with 'TV series'). The film tools are not changed.

One library folder per language, e.g. U:\\War Series (Russia): every subfolder is one show.
    tv-plan ROOT --lang RU        -> plans\\<name>_tvrename.xlsx   (review in Excel like the film plans)
    tv-copy PLAN [--go]           -> "<ROOT> (Plex)"                (original untouched; dry run unless --go)

Target (Plex standard, agreed with the owner):
    Show (Year) {tmdb-ID} Native title\\Season 01\\Show (Year) - s01e03 - Episode title [RES].ext
    (+ .ru.srt etc. with the same stem; extras in the show's Featurettes / Trailers folder; specials = Season 00)
Identity (never invented): ids in the folder name -> Plex's own match -> TMDB TV search in the folder's language
(Cyrillic / Latin parts of the name) -> Plex's TV service (knows IMDb-only shows). Unsure = blank row for review.
"""
import argparse
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import plexname as pn

TVFIELDS = ["Apply", "Kind", "Folder", "File", "Season", "Episode", "NewFolder", "NewFile", "EpisodeTitle", "Title",
            "OriginalTitle", "Year", "TMDB", "IMDb", "Res", "Source", "Note", "Root", "ToolFolder", "ToolFile"]
EXTRA_DIR_RE = re.compile(r"(?i)\b(featurettes?|extras?|bonus|behind the scenes|trailers?|interviews?|deleted scenes)\b")
EXTRA_FILE_RE = re.compile(r"(?i)\b(trailer|teaser|featurettes?|documentary|making of|behind the scenes|commentary|"
                           r"master ?class|interview|bonus|extra)\b")
ALL_EPISODES_RE = re.compile(r"(?i)все\s*серии|all\s*episodes|complete\s*series|смотреть\s*все")
JUNK_RE = re.compile(r"(?i)\b(19|20)\d{2}\b|\b\d{3,4}[pi]\b|[xh]\.?26[45]|\b(ddp?|aac|ac3|dts|e-?ac3)[ ._]?\d([ ._]\d)?\b|"
                     r"\b\d+(\.\d+)?\s*gb\b|\b10bit\b|\b\d\.\d\b|"
                     r"\b(hdtv\w*|web-?dl\w*|webrip|dvdrip|bdrip|bluray|hdrip|avc|proper|repack|multi)\b")
ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}
WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


# ---------------------------------------------------------------- episode numbers from file names

def parse_episode(stem: str, folder_hint: str = ""):
    """(season or None, first episode, last episode) from a file name, or None. Knows S01E03, s01e01-e04, 1x03,
    E03 / EP03 / Episode 3 / Episodio 3 / серия 3 / (3 сер.), Part I-X / Part One, E01-E04, '03_…', '04. Title',
    'Title.03.2020', 'Title 03'. Years, resolutions and codec numbers are ignored."""
    s = stem.replace("_", " ")
    m = re.search(r"(?i)s(\d{1,2})[ ._-]*e(\d{1,3})(?:[ ._-]*-?[ ._-]*e(\d{1,3}))?", s)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3) or m.group(2))
    m = re.search(r"(?i)\b(\d{1,2})x(\d{2,3})\b", s)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(2))
    c = JUNK_RE.sub(" ", s)
    m = re.search(r"(?i)\be(\d{1,3})\s*-\s*e(\d{1,3})\b", c)
    if m:
        return None, int(m.group(1)), int(m.group(2))
    for m in re.finditer(r"(?i)(?:^|[ .\-\[(])(?:e|ep|episode|episodio|серия|эпизод)[ .\-]*(\d{1,3})\b", c):
        if int(m.group(1)) > 0:  # 'E0 6Private Schulz - Episode 6': skip the typo'd 'E0', use 'Episode 6'
            return None, int(m.group(1)), int(m.group(1))
    m = re.search(r"(?i)\b(\d{1,3})\s*(?:-?я\s*)?(?:серия|сер)\b", c)
    if m:
        return None, int(m.group(1)), int(m.group(1))
    m = re.search(r"(?i)\b(?:part|часть|film)[ .\-]*(\d{1,2}|[ivx]{1,4}|one|two|three|four|five|six|seven|eight|nine|ten)\b",
                  c)
    if m:
        v = m.group(1).lower()
        n = int(v) if v.isdigit() else ROMAN.get(v) or WORDS.get(v)
        if n:
            return None, n, n
    for pat in (r"^\s*(\d{1,3})\s*[ .\-]", r"[ .](\d{1,2})[ .]+$", r"[ .](\d{1,3})\s*$"):
        m = re.search(pat, c)
        if m and int(m.group(1)) > 0:
            return None, int(m.group(1)), int(m.group(1))
    return None


def folder_season(path: Path, show_dir: Path):
    """Season from the folders between the show folder and the file ('Season 2', 'S02'), or None."""
    for part in path.relative_to(show_dir).parts[:-1]:
        m = re.search(r"(?i)\b(?:season|сезон)\s*(\d{1,2})\b|\bs(\d{2})\b", part)
        if m:
            return int(m.group(1) or m.group(2))
    return None


# ---------------------------------------------------------------- which show is it?

def name_parts(name: str):
    """Search texts from a folder name: Cyrillic part, Latin part(s), whole cleaned name; plus the year."""
    y = re.search(r"(?<!\d)(19|20)\d{2}(?!\d)", name)
    n = re.sub(r"[\[({][^\])}]*[\])}]", " ", name)
    n = re.sub(r"(?i)\b(19|20)\d{2}\b|\b\d{3,4}[pi]\b|\bS\d{2}\b|WEB-?DL|HDTV\w*|BluRay|DVDRip|RHS|shevanlk|the series\d*",
               " ", n)
    n = re.sub(r"[._]+", " ", n).replace("📁", " ")
    cyr = " ".join(re.findall(r"[А-Яа-яЁё][А-Яа-яЁё0-9«»\-\s]*", n)).strip()
    lat = re.sub(r"[А-Яа-яЁё«»]+", " ", n)
    parts = [p.strip(" -,)") for p in re.split(r"\s{2,}|\s-\s", lat) if len(p.strip(" -,)")) > 1]
    out = [cyr] if cyr else []
    out += parts + [re.sub(r"\s+", " ", lat).strip(" -,)")]
    seen, texts = set(), []
    for t in out:
        if t and t.casefold() not in seen:
            seen.add(t.casefold())
            texts.append(t)
    return texts, (y.group(0) if y else "")


_TRANSLIT = [("shch", "щ"), ("sch", "щ"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("ch", "ч"), ("sh", "ш"),
             ("yu", "ю"), ("ya", "я"), ("yo", "ё"), ("ye", "е"), ("iyu", "ию"), ("iya", "ия"), ("iy", "ий"), ("yy", "ый"), ("a", "а"), ("b", "б"),
             ("v", "в"), ("g", "г"), ("d", "д"), ("e", "е"), ("z", "з"), ("i", "и"), ("k", "к"), ("l", "л"),
             ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"), ("r", "р"), ("s", "с"), ("t", "т"), ("u", "у"),
             ("f", "ф"), ("h", "х"), ("c", "к"), ("w", "в"), ("x", "кс"), ("q", "к")]


def tw(s: str) -> list:
    """Words of a title for comparing - keeps Cyrillic, CJK and other letters (plexname.words_of keeps only a-z/0-9,
    so two Cyrillic titles would both be empty and look 'equal')."""
    return [w for w in re.findall(r"\w+", (s or "").casefold().replace("ё", "е")) if w not in pn.SMALL_WORDS]


def similar(a: str, b: str) -> float:
    import difflib
    x, y = " ".join(tw(a)), " ".join(tw(b))
    return difflib.SequenceMatcher(None, x, y).ratio() if x and y else 0.0


def to_cyrillic(text: str) -> str:
    """Rough reverse transliteration ('Molchanie' -> 'молчание', 'Pryzhok bogomola' -> 'прыжок богомола'), used only
    as an extra TMDB search text for Russian folders whose names are transliterated."""
    out = []
    for word in re.findall(r"[A-Za-z']+", text.lower().replace("'", "")):
        w, res = word, ""
        while w:
            if w[:2] in ("ya", "yu", "yo", "ye"):  # 'Poyas' -> пояс (before the single-'y' rule)
                res += {"ya": "я", "yu": "ю", "yo": "ё", "ye": "е"}[w[:2]]
                w = w[2:]
                continue
            if w[0] == "y":  # 'y' after a vowel = й, otherwise ы
                res += "й" if res and res[-1] in "аеёиоуыэюя" else "ы"
                w = w[1:]
                continue
            for lat, cyr in _TRANSLIT:
                if w.startswith(lat):
                    res += cyr
                    w = w[len(lat):]
                    break
            else:
                w = w[1:]
        out.append(res)
    return " ".join(out)


def tv_details(tmdb, tid):
    k = f"tv:{tid}"
    if k not in tmdb.cache:
        try:
            j = tmdb._get(f"tv/{tid}", language=tmdb.lang, append_to_response="external_ids")
            tmdb.cache[k] = dict(id=str(j["id"]), title=j.get("name") or "", orig=j.get("original_name") or "",
                                 year=(j.get("first_air_date") or "")[:4], lang=j.get("original_language") or "",
                                 imdb=(j.get("external_ids") or {}).get("imdb_id") or "",
                                 seasons={str(s["season_number"]): s.get("episode_count", 0) for s in j.get("seasons", [])})
        except Exception:
            tmdb.cache[k] = None
    return tmdb.cache[k]


def tv_season(tmdb, tid, n):
    """{episode number: title} of a TMDB season."""
    k = f"tvseason:{tid}:{n}"
    if k not in tmdb.cache:
        try:
            j = tmdb._get(f"tv/{tid}/season/{n}", language=tmdb.lang)
            tmdb.cache[k] = {str(e["episode_number"]): e.get("name") or "" for e in j.get("episodes", [])}
        except Exception:
            tmdb.cache[k] = {}
    return {int(e): t for e, t in tmdb.cache[k].items()}


def tv_search(tmdb, text, year, lang):
    k = f"tvsearch:{text}|{year}|{lang}"
    if k not in tmdb.cache:
        params = dict(query=text, language=tmdb.lang)
        if year:
            params["first_air_date_year"] = year
        res = tmdb._get("search/tv", **params).get("results", [])
        if year and not res:
            res = tmdb._get("search/tv", query=text, language=tmdb.lang).get("results", [])
        tmdb.cache[k] = [dict(id=str(r["id"]), name=r.get("name") or "", orig=r.get("original_name") or "",
                              year=(r.get("first_air_date") or "")[:4], lang=r.get("original_language") or "")
                         for r in res[:10]]
    return tmdb.cache[k]


def identify_show(d: Path, lang: str, tmdb, plex_ids: dict):
    """(TMDB show dict or None, source, confidence note)."""
    m = re.search(r"tmdb-(\d+)", d.name)
    if m:
        s = tv_details(tmdb, m.group(1))
        if s:
            return s, "folder id", ""
        return None, "", f"the folder's tmdb-{m.group(1)} is not a TV show on TMDB (a film id?) - check"
    m = re.search(r"(tt\d{6,10})", d.name)
    if m:
        tid = str(((tmdb._get(f"find/{m.group(1)}", external_source="imdb_id").get("tv_results") or [{}])[0]).get("id", ""))
        if tid:
            return tv_details(tmdb, tid), "folder imdb id", ""
    if plex_ids.get(d.name):
        s = tv_details(tmdb, plex_ids[d.name])
        if s:
            return s, "plex", ""
    texts, year = name_parts(d.name)
    if lang == "ru":  # transliterated folder names: also search the Cyrillic spelling
        texts += [c for c in (to_cyrillic(t) for t in texts if not re.search("[А-Яа-я]", t)) if c and c not in texts]
    same_lang = lambda r:not lang or r["lang"] == lang or (lang == "zh" and r["lang"] in ("cn", "zh"))
    near = lambda r: not year or (r["year"] and abs(int(r["year"]) - int(year)) <= 1)  # 'Omega 2019' != 1975
    exact_title = lambda r, t: bool(tw(t)) and (tw(r["name"]) == tw(t) or tw(r["orig"]) == tw(t))
    found, other = {}, {}
    for t in texts:
        for r in tv_search(tmdb, t, year, lang):
            if not near(r):
                continue
            if same_lang(r):  # a Russian folder prefers Russian-language shows (filters look-alike titles)
                found.setdefault(r["id"], dict(r, q=t))
            elif exact_title(r, t):  # e.g. the American 'Generation Kill' in a Russian folder
                other.setdefault(r["id"], dict(r, q=t))
        if found:
            break
    source, note0 = "tmdb-search", ""
    if not found and other:
        found, note0 = other, "show in another language than the folder - check"
    if not found:  # transliterated names ('Molchanie', 'Orden'): Plex's own TV service often knows them
        try:
            import plexfix as pf
            api = pf.PlexAPI()
            for t in texts:
                for r in pf.plex_search(api, None, t, year, tv=True)[:5]:
                    if not near(r):
                        continue
                    dd = pf.plex_details(api, r["guid"])
                    if dd.get("tmdb"):
                        s = tv_details(tmdb, dd["tmdb"])
                        c = s and dict(id=s["id"], name=s["title"], orig=s["orig"], year=s["year"], lang=s["lang"], q=t)
                        alike = c and max(similar(x, q) for x in (c["name"], c["orig"]) for q in texts) >= 0.7
                        if c and alike and (same_lang(c) or exact_title(c, t)):  # same checks as the TMDB search
                            found.setdefault(s["id"], c)
                if found:
                    source = "plex-tv"
                    break
        except Exception:
            pass
    if not found:
        return None, "", "not found on TMDB - search the web (Fix one film) or type the names yourself"
    cands = list(found.values())
    exact = [r for r in cands if year and r["year"] == year]
    titled = [r for r in (exact or cands) if exact_title(r, r["q"])]
    if not titled:  # 'Battalions' vs TMDB's 'Batallions': nearly the same title counts
        titled = [r for r in (exact or cands) if max(similar(x, r["q"]) for x in (r["name"], r["orig"])) >= 0.85]
    pick = (titled[0] if len(titled) == 1 else exact[0] if len(exact) == 1 else cands[0] if len(cands) == 1 else None)
    if not pick:
        return None, "", "several TMDB shows fit: " + "; ".join(f"{r['name']} ({r['year']}) tmdb-{r['id']}"
                                                                  for r in cands[:4])
    note = note0 or ("" if pick in exact else f"TMDB year {pick['year']} vs folder year {year or '-'} - check")
    return tv_details(tmdb, pick["id"]), source, note


def english_title(show: dict, folder_name: str) -> str:
    """Owner's rule (29 Sep): when TMDB has no English title (title = the Cyrillic/native original), use the English
    name written in the folder (e.g. "Orion's Belt (2020) Poyas Oriona" -> "Orion's Belt"); the native title follows."""
    t = show.get("title", "")
    if t and tw(t) != tw(show.get("orig", "")) or not re.search(r"[^\x00-\x7f]", t or ""):
        return t  # TMDB has an English title (or the title is Latin anyway)
    texts, _ = name_parts(folder_name)
    lat = [x for x in texts if re.search("[A-Za-z]{3}", x) and not re.search("[^\x00-\x7f]", x)]
    return lat[0] if lat else t


def show_folder_name(show: dict, folder_name: str) -> str:
    title = english_title(show, folder_name)
    orig = show.get("orig", "")
    native = pn.safe_name(orig) if orig and tw(orig) != tw(title) else ""
    idtag = f"{{tmdb-{show['id']}}}" if show.get("id") else (f"{{imdb-{show['imdb']}}}" if show.get("imdb") else "")
    return pn.safe_name(" ".join(x for x in (f"{title} ({show.get('year', '')})" if show.get("year") else title,
                                             idtag, native) if x))


def episode_mapping(d: Path, files: list, title: str, year: str, tid: str, tmdb, res: dict) -> list:
    """[(relative old path, 'Season 01\\new file name' or '(… reason)')] for one show folder - used by the one-film
    tool in Series mode (same episode rules as tv-plan)."""
    videos = [p for p in files if pn.is_video(p)]
    extras = [p for p in videos if EXTRA_DIR_RE.search(str(p.parent.relative_to(d))) or EXTRA_FILE_RE.search(p.stem)]
    stems, out, used = {}, [], set()
    for p in videos:
        rel = str(p.relative_to(d))
        if p in extras:
            kind = "Trailers" if re.search(r"(?i)trailer|teaser", p.stem) else "Featurettes"
            out.append((rel, f"{kind}\\{pn.safe_name(p.stem)}{p.suffix.lower()}"))
            continue
        r = parse_episode(p.stem)
        if not r and tid:  # no number: try TMDB episode titles
            words = set(tw(p.stem.replace(".", " ").replace("_", " ")))
            for sn in range(0, 30):
                t = tv_season(tmdb, tid, sn)
                if not t and sn > 1:
                    break
                hit = next((e for e, x in t.items() if x and set(tw(x)) and set(tw(x)) <= words), None)
                if hit:
                    r = (sn, hit, hit)
                    break
        if not r:
            out.append((rel, "(no episode number found - rename by hand)"))
            continue
        s_, e1, e2 = r
        if s_ is None:
            book = re.search(r"(?i)\bbook\s*(\d)\b", p.stem)
            s_ = folder_season(p, d) or (int(book.group(1)) if book else 1)
        code = f"s{s_:02d}e{e1:02d}" + (f"-e{e2:02d}" if e2 != e1 else "")
        et = tv_season(tmdb, tid, s_).get(e1, "") if tid else ""
        et = re.sub(r"(?i)^(episode|серия)\s*\d+$", "", et or "").strip()
        stem = pn.safe_name(f"{pn.safe_name(title)} ({year}) - {code}" + (f" - {pn.safe_name(et)}" if et else "") +
                            (f" [{res.get(p)}]" if res.get(p) else "") + (" - version 2" if (s_, e1) in used else ""))
        used.add((s_, e1))
        stems[p] = f"Season {s_:02d}\\{stem}"
        out.append((rel, stems[p] + pn.out_ext(p)))
    eps = sorted(((pn.norm(p.stem), p) for p in stems), key=lambda t: -len(t[0]))
    for p in files:
        if pn.is_video(p):
            continue
        rel = str(p.relative_to(d))
        if p.suffix.lower() in pn.SUB_EXT:
            owner = next((v for s, v in eps if s and pn.norm(p.stem).startswith(s)), None) or \
                (next(iter(stems)) if len(stems) == 1 else None)
            if owner:
                code, flags = pn.sub_language(p)
                out.append((rel, stems[owner] + (f".{code}" if code else "") + flags + p.suffix.lower()))
            else:
                out.append((rel, "(subtitle not matched to an episode)"))
        else:
            out.append((rel, "(leftover - not copied)"))
    return out


def plex_show_ids(root: Path) -> dict:
    """{show folder name: TMDB id} from Plex's own TV matches of files under root (read-only)."""
    out = {}
    con = sqlite3.connect(f"file:{pn.PLEX_DB}?mode=ro", uri=True)
    for form in {pn.unc_path(root), str(root)}:
        q = """SELECT mp.file, (SELECT group_concat(t.tag, ' ') FROM taggings tg JOIN tags t ON t.id = tg.tag_id
                                 WHERE tg.metadata_item_id = sh.id AND t.tag LIKE 'tmdb://%')
               FROM media_parts mp JOIN media_items mi ON mi.id = mp.media_item_id
               JOIN metadata_items ep ON ep.id = mi.metadata_item_id AND ep.metadata_type = 4
               JOIN metadata_items se ON se.id = ep.parent_id JOIN metadata_items sh ON sh.id = se.parent_id
               WHERE mp.file LIKE ?"""
        for f, tags in con.execute(q, (form.rstrip("\\") + "\\%",)):
            m = re.search(r"tmdb://(\d+)", tags or "")
            if m:
                rel = f[len(form.rstrip("\\")) + 1:]
                out.setdefault(rel.split("\\")[0], m.group(1))
    return out


# ---------------------------------------------------------------- tv-plan

def plan_show(root: Path, d: Path, lang: str, tmdb, plex_ids, facts) -> list:
    files = sorted(p for p in d.rglob("*") if p.is_file())
    videos = [p for p in files if pn.is_video(p)]
    base = dict(Folder=d.name, Root=str(root))
    if not videos:
        return [dict(base, Apply="n/a", Kind="REVIEW", Note="no video file in this folder - nothing is copied")]
    show, source, why = identify_show(d, lang, tmdb, plex_ids)
    if show:
        title, year, orig = english_title(show, d.name), show["year"], show["orig"]
        show_dir = show_folder_name(show, d.name)
    else:
        (title, year), native = pn.clean_from_filename(d.name), ""
        show_dir = ""
    common = dict(base, Title=title, OriginalTitle=show["orig"] if show else "", Year=year,
                  TMDB=show["id"] if show else "", IMDb=show["imdb"] if show else "", Source=source)
    extras = [p for p in videos if EXTRA_DIR_RE.search(str(p.parent.relative_to(d))) or EXTRA_FILE_RE.search(p.stem)]
    eps = [p for p in videos if p not in extras]
    rows, stems = [], {}
    # one file without an episode number in the whole folder: a film, or all episodes in one file
    if len(eps) == 1 and not parse_episode(eps[0].stem):
        allin = bool(ALL_EPISODES_RE.search(eps[0].stem))
        n = (show or {}).get("seasons", {}).get("1", 0) if show else 0
        if allin and show and n:
            parsed = {eps[0]: (1, 1, n)}
        else:
            return [dict(common, Apply="", Kind="FILM", File=str(eps[0].relative_to(d)), Note=(
                "one file without an episode number - probably a FILM: belongs in a movie library (not copied). "
                "If it is all episodes in one file, type NewFolder/NewFile yourself and Y.")
                + (f" {why}" if why else ""))]
    else:
        parsed = {}
        for p in eps:
            r = parse_episode(p.stem)
            if r:
                s_, e1, e2 = r
                if s_ is None:
                    fs = folder_season(p, d)
                    book = re.search(r"(?i)\bbook\s*(\d)\b", p.stem)
                    s_ = fs or (int(book.group(1)) if book else 1)
                parsed[p] = (s_, e1, e2)
    # files without a number: match TMDB episode titles (e.g. 'Sharpe's Challenge')
    if show:
        for p in eps:
            if p in parsed:
                continue
            words = set(tw(p.stem.replace(".", " ").replace("_", " ")))
            for sn in sorted(show["seasons"], key=int):
                hit = next((e for e, t in tv_season(tmdb, show["id"], int(sn)).items()
                            if t and set(tw(t)) and set(tw(t)) <= words), None)
                if hit:
                    parsed[p] = (int(sn), hit, hit)
                    break
    used = set()
    for p in eps:
        rel = str(p.relative_to(d))
        res = (pn.lookup(facts, p) or {}).get("res") or pn.mediainfo_res(p)
        r = dict(common, File=rel, Res=res)
        if p not in parsed:
            rows.append(dict(r, Apply="", Kind="REVIEW", Note="no episode number found - type Season/Episode into "
                                                              "NewFile (… - s01e05 - …) and Y, or leave blank"))
            continue
        s_, e1, e2 = parsed[p]
        ep_code = f"s{s_:02d}e{e1:02d}" + (f"-e{e2:02d}" if e2 != e1 else "")
        titles = tv_season(tmdb, show["id"], s_) if show else {}
        et = titles.get(e1, "")
        if e2 != e1 and titles.get(e2):
            et = f"{et} - {titles[e2]}" if et else titles[e2]
        notes, ok = [], bool(show) and not why
        if show and titles and e1 not in titles:
            notes.append(f"TMDB season {s_} has no episode {e1} (it has {min(titles)}-{max(titles)}) - check")
            ok = False
        if show and not titles:
            notes.append(f"TMDB has no season {s_} for this show - check")
            ok = False
        if (s_, e1) in used:
            notes.append(f"{ep_code} appears twice in this folder - duplicate? (second one: version 2)")
            ok = False
        used.add((s_, e1))
        if why:
            notes.append(why)
        if not show:
            notes.append("show not identified - " + (why or "check the name"))
        et = re.sub(r"(?i)^episode\s*\d+$", "", et or "").strip()
        stem = f"{pn.safe_name(title)} ({year}) - {ep_code}" + (f" - {pn.safe_name(et)}" if et else "") + \
               (f" [{res}]" if res else "") + (" - version 2" if (s_, e1) in used and "twice" in " ".join(notes) else "")
        stem = pn.safe_name(stem)
        folder = f"{show_dir or pn.safe_name(d.name)}\\Season {s_:02d}"
        if len(str(root) + folder + stem) > pn.MAX_PATH - 20 and et:  # keep paths short: drop the episode title
            stem = pn.safe_name(f"{pn.safe_name(title)} ({year}) - {ep_code}" + (f" [{res}]" if res else ""))
        stems[p] = (folder, stem)
        rows.append(dict(r, Apply="Y" if ok else "", Kind="VIDEO", Season=str(s_), Episode=ep_code.split("e", 1)[1],
                         NewFolder=folder, NewFile=stem + pn.out_ext(p), EpisodeTitle=et, Note="; ".join(notes)))
    for p in extras:
        kind_dir = "Trailers" if re.search(r"(?i)trailer|teaser", p.stem) else "Featurettes"
        rows.append(dict(common, Apply="Y" if show_dir else "", Kind="EXTRA", File=str(p.relative_to(d)),
                         NewFolder=f"{show_dir or pn.safe_name(d.name)}\\{kind_dir}", NewFile=pn.safe_name(p.stem) + p.suffix.lower(),
                         Note="extra (trailer / featurette / documentary) - shown under the show's extras in Plex"))
    # subtitles follow their episode (same name start), else the only episode of the folder
    vids = sorted(((pn.norm(p.stem), p) for p in stems), key=lambda t: -len(t[0]))
    for p in files:
        if pn.is_video(p):
            continue
        rel = str(p.relative_to(d))
        if p.suffix.lower() in pn.SUB_EXT:
            owner = next((v for s, v in vids if s and pn.norm(p.stem).startswith(s)), None) or \
                (next(iter(stems)) if len(stems) == 1 else None)
            if owner:
                code, flags = pn.sub_language(p)
                folder, stem = stems[owner]
                rows.append(dict(common, Apply="auto", Kind="SUB", File=rel, NewFolder=folder,
                                 NewFile=stem + (f".{code}" if code else "") + flags + p.suffix.lower(),
                                 Note="subtitle - goes with its episode automatically (type n to leave it out)"))
            else:
                rows.append(dict(common, Apply="", Kind="REVIEW", File=rel,
                                 Note="subtitle not matched to an episode - fill NewFolder/NewFile yourself"))
        else:
            rows.append(dict(common, Apply="Y", Kind="TO_DELETE", File=rel, NewFolder="_to_delete",
                             Note="leftover (.nfo/.jpg/.txt/Thumbs.db...)"))
    return rows


def cmd_tv_plan(a):
    root = Path(a.root)
    if not root.is_dir():
        sys.exit(f"Not a folder: {root}")
    lang = pn.LANGS[a.lang]
    out = pn.PLAN_DIR / f"{pn.plan_tag(root)}_tvrename.xlsx"
    if pn.is_locked(out):
        sys.exit(f"The TV plan is open in Excel - save it, close Excel, then run again.\n   {out}")
    print(f"Series folder    : {root}   language {a.lang}")
    tmdb = pn.TMDB()
    plex_ids = plex_show_ids(root)
    facts = pn.PlexDB().parts_under(root)
    shows = sorted(d for d in root.iterdir() if d.is_dir() and not d.name.startswith(("_", "#", "@")))
    if a.only:
        shows = [d for d in shows if a.only.casefold() in d.name.casefold()]
    print(f"Shows (folders)  : {len(shows)}   (Plex already matched: {len(plex_ids)})")
    rows = []
    for i, d in enumerate(shows, 1):
        try:
            rows += plan_show(root, d, lang, tmdb, plex_ids, facts)
        except Exception as e:  # one bad folder must not stop the run
            rows.append(dict(Folder=d.name, Root=str(root), Apply="", Kind="REVIEW", Note=f"error: {e}"))
        if i % 5 == 0:
            print(f"   ... {i}/{len(shows)} shows", flush=True)
            tmdb.save()
    tmdb.save()
    for r in rows:
        r["ToolFolder"], r["ToolFile"] = r.get("NewFolder", ""), r.get("NewFile", "")
    pn.PLAN_DIR.mkdir(parents=True, exist_ok=True)
    pn.backup_plan(out)
    out = pn.write_plan(out, rows, TVFIELDS, new_if_locked=False)
    vids = [r for r in rows if r.get("Kind") == "VIDEO"]
    ident = {r["Folder"] for r in rows if r.get("TMDB")}
    print(f"Shows identified : {len(ident)} of {len(shows)}")
    print(f"Episodes         : {len(vids)}  ready (Y): {sum(r['Apply'] == 'Y' for r in vids)}")
    print(f"Films in the folder (not copied): {sum(r.get('Kind') == 'FILM' for r in rows)}")
    print(f"Need your review : {sum(1 for r in rows if r.get('Check') == 'CHECK')} red, "
          f"{sum(1 for r in rows if r.get('Check') == 'review')} yellow rows")
    print(f"Plan written     : {out}")


# ---------------------------------------------------------------- tv-copy

def cmd_tv_copy(a):
    plan = Path(a.plan)
    rows = pn.read_plan(plan)[1]
    root = Path(rows[0]["Root"])
    target = root.with_name(root.name + " (Plex)")
    yes_eps = {(r["Folder"], r["File"]) for r in rows if r.get("Kind") == "VIDEO" and r["Apply"].strip().upper() == "Y"}
    renamed, skip, problems = {}, set(), []
    for r in rows:
        if not r.get("File"):
            continue
        src = str(root / r["Folder"] / r["File"]).casefold()
        kind, ap = r.get("Kind"), r["Apply"].strip().upper()
        if kind in ("TO_DELETE", "FILM") and ap != "Y" or kind == "TO_DELETE":
            skip.add(src)  # leftovers never; films stay out (they belong in a movie library)
            continue
        follow = kind == "SUB" and pn.follows_film(r) and any(f == r["Folder"] for f, _ in yes_eps)
        if ap == "Y" or follow:
            nf, nfile = r.get("NewFolder", ""), r.get("NewFile", "")
            if not nf or not nfile or any(pn.safe_name(x) != x for x in nf.split("\\") + [nfile]):
                problems.append(f"bad new name for {r['Folder']}\\{r['File']} - copied unchanged")
                continue
            renamed[src] = target / nf / nfile
    in_plan = {r["Folder"].casefold() for r in rows if r.get("Folder")}
    ops, used = [], set()
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.relative_to(root).parts[0].casefold() not in in_plan:
            continue
        k = str(p).casefold()
        if k in skip or (k not in renamed and (p.suffix.lower() in pn.LEFTOVER_EXT or p.name.lower() == "thumbs.db")):
            continue
        dst = renamed.get(k) or target / p.relative_to(root)
        if str(dst).casefold() in used:
            problems.append(f"two files want {dst.name} - second copied unchanged")
            dst = target / p.relative_to(root)
        used.add(str(dst).casefold())
        ops.append((p, dst))
    print(f"TV plan      : {plan}")
    print(f"New folder   : {target}   (original untouched)")
    print(f"Renamed      : {sum(1 for s, d in ops if str(s).casefold() in renamed)} files; "
          f"copied unchanged: {sum(1 for s, d in ops if str(s).casefold() not in renamed)}; "
          f"left out (films, leftovers): {len(skip)}")
    for p in problems[:20]:
        print("   !", p)
    pn.copy_files(ops, root, target, plan.stem, a.go)
    if not a.go:
        print("\nDRY RUN - nothing copied. Examples:")
        for s, d in [o for o in ops if str(o[0]).casefold() in renamed][:6]:
            print(f"   {s.relative_to(root)}\n      -> {d.relative_to(target)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("tv-plan", help="plan Plex names for every show and episode (changes nothing)")
    p.add_argument("root")
    p.add_argument("--lang", choices=sorted(pn.LANGS), required=True)
    p.add_argument("--only", help="only show folders whose name contains this text")
    p.set_defaults(func=cmd_tv_plan)
    p = sub.add_parser("tv-copy", help="copy every show into '<root> (Plex)' under its new names (dry run unless --go)")
    p.add_argument("plan")
    p.add_argument("--go", action="store_true")
    p.set_defaults(func=cmd_tv_copy)
    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
