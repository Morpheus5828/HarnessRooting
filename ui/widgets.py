"""
Briques d'interface reutilisables : cartes, tuiles d'indicateurs, console de
journal, champs de parametres avec aide contextuelle, bandeaux d'etat.

Les widgets sont construits sur `tk.Frame` plutot que `ttk.Frame` quand ils ont
besoin d'une bordure fine : c'est le seul moyen d'obtenir le meme rendu sur
Windows, Linux et macOS.
"""

from __future__ import annotations
import math
import time
import queue
import tkinter as tk
from tkinter import ttk

from .theme import C, FONTS, RADIUS, mix, round_rect
from .. import log as applog
from ..core.tool import _duree as format_duree


def format_chrono(secondes) -> str:
    """
    Duree lisible d'un coup d'oeil, au dixieme de seconde.

    Le dixieme n'est pas de la precision : c'est ce qui fait vivre le
    minuteur. Sans lui, un calcul de trois minutes affiche un nombre fige, et
    l'utilisateur se demande si l'application a decroche.
    """
    s = max(0.0, float(secondes))
    if s < 60.0:
        return f"{s:.1f} s"
    minutes, reste = divmod(s, 60.0)
    if minutes < 60:
        return f"{int(minutes)}:{reste:04.1f}"
    heures, minutes = divmod(int(minutes), 60)
    return f"{heures}:{minutes:02d}:{int(reste):02d}"


class Spinner(tk.Canvas):
    """
    Minuteur du calcul : un anneau qui tourne, et le temps ecoule au centre.

    Trois choses le rendent fluide la ou un `after(40)` classique saccade :

    * l'angle est calcule a partir de l'HORLOGE (`time.monotonic`), pas
      incremente d'un pas fixe : une image sautee ne decale plus l'anneau, la
      rotation garde sa vitesse quoi qu'il arrive ;
    * la comete est faite d'une vingtaine d'arcs dont la couleur s'eteint
      progressivement : l'oeil lit un degrade, pas un segment qui clignote ;
    * sa longueur RESPIRE (elle s'allonge puis se retracte), ce qui donne
      l'impression d'une avance continue meme quand le calcul ne sait pas
      dire ou il en est.

    En mode determine (`set_progress`), la comete laisse la place a un arc
    de progression et le pourcentage s'affiche sous le chrono.
    """

    TAILLE = 164
    EPAISSEUR = 9
    SEGMENTS = 22                 # longueur du degrade de la comete
    TOUR_S = 2.6                  # duree d'un tour complet, en secondes
    PERIODE = 16                  # une image toutes les 16 ms = 60 par seconde

    def __init__(self, parent, texte="", taille=None, bg=None):
        taille = int(taille or self.TAILLE)
        bg = bg or C["surface"]
        super().__init__(parent, width=taille, height=taille, bg=bg,
                         highlightthickness=0, bd=0)
        self.taille = taille
        self.texte = texte
        self._t0 = None
        self._fige = 0.0              # duree gelee apres un stop()
        self._job = None
        self._ratio = None            # None = mode indetermine
        self._dernier_texte = ""

        marge = self.EPAISSEUR + 6
        x0, y0, x1, y1 = marge, marge, taille - marge, taille - marge

        # piste : un anneau tres clair, jamais noir -- c'est ce qui donne la
        # legerete d'une interface Apple.
        self.create_oval(x0, y0, x1, y1, outline=C["border_soft"],
                         width=self.EPAISSEUR)

        pas = 360.0 / self.SEGMENTS
        self._queue_arcs = []
        for i in range(self.SEGMENTS):
            # du plus vif (tete) au presque invisible (fin de comete)
            t = i / float(self.SEGMENTS - 1)
            couleur = mix(C["accent"], C["border_soft"], t ** 0.85)
            self._queue_arcs.append(self.create_arc(
                x0, y0, x1, y1, start=0, extent=-pas * 1.06, style=tk.ARC,
                outline=couleur, width=self.EPAISSEUR))

        self._arc_progres = self.create_arc(x0, y0, x1, y1, start=90,
                                            extent=0, style=tk.ARC,
                                            outline=C["accent"],
                                            width=self.EPAISSEUR,
                                            state=tk.HIDDEN)
        self._tete = self.create_oval(0, 0, 0, 0, outline="", fill=C["accent"],
                                      state=tk.HIDDEN)
        self._chrono = self.create_text(taille // 2, taille // 2 - 9,
                                        text="0.0 s", fill=C["text"],
                                        font=FONTS["timer"])
        self._sous_titre = self.create_text(taille // 2, taille // 2 + 22,
                                            text=texte, fill=C["faint"],
                                            font=FONTS["timer_small"],
                                            width=taille - 4 * self.EPAISSEUR)
        self.bind("<Destroy>", lambda _e: self.stop())

    # -- pilotage ------------------------------------------------------
    def start(self, texte=None):
        if texte is not None:
            self.set_caption(texte)
        self._t0 = time.monotonic()
        self._fige = 0.0
        self.itemconfigure(self._tete, state=tk.NORMAL)
        if self._job is None:
            self._anime()

    def stop(self, garder_chrono=True):
        """Arrete l'animation. Le chrono reste affiche sur sa derniere valeur."""
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None
        if self._t0 is not None:
            self._fige = time.monotonic() - self._t0
        self._t0 = None
        try:
            self.itemconfigure(self._tete, state=tk.HIDDEN)
            for arc in self._queue_arcs:
                self.itemconfigure(arc, state=tk.HIDDEN)
            if not garder_chrono:
                self.itemconfigure(self._chrono, text="0.0 s")
        except tk.TclError:
            pass

    def reset(self):
        self.stop(garder_chrono=False)
        self._fige = 0.0
        self._ratio = None
        try:
            self.itemconfigure(self._arc_progres, state=tk.HIDDEN)
            for arc in self._queue_arcs:
                self.itemconfigure(arc, state=tk.NORMAL)
            self.itemconfigure(self._chrono, text="0.0 s", fill=C["text"])
        except tk.TclError:
            pass

    def elapsed(self) -> float:
        if self._t0 is None:
            return self._fige
        return time.monotonic() - self._t0

    def set_caption(self, texte):
        self.texte = texte or ""
        try:
            self.itemconfigure(self._sous_titre, text=self.texte)
        except tk.TclError:
            pass

    def set_progress(self, done=None, total=None):
        """
        Passe en mode determine. `set_progress(None)` revient a l'anneau
        tournant -- utile quand une phase du calcul ne se compte pas.
        """
        if done is None or not total:
            self._ratio = None
            try:
                self.itemconfigure(self._arc_progres, state=tk.HIDDEN)
                for arc in self._queue_arcs:
                    self.itemconfigure(arc, state=tk.NORMAL)
            except tk.TclError:
                pass
            return
        self._ratio = max(0.0, min(1.0, float(done) / float(total)))
        try:
            self.itemconfigure(self._arc_progres, state=tk.NORMAL)
            for arc in self._queue_arcs:     # la comete cede la place a l'arc
                self.itemconfigure(arc, state=tk.HIDDEN)
        except tk.TclError:
            pass

    def set_color(self, couleur):
        """Teinte l'anneau (vert a la reussite, rouge a l'echec)."""
        try:
            self.itemconfigure(self._arc_progres, outline=couleur)
            self.itemconfigure(self._chrono, fill=couleur)
            self.itemconfigure(self._tete, fill=couleur)
        except tk.TclError:
            pass

    # -- animation -----------------------------------------------------
    def _anime(self):
        try:
            self._dessine()
        except tk.TclError:                 # widget detruit entre deux images
            self._job = None
            return
        self._job = self.after(self.PERIODE, self._anime)

    def _dessine(self):
        t = time.monotonic()
        ecoule = self.elapsed()

        if self._ratio is None:
            # --- comete : l'angle vient de l'horloge, la longueur respire ---
            angle = -(t / self.TOUR_S) * 360.0
            respiration = 0.5 * (1.0 - math.cos(2.0 * math.pi * (t / 3.4)))
            etale = 0.45 + 0.55 * respiration      # de 45 % a 100 % de la comete
            pas = 360.0 / self.SEGMENTS
            for i, arc in enumerate(self._queue_arcs):
                debut = angle - i * pas * etale
                self.itemconfigure(arc, start=debut % 360.0,
                                   extent=-pas * etale * 1.08)
            self._place_tete(angle)
        else:
            # --- progression : l'arc part du haut, dans le sens horaire ---
            self.itemconfigure(self._arc_progres, start=90.0,
                               extent=-359.999 * self._ratio)
            self._place_tete(90.0 - 360.0 * self._ratio)

        # le chrono ne se redessine qu'au dixieme : pas de scintillement
        texte = format_chrono(ecoule)
        if texte != self._dernier_texte:
            self._dernier_texte = texte
            self.itemconfigure(self._chrono, text=texte)
            if self._ratio is not None:
                self.itemconfigure(self._sous_titre,
                                   text=f"{100.0 * self._ratio:.0f} %"
                                        + (f"  -  {self.texte}" if self.texte else ""))

    def _place_tete(self, angle_deg):
        """Pastille ronde au bout de la comete : le trait ne s'arrete pas net."""
        r = 0.5 * (self.taille - 2 * (self.EPAISSEUR + 6))
        cx = cy = self.taille / 2.0
        a = math.radians(angle_deg)
        x, y = cx + r * math.cos(a), cy - r * math.sin(a)
        d = self.EPAISSEUR / 2.0
        self.coords(self._tete, x - d, y - d, x + d, y + d)
        self.itemconfigure(self._tete, state=tk.NORMAL)


