"""Shared look for all Plex Tools windows: a coloured title band, calm background, blue step buttons,
green buttons for actions that change Plex or files. Uses the ttk 'clam' theme, which allows colours."""
import tkinter as tk
from tkinter import ttk

BG = "#f3f5f8"          # window background
PANEL = "#ffffff"       # text areas, lists
HEAD = "#1f3b57"        # title band
HEAD_SUB = "#c9d6e3"    # subtitle in the band
ACCENT = "#2f6f9f"      # normal step buttons, selected tab
GO = "#2e7d32"          # buttons that change Plex / files
GO_DARK = "#1b5e20"
WARN = "#b3261e"
MUTED = "#5f6b76"       # help text
FONT = ("Segoe UI", 9)


def apply(root: tk.Misc):
    """Call once per window (Tk or Toplevel) before building widgets."""
    s = ttk.Style(root)
    try:
        s.theme_use("clam")
    except tk.TclError:
        pass
    root.configure(bg=BG)
    s.configure(".", background=BG, font=FONT)
    s.configure("TFrame", background=BG)
    s.configure("TLabel", background=BG)
    s.configure("Muted.TLabel", background=BG, foreground=MUTED)
    s.configure("TLabelframe", background=BG, bordercolor="#d5dbe1")
    s.configure("TLabelframe.Label", background=BG, foreground=HEAD, font=("Segoe UI", 9, "bold"))
    s.configure("TRadiobutton", background=BG)
    s.configure("TCheckbutton", background=BG)
    s.configure("TButton", padding=(8, 3))
    s.configure("Accent.TButton", background=ACCENT, foreground="white", bordercolor=ACCENT)
    s.map("Accent.TButton", background=[("active", "#24587f"), ("disabled", "#9db4c7")])
    s.configure("Go.TButton", background=GO, foreground="white", bordercolor=GO, font=("Segoe UI", 9, "bold"))
    s.map("Go.TButton", background=[("active", GO_DARK), ("disabled", "#9fc3a2")])
    s.configure("Stop.TButton", foreground=WARN)
    s.configure("Head.TFrame", background=HEAD)
    s.configure("Head.TLabel", background=HEAD, foreground="white", font=("Segoe UI", 15, "bold"))
    s.configure("HeadSub.TLabel", background=HEAD, foreground=HEAD_SUB)
    s.configure("TNotebook", background=BG, borderwidth=0)
    s.configure("TNotebook.Tab", padding=(12, 4), background="#dfe5eb")
    s.map("TNotebook.Tab", background=[("selected", PANEL)], foreground=[("selected", ACCENT)])
    s.configure("Treeview", background=PANEL, fieldbackground=PANEL)
    s.configure("Treeview.Heading", background="#dfe5eb", font=("Segoe UI", 9, "bold"))
    s.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "white")])


def header(parent, title: str, subtitle: str = ""):
    """The coloured title band at the top of a window."""
    band = ttk.Frame(parent, style="Head.TFrame", padding=(14, 10))
    band.pack(fill="x")
    ttk.Label(band, text=title, style="Head.TLabel").pack(anchor="w")
    if subtitle:
        ttk.Label(band, text=subtitle, style="HeadSub.TLabel", wraplength=900, justify="left").pack(anchor="w")
    return band
