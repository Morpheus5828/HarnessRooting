"""
Briques d'interface reutilisables : cartes, tuiles d'indicateurs, console de
journal, champs de parametres avec aide contextuelle, bandeaux d'etat.

Les widgets sont construits sur `tk.Frame` plutot que `ttk.Frame` quand ils ont
besoin d'une bordure fine : c'est le seul moyen d'obtenir le meme rendu sur
Windows, Linux et macOS.
"""

from __future__ import annotations
import time
import queue
import tkinter as tk
from tkinter import ttk

from .theme import C, FONTS
from .. import log as applog
from ..core.tool import _duree as format_duree


class Spinner(tk.Canvas):
    TAILLE = 132
    EPAISSEUR = 9

    def __init__(self, parent, texte="Rooting is loading, please wait"):
        super().__init__(parent, width=self.TAILLE, height=self.TAILLE,
                         bg=C["surface"], highlightthickness=0)
        self.texte = texte
        self._angle = 0
        self._t0 = None
        self._job = None
        marge = self.EPAISSEUR
        self._boite = (marge, marge, self.TAILLE - marge, self.TAILLE - marge)

        # Explicit coordinates instead of *unpacking to satisfy type checkers
        x0, y0, x1, y1 = self._boite
        self.create_oval(x0, y0, x1, y1, outline=C["border"],
                         width=self.EPAISSEUR)
        self._arc = self.create_arc(x0, y0, x1, y1, start=0, extent=90,
                                    style=tk.ARC, outline=C["accent"],
                                    width=self.EPAISSEUR)
        self._chrono = self.create_text(self.TAILLE // 2, self.TAILLE // 2,
                                        text="0 s", fill=C["text"],
                                        font=FONTS["kpi"])

    def start(self):
        self._t0 = time.time()
        self._anime()

    def stop(self):
        if self._job is not None:
            self.after_cancel(self._job)
            self._job = None

    def elapsed(self) -> float:
        return 0.0 if self._t0 is None else time.time() - self._t0

    def _anime(self):
        self._angle = (self._angle + 6) % 360
        self.itemconfigure(self._arc, start=self._angle)
        secondes = self.elapsed()
        self.itemconfigure(self._chrono, text=format_duree(secondes))
        self._job = self.after(40, self._anime)


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
        applog.DEBUG: ("#7E8FA3", ""),
        applog.INFO: ("#D6E1EC", ""),
        applog.OK: ("#6FD08C", ""),
        applog.WARN: ("#F0BE5A", ""),
        applog.ERROR: ("#FF8A80", ""),
    }

    def __init__(self, parent, height=8, show_debug=False, collapsed=False):
        super().__init__(parent, bg=C["console_bg"], highlightthickness=1,
                         highlightbackground=C["border"], bd=0)
        self._queue = queue.Queue()
        self._show_debug = bool(show_debug)
        self._paused = False

        bar = tk.Frame(self, bg="#16263A")
        bar.pack(fill=tk.X)
        tk.Label(bar, text="JOURNAL", font=FONTS["tiny"], bg="#16263A",
                 fg="#7E8FA3").pack(side=tk.LEFT, padx=10, pady=4)
        self._count_lbl = tk.Label(bar, text="", font=FONTS["tiny"], bg="#16263A",
                                   fg="#7E8FA3")
        self._count_lbl.pack(side=tk.LEFT, padx=6)

        self._debug_var = tk.BooleanVar(value=self._show_debug)
        tk.Checkbutton(bar, text="details", variable=self._debug_var,
                       command=self._toggle_debug, bg="#16263A", fg="#7E8FA3",
                       selectcolor="#16263A", activebackground="#16263A",
                       activeforeground="#D6E1EC", font=FONTS["tiny"],
                       bd=0, highlightthickness=0).pack(side=tk.RIGHT, padx=(0, 8))
        tk.Button(bar, text="effacer", command=self.clear, bg="#16263A",
                  fg="#7E8FA3", activebackground="#22394F", activeforeground="#D6E1EC",
                  font=FONTS["tiny"], bd=0, highlightthickness=0,
                  cursor="hand2").pack(side=tk.RIGHT, padx=6)
        # Sur un ecran court, le journal vaut une centaine de pixels : il se
        # replie d'un clic, et sa barre de titre reste la pour le rouvrir.
        self._btn_fold = tk.Button(bar, text="", command=self.toggle, bg="#16263A",
                                   fg="#7E8FA3", activebackground="#22394F",
                                   activeforeground="#D6E1EC", font=FONTS["tiny"],
                                   bd=0, highlightthickness=0, cursor="hand2")
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
        self.text.tag_configure("stamp", foreground="#5A6E85")
        self.text.tag_configure("tag", foreground="#4E9BD6")

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