class Tooltip:
    """Bulle d'aide au survol. Indispensable : les utilisateurs ne connaissent
    ni la HRH ni ses hyperparametres."""

    def __init__(self, widget, text, delay=450, width=320):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.width = width
        self._after = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def set_text(self, text):
        self.text = text

    def _schedule(self, _=None):
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def _show(self):
        if self._tip is not None or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 18
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        except tk.TclError:
            return
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        frame = tk.Frame(self._tip, bg=C["primary"], padx=1, pady=1)
        frame.pack()
        tk.Label(frame, text=self.text, justify="left", wraplength=self.width,
                 bg="#12233A", fg="#EAF1F8", font=FONTS["small"],
                 padx=10, pady=7).pack()

    def _hide(self, _=None):
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


def help_dot(parent, text, bg=None):
    """Petit "?" cliquable/survolable, pose a cote d'un libelle."""
    lbl = tk.Label(parent, text="?", font=FONTS["tiny"], fg=C["muted"],
                   bg=bg or C["surface"], cursor="question_arrow",
                   width=2, relief="flat")
    Tooltip(lbl, text)
    return lbl


# --------------------------------------------------------------------------
# conteneurs
# --------------------------------------------------------------------------

class Card(tk.Frame):
    """Carte blanche a bordure fine, avec titre, sous-titre et zone d'actions."""

    def __init__(self, parent, title=None, subtitle=None, icon=None,
                 padding=14, **kw):
        super().__init__(parent, bg=C["surface"], highlightthickness=1,
                         highlightbackground=C["border"],
                         highlightcolor=C["border"], bd=0, **kw)
        self.header = None
        self.actions = None
        if title:
            self.header = tk.Frame(self, bg=C["surface"])
            self.header.pack(fill=tk.X, padx=padding, pady=(padding, 0))
            left = tk.Frame(self.header, bg=C["surface"])
            left.pack(side=tk.LEFT, fill=tk.X, expand=True)
            text = f"{icon}  {title}" if icon else title
            tk.Label(left, text=text, font=FONTS["h2"], fg=C["primary"],
                     bg=C["surface"], anchor="w").pack(anchor="w")
            if subtitle:
                self.subtitle_lbl = tk.Label(left, text=subtitle, font=FONTS["small"],
                                             fg=C["muted"], bg=C["surface"],
                                             anchor="w", justify="left",
                                             wraplength=320)
                self.subtitle_lbl.pack(anchor="w", pady=(2, 0))
                # le sous-titre doit se replier a la largeur reelle de la carte,
                # sinon il deborde des la premiere colonne etroite.
                self._wrap = 320
                self._pad = padding
                self.bind("<Configure>", self._resize_subtitle)
            self.actions = tk.Frame(self.header, bg=C["surface"])
            self.actions.pack(side=tk.RIGHT)
        self.body = tk.Frame(self, bg=C["surface"])
        self.body.pack(fill=tk.BOTH, expand=True, padx=padding,
                       pady=(10 if title else padding, padding))

    def _resize_subtitle(self, event):
        wrap = max(160, event.width - 2 * self._pad - 10)
        if abs(wrap - self._wrap) > 4:        # evite une boucle de <Configure>
            self._wrap = wrap
            self.subtitle_lbl.config(wraplength=wrap)

    def set_subtitle(self, text):
        if getattr(self, "subtitle_lbl", None) is not None:
            self.subtitle_lbl.config(text=text)


