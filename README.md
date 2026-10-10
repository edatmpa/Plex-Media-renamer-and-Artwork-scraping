# Plex Media Renamer and Artwork Scraping

Windows tools for **Plex Media Server** owners with large, messy film and TV collections, especially
**non-English collections** (Japanese, Korean, Chinese, Russian and others). The tools are built for people
who don't code:

1. **Rename a library folder.** Every film gets a clean Plex name with its TMDB id, the original-language
   title and the measured resolution. Folders that hold several films are split first. You check every
   change in Excel before anything is copied.
2. **Fix one film (or one TV series).** Find the right entry on TMDB, IMDb or Plex's own service, rename the
   folder, match the film in Plex, write the title and description, and pick the poster, all with one
   "Accept & update Plex" button.
3. **Artwork for a whole library.** Replace the blurry video frames and empty posters that Plex often shows
   with a proper poster. An AI can help choose the original release poster, and you review its choices
   before Plex is changed.

All three start from one window: double-click `Plex Tools.bat`. Its **Guide** tab explains every step.

Example of the naming the tools produce:

```
Isshin Tasuke - A Man Among Men (1959) {tmdb-451248} 一心太助 男の中の男一匹 [SD]\
    Isshin Tasuke - A Man Among Men (1959) {tmdb-451248} 一心太助 男の中の男一匹 [SD].mkv
    Isshin Tasuke - A Man Among Men (1959) {tmdb-451248} 一心太助 男の中の男一匹 [SD].en.srt
```

TV: `Show (Year) {tmdb-ID} Native title\Season 01\Show (Year) - s01e03 - Episode title [1080p].mkv`

---

## Safe by design

- **Your original folders are never renamed, moved or deleted.** Renamed copies go into a new folder next
  to the original, `<folder> (Plex)`. Once you've checked it in Plex, you decide what to do with the old one.
- **Nothing happens without your review.** Rename plans open in Excel with colour-coded rows: red means
  doubtful, yellow means probably right. Only the rows you approve are acted on.
- **Every step has a dry run**, a preview that changes nothing, and asks before it starts.
- **Every change is logged**, and the **Undo a run…** button reverses it.
- **Plex is only changed through its own web interface**, the same way as using Edit in the Plex app.
  The Plex database is only ever *read*. Values the tools set are locked, so a Plex refresh can't
  overwrite them.

---

## How AI is used (plain-language explanation)

You don't need to know anything about AI to use these tools. AI is optional and only used for a few
choosing tasks.

**What is an "AI model" here?** It's an online service you send a question to and get an answer back
from, like a very well-read assistant. These tools ask it short, specific questions, such as *"Which of
these 6 posters is the original 1959 Japanese release poster?"* or *"Which of these 3 TMDB entries matches
the folder `Kumonosu-jo 1957 DVDrip`?"*

**The AI never invents facts.** It only *chooses* among real candidates the tools found first: films that
exist on TMDB, and posters that exist on Plex, TMDB or Wikipedia. If it answers with anything outside that
list, the answer is rejected. Film ids, titles and years always come from TMDB, IMDb or Plex itself.
You review every choice.

| Service | Model | What it does in the tools | Needed? |
|---|---|---|---|
| **Z.ai** (Zhipu AI) | `glm-4.6v` (a vision model, it can look at pictures) | Looks at small poster thumbnails and ranks them: original release poster in the style of the film's year and country first, posters with cast names preferred, video frames and fan art last. | Only for the AI artwork methods (tool 3 method A/B, the AI pick in tool 2) |
| **DeepSeek** | `deepseek-chat` | When a folder name is messy and TMDB returns several possible films, it picks the most likely one or suggests better search words. | Optional (tool 1 works without it, you then decide those rows yourself) |
| **DeepL** | translation service (not a chat AI) | Translates a Japanese/Russian/… film description into English. | Optional |
| **Tesseract OCR** | runs on your own PC, free | Reads the text printed on posters (artwork method B). | Optional |

**What is sent to these services:** small poster thumbnails, film titles, years, folder and file names,
and lists of TMDB candidates. Descriptions are sent to DeepL for translation.
**What is never sent:** your videos, your Plex token, or anything else from your PC.

