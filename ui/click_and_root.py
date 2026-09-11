
import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .schematic import HarnessSchematic
from .theme import C, FONTS
from .widgets import LogConsole, Spinner
from ..log import Logger
from ..core.hrh import RoutingParams

LOG = Logger("click-and-root")

DEPARTS, ARRIVEES, SCHEMA, ROUTAGE, FINI = "departs", "arrivees", "schema", "routage", "fini"

CONSIGNES = {
    DEPARTS: ("Cliquez les DÉPARTS dans CATIA",
              "Chaque clic ajoute un point. Échap dans CATIA termine la sélection, puis P pour valider."),
    ARRIVEES: (
    "Cliquez les ARRIVÉES dans CATIA", "Même chose. Échap dans CATIA termine, puis P pour construire le schéma."),
    SCHEMA: ("Schéma et Réglages", "Vérifiez les connexions, choisissez les fixations, puis R pour cheminer."),
    ROUTAGE: ("Cheminement en cours", "Calcul du faisceau optimal..."),
    FINI: ("Terminé", "Le harnais a été exporté vers CATIA."),
}


class ClickAndRootView(tk.Frame):
    def __init__(self, root, controller):
        super().__init__(root, bg=C["bg"])
        self.root = root
        self.controller = controller
        self.controller.view = self

        self.phase = DEPARTS
        self.departs = []
        self.arrivees = []
        self.dossier_fixations = None

        self._build()
        self.after(400, self._commencer)

    def _build(self):
        self.pack(fill=tk.BOTH, expand=True)

        head = tk.Frame(self, bg=C["primary"])
        head.pack(fill=tk.X)
        tk.Label(head, text="Click & Root", bg=C["primary"], fg="#FFFFFF", font=FONTS["h1"]).pack(anchor="w", padx=16,
                                                                                                  pady=(10, 0))
        self.lbl_phase = tk.Label(head, text="", bg=C["primary"], fg="#8FB2E0", font=FONTS["small"], anchor="w",
                                  justify="left", wraplength=760)
        self.lbl_phase.pack(anchor="w", padx=16, pady=(0, 10))

        corps = tk.Frame(self, bg=C["bg"])
        corps.pack(fill=tk.BOTH, expand=True, padx=16, pady=12)

        # --- Cadre Points ---
        self.cadre_points = tk.Frame(corps, bg=C["surface"], highlightthickness=1, highlightbackground=C["border"])
        self.lbl_titre = tk.Label(self.cadre_points, text="", bg=C["surface"], fg=C["primary"], font=FONTS["h2"],
                                  anchor="w")
        self.lbl_titre.pack(fill=tk.X, padx=14, pady=(12, 4))
        self.liste = tk.Listbox(self.cadre_points, height=7, bd=0, highlightthickness=0, bg=C["surface_alt"],
                                fg=C["text"], font=FONTS["mono"], selectbackground=C["accent"])
        self.liste.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 12))

        # --- Cadre Schéma et Réglages ---
        self.cadre_schema = tk.Frame(corps, bg=C["bg"])
        self.schema = HarnessSchematic(self.cadre_schema, height=200)
        self.schema.pack(fill=tk.BOTH, expand=True)

        # Affichage des hyperparamètres et bouton fixations
        reglages = tk.Frame(self.cadre_schema, bg=C["surface"], highlightthickness=1, highlightbackground=C["border"])
        reglages.pack(fill=tk.X, pady=(10, 0), ipadx=10, ipady=10)

        row_fix = tk.Frame(reglages, bg=C["surface"])
        row_fix.pack(fill=tk.X, pady=(0, 10))
        ttk.Button(row_fix, text="Dossier de fixations (Optionnel)...", command=self.action_choisir_fixations).pack(
            side=tk.LEFT)
        self.lbl_fixations = tk.Label(row_fix, text="Aucune fixation (le harnais sera libre)", bg=C["surface"],
                                      fg=C["muted"], font=FONTS["small"])
        self.lbl_fixations.pack(side=tk.LEFT, padx=10)

        # Importation dynamique pour l'affichage (ajout de USE_SHRH)
        from .config import (RESOLUTION, WEIGHT_BUNDLE, EPS, MAX_SWEEPS,
                             SECURITY_DISTANCE, MINIMAL_BEND_RADIUS, K_FAR, USE_SHRH)

        w_l = round(1.0 - WEIGHT_BUNDLE, 2)
        collage = "Actif" if K_FAR > 0 else "Inactif"
        shrh_status = "Activé" if USE_SHRH else "Désactivé"

        params_texte = (
            f"Hyperparamètres appliqués (issus de config.py) :\n"
            f"• Maille : {RESOLUTION} mm  |  w_L / w_B : {w_l} / {WEIGHT_BUNDLE}  |  A* eps : {EPS}  | Passes : {MAX_SWEEPS}\n"
            f"• Garde : {SECURITY_DISTANCE} mm    |  Rayon min : {MINIMAL_BEND_RADIUS} mm      |  Collage : {collage}\n"
            f"• Relaxation Lagrangienne (SHRH) : {shrh_status}"
        )
        tk.Label(reglages, text=params_texte, bg=C["surface"], fg=C["muted"], font=FONTS["tiny"], justify="left",
                 anchor="w").pack(fill=tk.X)

        # --- Cadre Calcul ---
        self.cadre_calcul = tk.Frame(corps, bg=C["surface"], highlightthickness=1, highlightbackground=C["border"])
        self.spinner = Spinner(self.cadre_calcul)
        self.spinner.pack(pady=(26, 10))
        self.lbl_attente = tk.Label(self.cadre_calcul, text="Calcul en cours, patientez", bg=C["surface"],
                                    fg=C["primary"], font=FONTS["h2"])
        self.lbl_attente.pack()
        self.lbl_etape = tk.Label(self.cadre_calcul, text="", bg=C["surface"], fg=C["muted"], font=FONTS["small"],
                                  wraplength=620)
        self.lbl_etape.pack(pady=(6, 26))

        # --- Pied de page ---
        pied = tk.Frame(self, bg=C["bg"])
        pied.pack(fill=tk.X, padx=16, pady=(0, 12))
        self.console = LogConsole(pied, height=5, collapsed=True)
        self.console.pack(fill=tk.X, pady=(0, 8))

        barre = tk.Frame(pied, bg=C["bg"])
        barre.pack(fill=tk.X)
        self.lbl_touches = tk.Label(barre, text="", bg=C["bg"], fg=C["muted"], font=FONTS["small"], anchor="w")
        self.lbl_touches.pack(side=tk.LEFT)
        self.btn_action = ttk.Button(barre, text="P  Valider", style="Success.TButton", command=self.action_suivant)
        self.btn_action.pack(side=tk.RIGHT)
        ttk.Button(barre, text="Quitter", style="Ghost.TButton", command=self.root.destroy).pack(side=tk.RIGHT, padx=8)

        for touche in ("p", "P"): self.root.bind(f"<KeyPress-{touche}>", lambda _e: self.action_suivant())
        for touche in ("r", "R"): self.root.bind(f"<KeyPress-{touche}>", lambda _e: self.action_router())
        self.root.bind("<Escape>", lambda _e: None)

    def _commencer(self):
        self._set_phase(DEPARTS)
        self._lancer_selection(DEPARTS)

    def _set_phase(self, phase):
        self.phase = phase
        titre, aide = CONSIGNES[phase]
        self.lbl_phase.config(text=aide)
        if hasattr(self, 'lbl_titre'):
            self.lbl_titre.config(text=titre)

        self.cadre_points.pack_forget()
        self.cadre_schema.pack_forget()
        self.cadre_calcul.pack_forget()

        if phase in (DEPARTS, ARRIVEES):
            self.cadre_points.pack(fill=tk.BOTH, expand=True)
            self.lbl_touches.config(text="Échap (dans CATIA) termine la sélection  -  P valide la phase")
            self.btn_action.config(text="P  Valider", state=tk.NORMAL)
        elif phase == SCHEMA:
            self.cadre_schema.pack(fill=tk.BOTH, expand=True)
            self.schema.redraw()
            self.lbl_touches.config(text="R lance le cheminement")
            self.btn_action.config(text="R  Cheminer", state=tk.NORMAL)
        else:
            self.cadre_calcul.pack(fill=tk.BOTH, expand=True)
            self.lbl_touches.config(text="")
            self.btn_action.config(state=tk.DISABLED)

    def action_choisir_fixations(self):
        dossier = filedialog.askdirectory(title="Dossier contenant les colliers (STL)")
        if dossier:
            self.dossier_fixations = dossier
            self.lbl_fixations.config(text=f"Fixations : {os.path.basename(dossier)}", fg=C["ok"])

    def _lancer_selection(self, phase):
        libelle = "DEPART" if phase == DEPARTS else "ARRIVEE"
        self.liste.delete(0, tk.END)
        self.liste.insert(tk.END, "  en attente d'un clic dans CATIA...")
        self.controller.start_selection(phase, libelle)

    def action_suivant(self):
        if self.phase not in (DEPARTS, ARRIVEES): return
        if self.controller.session and not self.controller.session.finished.is_set():
            messagebox.showinfo("Sélection en cours", "Terminez d'abord la sélection dans CATIA (touche Échap).")
            return

        points = self.departs if self.phase == DEPARTS else self.arrivees
        if not points:
            messagebox.showwarning("Aucun point", "Aucun point n'a été sélectionné.")
            self._lancer_selection(self.phase)
            return

        if self.phase == DEPARTS:
            self._set_phase(ARRIVEES)
            self._lancer_selection(ARRIVEES)
        else:
            self._construire_schema()

    def _construire_schema(self):
        from .controller import appairer
        rows = appairer(self.departs, self.arrivees)
        self.schema.set_rows(rows)
        LOG.ok(
            f"schéma construit : {len(self.departs)} départ(s), {len(self.arrivees)} arrivée(s), {len(rows)} câble(s)")
        self._set_phase(SCHEMA)

    def action_router(self):
        if self.phase != SCHEMA: return
        terminals = self.schema.rows()
        if not terminals:
            messagebox.showwarning("Aucun câble", "Reliez au moins un départ à une arrivée.")
            return

        self._set_phase(ROUTAGE)
        self.spinner.start()

        # Import COMPLET des paramètres de config.py, y compris USE_SHRH
        from .config import (SECURITY_DISTANCE, IDEAL_DISTANCE_WITH_STRUCTURE, K_FAR,
                             MINIMAL_BEND_RADIUS, CONNECTOR_LEAD_IN, CLIP_SPACING,
                             PASSAGE_MAX_SPAN, RESAMPLE_STEP, SMOOTH_ITERS,
                             SNAP_RADIUS_MM, PASSAGE_RADIUS_MM,
                             FIXATION_ATTRACTIVE, PASSAGE_MAX_DETOUR, RESOLUTION, EPS, TIME_BUDGET_S,
                             WEIGHT_BUNDLE, USE_SHRH)

        params = RoutingParams(
            w_L=round(1.0 - WEIGHT_BUNDLE, 4), w_B=WEIGHT_BUNDLE,
            resolution=RESOLUTION, eps=EPS, max_sweeps=10, time_budget_s=TIME_BUDGET_S,
            clearance=SECURITY_DISTANCE,
            d_pref=IDEAL_DISTANCE_WITH_STRUCTURE,
            k_far=K_FAR,
            min_bend_radius=MINIMAL_BEND_RADIUS,
            connector_lead_in=CONNECTOR_LEAD_IN,
            clip_spacing=CLIP_SPACING,
            passage_max_span=PASSAGE_MAX_SPAN,
            passage_reward=FIXATION_ATTRACTIVE,
            passage_max_detour=PASSAGE_MAX_DETOUR,
            resample_step=RESAMPLE_STEP,
            smooth_iters=SMOOTH_ITERS,
            snap_radius_cells=SNAP_RADIUS_MM / RESOLUTION,
            passage_radius_cells=PASSAGE_RADIUS_MM / RESOLUTION,
            snap_passages=True if self.dossier_fixations else False
        )

        poses = self.schema.poses()
        models = {m.name: m for m in getattr(self.schema, "library", {}).models} if hasattr(self.schema, "library") and self.schema.library else {}
        cable_dia = getattr(self.controller, "cable_diameter", 6.0)

        # On transmet USE_SHRH dans le paramètre deep
        self.controller.start_routing(terminals, params, cable_dia, USE_SHRH, poses, [], models, dossier_fixations=self.dossier_fixations)

    def on_point_added(self, phase, xyz, nom):
        cible = self.departs if phase == DEPARTS else self.arrivees
        cible.append(tuple(xyz))
        if len(cible) == 1: self.liste.delete(0, tk.END)
        self.liste.insert(tk.END, f"  {len(cible):2d}.  {xyz[0]:9.1f}  {xyz[1]:9.1f}  {xyz[2]:9.1f}    {nom}")
        self.liste.see(tk.END)

    def on_selection_ended(self, phase, points, erreur):
        if erreur:
            messagebox.showerror("Sélection dans CATIA", erreur)
            self.lbl_titre.config(text="Sélection impossible", fg=C["error"])
            return
        cible = self.departs if phase == DEPARTS else self.arrivees
        titre, _aide = CONSIGNES[phase]
        self.lbl_titre.config(text=f"{titre}  -  {len(cible)} point(s), appuyez sur P")

    def on_step_update(self, texte):
        self.lbl_etape.config(text=texte)
        LOG.info(texte)

    def on_progress_update(self, done, total, texte):
        self.lbl_etape.config(text=texte)

    def on_routing_done(self, bilan):
        self.spinner.stop()
        resume, palier = bilan["resume"], bilan["palier"]
        crabes = len(palier.get("crabs") or [])

        self.lbl_attente.config(text="Terminé !", fg=C["ok"])
        self.lbl_etape.config(
            text=f"{resume['n_cables']} câble(s), {resume['total_length']:.0f} mm, "
                 f"lissage {palier['level']} %, {crabes} crabe(s)\n" + "\n".join(bilan["messages"]))

        self._set_phase(FINI)
        self.lbl_phase.config(text=CONSIGNES[FINI][1])
        LOG.ok(f"click & root terminé : {resume['total_length']:.0f} mm")

    def on_routing_failed(self, erreur):
        self.spinner.stop()
        self.lbl_attente.config(text="Le cheminement a échoué", fg=C["error"])
        self.lbl_etape.config(text=erreur)
        messagebox.showerror("Cheminement", erreur)