class ScrollFrame(tk.Frame):
    """Zone defilante verticale (colonne de parametres de la page 2)."""

    def __init__(self, parent, bg=None, width=None, **kw):
        bg = bg or C["bg"]
        super().__init__(parent, bg=bg, **kw)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.scroll = ttk.Scrollbar(self, orient="vertical",
                                    command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        if width:
            self.canvas.configure(width=width)

        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._sync_region)
        self.canvas.bind("<Configure>", self._sync_width)
        for w in (self.canvas, self.inner):
            w.bind("<Enter>", self._bind_wheel)
            w.bind("<Leave>", self._unbind_wheel)

    def _show_bar(self, besoin):
        """Affiche la barre si, et seulement si, il y a de quoi defiler."""
        if besoin and not self.scroll.winfo_ismapped():
            self.scroll.pack(side=tk.RIGHT, fill=tk.Y)
        elif not besoin and self.scroll.winfo_ismapped():
            self.scroll.pack_forget()

    def _on_scroll(self, first, last):
        self._show_bar(not (float(first) <= 0.0 and float(last) >= 1.0))
        self.scroll.set(first, last)

    def _sync_region(self, _=None):
        # La barre se decide sur la GEOMETRIE, pas sur `yview()` : juste apres
        # avoir change la region defilante, Tk rend encore l'ancienne fraction
        # (0, 1), et une page deux fois plus haute que la fenetre n'affichait
        # aucune barre -- rien ne laissait deviner qu'il y avait la suite en
        # dessous.
        box = self.canvas.bbox("all")
        self.canvas.configure(scrollregion=box or (0, 0, 0, 0))
        self._show_bar(bool(box)
                       and (box[3] - box[1]) > self.canvas.winfo_height() + 1)

    def _sync_width(self, event):
        self.canvas.itemconfigure(self._win, width=event.width)

    def _bind_wheel(self, _=None):
        self.canvas.bind_all("<MouseWheel>", self._wheel)
        self.canvas.bind_all("<Button-4>", self._wheel)
        self.canvas.bind_all("<Button-5>", self._wheel)

    def _unbind_wheel(self, _=None):
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.unbind_all(seq)

    def _wheel(self, event):
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        else:
            delta = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(delta, "units")


class Section(tk.Frame):
    """Titre de section discret a l'interieur d'une carte."""

    def __init__(self, parent, title, hint=None, bg=None, first=False):
        bg = bg or C["surface"]
        super().__init__(parent, bg=bg)
        row = tk.Frame(self, bg=bg)
        row.pack(fill=tk.X, pady=(0 if first else 14, 6))
        tk.Label(row, text=title.upper(), font=FONTS["tiny"], fg=C["muted"],
                 bg=bg, anchor="w").pack(side=tk.LEFT)
        if hint:
            help_dot(row, hint, bg=bg).pack(side=tk.LEFT, padx=(4, 0))
        tk.Frame(row, bg=C["border_soft"], height=1).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0), pady=(6, 0))


# --------------------------------------------------------------------------
# indicateurs
# --------------------------------------------------------------------------

class StatTile(tk.Frame):
    """Tuile d'indicateur : valeur en gros, libelle en petit, etat colore."""

    def __init__(self, parent, label, value="--", unit="", hint=None,
                 compact=False):
        super().__init__(parent, bg=C["surface"], highlightthickness=1,
                         highlightbackground=C["border_soft"], bd=0)
        self.accent = tk.Frame(self, bg=C["border"], height=3)
        self.accent.pack(fill=tk.X, side=tk.TOP)
        inner = tk.Frame(self, bg=C["surface"])
        inner.pack(fill=tk.BOTH, expand=True, padx=10, pady=(6, 8))

        head = tk.Frame(inner, bg=C["surface"])
        head.pack(fill=tk.X)
        tk.Label(head, text=label.upper(), font=FONTS["tiny"], fg=C["muted"],
                 bg=C["surface"], anchor="w").pack(side=tk.LEFT)
        if hint:
            help_dot(head, hint).pack(side=tk.LEFT, padx=(3, 0))

        row = tk.Frame(inner, bg=C["surface"])
        row.pack(fill=tk.X, pady=(2, 0))
        self.value_lbl = tk.Label(row, text=value, bg=C["surface"],
                                  fg=C["primary"],
                                  font=FONTS["kpi_small"] if compact else FONTS["kpi"],
                                  anchor="w")
        self.value_lbl.pack(side=tk.LEFT)
        self.unit_lbl = tk.Label(row, text=unit, bg=C["surface"], fg=C["muted"],
                                 font=FONTS["small"], anchor="sw")
        self.unit_lbl.pack(side=tk.LEFT, padx=(4, 0), pady=(0, 3))

    def set(self, value, status=None, unit=None):
        self.value_lbl.config(text=str(value))
        if unit is not None:
            self.unit_lbl.config(text=unit)
        colors = {"ok": C["ok"], "warn": C["warn"], "error": C["error"],
                  None: C["primary"], "neutral": C["primary"]}
        color = colors.get(status, C["primary"])
        self.value_lbl.config(fg=color)
        self.accent.config(bg=color if status in ("ok", "warn", "error")
                           else C["border"])