**Cost.** AI services charge per use, measured in "tokens" (roughly pieces of words). The amounts here are
small. Choosing a poster for one film uses about 1,000 to 3,000 tokens, so a library of 500 films is
about 1 to 1.5 million tokens. Prices change, so check each provider's pricing page. In practice a small
prepaid credit (a few US dollars) goes a long way. Answers are saved in the `cache` folder, so running the
same library again costs almost nothing.

**Working without AI:** artwork method C ("Plex's first poster") uses no AI. Tool 1 has a DeepSeek on/off
switch. Without AI you simply review more rows yourself.

---

## What you need

- **Windows 10 or 11**, on the **same PC as your Plex Media Server**. The tools read Plex's database
  file, read-only.
- **Python 3.12 or newer** from [python.org](https://www.python.org/downloads/). In the installer, tick
  **"Add python.exe to PATH"**.
- **Microsoft Excel**, or another spreadsheet program that opens `.xlsx`, to review the rename plans.
- A **TMDB API key** (free). The other keys are optional, see below.
- Optional: **MediaInfo** (measures the video resolution when Plex doesn't know it) and **Tesseract OCR**.
  `Setup.bat` offers to install both.

## Installation

1. Download this project: the green **Code** button, then **Download ZIP**, then unzip it, e.g. to
   `C:\PlexTools`. Or use `git clone`.
2. Double-click **`Setup.bat`**. It checks Python, creates a private Python environment (`.venv`) in the
   project folder with the packages the tools need (`requests`, `openai`, `openpyxl`), so nothing is
   installed into your system Python. It also offers MediaInfo and Tesseract, and creates your settings
   file `config.json`, which opens in Notepad. Run `Setup.bat` again any time to repair or update.
3. Paste your API keys into `config.json` (see the next section), then save and close Notepad.
4. Double-click **`Plex Tools.bat`** and read the **Guide** tab.

You can open the settings again at any time with the **Edit settings** button in the start window.

## API keys: how to get them

An API key is a password-like code that lets a program use an online service in your name. Keep keys
private. `config.json` is never uploaded (it is listed in `.gitignore`).

In `config.json`, write each key between the quotes:

```json
"keys": {
  "tmdb": "1a2b3c4d5e...",
  "deepseek": "sk-...",
  "zai": "...",
  "deepl": "...:fx"
}
```

You can instead give the path of a text file whose first line is the key, e.g.
`"tmdb": "D:/Keys/tmdb.txt"`. Use `/` in paths.

### TMDB (required, free)
Film data: titles, years, original titles, descriptions, posters.
1. Create a free account at [themoviedb.org](https://www.themoviedb.org/signup).
2. Go to **Settings → API** and request an API key (choose "Developer", personal/non-commercial use).
3. Copy the **API Key** (the short one, *not* the long "API Read Access Token") into `"tmdb"`.

### Z.ai (for AI poster selection, prepaid credit)
1. Create an account on the [Z.ai API platform](https://z.ai/manage-apikey/apikey-list).
2. Add credit (top up) in the billing section. A small amount is enough to start.
3. Create an API key and paste it into `"zai"`.
   Use a normal API key with credit. The "GLM Coding Plan" subscription is for coding tools and does not
   work here.

### DeepSeek (optional, prepaid credit)
1. Create an account on the [DeepSeek platform](https://platform.deepseek.com/).
2. Top up a small credit, then go to **API keys** and create a key (it starts with `sk-`).
3. Paste it into `"deepseek"`.

### DeepL (optional, free plan available)
1. Sign up for **DeepL API Free** at [deepl.com/pro-api](https://www.deepl.com/pro-api). The free plan
   includes a monthly character allowance. DeepL asks for a card to verify the account.
2. In your account, copy the **Authentication Key** (free keys end in `:fx`) into `"deepl"`.

### Plex
Nothing to do. The tools read your Plex login token from the Plex server's own settings on this PC. If that
fails (e.g. Plex runs under another Windows account), put the token into `"plex_token"`; Plex's support
article "Finding an authentication token" shows how to find it.

## Settings reference (`config.json`)

| Setting | Default | When to change it |
|---|---|---|
| `plex_db` | found automatically (also custom Plex data folders) | Only if the tools say the Plex database was not found. Full path of `com.plexapp.plugins.library.db`. |
| `plex_url` | `http://127.0.0.1:32400` | Plex runs on a different port. |
| `plex_token` | read from Plex's settings | See "Plex" above. |
| `tesseract` | `C:/Program Files/Tesseract-OCR/tesseract.exe` | Tesseract installed elsewhere. |
| `nas_hosts` | none | Your drive letters are mapped by NAS *name* (`\\MyNAS\share`) but Plex's libraries were added by *IP address* (`\\192.168.1.10\share`). Example: `{"mynas": "192.168.1.10"}` |
| `keys` | none | Your API keys, see above. |

---

## Folders the tools create

| Folder | What |
|---|---|
| `plans` | Plans you review: Excel rename plans and poster plans. Older versions move to `plans\_obsolete`. |
| `logs` | A record of every run that changed files or Plex, used by **Undo a run…** |
| `cache` | Saved look-ups (TMDB, AI answers, poster thumbnails). Safe to delete; runs are just slower. |
| `settings*.json` | Each window remembers your last choices. Safe to delete. |

## Troubleshooting

- **"Close the plan in Excel first."** The tools rewrite the plan file. Save it with Ctrl+S, close Excel,
  click Retry.
- **TMDB or AI requests hang.** Some home networks have broken IPv6. The tools already force IPv4 for their
  own requests. If it still hangs, check your internet connection or firewall.
- **"Plex: not matched" for a film you know Plex has.** Plex stores library paths in the form they were
  added in (drive letter or `\\IP\share`). If your drives are mapped by NAS name, fill in `nas_hosts`.
- **A film is not on TMDB.** In tool 2, search IMDb (via Plex), or use the "Not on TMDB? Search:" buttons
  that open film sites for that language. Then type the details yourself.

## For developers

The window tools are thin layers over command-line engines; every command without `--go` is a dry run:

```
python plexname.py split-plan  "D:\Films\Samurai" --lang JP --type movie
python plexname.py split-copy  plans\<plan>.xlsx [--go]
python plexname.py rename-plan "D:\Films\Samurai" --lang JP --type movie
python plexname.py rename-copy plans\<plan>.xlsx [--go]
python plexname.py undo        logs\<log>.csv [--go]
python plextv.py   tv-plan     "D:\Series\Russian" --lang RU
python plexfix.py  posters-plan --folder "D:\Films\Samurai" --lang JP [--limit N]
python plexfix.py  posters-apply plans\<tag>_posters.json [--go]
```

| File | Role |
|---|---|
| `launcher.py` | Start window (Tools / Guide / Setup & safety) |
| `plexname.py` | Film engine: split plans, rename plans, copying, undo, naming rules, TMDB, DeepSeek |
| `plextv.py` | TV-series engine: episode numbers, show identification, TV rename plans |
| `plexfix.py` | Plex web API (scan, match, fields, posters), poster plans, Z.ai ranking, Wikipedia posters, DeepL, OCR |
| `plexname_gui.py`, `lookup_gui.py`, `artwork_gui.py` | The three tool windows |
| `config.py` | Reads `config.json` |
| `ui_theme.py` | Shared look of the windows |

### How this project was made
The tools were written with **Claude Code**, Anthropic's AI coding assistant. The author described each
need in plain language ("never touch the originals", "let me review everything in Excel", "pick the
original Japanese poster"), tested every step on a real collection of about 2,500 files, and reviewed the
results. The design rules (dry runs, review before change, logs and undo, AI only chooses among real
candidates) came out of that collaboration.

## Credits and licence

- This product uses the TMDB API but is not endorsed or certified by TMDB.
  Film data and posters © their owners, provided by [The Movie Database](https://www.themoviedb.org/).
- Poster information from [Wikidata](https://www.wikidata.org/) and Wikipedia.
- Not affiliated with Plex Inc., Z.ai, DeepSeek or DeepL.
- Licence: MIT, see [LICENSE](LICENSE).
