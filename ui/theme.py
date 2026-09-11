"""
Charte graphique de l'application.

Un seul endroit definit les couleurs, les polices et les styles ttk. Les
graphiques matplotlib reprennent la meme palette pour que la page ne donne pas
l'impression d'assembler deux logiciels differents.

Le parti pris est celui des interfaces d'Apple : un fond gris tres clair, des
cartes blanches separees par des filets d'un pixel, une seule couleur
d'action, beaucoup de blanc, et de la typographie plutot que des bordures
pour hierarchiser. Tkinter ne sait pas arrondir un bouton natif : les
elements qui doivent l'etre (boutons pilules, controle segmente, minuteur)
sont dessines sur un canevas, dans `widgets.py`.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# --------------------------------------------------------------------------
# palette
# --------------------------------------------------------------------------
C = {
    "bg":          "#F5F5F7",   # fond general (gris d'Apple)
    "surface":     "#FFFFFF",   # cartes
    "surface_alt": "#FAFAFC",   # zones secondaires (tableaux, champs)
    "surface_sunk": "#F0F0F3",  # champs enfonces, pistes de curseur
    "border":      "#D2D2D7",   # filet d'un pixel
    "border_soft": "#E9E9EE",
    "hairline":    "#E5E5EA",

    "primary":     "#1D1D1F",   # bandeau, titres : le presque-noir d'Apple
    "primary_alt": "#2C2C2E",
    "accent":      "#0071E3",   # bleu d'action, unique
    "accent_dark": "#0058B4",
    "accent_soft": "#EAF3FE",
    "accent_ring": "#9CC7F5",   # halo de focus

    "text":        "#1D1D1F",
    "text_soft":   "#3A3A3C",
    "muted":       "#6E6E73",
    "faint":       "#8E8E93",
    "on_primary":  "#FFFFFF",
    "on_primary_muted": "#A1A1A6",
    "on_primary_soft":  "#48484A",   # separateurs dans le bandeau

    "ok":          "#1D8649",
    "ok_soft":     "#E7F5EC",
    "ok_vivid":    "#34C759",
    "warn":        "#B25E09",
    "warn_soft":   "#FDF3E6",
    "warn_vivid":  "#FF9F0A",
    "error":       "#D70015",
    "error_soft":  "#FDECEC",
    "error_vivid": "#FF3B30",
    "info":        "#0071E3",

    # courbes
    "serie1":      "#0071E3",
    "serie2":      "#1D1D1F",
    "serie3":      "#7B61FF",
    "serie4":      "#34C759",
    "grid":        "#E5E5EA",
    "console_bg":  "#1C1C1E",
    "console_fg":  "#E5E5EA",
    "console_bar": "#2C2C2E",
    "console_dim": "#8E8E93",
}

# palette des camemberts / barres de conformite
STATUS_COLORS = {
    "conforme": "#34C759",
    "limite": "#FF9F0A",
    "interference": "#FF3B30",
    "critique": "#C00E0E",
    "majeure": "#FF3B30",
    "mineure": "#FF9F0A",
}

# Rayons et espacements, pour que tout le monde arrondisse pareil.
RADIUS = dict(sm=6, md=10, lg=14, xl=20, pill=999)
SPACE = dict(xs=4, sm=8, md=14, lg=20, xl=28)

FONTS = {}


# --------------------------------------------------------------------------
# petits outils de couleur (degrades, survols, ombres simulees)
# --------------------------------------------------------------------------

def rgb(color: str):
    """Triplet 0-255 d'une couleur "#RRGGBB"."""
    c = color.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def hexa(triplet) -> str:
    r, g, b = (max(0, min(255, int(round(v)))) for v in triplet)
    return f"#{r:02X}{g:02X}{b:02X}"


def mix(color_a: str, color_b: str, t: float) -> str:
    """Interpolation lineaire entre deux couleurs (t = 0 -> a, 1 -> b)."""
    t = max(0.0, min(1.0, float(t)))
    a, b = rgb(color_a), rgb(color_b)
    return hexa(a[i] + (b[i] - a[i]) * t for i in range(3))


def lighten(color: str, t: float) -> str:
    return mix(color, "#FFFFFF", t)


def darken(color: str, t: float) -> str:
    return mix(color, "#000000", t)


def round_rect(canvas, x0, y0, x1, y1, r, **kw):
    """
    Rectangle a coins arrondis sur un canevas Tk.

    Tk n'a pas de primitive : on ferme un polygone lisse sur les points de
    tangence. `smooth=True` arrondit les angles sans dessiner d'arcs, ce qui
    evite les coutures visibles entre arcs et segments.
    """
    r = max(0, min(float(r), abs(x1 - x0) / 2.0, abs(y1 - y0) / 2.0))
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
           x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
           x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return canvas.create_polygon(pts, smooth=True, **kw)