class Banner(tk.Frame):
    """Bandeau d'etat colore (succes / avertissement / erreur / information)."""

    STYLES = {
        "ok": (C["ok_soft"], C["ok"], "OK"),
        "warn": (C["warn_soft"], C["warn"], "!"),
        "error": (C["error_soft"], C["error"], "X"),
        "info": (C["accent_soft"], C["accent"], "i"),
    }

    def __init__(self, parent, text="", kind="info"):
        super().__init__(parent, bg=C["accent_soft"], highlightthickness=1,
                         highlightbackground=C["border_soft"], bd=0)
        self.stripe = tk.Frame(self, bg=C["accent"], width=4)
        self.stripe.pack(side=tk.LEFT, fill=tk.Y)
        self.icon = tk.Label(self, text="i", bg=C["accent_soft"], fg=C["accent"],
                             font=FONTS["bold"], width=3)
        self.icon.pack(side=tk.LEFT, pady=8)
        self.label = tk.Label(self, text=text, bg=C["accent_soft"], fg=C["text"],
                              font=FONTS["small"], anchor="w", justify="left",
                              wraplength=760)
        self.label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 12), pady=8)
        self._wrap = 760
        self.bind("<Configure>", self._resize)
        self.set(text, kind)

    def _resize(self, event):
        wrap = max(240, event.width - 90)
        if abs(wrap - self._wrap) > 8:
            self._wrap = wrap
            self.label.config(wraplength=wrap)

    def set(self, text, kind="info"):
        bg, fg, mark = self.STYLES.get(kind, self.STYLES["info"])
        self.configure(bg=bg)
        self.stripe.configure(bg=fg)
        self.icon.configure(bg=bg, fg=fg, text=mark)
        self.label.configure(bg=bg, text=text)

    def set_wrap(self, width):
        self.label.configure(wraplength=max(200, width))


# --------------------------------------------------------------------------
# journal
# --------------------------------------------------------------------------

