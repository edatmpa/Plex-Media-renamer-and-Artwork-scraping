"""Start window of Plex Tools - Plex rename and metadata repair tools.
Tabs: Tools (start a tool), Guide (what each tool does and how to use it), Setup & safety."""
import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ui_theme as ui  # noqa: E402


def edit_settings():
    """Open config.json in Notepad; the first time it is made from config.example.json."""
    cfg = HERE / "config.json"
    if not cfg.exists():
        cfg.write_text((HERE / "config.example.json").read_text(encoding="utf-8"), encoding="utf-8")
    subprocess.Popen(["notepad.exe", str(cfg)])


def launch(script):
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")  # the windowed Python: no console window behind the tool windows
    subprocess.Popen([str(pyw if pyw.exists() else exe), str(HERE / script)], cwd=str(HERE),
                     env=dict(os.environ, PYTHONNOUSERSITE="1"),
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


TOOLS = (
    ("1   Rename a library folder", "plexname_gui.py",
     "Gives every film in a library folder a clean Plex name with its TMDB id, native title and resolution. "
     "Multi-film folders are split first. You review everything in Excel; results are copied into a new "
     "'<folder> (Plex)' - the original is never changed."),
    ("2   Fix one film", "lookup_gui.py",
     "For a single film that is wrong or missing in Plex: find it on TMDB, rename its folder, match it in Plex, "
     "fix title and description, pick the artwork - then one click 'Accept & update Plex'."),
    ("3   Artwork for a whole library", "artwork_gui.py",
     "Replaces video-frame or missing posters for a whole library. Three methods: full search + AI (non-English "
     "films), Plex's posters with OCR + AI (English films), or a quick fix. You review before Plex is changed."),
)

# Guide text: (style, text) pieces. Styles: h1, h2, p, b (bullet), n (numbered step), tip, warn
GUIDE = [
    ("h1", "How the tools fit together"),
    ("p", "Plex matches a film by its folder and file name. Old collections often have messy names, several films "
          "in one folder, or films Plex matched wrongly. These tools clean that up in three stages:"),
    ("n", "1  Rename a library folder - clean names with ids, so Plex matches every film correctly."),
    ("n", "2  Fix one film - repair single films by hand (wrong match, missing description, own choice)."),
    ("n", "3  Artwork for a whole library - make every film show a proper poster."),
    ("tip", "Safe by design: originals are never changed, every step has a dry run (a preview that changes nothing), "
            "and every change is written to a log that 'Undo a run…' can reverse."),

    ("h1", "Tool 1 - Rename a library folder"),
    ("p", "A library folder is a folder whose subfolders are films, e.g. V:\\Japanese\\Samurai. The tool never "
          "touches it: split films go into '<folder> (split temp)', the renamed library into '<folder> (Plex)'."),
    ("h2", "Steps"),
    ("n", "1  Browse… to the library folder, choose the original language and 'Movies'."),
    ("n", "2  '1 Split: plan' - finds folders that hold several films. The plan opens in Excel."),
    ("n", "3  Check the coloured rows: red = doubtful, yellow = probably right. The 'What to do' column explains "
          "each one. Type Y in 'Apply' to accept a row. Save with Ctrl+S and close Excel."),
    ("n", "4  'Copy to temp: dry run', then 'DO IT' - copies the split films into the temp folder."),
    ("n", "5  '2 Rename: plan' - proposes the final name of every film. Review it in Excel the same way."),
    ("n", "6  'Copy renamed: dry run', then 'DO IT' - builds '<folder> (Plex)'."),
    ("n", "7  In Plex, point the library at the new '(Plex)' folder and scan it."),
    ("tip", "Make and finish the rename plan BEFORE you point Plex at the new folder - the plan uses Plex's "
            "existing matches for the original folder."),
    ("b", "You can re-run a plan as often as you like; your Y marks and name changes are kept."),
    ("b", "Rows left blank are still copied - just with their old names. Nothing is lost."),
    ("b", "Name format: English Title (Year) {tmdb-id} Native title [resolution]."),

    ("h1", "Tool 2 - Fix one film (or one series)"),
    ("p", "Use it for a film that is unmatched, wrongly matched, or has a poor description or poster. Tick 'Series' "
          "next to the language for a TV show: then the whole show folder is handled - every episode gets its "
          "number (s01e03), the show is matched in Plex, and the show's description and poster are set."),
    ("h2", "Steps"),
    ("n", "1  Browse… to the film's folder (normally inside '<folder> (Plex)') and click Analyse."),
    ("n", "2  Pick the right film in the results list. 'TMDB' searches TMDB; 'IMDb (via Plex)' searches Plex's own "
          "film service, which also knows films that are only on IMDb (TMDB mode uses it automatically when TMDB "
          "finds nothing). Click a result: title, year, native title, id and description are filled in. You can "
          "also paste an IMDb or TMDB link into 'id / link' and click Load. Not found anywhere? The site buttons "
          "open specialist film sites; type the details into the fields yourself."),
    ("n", "3  Tab 'Plex: description & artwork': check the description (Translate to English if needed) and "
          "click the poster you want (the AI's pick is marked)."),
    ("n", "4  Click '✔ Accept & update Plex'. It renames the folder, matches the film in Plex, writes the "
          "details and sets the poster - all locked, so a Plex refresh can't undo them."),
    ("b", "More artwork: 'Find more on the web' opens an image search; right-click a poster > Copy image address, "
          "paste it into 'Add image address', click Add."),
    ("b", "Renaming works only in the new '(Plex)' or temp folders, never in the original."),
    ("b", "The 'Not on TMDB? Search:' buttons change with the language. '✎ Edit sites…' lets you add your own film "
          "sites: type just the site (e.g. russianfilmhub.com) or its search address with {q} where the title goes."),

    ("h1", "Tool 3 - Artwork for a whole library"),
    ("p", "Many films show a blurry frame from the video instead of a poster, even though Plex downloaded real "
          "posters. This tool picks a proper poster for every such film. For a TV-series library it does the same "
          "for the shows (recognised automatically from the Plex library). Plex does not re-select posters for "
          "items it already has - not even with 'Refresh metadata' - so this tool is the way to fix them."),
    ("h2", "Choose a method"),
    ("b", "A  Full search + AI - looks at Plex, all TMDB posters and Wikipedia; an AI picks the original release "
          "poster of the film's own year and country, preferring ones with cast names. Best for Japanese, Korean, "
          "Chinese, Russian… libraries."),
    ("b", "B  Plex's posters, OCR + AI - uses only the posters Plex already offers; a text reader (OCR) picks the one "
          "showing cast names or a tagline, the AI decides when that is unclear. Best for English libraries."),
    ("b", "C  Plex's first poster - no AI: simply the first real poster Plex offers (your own poster file if the "
          "folder has one). Fastest."),
    ("h2", "Steps"),
    ("n", "1  Pick the library folder and language; the method is suggested (you can change it). For a test, "
          "type e.g. 20 in 'Number of films'."),
    ("n", "2  '1 Find posters' - runs in the background (■ Stop keeps what is done). The Review tab opens itself."),
    ("n", "3  Review: click a film on the left, then the poster you want on the right (green frame = AI pick, "
          "orange = your choice). 'Approve all shown' accepts the rest."),
    ("n", "4  '3 Set in Plex' - shows how many films will change, asks, then sets the posters and locks them."),
    ("tip", "'Undo a run…' puts the old posters back from the log."),
]

SETUP = [
    ("h1", "What the tools need"),
    ("b", "Windows 10/11 and Python 3.12 or newer (python.org). 'Setup.bat' makes a private Python environment "
          "(.venv) with the packages from requirements.txt (requests, openai, openpyxl)."),
    ("b", "A Plex Media Server on this PC (the tools talk to it at 127.0.0.1:32400 and read its database read-only)."),
    ("b", "Your media on drives or network shares this PC can open."),
    ("b", "MediaInfo (command line) - measures the resolution when Plex doesn't know it."),
    ("b", "Tesseract OCR - only for artwork method B (text reading on posters)."),
    ("h2", "API keys (free or low-cost accounts)"),
    ("b", "TMDB - film data and posters (required)."),
    ("b", "Z.ai - the AI that looks at posters (artwork AI methods)."),
    ("b", "DeepSeek - helps choose among TMDB search results in tool 1 (optional)."),
    ("b", "DeepL - translates descriptions into English (optional, free plan is enough)."),
    ("p", "Put your keys into the settings file: click 'Edit settings' on the Tools tab (it opens config.json in "
          "Notepad). Write each key between the quotes - or the path of a text file whose first line is the key. "
          "Use / in paths. The tools read keys but never show or log them, and config.json is never uploaded."),
    ("p", "The same file holds the Plex address and database location (found automatically for a normal Plex "
          "install), the Tesseract path, and NAS names if your drives are mapped by NAS name. README.md explains "
          "every setting and how to get each key."),
    ("h1", "Safety rules the tools follow"),
    ("b", "Your original media folders are never renamed, moved or deleted."),
    ("b", "Every step that changes something has a dry run first and asks before it starts."),
    ("b", "Every change is logged in the 'logs' folder; 'Undo a run…' reverses it."),
    ("b", "Plex is changed only through its own web interface (the same as Edit in Plex), never by writing its "
          "database. Values you set are locked so a Plex refresh can't overwrite them."),
    ("b", "The AI only chooses among real candidates (films found on TMDB, posters that exist) - it never invents ids."),
    ("b", "Nothing is scraped from sites that block automated access; web searches open in your own browser."),
    ("h2", "Where things are"),
    ("b", "plans - the plans you review (Excel files for renaming, poster plans for artwork). Older versions move "
          "to plans\\_obsolete."),
    ("b", "logs - records of every run, used by Undo."),
    ("b", "cache - saved look-ups (TMDB, AI answers, poster thumbnails). Safe to delete; runs are just slower."),
]


def fill(text: tk.Text, parts):
    text.tag_configure("h1", font=("Segoe UI", 13, "bold"), foreground=ui.HEAD, spacing1=14, spacing3=4)
    text.tag_configure("h2", font=("Segoe UI", 10, "bold"), foreground=ui.ACCENT, spacing1=8, spacing3=2)
    text.tag_configure("p", spacing3=6, lmargin1=4, lmargin2=4)
    text.tag_configure("b", lmargin1=14, lmargin2=28, spacing3=3)
    text.tag_configure("n", lmargin1=14, lmargin2=34, spacing3=3)
    text.tag_configure("tip", background="#e7f3ea", foreground="#1b5e20", lmargin1=10, lmargin2=10, rmargin=10,
                       spacing1=6, spacing3=6)
    for style, t in parts:
        text.insert("end", ("•  " if style == "b" else "") + t + "\n", style)
    text.configure(state="disabled")


root = tk.Tk()
ui.apply(root)
root.title("Plex Tools")
root.geometry("820x600")
root.minsize(700, 480)
ui.header(root, "Plex rename and metadata repair tools",
          "Originals are never changed. Plex is changed only through its own web interface, after you confirm.")
tabs = ttk.Notebook(root)
tabs.pack(fill="both", expand=True, padx=10, pady=10)

t1 = ttk.Frame(tabs, padding=14)
tabs.add(t1, text="Tools")
for title, script, text in TOOLS:
    row = ttk.Frame(t1)
    row.pack(fill="x", pady=8)
    ttk.Button(row, text=title, width=32, style="Accent.TButton", command=lambda s=script: launch(s)).pack(
        side="left", anchor="n")
    ttk.Label(row, text=text, wraplength=470, justify="left").pack(side="left", padx=12)
ttk.Label(t1, text="New here? Read the Guide tab first - it explains each tool step by step.",
          style="Muted.TLabel").pack(anchor="w", pady=(12, 0))
bar = ttk.Frame(t1)
bar.pack(fill="x", side="bottom")
def open_dir(p: Path):
    p.mkdir(exist_ok=True)  # a fresh install has no plans/logs yet
    os.startfile(p)


ttk.Button(bar, text="Open plans folder", command=lambda: open_dir(HERE / "plans")).pack(side="left")
ttk.Button(bar, text="Open logs folder", command=lambda: open_dir(HERE / "logs")).pack(side="left", padx=6)
ttk.Button(bar, text="Open project folder", command=lambda: os.startfile(HERE)).pack(side="left")
ttk.Button(bar, text="Edit settings", command=edit_settings).pack(side="left", padx=6)
ttk.Button(bar, text="Close", command=root.destroy).pack(side="right")

for name, parts in (("Guide", GUIDE), ("Setup & safety", SETUP)):
    f = ttk.Frame(tabs)
    tabs.add(f, text=name)
    txt = tk.Text(f, wrap="word", font=ui.FONT, bg=ui.PANEL, relief="flat", padx=16, pady=10)
    sb = ttk.Scrollbar(f, command=txt.yview)
    txt.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    txt.pack(fill="both", expand=True)
    fill(txt, parts)

root.mainloop()
