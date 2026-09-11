"""
Charte graphique de l'application.

Un seul endroit definit les couleurs, les polices et les styles ttk. Les
graphiques matplotlib reprennent la meme palette pour que la page ne donne pas
l'impression d'assembler deux logiciels differents.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# --------------------------------------------------------------------------
# palette
# --------------------------------------------------------------------------
C = {
    "bg":          "#EEF2F6",   # fond general
    "surface":     "#FFFFFF",   # cartes
    "surface_alt": "#F7F9FC",   # zones secondaires (tableaux, champs)
    "border":      "#D8E0E9",
    "border_soft": "#E8EEF4",

    "primary":     "#00205B",   # bleu profond : bandeau, titres
    "primary_alt": "#0B3A8D",
    "accent":      "#0072CE",   # bleu d'action : boutons principaux
    "accent_dark": "#005AA3",
    "accent_soft": "#E3F0FB",

    "text":        "#12233A",
    "muted":       "#64748B",
    "on_primary":  "#FFFFFF",

    "ok":          "#1E8E3E",
    "ok_soft":     "#E6F4EA",
    "warn":        "#C77700",
    "warn_soft":   "#FDF3E2",
    "error":       "#C5221F",
    "error_soft":  "#FCE8E6",
    "info":        "#0072CE",

    # courbes
    "serie1":      "#0072CE",
    "serie2":      "#00205B",
    "serie3":      "#7B61FF",
    "grid":        "#DCE4EC",
    "console_bg":  "#0F1B2A",
    "console_fg":  "#D6E1EC",
}

# palette des camemberts / barres de conformite
STATUS_COLORS = {
    "conforme": "#2E9E5B",
    "limite": "#E0A03B",
    "interference": "#D2483F",
    "critique": "#A32118",
    "majeure": "#D2483F",
    "mineure": "#E0A03B",
}

FONTS = {}


def _pick_family(root, candidates):
    available = set(tkfont.families(root))
    for name in candidates:
        if name in available:
            return name
    return "TkDefaultFont"


def screen_profile(root):
    """
    L'ecran de l'utilisateur, et ce qu'il impose a la mise en page.

    Un portable d'ingenierie affiche souvent 1366 x 768 : la fenetre y perd
    ses boutons du bas si on la dimensionne pour un 24 pouces. `compact`
    resserre alors polices, marges et graphes ; `tres_compact` (moins de
    800 px de haut) va plus loin -- schema replie et journal reduit.
    """
    try:
        w, h = root.winfo_screenwidth(), root.winfo_screenheight()
    except tk.TclError:                       # pas d'ecran : valeurs de bureau
        w, h = 1920, 1080
    return dict(width=int(w), height=int(h), compact=h < 900,
                tres_compact=h < 800)


def setup_fonts(root, compact=False):
    base = _pick_family(root, ["Segoe UI", "Inter", "Roboto", "DejaVu Sans",
                               "Helvetica Neue", "Helvetica", "Arial"])
    mono = _pick_family(root, ["Cascadia Mono", "Consolas", "JetBrains Mono",
                               "DejaVu Sans Mono", "Menlo", "Courier New"])
    # Un point de moins sur un ecran court : c'est une trentaine de pixels
    # regagnes par colonne de cartes, sans rien retirer a l'utilisateur.
    d = 1 if compact else 0
    for name, size, weight in (("TkDefaultFont", 10 - d, "normal"),
                               ("TkTextFont", 10 - d, "normal"),
                               ("TkMenuFont", 10 - d, "normal")):
        try:
            tkfont.nametofont(name).configure(family=base, size=size, weight=weight)
        except tk.TclError:
            pass
    FONTS.update(
        base=(base, 10 - d),
        small=(base, 9 - d),
        tiny=(base, 8 - d),
        bold=(base, 10 - d, "bold"),
        h1=(base, 19 - 3 * d, "bold"),
        h2=(base, 13 - d, "bold"),
        h3=(base, 11 - d, "bold"),
        kpi=(base, 20 - 3 * d, "bold"),
        kpi_small=(base, 15 - 2 * d, "bold"),
        mono=(mono, 9 - d),
        badge=(base, 9 - d, "bold"),
    )
    return FONTS


def apply_theme(root, compact=False):
    """Applique la charte a la fenetre et renvoie le `ttk.Style` configure."""
    setup_fonts(root, compact=compact)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")          # seul theme ttk entierement colorisable
    except tk.TclError:
        pass

    root.configure(bg=C["bg"])

    style.configure(".", background=C["bg"], foreground=C["text"],
                    font=FONTS["base"], borderwidth=0, focuscolor=C["accent"])

    # --- conteneurs ---
    style.configure("TFrame", background=C["bg"])
    style.configure("Surface.TFrame", background=C["surface"])
    style.configure("Alt.TFrame", background=C["surface_alt"])
    style.configure("Primary.TFrame", background=C["primary"])
    style.configure("Card.TFrame", background=C["surface"], relief="flat",
                    borderwidth=1, bordercolor=C["border"])
    style.configure("Sep.TFrame", background=C["border"])

    # --- textes ---
    style.configure("TLabel", background=C["bg"], foreground=C["text"])
    style.configure("Surface.TLabel", background=C["surface"], foreground=C["text"])
    style.configure("Alt.TLabel", background=C["surface_alt"], foreground=C["text"])
    style.configure("H1.TLabel", background=C["bg"], foreground=C["primary"],
                    font=FONTS["h1"])
    style.configure("H2.TLabel", background=C["surface"], foreground=C["primary"],
                    font=FONTS["h2"])
    style.configure("H3.TLabel", background=C["surface"], foreground=C["text"],
                    font=FONTS["h3"])
    style.configure("Muted.TLabel", background=C["surface"], foreground=C["muted"],
                    font=FONTS["small"])
    style.configure("MutedBg.TLabel", background=C["bg"], foreground=C["muted"],
                    font=FONTS["small"])
    style.configure("Kpi.TLabel", background=C["surface"], foreground=C["primary"],
                    font=FONTS["kpi"])
    style.configure("KpiSmall.TLabel", background=C["surface"], foreground=C["primary"],
                    font=FONTS["kpi_small"])
    style.configure("OnPrimary.TLabel", background=C["primary"],
                    foreground=C["on_primary"])
    style.configure("OnPrimaryMuted.TLabel", background=C["primary"],
                    foreground="#9FB6D9", font=FONTS["small"])
    style.configure("OnPrimaryTitle.TLabel", background=C["primary"],
                    foreground=C["on_primary"], font=FONTS["h2"])
    for key in ("ok", "warn", "error", "muted", "accent"):
        style.configure(f"{key.capitalize()}.TLabel", background=C["surface"],
                        foreground=C[key])
        style.configure(f"{key.capitalize()}Small.TLabel", background=C["surface"],
                        foreground=C[key], font=FONTS["small"])

    # --- boutons ---
    style.configure("TButton", background=C["surface"], foreground=C["text"],
                    bordercolor=C["border"], borderwidth=1, focusthickness=0,
                    padding=(12, 7), relief="flat")
    style.map("TButton",
              background=[("active", C["accent_soft"]), ("disabled", C["surface_alt"])],
              foreground=[("disabled", "#A9B4C0")],
              bordercolor=[("active", C["accent"])])

    style.configure("Primary.TButton", background=C["accent"],
                    foreground=C["on_primary"], bordercolor=C["accent"],
                    font=FONTS["bold"], padding=(16, 9))
    style.map("Primary.TButton",
              background=[("active", C["accent_dark"]), ("disabled", "#B9CDE0")],
              foreground=[("disabled", "#EDF3F8")],
              bordercolor=[("active", C["accent_dark"]), ("disabled", "#B9CDE0")])

    style.configure("Success.TButton", background=C["ok"], foreground="#FFFFFF",
                    bordercolor=C["ok"], font=FONTS["bold"], padding=(16, 9))
    style.map("Success.TButton",
              background=[("active", "#16702F"), ("disabled", "#BFD9C7")],
              bordercolor=[("disabled", "#BFD9C7")],
              foreground=[("disabled", "#F0F6F2")])

    style.configure("Danger.TButton", background=C["surface"], foreground=C["error"],
                    bordercolor="#F0BDBA", padding=(12, 7))
    style.map("Danger.TButton", background=[("active", C["error_soft"])])

    style.configure("Ghost.TButton", background=C["surface"], foreground=C["accent"],
                    bordercolor=C["surface"], padding=(8, 4), font=FONTS["small"])
    style.map("Ghost.TButton", background=[("active", C["accent_soft"])])

    style.configure("Link.TButton", background=C["surface"], foreground=C["accent"],
                    bordercolor=C["surface"], padding=(2, 1), font=FONTS["small"])
    style.map("Link.TButton", background=[("active", C["surface"])])

    # --- champs ---
    style.configure("TEntry", fieldbackground=C["surface"], foreground=C["text"],
                    bordercolor=C["border"], lightcolor=C["border"],
                    darkcolor=C["border"], borderwidth=1, padding=5,
                    insertcolor=C["text"])
    style.map("TEntry",
              bordercolor=[("focus", C["accent"])],
              lightcolor=[("focus", C["accent"])],
              darkcolor=[("focus", C["accent"])])
    style.configure("Error.TEntry", fieldbackground=C["error_soft"],
                    bordercolor=C["error"], lightcolor=C["error"],
                    darkcolor=C["error"])

    style.configure("TCombobox", fieldbackground=C["surface"], background=C["surface"],
                    bordercolor=C["border"], arrowcolor=C["primary"], padding=4)
    style.map("TCombobox", fieldbackground=[("readonly", C["surface"])],
              bordercolor=[("focus", C["accent"])])

    style.configure("TCheckbutton", background=C["surface"], foreground=C["text"])
    style.map("TCheckbutton", background=[("active", C["surface"])])
    style.configure("Alt.TCheckbutton", background=C["surface_alt"],
                    foreground=C["text"])

    style.configure("TRadiobutton", background=C["surface"], foreground=C["text"])
    style.map("TRadiobutton", background=[("active", C["surface"])])

    style.configure("Horizontal.TScale", background=C["surface"],
                    troughcolor=C["border_soft"])

    # --- progression ---
    style.configure("TProgressbar", background=C["accent"],
                    troughcolor=C["border_soft"], bordercolor=C["border_soft"],
                    lightcolor=C["accent"], darkcolor=C["accent"], thickness=8)
    style.configure("Success.Horizontal.TProgressbar", background=C["ok"],
                    troughcolor=C["border_soft"], lightcolor=C["ok"],
                    darkcolor=C["ok"], thickness=8)
    style.configure("Error.Horizontal.TProgressbar", background=C["error"],
                    troughcolor=C["border_soft"], lightcolor=C["error"],
                    darkcolor=C["error"], thickness=8)

    # --- onglets ---
    style.configure("TNotebook", background=C["bg"], bordercolor=C["border"],
                    tabmargins=(0, 4, 0, 0))
    style.configure("TNotebook.Tab", background=C["surface_alt"],
                    foreground=C["muted"], padding=(16, 8), borderwidth=0,
                    font=FONTS["small"])
    style.map("TNotebook.Tab",
              background=[("selected", C["surface"])],
              foreground=[("selected", C["primary"])],
              font=[("selected", FONTS["bold"])])

    # --- tableaux ---
    style.configure("Treeview", background=C["surface"], fieldbackground=C["surface"],
                    foreground=C["text"], bordercolor=C["border"], rowheight=24,
                    borderwidth=1)
    style.configure("Treeview.Heading", background=C["surface_alt"],
                    foreground=C["muted"], font=FONTS["small"], relief="flat",
                    padding=(6, 5))
    style.map("Treeview.Heading", background=[("active", C["border_soft"])])
    style.map("Treeview", background=[("selected", C["accent_soft"])],
              foreground=[("selected", C["primary"])])

    style.configure("TSeparator", background=C["border"])
    style.configure("Vertical.TScrollbar", background=C["border_soft"],
                    troughcolor=C["bg"], bordercolor=C["bg"],
                    arrowcolor=C["muted"], width=11)
    style.map("Vertical.TScrollbar", background=[("active", C["border"])])
    style.configure("Horizontal.TScrollbar", background=C["border_soft"],
                    troughcolor=C["bg"], bordercolor=C["bg"],
                    arrowcolor=C["muted"])
    return style


def style_matplotlib():
    """Aligne matplotlib sur la charte (appele une fois au demarrage)."""
    import matplotlib as mpl
    mpl.rcParams.update({
        "figure.facecolor": C["surface"],
        "axes.facecolor": C["surface"],
        "axes.edgecolor": C["border"],
        "axes.labelcolor": C["muted"],
        "axes.titlecolor": C["primary"],
        "axes.titlesize": 10,
        "axes.titleweight": "bold",
        "axes.labelsize": 8,
        "axes.grid": True,
        "grid.color": C["grid"],
        "grid.linewidth": 0.7,
        "grid.alpha": 0.9,
        "xtick.color": C["muted"],
        "ytick.color": C["muted"],
        "xtick.labelsize": 8,
        "ytick.labelsize": 8,
        "text.color": C["text"],
        "legend.frameon": False,
        "legend.fontsize": 8,
        "lines.linewidth": 1.8,
        "figure.autolayout": False,
    })