class LogConsole(tk.Frame):
    """
    Console integree : miroir du flux CLI, avec filtre de niveau.

    Les messages arrivent depuis des threads de calcul : ils transitent par une
    file, videe par la boucle Tk. C'est la seule facon sure de toucher a un
    widget depuis un autre thread.
    """

    TAGS = {
        applog.DEBUG: ("#8E8E93", ""),
        applog.INFO: ("#E5E5EA", ""),
        applog.OK: ("#63DA83", ""),
        applog.WARN: ("#FFB340", ""),
        applog.ERROR: ("#FF6961", ""),
    }

    def __init__(self, parent, height=8, show_debug=False, collapsed=False):
        super().__init__(parent, bg=C["console_bg"], highlightthickness=1,
                         highlightbackground=C["border"], bd=0)
        self._queue = queue.Queue()
        self._show_debug = bool(show_debug)
        self._paused = False

        bar = tk.Frame(self, bg=C["console_bar"])
        bar.pack(fill=tk.X)
        tk.Label(bar, text="JOURNAL", font=FONTS["tiny"],
                 bg=C["console_bar"], fg=C["console_dim"]).pack(
            side=tk.LEFT, padx=12, pady=5)
        self._count_lbl = tk.Label(bar, text="", font=FONTS["tiny"],
                                   bg=C["console_bar"], fg=C["console_dim"])
        self._count_lbl.pack(side=tk.LEFT, padx=6)

        self._debug_var = tk.BooleanVar(value=self._show_debug)
        tk.Checkbutton(bar, text="details", variable=self._debug_var,
                       command=self._toggle_debug, bg=C["console_bar"],
                       fg=C["console_dim"], selectcolor=C["console_bar"],
                       activebackground=C["console_bar"],
                       activeforeground=C["console_fg"], font=FONTS["tiny"],
                       bd=0, highlightthickness=0).pack(side=tk.RIGHT, padx=(0, 8))
        tk.Button(bar, text="effacer", command=self.clear,
                  bg=C["console_bar"], fg=C["console_dim"],
                  activebackground=C["primary_alt"],
                  activeforeground=C["console_fg"], font=FONTS["tiny"], bd=0,
                  highlightthickness=0, cursor="hand2").pack(side=tk.RIGHT, padx=6)
        # Sur un ecran court, le journal vaut une centaine de pixels : il se
        # replie d'un clic, et sa barre de titre reste la pour le rouvrir.
        self._btn_fold = tk.Button(bar, text="", command=self.toggle,
                                   bg=C["console_bar"], fg=C["console_dim"],
                                   activebackground=C["primary_alt"],
                                   activeforeground=C["console_fg"],
                                   font=FONTS["tiny"], bd=0,
                                   highlightthickness=0, cursor="hand2")
        self._btn_fold.pack(side=tk.RIGHT, padx=6)

        self.text = tk.Text(self, height=height, bg=C["console_bg"],
                            fg=C["console_fg"], font=FONTS["mono"], bd=0,
                            highlightthickness=0, wrap="none", padx=10, pady=6,
                            insertbackground=C["console_fg"], state=tk.DISABLED)
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        for level, (fg, _) in self.TAGS.items():
            self.text.tag_configure(f"lvl{level}", foreground=fg)
        self.text.tag_configure("stamp", foreground="#6E6E73")
        self.text.tag_configure("tag", foreground="#5AA9F0")

        self._body = (self.text, vsb)
        self._collapsed = False
        if collapsed:
            self.toggle()
        else:
            self._btn_fold.config(text="replier")

        self._n = 0
        applog.add_ui_sink(self._sink)
        self.after(150, self._drain)
        self.bind("<Destroy>", lambda e: applog.remove_ui_sink(self._sink))

    def toggle(self):
        """Replie ou deplie le corps du journal."""
        texte, vsb = self._body
        if self._collapsed:
            vsb.pack(side=tk.RIGHT, fill=tk.Y)
            texte.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            self._btn_fold.config(text="replier")
        else:
            texte.pack_forget()
            vsb.pack_forget()
            self._btn_fold.config(text="deplier")
        self._collapsed = not self._collapsed

    # -- appele depuis n'importe quel thread --
    def _sink(self, level, tag, message, when):
        self._queue.put((level, tag, message, when))

    def _toggle_debug(self):
        self._show_debug = bool(self._debug_var.get())

    def _drain(self):
        batch = []
        try:
            while len(batch) < 400:
                batch.append(self._queue.get_nowait())
        except queue.Empty:
            pass
        if batch:
            self.text.configure(state=tk.NORMAL)
            for level, tag, message, when in batch:
                if level < applog.INFO and not self._show_debug:
                    continue
                self.text.insert(tk.END, when.strftime("%H:%M:%S "), "stamp")
                self.text.insert(tk.END, f"{tag:<10s} ", "tag")
                self.text.insert(tk.END, message + "\n", f"lvl{level}")
                self._n += 1
            if self._n > 4000:                 # borne la memoire sur longue session
                self.text.delete("1.0", "1500.0")
                self._n -= 1500
            self.text.configure(state=tk.DISABLED)
            if not self._paused:
                self.text.see(tk.END)
            self._count_lbl.config(text=f"{self._n} ligne(s)")
        try:
            self.after(150, self._drain)
        except tk.TclError:
            pass

    def clear(self):
        self.text.configure(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        self.text.configure(state=tk.DISABLED)
        self._n = 0
        self._count_lbl.config(text="")


# --------------------------------------------------------------------------
# champ de parametre
# --------------------------------------------------------------------------

class ParamField(tk.Frame):
    """
    Ligne "libelle - champ - unite" avec aide au survol et validation.

    `kind` vaut "float", "int" ou "text". La validation ne bloque pas la saisie
    (frustrant) : elle colore le champ et remonte un message a la validation
    du formulaire.
    """

    def __init__(self, parent, label, value, unit="", hint="", kind="float",
                 vmin=None, vmax=None, width=9, bg=None):
        bg = bg or C["surface"]
        super().__init__(parent, bg=bg)
        self.kind, self.vmin, self.vmax = kind, vmin, vmax
        self.label_text = label

        head = tk.Frame(self, bg=bg)
        head.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.label = tk.Label(head, text=label, bg=bg, fg=C["text"],
                              font=FONTS["small"], anchor="w")
        self.label.pack(side=tk.LEFT)
        if hint:
            help_dot(head, hint, bg=bg).pack(side=tk.LEFT, padx=(2, 0))

        self.var = tk.StringVar(value=str(value))
        self.entry = ttk.Entry(self, textvariable=self.var, width=width,
                               justify="right")
        self.entry.pack(side=tk.LEFT, padx=(6, 4))
        self.var.trace_add("write", lambda var, indx, mode: self._live_check())
        if unit:
            tk.Label(self, text=unit, bg=bg, fg=C["muted"], font=FONTS["tiny"],
                     width=4, anchor="w").pack(side=tk.LEFT)

    # -- valeur --
    def get(self):
        raw = self.var.get().strip().replace(",", ".")
        if self.kind == "text":
            return raw
        if raw == "":
            raise ValueError(f"{self.label_text} : valeur manquante.")
        try:
            val = float(raw)
        except ValueError:
            raise ValueError(f"{self.label_text} : \"{raw}\" n'est pas un nombre.")
        if self.vmin is not None and val < self.vmin:
            raise ValueError(f"{self.label_text} : minimum {self.vmin:g}.")
        if self.vmax is not None and val > self.vmax:
            raise ValueError(f"{self.label_text} : maximum {self.vmax:g}.")
        return int(round(val)) if self.kind == "int" else val

    def set(self, value):
        self.var.set(str(value))

    def is_valid(self):
        try:
            self.get()
            return True
        except ValueError:
            return False

    def _live_check(self):
        self.entry.configure(style="TEntry" if self.is_valid() else "Error.TEntry")

    def set_state(self, state):
        self.entry.configure(state=state)


class SliderField(tk.Frame):
    """Curseur pour un reglage que l'on veut faire sentir plutot que taper
    (compromis longueur / groupage de l'article : w_B)."""

    def __init__(self, parent, label, value, vmin, vmax, hint="", fmt="{:.2f}",
                 left="", right="", bg=None, command=None):
        bg = bg or C["surface"]
        super().__init__(parent, bg=bg)
        self.fmt = fmt
        self._command = command

        head = tk.Frame(self, bg=bg)
        head.pack(fill=tk.X)
        tk.Label(head, text=label, bg=bg, fg=C["text"], font=FONTS["small"],
                 anchor="w").pack(side=tk.LEFT)
        if hint:
            help_dot(head, hint, bg=bg).pack(side=tk.LEFT, padx=(2, 0))
        self.value_lbl = tk.Label(head, text=fmt.format(value), bg=bg,
                                  fg=C["accent"], font=FONTS["bold"])
        self.value_lbl.pack(side=tk.RIGHT)

        self.var = tk.DoubleVar(value=float(value))
        self.scale = ttk.Scale(self, from_=vmin, to=vmax, variable=self.var,
                               orient="horizontal", command=self._on_move)
        self.scale.pack(fill=tk.X, pady=(4, 0))

        legend = tk.Frame(self, bg=bg)
        legend.pack(fill=tk.X)
        tk.Label(legend, text=left, bg=bg, fg=C["muted"],
                 font=FONTS["tiny"]).pack(side=tk.LEFT)
        tk.Label(legend, text=right, bg=bg, fg=C["muted"],
                 font=FONTS["tiny"]).pack(side=tk.RIGHT)

    def _on_move(self, _=None):
        self.value_lbl.config(text=self.fmt.format(self.var.get()))
        if self._command:
            self._command(self.var.get())

    def get(self):
        return float(self.var.get())

    def set(self, value):
        self.var.set(float(value))
        self._on_move()


# --------------------------------------------------------------------------
# divers
# --------------------------------------------------------------------------

def separator(parent, pad=10, bg=None):
    f = tk.Frame(parent, bg=bg or C["border_soft"], height=1)
    f.pack(fill=tk.X, pady=pad)
    return f


def key_value_table(parent, rows, bg=None, key_width=24):
    """Petit tableau cle/valeur aligne (resume de maquette, resume de calcul)."""
    bg = bg or C["surface"]
    frame = tk.Frame(parent, bg=bg)
    for i, (k, v) in enumerate(rows):
        tk.Label(frame, text=k, bg=bg, fg=C["muted"], font=FONTS["small"],
                 anchor="w", width=key_width).grid(row=i, column=0, sticky="w",
                                                   pady=2)
        tk.Label(frame, text=str(v), bg=bg, fg=C["text"], font=FONTS["bold"],
                 anchor="w").grid(row=i, column=1, sticky="w", pady=2)
    frame.columnconfigure(1, weight=1)
    return frame


def set_children_state(widget, state):
    """Active/desactive recursivement une branche de widgets."""
    for child in widget.winfo_children():
        try:
            child.configure(state=state)
        except tk.TclError:
            pass
        set_children_state(child, state)


# --------------------------------------------------------------------------
# commandes dessinees : ce que ttk ne sait pas arrondir
# --------------------------------------------------------------------------

class PillButton(tk.Canvas):
    """
    Bouton a coins ronds, dessine sur un canevas.

    Tkinter ne sait pas arrondir un bouton natif -- et c'est precisement ce
    qui trahit une interface "faite en Tk". On dessine donc la pilule
    soi-meme : aplat plein pour l'action principale, teinte tres claire pour
    les actions secondaires, simple texte pour les actions discretes.

    `kind` vaut "filled", "tinted", "plain" ou "danger".
    """

    HAUTEUR = 38
    PAD_X = 22

    STYLES = {
        "filled": dict(fond="accent", texte="on_primary", bord=None),
        "success": dict(fond="ok", texte="on_primary", bord=None),
        "tinted": dict(fond="accent_soft", texte="accent", bord=None),
        "plain": dict(fond="surface", texte="text", bord="border"),
        "danger": dict(fond="error_soft", texte="error", bord=None),
        "ghost": dict(fond=None, texte="accent", bord=None),
    }

    def __init__(self, parent, text, command=None, kind="filled", width=None,
                 height=None, bg=None, font=None, icon=None, state=tk.NORMAL):
        self.kind = kind if kind in self.STYLES else "filled"
        self.bg = bg or (parent["bg"] if isinstance(parent, tk.Frame) else C["surface"])
        self.font = font or FONTS["bold"]
        self.texte = f"{icon}  {text}" if icon else text
        self.command = command
        self.hauteur = int(height or self.HAUTEUR)
        self._etat = state
        self._survol = False
        self._enfonce = False

        largeur = int(width or self._largeur_texte() + 2 * self.PAD_X)
        super().__init__(parent, width=largeur, height=self.hauteur, bg=self.bg,
                         highlightthickness=0, bd=0,
                         cursor="hand2" if state == tk.NORMAL else "arrow")
        self.largeur = largeur

        self._forme = None
        self._label = None
        self._dessine()

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Configure>", lambda _e: self._dessine())

    # -- mesure ---------------------------------------------------------
    def _largeur_texte(self):
        try:
            from tkinter import font as tkfont
            return tkfont.Font(font=self.font).measure(self.texte)
        except tk.TclError:
            return 8 * len(self.texte)

    # -- rendu ----------------------------------------------------------
    def _couleurs(self):
        s = self.STYLES[self.kind]
        fond = C[s["fond"]] if s["fond"] else self.bg
        texte = C[s["texte"]]
        bord = C[s["bord"]] if s["bord"] else ""
        if self._etat == tk.DISABLED:
            return (mix(fond, self.bg, 0.55), mix(texte, self.bg, 0.55),
                    mix(bord, self.bg, 0.6) if bord else "")
        if self._enfonce:
            fond = mix(fond, "#000000", 0.14 if s["fond"] else 0.0)
            if not s["fond"]:
                fond = mix(self.bg, C["accent"], 0.14)
        elif self._survol:
            fond = mix(fond, "#000000", 0.07) if s["fond"] else \
                mix(self.bg, C["accent"], 0.07)
            bord = C["accent"] if bord else bord
        return fond, texte, bord

    def _dessine(self):
        self.delete("all")
        w = int(self.winfo_width() or self.largeur)
        h = int(self.winfo_height() or self.hauteur)
        fond, texte, bord = self._couleurs()
        r = h / 2.0
        round_rect(self, 1, 1, w - 1, h - 1, r, fill=fond,
                   outline=bord or fond, width=1)
        self.create_text(w / 2.0, h / 2.0 + 1, text=self.texte, fill=texte,
                         font=self.font)

    # -- interactions ---------------------------------------------------
    def _on_enter(self, _e=None):
        if self._etat != tk.DISABLED:
            self._survol = True
            self._dessine()

    def _on_leave(self, _e=None):
        self._survol = self._enfonce = False
        self._dessine()

    def _on_press(self, _e=None):
        if self._etat != tk.DISABLED:
            self._enfonce = True
            self._dessine()

    def _on_release(self, _e=None):
        if self._etat == tk.DISABLED:
            return
        lance = self._enfonce
        self._enfonce = False
        self._dessine()
        if lance and self.command:
            self.command()

    # -- API facon ttk ---------------------------------------------------
    def configure(self, **kw):                       # noqa: D401 - API Tk
        if "text" in kw:
            self.texte = kw.pop("text")
        if "state" in kw:
            self._etat = kw.pop("state")
            try:
                self.config(cursor="hand2" if self._etat == tk.NORMAL else "arrow")
            except tk.TclError:
                pass
        if "command" in kw:
            self.command = kw.pop("command")
        if "kind" in kw:
            self.kind = kw.pop("kind")
        if kw:
            super().configure(**kw)
        self._dessine()

    config = configure

    def set_text(self, text):
        self.configure(text=text)


class SegmentedControl(tk.Canvas):
    """
    Controle segmente facon iOS : deux ou trois options, une seule active.

    C'est la bonne commande pour un choix exclusif et court -- HRH ou SHRH --
    la ou deux boutons radio font formulaire administratif. Le curseur blanc
    glisse d'un segment a l'autre.
    """

    HAUTEUR = 34
    MARGE = 3

    def __init__(self, parent, options, value=None, command=None, width=None,
                 bg=None, font=None):
        self.options = [(o, o) if isinstance(o, str) else tuple(o)
                        for o in options]
        self.bg = bg or C["surface"]
        self.font = font or FONTS["small"]
        self.command = command
        self.value = value if value is not None else self.options[0][0]

        largeur = int(width or max(92 * len(self.options), 200))
        super().__init__(parent, width=largeur, height=self.HAUTEUR, bg=self.bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.largeur = largeur
        self._cible = self._index()          # position visee du curseur
        self._pos = float(self._cible)       # position animee
        self._job = None

        self.bind("<Button-1>", self._on_click)
        self.bind("<Configure>", lambda _e: self._dessine())
        self.bind("<Destroy>", lambda _e: self._stop())
        self._dessine()

    def _index(self, value=None):
        cible = self.value if value is None else value
        for i, (cle, _lib) in enumerate(self.options):
            if cle == cible:
                return i
        return 0

    def get(self):
        return self.value

    def set(self, value, notifier=False):
        if self._index(value) == self._index() and value == self.value:
            return
        self.value = value
        self._cible = self._index()
        self._anime()
        if notifier and self.command:
            self.command(self.value)

    def _on_click(self, event):
        w = int(self.winfo_width() or self.largeur)
        i = min(len(self.options) - 1,
                max(0, int(event.x / (w / float(len(self.options))))))
        cle = self.options[i][0]
        if cle != self.value:
            self.value = cle
            self._cible = i
            self._anime()
            if self.command:
                self.command(cle)

    def _stop(self):
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def _anime(self):
        """Glissement amorti du curseur : 8 images suffisent a le rendre doux."""
        self._stop()

        def pas():
            ecart = self._cible - self._pos
            if abs(ecart) < 0.01:
                self._pos = float(self._cible)
                self._dessine()
                self._job = None
                return
            self._pos += ecart * 0.35
            self._dessine()
            self._job = self.after(16, pas)

        pas()

    def _dessine(self):
        self.delete("all")
        w = int(self.winfo_width() or self.largeur)
        h = int(self.winfo_height() or self.HAUTEUR)
        n = max(1, len(self.options))
        round_rect(self, 0, 0, w, h, h / 2.0, fill=C["surface_sunk"],
                   outline=C["surface_sunk"])

        larg = (w - 2 * self.MARGE) / float(n)
        x = self.MARGE + self._pos * larg
        round_rect(self, x, self.MARGE, x + larg, h - self.MARGE,
                   (h - 2 * self.MARGE) / 2.0, fill=C["surface"],
                   outline=C["border_soft"])

        for i, (cle, libelle) in enumerate(self.options):
            actif = abs(self._pos - i) < 0.5
            self.create_text(self.MARGE + (i + 0.5) * larg, h / 2.0 + 1,
                             text=libelle,
                             fill=C["text"] if actif else C["muted"],
                             font=FONTS["bold"] if actif else self.font)


class Chip(tk.Canvas):
    """Petite pastille d'information a coins ronds (etat, compteur, filtre)."""

    def __init__(self, parent, text="", kind="neutral", bg=None, font=None):
        self.bg = bg or C["surface"]
        self.font = font or FONTS["tiny"]
        self.texte = text
        self.kind = kind
        super().__init__(parent, height=22, bg=self.bg, highlightthickness=0,
                         bd=0, width=self._largeur())
        self.bind("<Configure>", lambda _e: self._dessine())
        self._dessine()

    TONS = {
        "neutral": ("surface_sunk", "muted"),
        "info": ("accent_soft", "accent"),
        "ok": ("ok_soft", "ok"),
        "warn": ("warn_soft", "warn"),
        "error": ("error_soft", "error"),
    }

    def _largeur(self):
        try:
            from tkinter import font as tkfont
            return tkfont.Font(font=self.font).measure(self.texte) + 22
        except tk.TclError:
            return 7 * len(self.texte) + 22

    def set(self, text=None, kind=None):
        if text is not None:
            self.texte = text
        if kind is not None:
            self.kind = kind
        self.configure(width=self._largeur())
        self._dessine()

    def _dessine(self):
        self.delete("all")
        w = int(self.winfo_width() or self._largeur())
        h = int(self.winfo_height() or 22)
        fond, texte = self.TONS.get(self.kind, self.TONS["neutral"])
        round_rect(self, 0, 1, w, h - 1, (h - 2) / 2.0, fill=C[fond],
                   outline=C[fond])
        self.create_text(w / 2.0, h / 2.0, text=self.texte, fill=C[texte],
                         font=self.font)


class SoftCard(tk.Frame):
    """
    Carte blanche a coins ronds et ombre douce.

    Tk ne connait ni `border-radius` ni `box-shadow` : l'ombre est un canevas
    place derriere la carte, sur lequel on empile trois rectangles arrondis
    de plus en plus clairs. De pres ce n'est pas un flou gaussien ; a l'ecran,
    la carte decolle du fond exactement comme il faut.
    """

    def __init__(self, parent, padding=18, radius=None, bg=None, shadow=True,
                 **kw):
        self.fond = bg or C["bg"]
        self.rayon = int(radius or RADIUS["lg"])
        super().__init__(parent, bg=self.fond, **kw)

        self._fond_canvas = tk.Canvas(self, bg=self.fond, highlightthickness=0,
                                      bd=0)
        self._fond_canvas.place(x=0, y=0, relwidth=1.0, relheight=1.0)
        self._ombre = bool(shadow)

        self.body = tk.Frame(self, bg=C["surface"])
        self.body.pack(fill=tk.BOTH, expand=True, padx=padding, pady=padding)
        self.bind("<Configure>", self._dessine)

    def _dessine(self, _event=None):
        cv = self._fond_canvas
        cv.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4 or h < 4:
            return
        if self._ombre:
            for i, t in enumerate((0.35, 0.55, 0.75)):
                d = 3 - i
                round_rect(cv, d, d + 1, w - d, h - d + 1, self.rayon + d,
                           fill=mix(C["border"], self.fond, t), outline="")
        round_rect(cv, 0, 0, w - 1, h - 3, self.rayon, fill=C["surface"],
                   outline=C["border_soft"])


class HeaderBar(tk.Frame):
    """
    Bandeau de tete : titre, sous-titre, zone d'actions a droite.

    Fond presque noir, texte blanc, un filet clair en bas : la barre de titre
    des applications macOS recentes.
    """

    def __init__(self, parent, title, subtitle="", badge=None, height=74):
        super().__init__(parent, bg=C["primary"], height=height)
        self.pack_propagate(False)

        gauche = tk.Frame(self, bg=C["primary"])
        gauche.pack(side=tk.LEFT, fill=tk.Y, padx=22)
        interieur = tk.Frame(gauche, bg=C["primary"])
        interieur.pack(expand=True)

        ligne = tk.Frame(interieur, bg=C["primary"])
        ligne.pack(anchor="w")
        self.lbl_titre = tk.Label(ligne, text=title, bg=C["primary"],
                                  fg=C["on_primary"], font=FONTS["h1"])
        self.lbl_titre.pack(side=tk.LEFT)
        if badge:
            Chip(ligne, text=badge, kind="info",
                 bg=C["primary"]).pack(side=tk.LEFT, padx=10, pady=(6, 0))

        self.lbl_sous = tk.Label(interieur, text=subtitle, bg=C["primary"],
                                 fg=C["on_primary_muted"], font=FONTS["small"],
                                 anchor="w", justify="left")
        self.lbl_sous.pack(anchor="w", pady=(2, 0))

        self.actions = tk.Frame(self, bg=C["primary"])
        self.actions.pack(side=tk.RIGHT, padx=18)

    def set_subtitle(self, text):
        self.lbl_sous.config(text=text)

    def set_title(self, text):
        self.lbl_titre.config(text=text)


class StepDots(tk.Canvas):
    """
    Fil d'etapes minimaliste : des pastilles reliees par un trait.

    L'etape en cours s'allonge en gelule et porte son nom ; les etapes
    franchies se remplissent d'une coche ; les suivantes restent des cercles
    vides numerotes. C'est lisible d'un coup d'oeil, et ca tient sur une
    seule ligne meme avec cinq etapes.
    """

    HAUTEUR = 30

    def __init__(self, parent, steps, bg=None, height=None):
        self.etapes = list(steps)
        self.bg = bg or C["bg"]
        self.index = 0
        super().__init__(parent, height=int(height or self.HAUTEUR), bg=self.bg,
                         highlightthickness=0, bd=0)
        self.bind("<Configure>", lambda _e: self._dessine())

    def set_index(self, index):
        self.index = max(0, min(len(self.etapes) - 1, int(index)))
        self._dessine()

    def _mesure(self, texte, font):
        try:
            from tkinter import font as tkfont
            return tkfont.Font(font=font).measure(texte)
        except tk.TclError:
            return 7 * len(texte)

    def _dessine(self):
        self.delete("all")
        w = int(self.winfo_width() or 480)
        h = int(self.winfo_height() or self.HAUTEUR)
        n = max(1, len(self.etapes))
        cy = h / 2.0
        pas = w / float(n)
        r = min(9.0, (h - 6) / 2.0)

        demi = []                      # demi-largeur de chaque jalon
        for i, nom in enumerate(self.etapes):
            if i == self.index:
                demi.append(self._mesure(nom, FONTS["tiny"]) / 2.0 + 14.0)
            else:
                demi.append(r)

        for i, nom in enumerate(self.etapes):
            cx = (i + 0.5) * pas
            fait = i < self.index
            actif = i == self.index
            if i:
                x0 = (i - 0.5) * pas + demi[i - 1] + 6
                x1 = cx - demi[i] - 6
                if x1 > x0:
                    self.create_line(x0, cy, x1, cy, width=2,
                                     fill=C["accent"] if fait else C["border_soft"])
            if actif:
                round_rect(self, cx - demi[i], cy - r, cx + demi[i], cy + r, r,
                           fill=C["accent"], outline=C["accent"])
                self.create_text(cx, cy + 1, text=nom, fill=C["on_primary"],
                                 font=FONTS["tiny"])
            elif fait:
                self.create_oval(cx - r, cy - r, cx + r, cy + r,
                                 fill=C["accent"], outline=C["accent"])
                # coche dessinee : aucune police ne la garantit
                self.create_line(cx - 4, cy, cx - 1, cy + 3.5, cx + 4.5, cy - 4,
                                 fill=C["on_primary"], width=2,
                                 capstyle=tk.ROUND, joinstyle=tk.ROUND)
            else:
                self.create_oval(cx - r, cy - r, cx + r, cy + r, fill=self.bg,
                                 outline=C["border"], width=2)
                self.create_text(cx, cy + 1, text=str(i + 1), fill=C["faint"],
                                 font=FONTS["tiny"])