# --------------------------------------------------------------------------
# polices et ecran
# --------------------------------------------------------------------------

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
    # Les polices systeme d'Apple ne sont pas sur un poste Windows : on prend
    # la plus proche disponible, dans l'ordre.
    base = _pick_family(root, ["SF Pro Text", "SF Pro Display",
                               "Segoe UI Variable Text", "Segoe UI", "Inter",
                               "Helvetica Neue", "Roboto", "DejaVu Sans",
                               "Helvetica", "Arial"])
    titre = _pick_family(root, ["SF Pro Display", "Segoe UI Variable Display",
                                "Segoe UI Semibold", "Segoe UI", "Inter",
                                "Helvetica Neue", "DejaVu Sans", "Arial"])
    mono = _pick_family(root, ["SF Mono", "Cascadia Mono", "Consolas",
                               "JetBrains Mono", "DejaVu Sans Mono", "Menlo",
                               "Courier New"])
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
        family=base,
        family_title=titre,
        family_mono=mono,
        base=(base, 10 - d),
        small=(base, 9 - d),
        tiny=(base, 8 - d),
        bold=(base, 10 - d, "bold"),
        semibold=(base, 10 - d, "bold"),
        h0=(titre, 30 - 4 * d, "bold"),       # ecran de demarrage
        h1=(titre, 19 - 3 * d, "bold"),
        h2=(titre, 13 - d, "bold"),
        h3=(base, 11 - d, "bold"),
        lead=(base, 11 - d),                  # chapo sous un titre
        kpi=(titre, 20 - 3 * d, "bold"),
        kpi_small=(titre, 15 - 2 * d, "bold"),
        timer=(titre, 23 - 3 * d, "bold"),    # minuteur du cheminement
        timer_small=(base, 9 - d),
        mono=(mono, 9 - d),
        mono_small=(mono, 8 - d),
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
                    borderwidth=1, bordercolor=C["border_soft"])
    style.configure("Sep.TFrame", background=C["hairline"])
    style.configure("Toolbar.TFrame", background=C["surface"])

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
    style.configure("Lead.TLabel", background=C["surface"], foreground=C["muted"],
                    font=FONTS["lead"])
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
                    foreground=C["on_primary_muted"], font=FONTS["small"])
    style.configure("OnPrimaryTitle.TLabel", background=C["primary"],
                    foreground=C["on_primary"], font=FONTS["h2"])
    for key in ("ok", "warn", "error", "muted", "accent"):
        style.configure(f"{key.capitalize()}.TLabel", background=C["surface"],
                        foreground=C[key])
        style.configure(f"{key.capitalize()}Small.TLabel", background=C["surface"],
                        foreground=C[key], font=FONTS["small"])

    # --- boutons ---
    # Un bouton ttk reste rectangulaire ; on compense par des marges
    # genereuses, un aplat tres clair et une bordure d'un pixel. Les boutons
    # reellement arrondis sont les `PillButton` de widgets.py.
    style.configure("TButton", background=C["surface"], foreground=C["text"],
                    bordercolor=C["border"], borderwidth=1, focusthickness=0,
                    padding=(12, 7), relief="flat", font=FONTS["base"])
    style.map("TButton",
              background=[("pressed", C["surface_sunk"]),
                          ("active", C["surface_alt"]),
                          ("disabled", C["surface_alt"])],
              foreground=[("disabled", "#B0B0B5")],
              bordercolor=[("active", C["accent"]), ("disabled", C["border_soft"])])

    style.configure("Primary.TButton", background=C["accent"],
                    foreground=C["on_primary"], bordercolor=C["accent"],
                    font=FONTS["bold"], padding=(14, 8))
    style.map("Primary.TButton",
              background=[("pressed", darken(C["accent_dark"], 0.08)),
                          ("active", C["accent_dark"]),
                          ("disabled", "#BBD9F6")],
              foreground=[("disabled", "#F2F7FD")],
              bordercolor=[("active", C["accent_dark"]), ("disabled", "#BBD9F6")])

    style.configure("Success.TButton", background=C["ok"], foreground="#FFFFFF",
                    bordercolor=C["ok"], font=FONTS["bold"], padding=(14, 8))
    style.map("Success.TButton",
              background=[("pressed", darken(C["ok"], 0.15)),
                          ("active", darken(C["ok"], 0.08)),
                          ("disabled", "#C3E2CE")],
              bordercolor=[("disabled", "#C3E2CE")],
              foreground=[("disabled", "#F1F8F3")])

    style.configure("Danger.TButton", background=C["surface"], foreground=C["error"],
                    bordercolor="#F3C9C6", padding=(12, 7))
    style.map("Danger.TButton", background=[("active", C["error_soft"])])

    style.configure("Ghost.TButton", background=C["surface"], foreground=C["accent"],
                    bordercolor=C["surface"], padding=(10, 5), font=FONTS["small"])
    style.map("Ghost.TButton", background=[("active", C["accent_soft"])])

    style.configure("GhostBg.TButton", background=C["bg"], foreground=C["accent"],
                    bordercolor=C["bg"], padding=(10, 5), font=FONTS["small"])
    style.map("GhostBg.TButton", background=[("active", C["accent_soft"])],
              bordercolor=[("active", C["accent_soft"])])

    style.configure("Link.TButton", background=C["surface"], foreground=C["accent"],
                    bordercolor=C["surface"], padding=(2, 1), font=FONTS["small"])
    style.map("Link.TButton", background=[("active", C["surface"])])

    # --- champs ---
    style.configure("TEntry", fieldbackground=C["surface"], foreground=C["text"],
                    bordercolor=C["border"], lightcolor=C["border"],
                    darkcolor=C["border"], borderwidth=1, padding=6,
                    insertcolor=C["text"])
    style.map("TEntry",
              bordercolor=[("focus", C["accent"])],
              lightcolor=[("focus", C["accent"])],
              darkcolor=[("focus", C["accent"])])
    style.configure("Error.TEntry", fieldbackground=C["error_soft"],
                    bordercolor=C["error"], lightcolor=C["error"],
                    darkcolor=C["error"])

    style.configure("TCombobox", fieldbackground=C["surface"], background=C["surface"],
                    bordercolor=C["border"], arrowcolor=C["muted"], padding=5)
    style.map("TCombobox", fieldbackground=[("readonly", C["surface"])],
              bordercolor=[("focus", C["accent"])])

    style.configure("TCheckbutton", background=C["surface"], foreground=C["text"])
    style.map("TCheckbutton", background=[("active", C["surface"])])
    style.configure("Alt.TCheckbutton", background=C["surface_alt"],
                    foreground=C["text"])

    style.configure("TRadiobutton", background=C["surface"], foreground=C["text"])
    style.map("TRadiobutton", background=[("active", C["surface"])])

    style.configure("Horizontal.TScale", background=C["surface"],
                    troughcolor=C["surface_sunk"])

    # --- progression ---
    style.configure("TProgressbar", background=C["accent"],
                    troughcolor=C["surface_sunk"], bordercolor=C["surface_sunk"],
                    lightcolor=C["accent"], darkcolor=C["accent"], thickness=6)
    style.configure("Success.Horizontal.TProgressbar", background=C["ok"],
                    troughcolor=C["surface_sunk"], lightcolor=C["ok"],
                    darkcolor=C["ok"], thickness=6)
    style.configure("Error.Horizontal.TProgressbar", background=C["error"],
                    troughcolor=C["surface_sunk"], lightcolor=C["error"],
                    darkcolor=C["error"], thickness=6)

    # --- onglets ---
    style.configure("TNotebook", background=C["bg"], bordercolor=C["border_soft"],
                    tabmargins=(0, 4, 0, 0))
    style.configure("TNotebook.Tab", background=C["surface_alt"],
                    foreground=C["muted"], padding=(18, 9), borderwidth=0,
                    font=FONTS["small"])
    style.map("TNotebook.Tab",
              background=[("selected", C["surface"])],
              foreground=[("selected", C["primary"])],
              font=[("selected", FONTS["bold"])])

    # --- tableaux ---
    style.configure("Treeview", background=C["surface"], fieldbackground=C["surface"],
                    foreground=C["text"], bordercolor=C["border_soft"], rowheight=26,
                    borderwidth=1)
    style.configure("Treeview.Heading", background=C["surface_alt"],
                    foreground=C["muted"], font=FONTS["small"], relief="flat",
                    padding=(8, 6))
    style.map("Treeview.Heading", background=[("active", C["border_soft"])])
    style.map("Treeview", background=[("selected", C["accent_soft"])],
              foreground=[("selected", C["accent_dark"])])

    style.configure("TSeparator", background=C["hairline"])
    style.configure("Vertical.TScrollbar", background=C["border"],
                    troughcolor=C["bg"], bordercolor=C["bg"],
                    arrowcolor=C["muted"], width=10)
    style.map("Vertical.TScrollbar", background=[("active", C["faint"])])
    style.configure("Horizontal.TScrollbar", background=C["border"],
                    troughcolor=C["bg"], bordercolor=C["bg"],
                    arrowcolor=C["muted"])
    return style


def style_matplotlib():
    """Aligne matplotlib sur la charte (appele une fois au demarrage)."""
    import matplotlib as mpl
    mpl.rcParams.update({
        "figure.facecolor": C["surface"],
        "axes.facecolor": C["surface"],
        "axes.edgecolor": C["border_soft"],
        "axes.labelcolor": C["muted"],
        "axes.titlecolor": C["primary"],
        "axes.titlesize": 10,
        "axes.titleweight": "bold",
        "axes.labelsize": 8,
        "axes.grid": True,
        "axes.spines.top": False,
        "axes.spines.right": False,
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
        "lines.solid_capstyle": "round",
        "figure.autolayout": False,
    })
