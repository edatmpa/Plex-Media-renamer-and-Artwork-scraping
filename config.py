"""config - this PC's settings: Plex location, API keys, NAS names, Tesseract.

Values come from config.json next to this file (personal - never uploaded; start from config.example.json).
Anything missing falls back to the normal Plex defaults on Windows, so most users only fill in their keys.
An API key may be written into config.json directly, or as the path of a text file whose first line is the key.
"""
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.json"

KEY_NAMES = {"tmdb": "TMDB", "deepseek": "DeepSeek", "zai": "Z.ai", "deepl": "DeepL"}


def _load() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    text = CONFIG_FILE.read_text(encoding="utf-8-sig")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:  # common mistake: single backslashes in Windows paths ("E:\Plex") - accept them
            return json.loads(text.replace("\\", "\\\\"))
        except json.JSONDecodeError as e:
            raise SystemExit(f"config.json is not valid ({e}). Compare it with config.example.json.")


_cfg = _load()


def get(name: str, default=None):
    v = _cfg.get(name)
    return default if v in (None, "") else v


def _plex_data_dir() -> Path:
    """Plex's data folder: the custom location Plex stores in the registry, else the Windows default."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Plex, Inc.\Plex Media Server") as k:
            custom = winreg.QueryValueEx(k, "LocalAppDataPath")[0]
            if custom:
                return Path(custom) / "Plex Media Server"
    except OSError:
        pass
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Plex Media Server"


PLEX_DB = get("plex_db") or str(_plex_data_dir() / "Plug-in Support" / "Databases" / "com.plexapp.plugins.library.db")
PLEX_URL = get("plex_url", "http://127.0.0.1:32400").rstrip("/")
PLEX_TOKEN = get("plex_token", "")            # blank = read from Plex's own settings
TESSERACT = Path(get("tesseract", r"C:\Program Files\Tesseract-OCR\tesseract.exe"))
# NAS names -> IP address, for drives mapped by name while Plex stores the IP form: {"mynas": "192.168.1.10"}
NAS_HOSTS = {k.casefold(): v for k, v in (get("nas_hosts") or {}).items()}


def has_key(name: str) -> bool:
    v = str((get("keys") or {}).get(name) or "")
    return bool(v) and not v.startswith("paste-")


def key(name: str) -> str:
    """The API key `name` (tmdb, deepseek, zai, deepl). Never print or log the result."""
    v = str((get("keys") or {}).get(name) or "").strip()
    label = KEY_NAMES.get(name, name)
    if not v or v.startswith("paste-"):
        hint = "" if CONFIG_FILE.exists() else " (copy config.example.json to config.json first)"
        raise RuntimeError(f"No {label} API key: add it to config.json{hint} - see README, 'API keys'.")
    p = Path(os.path.expandvars(v))
    if len(v) < 260 and (p.suffix.lower() == ".txt" or p.is_file()):
        if not p.is_file():
            raise RuntimeError(f"{label} key file not found: {p}  (check config.json)")
        return p.read_text(encoding="utf-8-sig").splitlines()[0].strip()
    return v
