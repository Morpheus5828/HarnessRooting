# ui/page_import.py
from __future__ import annotations
import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .theme import C, FONTS
from .widgets import Banner, Card, LogConsole, ParamField, ScrollFrame, Section, StatTile, key_value_table, separator
from .plots import ScenePreview
from ..catia import bridge, mesh as meshio
from ..core import fixations
from ..log import Logger

LOG = Logger("page-import")

def _fmt_duration(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"

class ImportPage(tk.Frame):
    def __init__(self, parent, controller, state):
        super().__init__(parent, bg=C["bg"])
        self.controller = controller
        self.state = state
        self.compact = bool(getattr(controller.view, "compact", False))
        self.scan_info = None
        self._build()

    def _build(self):
        self._build_footer()
        body = tk.Frame(self, bg=C["bg"])
        body.pack(fill=tk.BOTH, expand=True, padx=18, pady=(14, 0))
        body.columnconfigure(0, weight=3, uniform="col")
        body.columnconfigure(1, weight=4, uniform="col")
        body.rowconfigure(0, weight=1)

        left = ScrollFrame(body, bg=C["bg"])
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 9))
        right = ScrollFrame(body, bg=C["bg"])
        right.grid(row=0, column=1, sticky="nsew")

        self._build_source(left.inner)
        self._build_fixations(left.inner)
        self._build_summary(right.inner)
        self._build_preview(right.inner)

    def _build_source(self, parent):
        card = Card(parent, "Source de la maquette 3D", icon="1", subtitle="Extraire l'assemblage ouvert dans CATIA V5, ou reprendre un dossier de STL.")
        card.pack(fill=tk.X, pady=(0, 12))
        b = card.body

        Section(b, "Depuis CATIA V5", first=True).pack(fill=tk.X)
        self.lbl_catia = tk.Label(b, text="", bg=C["surface"], fg=C["muted"], font=FONTS["small"], anchor="w", justify="left", wraplength=380)
        self.lbl_catia.pack(fill=tk.X, pady=(0, 6))
        self.field_exclude = ParamField(b, "Exclure des pieces", "", unit="", kind="text", width=22)
        self.field_exclude.pack(fill=tk.X, pady=(0, 8))

        row = tk.Frame(b, bg=C["surface"])
        row.pack(fill=tk.X)
        self.btn_scan = ttk.Button(row, text="Analyser l'assemblage actif", command=lambda: self.controller.action_count(self.field_exclude.get()))
        self.btn_scan.pack(side=tk.LEFT)
        self.btn_extract = ttk.Button(row, text="Extraire les pieces", style="Primary.TButton", command=self.action_extract_click)
        self.btn_extract.pack(side=tk.LEFT, padx=(8, 0))

        separator(b)
        Section(b, "Depuis un dossier de fichiers STL").pack(fill=tk.X)
        row2 = tk.Frame(b, bg=C["surface"])
        row2.pack(fill=tk.X)
        self.btn_folder = ttk.Button(row2, text="Choisir un dossier STL...", command=self.action_pick_folder)
        self.btn_folder.pack(side=tk.LEFT)
        self.btn_cache = ttk.Button(row2, text="Reprendre le cache", command=self.action_load_cache)
        self.btn_cache.pack(side=tk.LEFT, padx=(8, 0))
        self.lbl_folder = tk.Label(b, text="", bg=C["surface"], fg=C["muted"], font=FONTS["small"], anchor="w", justify="left", wraplength=380)
        self.lbl_folder.pack(fill=tk.X, pady=(6, 0))

        separator(b)
        self.progress = ttk.Progressbar(b, mode="determinate", maximum=100)
        self.progress.pack(fill=tk.X)
        prow = tk.Frame(b, bg=C["surface"])
        prow.pack(fill=tk.X, pady=(6, 0))
        self.lbl_progress = tk.Label(prow, text="Prêt.", bg=C["surface"], fg=C["muted"], font=FONTS["small"], anchor="w", justify="left", wraplength=330)
        self.lbl_progress.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.btn_cancel = ttk.Button(prow, text="Annuler", style="Danger.TButton", command=self.controller.cancel_operation, state=tk.DISABLED)
        self.btn_cancel.pack(side=tk.RIGHT)

    def _build_fixations(self, parent):
        card = Card(parent, "Fixations et passages imposés", icon="2", subtitle="Optionnel. Les passages attirent le faisceau.")
        card.pack(fill=tk.X, pady=(0, 12))
        b = card.body

        self.lbl_passages = tk.Label(b, text="Aucun passage chargé.", bg=C["surface"], fg=C["muted"], font=FONTS["small"], anchor="w", justify="left", wraplength=380)
        self.lbl_passages.pack(fill=tk.X, pady=(0, 8))

        row = tk.Frame(b, bg=C["surface"])
        row.pack(fill=tk.X)
        ttk.Button(row, text="Charger un fichier de passages...", command=self.action_load_passages).pack(side=tk.LEFT)
        ttk.Button(row, text="Modèle JSON", style="Ghost.TButton", command=self.action_template).pack(side=tk.LEFT, padx=(8, 0))
        
        row2 = tk.Frame(b, bg=C["surface"])
        row2.pack(fill=tk.X, pady=(8, 0))
        self.btn_clamps = ttk.Button(row2, text="Scanner un dossier de colliers...", command=self.action_scan_clamps)
        self.btn_clamps.pack(side=tk.LEFT)

    def _build_preview(self, parent):
        card = Card(parent, "Aperçu de la maquette", icon="3")
        card.pack(fill=tk.BOTH, expand=True)
        self.preview = ScenePreview(card.body)
        self.preview.pack(fill=tk.BOTH, expand=True)
        row = tk.Frame(card.actions, bg=C["surface"])
        row.pack()
        self.btn_pv = ttk.Button(row, text="Vue 3D interactive", command=self.controller.open_view3d_import, state=tk.DISABLED)
        self.btn_pv.pack(side=tk.LEFT)

    def _build_summary(self, parent):
        card = Card(parent, "Résumé de la maquette", icon="4")
        card.pack(side=tk.BOTTOM, fill=tk.X, pady=(12, 0))
        self.summary_box = tk.Frame(card.body, bg=C["surface"])
        self.summary_box.pack(fill=tk.X)
        self._render_summary(None)

        tiles = tk.Frame(card.body, bg=C["surface"])
        tiles.pack(fill=tk.X, pady=(12, 0))
        for i in range(3): tiles.columnconfigure(i, weight=1, uniform="t")
        self.tile_parts = StatTile(tiles, "Pièces", "--", compact=True)
        self.tile_tri = StatTile(tiles, "Triangles", "--", compact=True)
        self.tile_pts = StatTile(tiles, "Points de collision", "--", compact=True)
        for i, t in enumerate((self.tile_parts, self.tile_tri, self.tile_pts)):
            t.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 6, 0))

    def _build_footer(self):
        foot = tk.Frame(self, bg=C["bg"])
        foot.pack(side=tk.BOTTOM, fill=tk.X, expand=False, padx=18, pady=(12, 14))
        self.banner = Banner(foot, "Commencer par charger la maquette.", "info")
        self.banner.pack(fill=tk.X)
        self.console = LogConsole(foot, height=4 if self.compact else 7, collapsed=self.compact)
        self.console.pack(fill=tk.BOTH, expand=True, pady=(10, 10))
        
        bar = tk.Frame(foot, bg=C["bg"])
        bar.pack(fill=tk.X)
        tk.Label(bar, text=f"Cache de travail : {bridge.BASE_CACHE}", bg=C["bg"], fg=C["muted"], font=FONTS["tiny"]).pack(side=tk.LEFT)
        self.btn_next = ttk.Button(bar, text="Continuer vers le cheminement  >", style="Primary.TButton", state=tk.DISABLED, command=lambda: self.controller.show_page("routing"))
        self.btn_next.pack(side=tk.RIGHT)

    # ==================================================================
    # Actions UI vers Contrôleur
    # ==================================================================
    def action_extract_click(self):
        n = (self.scan_info or {}).get("n_parts")
        if n and n > 400 and not messagebox.askyesno("Extraction volumineuse", f"L'assemblage contient {n} pièces.\nContinuer ?"):
            return
        self.controller.action_extract(self.field_exclude.get())

    def action_pick_folder(self):
        folder = filedialog.askdirectory(title="Dossier contenant les STL")
        if not folder: return
        files = meshio.list_stl(folder)
        self.lbl_folder.config(text=f"{os.path.basename(folder) or folder} : {len(files)} fichier(s) STL")
        if not files:
            messagebox.showwarning("Dossier vide", "Aucun fichier .stl trouvé.")
            return
        self.controller.action_load_folder(folder)

    def action_load_cache(self):
        folder = str(bridge.STL_FOLDER)
        files = meshio.list_stl(folder)
        if not files:
            messagebox.showinfo("Cache vide", "Aucune extraction précédente.")
            return
        self.lbl_folder.config(text=f"Cache : {len(files)} fichier(s) STL")
        self.controller.action_load_folder(folder, "Cache d'extraction")

    def action_load_passages(self):
        path = filedialog.askopenfilename(title="Fichier de passages", filetypes=[("Passages", "*.json *.csv *.txt"), ("Tous", "*.*")])
        if path: self.controller.action_load_passages_file(path)

    def action_template(self):
        path = filedialog.asksaveasfilename(title="Enregistrer un modèle", defaultextension=".json", initialfile="passages_modele.json", filetypes=[("JSON", "*.json")])
        if path:
            fixations.write_passages_template(path)
            messagebox.showinfo("Modèle enregistré", f"Modèle écrit dans :\n{path}")

    def action_scan_clamps(self):
        folder = filedialog.askdirectory(title="Dossier des STL de colliers")
        if folder: self.controller.action_scan_clamps(folder)

    # ==================================================================
    # Mises à jour appelées par le Contrôleur
    # ==================================================================
    def set_busy_state(self, busy, message=""):
        state = tk.DISABLED if busy else tk.NORMAL
        for w in (self.btn_scan, self.btn_extract, self.btn_folder, self.btn_cache, self.btn_clamps):
            w.config(state=state)
        self.btn_cancel.config(state=tk.NORMAL if busy else tk.DISABLED)
        if message: self.lbl_progress.config(text=message)
        if busy: self.progress.config(value=0)

    def on_count_done(self, info):
        self.scan_info = info
        self.lbl_catia.config(text=f"Document actif : {info['document']}\n{info['n_parts']} pièce(s) (analyse en {info['seconds']:.1f} s)")
        self.banner.set(f"{info['n_parts']} pièce(s) détectées. Extraction estimée à {_fmt_duration(info['n_parts'] * 0.9)}.", "info")
        self.set_busy_state(False, "Analyse terminée.")

    def on_extract_progress(self, done, total, elapsed):
        if total:
            self.progress.config(value=100.0 * done / total)
            eta = (elapsed / done * (total - done)) if done else 0
            self.lbl_progress.config(text=f"Extraction : {done} / {total} pièce(s) - {_fmt_duration(elapsed)} écoulé, ~{_fmt_duration(eta)} restant")
        else:
            self.progress.config(value=4)
            self.lbl_progress.config(text=f"Analyse de l'arbre... ({_fmt_duration(elapsed)})")

    def on_load_progress(self, done, total, name):
        self.progress.config(value=100.0 * done / max(total, 1))
        self.lbl_progress.config(text=f"Lecture des STL : {done} / {total} - {name}")

    def on_scan_progress(self, done, total, name):
        self.progress.config(value=100.0 * done / max(total, 1))
        self.lbl_progress.config(text=f"Scan des colliers : {done} / {total} - {name}")

    def on_cloud_progress(self, msg):
        self.progress.config(value=99)
        self.lbl_progress.config(text=f"Préparation : {msg}")

    def on_scene_loaded(self, info):
        self.set_busy_state(False, "Maquette chargée.")
        self.progress.config(value=100)
        self._render_summary(info)
        self.tile_parts.set(f"{info.n_loaded}", "ok" if not info.n_failed else "warn")
        self.tile_tri.set(f"{info.n_cells / 1000:.0f}", unit="k")
        self.tile_pts.set(f"{info.n_cloud / 1000:.0f}", unit="k")
        self.preview.show_cloud(meshio.display_cloud(self.state.cloud))
        self.btn_pv.config(state=tk.NORMAL)
        self.btn_next.config(state=tk.NORMAL)
        self.banner.set(f"Maquette prête : {info.n_loaded} pièce(s), {info.n_cloud:,} points. Passer au cheminement.".replace(",", " "), "ok" if not info.n_failed else "warn")

    def on_passages_loaded(self, result):
        detail = getattr(result, "detail", "") if result else ""
        if result and getattr(result, "ran", False):
            self.lbl_passages.config(text=f"{result.n_passages} passage(s) disponibles.\n{detail}", fg=C["ok"])
            self.banner.set(f"{result.n_passages} passage(s) pris en compte.", "ok")
        else:
            self.lbl_passages.config(text=detail or "Aucun passage chargé.", fg=C["warn"] if detail else C["muted"])
            if detail: self.banner.set(f"Scan sans résultat. {detail}", "warn")

    def on_cancelled(self):
        self.set_busy_state(False, "Opération annulée.")
        self.banner.set("Opération annulée par l'utilisateur.", "warn")

    def on_error(self, detail, title):
        LOG.error(f"{title} : {detail}")
        self.set_busy_state(False, "Échec.")
        self.banner.set(f"{title} - {detail.splitlines()[0]}", "error")
        messagebox.showerror(title, detail)

    def _render_summary(self, info):
        for w in self.summary_box.winfo_children(): w.destroy()
        if not info:
            tk.Label(self.summary_box, text="Aucune maquette chargée.", bg=C["surface"], fg=C["muted"], font=FONTS["small"]).pack()
            return
        key_value_table(self.summary_box, info.as_rows()).pack(fill=tk.X)
