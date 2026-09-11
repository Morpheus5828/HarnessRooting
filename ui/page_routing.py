
from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .config import *
from .theme import C, FONTS
from .widgets import (Banner, Card, LogConsole, ParamField, ScrollFrame, SliderField, StatTile)
from .plots import MetricsDashboard, Result3D
from .schematic import HarnessSchematic
from ..catia.mesh import display_cloud
from ..core import geometry, crabs as crab_mod
from ..core.hrh import bundle_polylines, RoutingParams
from ..log import Logger

LOG = Logger("page-routage")

PRESETS = {
    "Rapide - degrossir une intention": dict(resolution=30.0, eps=1.30, max_sweeps=4, time_budget_s=120.0),
    "Equilibre - usage courant": dict(resolution=20.0, eps=1.15, max_sweeps=10, time_budget_s=300.0),
    "Precis - etude finale": dict(resolution=12.0, eps=1.00, max_sweeps=14, time_budget_s=900.0),
}
DEFAULT_PRESET = "Equilibre - usage courant"

class RoutingPage(tk.Frame):
    def __init__(self, parent, controller, state):
        super().__init__(parent, bg=C["bg"])
        self.controller = controller
        self.state = state
        self.compact = bool(getattr(controller.view, "compact", False))
        
        self.is_running = False
        self.fields = {}
        self.crab_model = None
        self.wishes = []
        self.reporter = None
        
        self._build()
        state.on("scene", lambda info: self._on_scene())
        state.on("passages", lambda r: self._refresh_context())

    def _build(self):
        self._build_footer()
        page = ScrollFrame(self, bg=C["bg"])
        page.pack(fill=tk.BOTH, expand=True, padx=18, pady=(14, 0))
        body = page.inner
        self._build_terminals(body)

        cols = tk.Frame(body, bg=C["bg"])
        cols.pack(fill=tk.BOTH, expand=True, pady=(12, 0))
        larg_gauche = 344 if self.compact else 396
        cols.columnconfigure(0, weight=0, minsize=larg_gauche)
        cols.columnconfigure(1, weight=1)
        cols.rowconfigure(0, weight=1)

        left = tk.Frame(cols, bg=C["bg"], width=larg_gauche)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        right = tk.Frame(cols, bg=C["bg"])
        right.grid(row=0, column=1, sticky="nsew")

        self._build_params(left)
        self._build_launch(left)
        self._build_tiles(right)
        self._build_smoothing(right)
        self._build_wishes(right)
        self._build_tabs(right)

        self.schema.on_change = self._refresh_terminal_labels
        self._refresh_terminal_labels()
        body.bind("<Configure>", lambda e: self.schema.redraw() if self.schema.winfo_ismapped() else None, add="+")

    def _build_terminals(self, parent):
        card = Card(parent, "Schéma du harnais", icon="1")
        card.pack(fill=tk.X)
        b = card.body
        self.btn_schema = ttk.Button(card.actions, text="Masquer", style="Ghost.TButton", command=self.action_toggle_schema)
        self.btn_schema.pack(side=tk.RIGHT)
        
        lib = tk.Frame(b, bg=C["surface"])
        lib.pack(fill=tk.X, pady=(0, 8))
        ttk.Button(lib, text="Dossier connecteurs...", command=self.action_choose_connectors).pack(side=tk.LEFT)
        self.lbl_library = tk.Label(lib, text="Aucun dossier", bg=C["surface"], fg=C["muted"], font=FONTS["small"])
        self.lbl_library.pack(side=tk.LEFT, padx=(10, 0))
        ttk.Button(lib, text="Modèle crabe...", style="Ghost.TButton", command=self.action_choose_crab).pack(side=tk.RIGHT, padx=6)
        
        self.schema = HarnessSchematic(b, on_place=self._preview_connectors, height=260 if self.compact else 380)
        self.schema.pack(fill=tk.BOTH, expand=True)
        self.lbl_terminals = tk.Label(b, text="", bg=C["surface"], fg=C["muted"], font=FONTS["small"])
        self.lbl_terminals.pack(fill=tk.X, pady=(6, 0))

    def _build_params(self, parent):
        card = Card(parent, "Réglages du cheminement", icon="2")
        card.pack(fill=tk.X, pady=(0, 12))
        b = card.body

        row = tk.Frame(b, bg=C["surface"])
        row.pack(fill=tk.X)
        self.preset_var = tk.StringVar(value=DEFAULT_PRESET)
        combo = ttk.Combobox(row, textvariable=self.preset_var, state="readonly", values=list(PRESETS), width=24)
        combo.pack(side=tk.RIGHT)
        combo.bind("<<ComboboxSelected>>", lambda e: self.apply_preset())

        self.slider_wb = SliderField(b, "Regroupement", WEIGHT_BUNDLE, 0.05, 0.95, left="courts", right="groupés")
        self.slider_wb.pack(fill=tk.X, pady=(2, 0))
        self.slider_trunk = SliderField(b, "Tension tronc", TRUNK_TENSION, 0.0, 1.0, left="libre", right="tendu")
        self.slider_trunk.pack(fill=tk.X, pady=(2, 0))

        self._add_field(b, "clearance", "Distance de sécurité", SECURITY_DISTANCE, "mm", vmin=0.5)
        self._add_field(b, "d_pref", "Distance idéale structure", IDEAL_DISTANCE_WITH_STRUCTURE, "mm")
        self._add_field(b, "k_far", "Force collage structure", K_FAR, "")
        
        self.use_fixations = tk.BooleanVar(value=USE_FIXATIONS)
        ttk.Checkbutton(b, text="Passer par les fixations existantes", variable=self.use_fixations, command=self._refresh_context).pack(anchor="w")
        self._add_field(b, "snap_radius_mm", "Distance détection", SNAP_RADIUS_MM, "mm")
        self._add_field(b, "clamp_center_bias", "Préférence milieu peigne", CLAMP_CENTER_BIAS, "")
        self.lbl_passages = tk.Label(b, text="", bg=C["surface"], fg=C["muted"], font=FONTS["tiny"])
        self.lbl_passages.pack(fill=tk.X)

        self._add_field(b, "min_bend_radius", "Rayon courbure mini", MINIMAL_BEND_RADIUS, "mm")
        self._add_field(b, "cable_diameter", "Diamètre câble", CABLE_DIAMETER, "mm")
        self._add_field(b, "L_min_BB", "L mini entre branchements", L_MIN_BB, "mm")
        self._add_field(b, "connector_lead_in", "Amorce droite sortie", CONNECTOR_LEAD_IN, "mm")
        self._add_field(b, "L_min_TB", "L mini conn-branchement", L_MIN_TB, "mm")
        self._add_field(b, "clip_spacing", "Espacement clips max", CLIP_SPACING, "mm")

        self.adv_visible = tk.BooleanVar(value=False)
        self.btn_advanced = ttk.Button(b, text="Afficher réglages avancés", style="Ghost.TButton", command=self._toggle_advanced)
        self.btn_advanced.pack(fill=tk.X, pady=(12, 0))
        self.adv_frame = tk.Frame(b, bg=C["surface"])
        self._add_field(self.adv_frame, "resolution", "Maille calcul", RESOLUTION, "mm")
        self._add_field(self.adv_frame, "eps", "Rapidité A*", EPS, "")
        self._add_field(self.adv_frame, "max_sweeps", "Passes", MAX_SWEEPS, "", kind="int")
        self._add_field(self.adv_frame, "time_budget_s", "Budget temps", TIME_BUDGET_S, "s")
        self._add_field(self.adv_frame, "passage_reward", "Attirance fixations", FIXATION_ATTRACTIVE, "")
        self._add_field(self.adv_frame, "passage_radius_mm", "Portée attirance", PASSAGE_RADIUS_MM, "mm")
        self._add_field(self.adv_frame, "passage_max_detour", "Détour max", PASSAGE_MAX_DETOUR, "mm")
        self._add_field(self.adv_frame, "passage_max_span", "Largeur max", PASSAGE_MAX_SPAN, "mm")
        self._add_field(self.adv_frame, "resample_step", "Finesse trace", RESAMPLE_STEP, "mm")
        self._add_field(self.adv_frame, "smooth_iters", "Lissage 100%", SMOOTH_ITERS, "", kind="int")
        self.apply_preset()

    def _add_field(self, parent, key, label, value, unit, kind="float", vmin=None, vmax=None):
        f = ParamField(parent, label, value, unit=unit, kind=kind, vmin=vmin, vmax=vmax)
        f.pack(fill=tk.X, pady=2)
        self.fields[key] = f

    def _toggle_advanced(self):
        if self.adv_visible.get():
            self.adv_frame.pack_forget()
            self.adv_visible.set(False)
        else:
            self.adv_frame.pack(fill=tk.X, pady=(8, 0))
            self.adv_visible.set(True)

    def _build_launch(self, parent):
        card = Card(parent, "Calcul", icon="3")
        card.pack(fill=tk.X, pady=(0, 12))
        b = card.body

        self.deep_search = tk.BooleanVar(value=False)
        ttk.Checkbutton(b, text="Recherche approfondie (SHRH)", variable=self.deep_search).pack(anchor="w", pady=(0, 8))

        self.btn_run = ttk.Button(b, text="Lancer le cheminement", style="Primary.TButton", command=self.action_run, state=tk.DISABLED)
        self.btn_run.pack(fill=tk.X)
        self.btn_stop = ttk.Button(b, text="Arrêter le calcul", style="Danger.TButton", command=self.controller.cancel_operation, state=tk.DISABLED)
        self.btn_stop.pack(fill=tk.X, pady=(6, 0))
        self.progress = ttk.Progressbar(b, mode="determinate", maximum=100)
        self.progress.pack(fill=tk.X, pady=(10, 4))
        self.lbl_phase = tk.Label(b, text="En attente de maquette.", bg=C["surface"], fg=C["muted"], font=FONTS["small"])
        self.lbl_phase.pack(fill=tk.X)

    def _build_tiles(self, parent):
        wrap = tk.Frame(parent, bg=C["bg"])
        wrap.pack(fill=tk.X, pady=(0, 12))
        specs = [("longueur", "Longueur", "mm"), ("commun", "Tronc commun", "%"), ("branch", "Branches", ""), ("clearance", "Clearance", "mm"), ("cintrage", "R min", "mm"), ("duree", "Durée", "s")]
        self.tiles = {}
        for i, (key, label, unit) in enumerate(specs):
            wrap.columnconfigure(i, weight=1, uniform="k")
            t = StatTile(wrap, label, "--", unit=unit, compact=True)
            t.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 6, 0))
            self.tiles[key] = t

    def _build_smoothing(self, parent):
        card = Card(parent, "Lissage du harnais", icon="3", padding=12)
        card.pack(fill=tk.X, pady=(0, 10))
        b = card.body
        row = tk.Frame(b, bg=C["surface"])
        row.pack(fill=tk.X)
        self.lbl_level = tk.Label(row, text="Lissage : 0 %", bg=C["surface"], fg=C["primary"], font=FONTS["bold"], width=15, anchor="w")
        self.lbl_level.pack(side=tk.LEFT)
        self.level_var = tk.DoubleVar(value=0.0)
        self.scale_level = ttk.Scale(row, from_=0, to=100, variable=self.level_var, orient="horizontal", command=self._on_level_move)
        self.scale_level.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self.scale_level.state(["disabled"])
        self.smooth_state = tk.Label(b, text="Disponible après calcul.", bg=C["surface"], fg=C["muted"], font=FONTS["small"])
        self.smooth_state.pack(fill=tk.X, pady=(4, 0))
        self.progress_ladder = ttk.Progressbar(b, mode="determinate", maximum=100)

    def _build_wishes(self, parent):
        card = Card(parent, "Retouches", icon="4", padding=12)
        card.pack(fill=tk.X, pady=(0, 10))
        b = card.body
        self.btn_edit3d = ttk.Button(b, text="Retoucher dans la vue 3D", command=self.action_edit_3d)
        self.btn_edit3d.pack(anchor="w", pady=(0, 8))
        self.lbl_wishes = tk.Label(b, text="Aucun souhait.", bg=C["surface"], fg=C["muted"], font=FONTS["tiny"])
        self.lbl_wishes.pack(fill=tk.X)
        self.btn_wish_apply = ttk.Button(b, text="Appliquer les souhaits", state=tk.DISABLED)
        self.btn_wish_apply.pack(anchor="w")

    def _build_tabs(self, parent):
        self.notebook = ttk.Notebook(parent)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        tab_m = tk.Frame(self.notebook, bg=C["surface"])
        self.notebook.add(tab_m, text="  Suivi du calcul  ")
        self.dashboard = MetricsDashboard(tab_m)
        self.dashboard.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        tab_3d = tk.Frame(self.notebook, bg=C["surface"])
        self.notebook.add(tab_3d, text="  Résultat 3D  ")
        bar = tk.Frame(tab_3d, bg=C["surface"])
        bar.pack(fill=tk.X, padx=8, pady=(8, 0))
        self.view_var = tk.StringVar(value="Isometrique")
        cb = ttk.Combobox(bar, textvariable=self.view_var, state="readonly", values=list(Result3D.VIEWS))
        cb.pack(side=tk.LEFT, padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self.result3d.set_view(self.view_var.get()))
        self.var_struct = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Structure", variable=self.var_struct, command=self._refresh_3d).pack(side=tk.LEFT, padx=10)
        self.result3d = Result3D(tab_3d)
        self.result3d.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        tab_seg = tk.Frame(self.notebook, bg=C["surface"])
        self.notebook.add(tab_seg, text="  Torons et clips  ")
        self.seg_tree = ttk.Treeview(tab_seg, columns=("id", "cables", "longueur", "diametre", "rmin"), show="headings")
        for k, text in zip(("id", "cables", "longueur", "diametre", "rmin"), ("Tronçon", "Câbles", "Longueur", "Diamètre", "R mini")):
            self.seg_tree.heading(k, text=text)
        self.seg_tree.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

    def _build_footer(self):
        foot = tk.Frame(self, bg=C["bg"])
        foot.pack(side=tk.BOTTOM, fill=tk.X, expand=False, padx=18, pady=(12, 14))
        self.banner = Banner(foot, "Renseigner les connexions.", "info")
        self.banner.pack(fill=tk.X)
        self.console = LogConsole(foot, height=4 if self.compact else 6, collapsed=self.compact)
        self.console.pack(fill=tk.BOTH, expand=True, pady=(10, 10))
        
        bar = tk.Frame(foot, bg=C["bg"])
        bar.pack(fill=tk.X)
        ttk.Button(bar, text="< Retour maquette", command=lambda: self.controller.show_page("import")).pack(side=tk.LEFT)
        self.btn_push = ttk.Button(bar, text="Envoyer CATIA", style="Success.TButton", state=tk.DISABLED, command=self.action_push_catia)
        self.btn_push.pack(side=tk.RIGHT)

    # ==================================================================
    # Actions UI vers Contrôleur
    # ==================================================================
    def action_choose_connectors(self):
        folder = filedialog.askdirectory(title="Dossier connecteurs")
        if folder: self.controller.load_connector_library(folder)

    def action_choose_crab(self):
        path = filedialog.askopenfilename(title="Modèle crabe", filetypes=[("STL", "*.stl")])
        if path:
            try:
                self.crab_model = crab_mod.load_crab(path)
                self.lbl_library.config(text=f"Crabe : {self.crab_model.name}", fg=C["ok"])
            except Exception as e:
                messagebox.showerror("Erreur", str(e))

    def action_run(self):
        if self.is_running: return
        terminals = self.schema.rows()
        if not terminals:
            messagebox.showwarning("Erreur", "Ajouter au moins un câble.")
            return
        
        values, errors = {}, []
        for key, field in self.fields.items():
            try: values[key] = field.get()
            except ValueError as e: errors.append(str(e))
        if errors:
            messagebox.showerror("Paramètres invalides", "\n".join(errors))
            return
            
        w_B = values.pop("w_B", 0.5)
        cable_dia = values.pop("cable_diameter", 6.0)
        res = values["resolution"]
        values["snap_radius_cells"] = values.pop("snap_radius_mm", 80.0) / res
        values["passage_radius_cells"] = values.pop("passage_radius_mm", 200.0) / res
        values["snap_passages"] = self.use_fixations.get()
        params = RoutingParams(w_L=round(1.0 - w_B, 4), w_B=w_B, **values)

        self.is_running = True
        self.btn_run.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.progress.config(value=2)
        self.dashboard.reset()
        self.notebook.select(0)
        
        poses = self.schema.poses()
        models = {m.name: m for m in self.controller.view.pages["routing"].schema.library.models} if hasattr(self.schema, "library") and self.schema.library else {}
        self.controller.start_routing(terminals, params, cable_dia, self.deep_search.get(), poses, self.wishes, models)

    def action_push_catia(self):
        self.controller.push_to_catia(
            lambda: geometry.harness_solid(self.state.result, cable_diameter=self.state.cable_diameter, polylines=self.state.result["smooth"]),
            lambda: geometry.export_catia_curves_csv(self.state.result, str(self.state.cache_dir("harnais") / "curves.csv"), self.state.cable_diameter)
        )

    def action_toggle_schema(self):
        if self.schema.winfo_ismapped():
            self.schema.pack_forget()
            self.btn_schema.config(text="Afficher")
        else:
            self.schema.pack(fill=tk.BOTH, expand=True)
            self.btn_schema.config(text="Masquer")

    def action_edit_3d(self):
        self.controller.open_view3d_import()

    def apply_preset(self):
        preset = PRESETS.get(self.preset_var.get(), {})
        for key, value in preset.items():
            if key in self.fields: self.fields[key].set(value)

    # ==================================================================
    # Mises à jour appelées par le Contrôleur
    # ==================================================================
    def update_phase(self, payload):
        self.lbl_phase.config(text=f"{payload['text']} ({payload['t']:.0f} s)")
        if payload.get("ratio"): self.progress.config(value=max(2, 100 * payload["ratio"]))

    def update_astar_tick(self, payload):
        self.lbl_phase.config(text=f"{self.lbl_phase.cget('text').split(' (')[0]} ({payload['nodes']} noeuds)")

    def on_routing_done(self, res, interf, summ, params):
        self.is_running = False
        self.btn_stop.config(state=tk.DISABLED)
        self.progress.config(value=100)
        self.lbl_phase.config(text=f"Terminé en {summ['t_route'] + summ['t_smooth']:.1f} s.")
        
        self.tiles["longueur"].set(f"{summ['total_length']:.0f}")
        self.tiles["clearance"].set(f"{summ['min_clearance']:.0f}")
        
        self.dashboard.finalize(res, interf, params)
        self._refresh_3d()
        self.btn_push.config(state=tk.NORMAL)
        self.notebook.select(1)
        
        self.smooth_state.config(text="Évaluation lissage...", fg=C["muted"])
        self.progress_ladder.pack(fill=tk.X)
        self.progress_ladder.config(value=0)

    def on_routing_cancelled(self):
        self.is_running = False
        self.btn_stop.config(state=tk.DISABLED)
        self.lbl_phase.config(text="Annulé.")
        self.btn_run.config(state=tk.NORMAL)

    def on_routing_failed(self, error):
        self.is_running = False
        self.btn_stop.config(state=tk.DISABLED)
        self.lbl_phase.config(text="Échec.")
        self.btn_run.config(state=tk.NORMAL)
        messagebox.showerror("Erreur cheminement", str(error))

    def on_ladder_level(self, rec, i, total):
        self.progress_ladder.config(value=100.0 * (i + 1) / max(total, 1))
        self.smooth_state.config(text=f"Palier {rec['level']}%")
        self.dashboard.set_ladder(self.state.ladder, self.state.params, self.state.level)

    def on_ladder_done(self, records):
        self.progress_ladder.pack_forget()
        self.scale_level.state(["!disabled"])

    def on_ladder_failed(self, error):
        self.progress_ladder.pack_forget()
        self.smooth_state.config(text=f"Erreur lissage: {error}")

    def _on_level_move(self, _=None):
        if not self.state.ladder: return
        target = float(self.level_var.get())
        rec = min(self.state.ladder, key=lambda r: abs(r["level"] - target))
        self.level_var.set(rec["level"])
        self.state.level = rec["level"]
        self.dashboard.set_ladder(self.state.ladder, self.state.params, rec["level"])

    def on_library_loaded(self, lib):
        self.schema.set_library(lib)
        self.lbl_library.config(text=f"{len(lib)} connecteur(s)")

    def _on_scene(self):
        self.btn_run.config(state=tk.NORMAL)

    def _refresh_context(self):
        pass

    def _refresh_terminal_labels(self):
        pass

    def _preview_connectors(self):
        pass

    def _refresh_3d(self):
        if not self.state.has_result: return
        self.result3d.show_structure = self.var_struct.get()
        self.result3d.set_result(dict(
            bundles=bundle_polylines(self.state.result["grid"], self.state.result["topo"], self.state.result["seg_geo"]),
            raw=self.state.result["raw"],
            cloud=display_cloud(self.state.cloud, 18000) if self.var_struct.get() else None
        ))
        self.result3d.set_view(self.view_var.get())

    def on_push_success(self, payload):
        """Succès de l'envoi vers CATIA."""
        self.btn_push.config(state=tk.NORMAL)
        mode = ("géométrie native (splines)" if payload.get("mode") == "native"
                else "maillage STL")
        self.banner.set(f"Harnais inséré dans CATIA en {mode}. "
                        f"{payload['message']}", "ok")
        messagebox.showinfo(
            "Harnais inséré dans CATIA",
            f"{payload['message']}\n\nMode : {mode}\n"
            f"Courbes : {payload.get('csv') or '-'}\n"
            f"Solide STL : {payload.get('path') or '-'}")

    def on_push_failed(self, payload):
        """Échec de l'envoi vers CATIA."""
        self.btn_push.config(state=tk.NORMAL)
        path, csv = payload.get("path"), payload.get("csv")
        self.banner.set(
            f"Envoi vers CATIA impossible : {payload['error'].splitlines()[0]}",
            "error")
        messagebox.showerror(
            "Envoi vers CATIA impossible",
            payload["error"]
            + (f"\n\nCourbes (CSV) :\n{csv}" if csv else "")
            + (f"\nSolide STL :\n{path}" if path else "")
            + "\n\nLe CATPart peut être inséré manuellement par "
              "Insertion > Composant existant, ou les courbes rejouées "
              "depuis le CSV.")
