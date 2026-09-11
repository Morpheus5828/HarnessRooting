# ui/app.py
from __future__ import annotations

import tkinter as tk

from .theme import C, FONTS, apply_theme, screen_profile, style_matplotlib
from .page_import import ImportPage
from .page_routing import RoutingPage
from .plots import set_figure_scale

VERSION = "1.0.0"
TITLE = "HarnessOpt - cheminement de harnais helicoptere"

STEPS = [("import", "1", "Maquette 3D", "Importer l'environnement CATIA"),
         ("routing", "2", "Cheminement", "Calculer et exporter le harnais")]

class StepBar(tk.Frame):
    """Fil d'Ariane cliquable dans le bandeau."""

    def __init__(self, parent, on_click):
        super().__init__(parent, bg=C["primary"])
        self.on_click = on_click
        self.items = {}
        for i, (key, num, title, subtitle) in enumerate(STEPS):
            if i:
                tk.Label(self, text=">", bg=C["primary"], fg="#5C7CB0",
                         font=FONTS["h3"]).pack(side=tk.LEFT, padx=10)
            item = tk.Frame(self, bg=C["primary"], cursor="hand2")
            item.pack(side=tk.LEFT)
            badge = tk.Label(item, text=num, bg="#1B3A73", fg="#8FB2E0",
                             font=FONTS["badge"], width=3, height=1)
            badge.pack(side=tk.LEFT, padx=(0, 8), pady=2)
            texts = tk.Frame(item, bg=C["primary"])
            texts.pack(side=tk.LEFT)
            lbl = tk.Label(texts, text=title, bg=C["primary"], fg="#9FB6D9",
                           font=FONTS["bold"], anchor="w")
            lbl.pack(anchor="w")
            sub = tk.Label(texts, text=subtitle, bg=C["primary"], fg="#5C7CB0",
                           font=FONTS["tiny"], anchor="w")
            sub.pack(anchor="w")
            for w in (item, badge, texts, lbl, sub):
                w.bind("<Button-1>", lambda e, k=key: self.on_click(k))
            self.items[key] = (badge, lbl, sub)

    def select(self, key):
        for k, (badge, lbl, sub) in self.items.items():
            active = (k == key)
            badge.config(bg=C["accent"] if active else "#1B3A73",
                         fg="#FFFFFF" if active else "#8FB2E0")
            lbl.config(fg="#FFFFFF" if active else "#9FB6D9")
            sub.config(fg="#B9CDE0" if active else "#5C7CB0")


class HarnessAppView:
    """La Vue principale : gère l'ossature Tkinter (bandeau, conteneur, statut)."""
    
    def __init__(self, root: tk.Tk, controller, state):
        self.root = root
        self.controller = controller
        self.controller.view = self
        self.state = state
        self.pages = {}

        self.screen = screen_profile(root)
        self.compact = bool(self.screen["compact"])
        larg = max(900, min(1640, self.screen["width"] - 60))
        haut = max(560, min(1000, self.screen["height"] - 90))

        apply_theme(root, compact=self.compact)
        style_matplotlib()
        set_figure_scale(0.78 if self.screen["tres_compact"]
                         else 0.88 if self.compact else 1.0)
        
        self.root.title(f"{TITLE}  -  v{VERSION}")
        self.root.geometry(f"{larg}x{haut}+20+20")
        self.root.minsize(min(1024, larg), min(600, haut))
        
        self._build_header()
        self._build_body()
        self._build_status()

    def _build_header(self):
        head = tk.Frame(self.root, bg=C["primary"], height=68)
        head.pack(fill=tk.X)
        head.pack_propagate(False)

        left = tk.Frame(head, bg=C["primary"])
        left.pack(side=tk.LEFT, padx=18)
        tk.Label(left, text="HarnessOpt", bg=C["primary"], fg="#FFFFFF",
                 font=FONTS["h1"]).pack(anchor="w", pady=(10, 0))
        tk.Label(left, text="Cheminement automatique de harnais - "
                            "heuristique HRH (Karlsson et al., 2023)",
                 bg=C["primary"], fg="#8FB2E0",
                 font=FONTS["tiny"]).pack(anchor="w")

        # Le bouton navigue en appelant le contrôleur
        self.stepbar = StepBar(head, self.controller.show_page)
        self.stepbar.pack(side=tk.LEFT, padx=40)

        right = tk.Frame(head, bg=C["primary"])
        right.pack(side=tk.RIGHT, padx=18)
        self.lbl_catia = tk.Label(right, text="", bg=C["primary"], fg="#8FB2E0",
                                  font=FONTS["small"], justify="right")
        self.lbl_catia.pack(anchor="e", pady=(16, 0))
        tk.Label(right, text=f"v{VERSION}", bg=C["primary"], fg="#4E6FA6",
                 font=FONTS["tiny"]).pack(anchor="e")

    def _build_body(self):
        self.container = tk.Frame(self.root, bg=C["bg"])
        self.container.pack(fill=tk.BOTH, expand=True)
        # On injecte le contrôleur dans les sous-pages plutôt que "self"
        self.pages["import"] = ImportPage(self.container, self.controller, self.state)
        self.pages["routing"] = RoutingPage(self.container, self.controller, self.state)

    def _build_status(self):
        bar = tk.Frame(self.root, bg=C["surface"], height=26)
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        bar.pack_propagate(False)
        self.lbl_status = tk.Label(bar, text="Pret.", bg=C["surface"],
                                   fg=C["muted"], font=FONTS["tiny"], anchor="w")
        self.lbl_status.pack(side=tk.LEFT, padx=14)
        tk.Label(bar, text="Les messages detailles sont aussi ecrits sur la "
                           "console (utile pour le debogage).",
                 bg=C["surface"], fg=C["muted"],
                 font=FONTS["tiny"]).pack(side=tk.RIGHT, padx=14)

    def display_page(self, key: str):
        """Exécute l'affichage de la page (appelé par le contrôleur)."""
        for name, page in self.pages.items():
            if name != key:
                page.pack_forget()
            else:
                page.pack(fill=tk.BOTH, expand=True)
        self.stepbar.select(key)

    def set_status(self, text: str):
        self.lbl_status.config(text=text)

    def set_catia_status(self, text: str, fg_color: str):
        self.lbl_catia.config(text=text, fg=fg_color)
