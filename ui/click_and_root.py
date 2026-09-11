"""
Parcours guide Click & Root.

L'utilisateur clique ses departs puis ses arrivees DANS CATIA, verifie le
schema, choisit son algorithme, et l'application fait le reste : cheminement,
lissage, pose des crabes, export, puis controle des regles HS par MOMOS.

Quatre ajouts par rapport a la premiere version :

* CTRL + Z revient sur un clic -- on se trompe de connecteur une fois sur
  trois, et il fallait jusqu'ici tout recommencer ;
* on peut REFAIRE un routage sans quitter l'application, soit sur les memes
  points (pour changer d'algorithme ou de fixations), soit sur de nouveaux ;
* le choix HRH / SHRH est offert a l'ecran, pas seulement dans config.py ;
* MOMOS controle les regles HS des la fin du calcul et ouvre sa vue 3D.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .schematic import HarnessSchematic
from .theme import C, FONTS
from .widgets import (Chip, LogConsole, PillButton, SegmentedControl, SoftCard,
                      Spinner, StepDots, HeaderBar)
from ..log import Logger
from ..core.hrh import RoutingParams

LOG = Logger("click-and-root")

DEPARTS, ARRIVEES, SCHEMA, ROUTAGE, FINI = (
    "departs", "arrivees", "schema", "routage", "fini")

ETAPES = [(DEPARTS, "Departs"), (ARRIVEES, "Arrivees"), (SCHEMA, "Schema"),
          (ROUTAGE, "Cheminement"), (FINI, "Regles HS")]

CONSIGNES = {
    DEPARTS: ("Cliquez les DEPARTS dans CATIA",
              "Chaque clic ajoute un connecteur. Ctrl+Z annule le dernier. "
              "Echap dans CATIA termine la selection, puis P pour valider."),
    ARRIVEES: ("Cliquez les ARRIVEES dans CATIA",
               "Meme chose. Ctrl+Z annule le dernier clic, Echap termine, "
               "puis P construit le schema."),
    SCHEMA: ("Schema et reglages",
             "Verifiez les liaisons, choisissez l'algorithme et les "
             "fixations, puis R pour cheminer."),
    ROUTAGE: ("Cheminement en cours",
              "Calcul du faisceau optimal, pose des crabes et export CATIA."),
    FINI: ("Termine",
           "Le harnais est dans CATIA. MOMOS a controle les regles HS."),
}

VERDICTS = {
    "ok": ("CONFORME", "ok"),
    "warn": ("CONFORME AVEC RESERVES", "warn"),
    "ko": ("NON CONFORME", "error"),
    "na": ("CONTROLE PARTIEL", "neutral"),
}


class ClickAndRootView(tk.Frame):
    """Vue du parcours guide : une carte par phase, un bandeau, un journal."""

    def __init__(self, root, controller):
        super().__init__(root, bg=C["bg"])
        self.root = root
        self.controller = controller
        self.controller.view = self

        self.phase = DEPARTS
        # un clic = un point ET le nom de la piece cliquee : c'est ce nom qui
        # permet ensuite de retrouver le STL du connecteur.
        self.clics = {DEPARTS: [], ARRIVEES: []}
        self.retablir = {DEPARTS: [], ARRIVEES: []}
        self.dossier_fixations = None
        self.bilan = None

        self._build()
        self.after(400, self._commencer)

    # ==================================================================
    # construction
    # ==================================================================
    def _build(self):
        self.pack(fill=tk.BOTH, expand=True)

        self.entete = HeaderBar(self, "Click & Root",
                                CONSIGNES[DEPARTS][1], badge="CATIA")
        self.entete.pack(fill=tk.X)
        PillButton(self.entete.actions, "Quitter", kind="ghost",
                   bg=C["primary"], command=self.root.destroy,
                   font=FONTS["small"]).pack(side=tk.RIGHT)

        corps = tk.Frame(self, bg=C["bg"])
        corps.pack(fill=tk.BOTH, expand=True, padx=20, pady=(14, 10))

        self.fil = StepDots(corps, [libelle for _k, libelle in ETAPES])
        self.fil.pack(fill=tk.X, pady=(0, 12))

        self.scene = tk.Frame(corps, bg=C["bg"])
        self.scene.pack(fill=tk.BOTH, expand=True)

        self._build_points()
        self._build_schema()
        self._build_calcul()
        self._build_fini()
        self._build_pied()
        self._raccourcis()

    # -- carte des points ---------------------------------------------
    def _build_points(self):
        self.cadre_points = SoftCard(self.scene)
        corps = self.cadre_points.body

        ligne = tk.Frame(corps, bg=C["surface"])
        ligne.pack(fill=tk.X)
        self.lbl_titre = tk.Label(ligne, text="", bg=C["surface"],
                                  fg=C["primary"], font=FONTS["h2"], anchor="w")
        self.lbl_titre.pack(side=tk.LEFT)
        self.chip_points = Chip(ligne, "0 point", kind="neutral",
                                bg=C["surface"])
        self.chip_points.pack(side=tk.LEFT, padx=10)

        self.lbl_aide = tk.Label(
            corps, bg=C["surface"], fg=C["muted"], font=FONTS["small"],
            anchor="w", justify="left",
            text="Le nom de la piece cliquee est conserve : c'est lui qui "
                 "permet de retrouver le STL du connecteur, d'en calculer la "
                 "boite englobante et d'accrocher le cable sur la face "
                 "tournee vers le harnais.")
        self.lbl_aide.pack(fill=tk.X, pady=(2, 10))

        cadre_liste = tk.Frame(corps, bg=C["surface_alt"],
                               highlightthickness=1,
                               highlightbackground=C["border_soft"])
        cadre_liste.pack(fill=tk.BOTH, expand=True)
        self.liste = tk.Listbox(cadre_liste, height=8, bd=0,
                                highlightthickness=0, bg=C["surface_alt"],
                                fg=C["text"], font=FONTS["mono"],
                                selectbackground=C["accent_soft"],
                                selectforeground=C["accent_dark"],
                                activestyle="none")
        barre = ttk.Scrollbar(cadre_liste, orient="vertical",
                              command=self.liste.yview)
        self.liste.configure(yscrollcommand=barre.set)
        barre.pack(side=tk.RIGHT, fill=tk.Y)
        self.liste.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)

        outils = tk.Frame(corps, bg=C["surface"])
        outils.pack(fill=tk.X, pady=(12, 0))
        self.btn_annuler = PillButton(
            outils, "Annuler le dernier clic", kind="tinted", bg=C["surface"],
            command=self.action_annuler, font=FONTS["small"])
        self.btn_annuler.pack(side=tk.LEFT)
        self.btn_retablir = PillButton(
            outils, "Retablir", kind="plain", bg=C["surface"],
            command=self.action_retablir, font=FONTS["small"])
        self.btn_retablir.pack(side=tk.LEFT, padx=8)
        PillButton(outils, "Tout effacer", kind="plain", bg=C["surface"],
                   command=self.action_vider,
                   font=FONTS["small"]).pack(side=tk.LEFT)
        tk.Label(outils, text="Ctrl+Z  /  Ctrl+Y", bg=C["surface"],
                 fg=C["faint"], font=FONTS["tiny"]).pack(side=tk.RIGHT)

    # -- carte du schema ----------------------------------------------
    def _build_schema(self):
        self.cadre_schema = tk.Frame(self.scene, bg=C["bg"])
        self.schema = HarnessSchematic(self.cadre_schema, height=300)
        self.schema.pack(fill=tk.BOTH, expand=True)

        reglages = SoftCard(self.cadre_schema, padding=14)
        reglages.pack(fill=tk.X, pady=(12, 0))
        corps = reglages.body

        # --- choix de l'algorithme ---
        haut = tk.Frame(corps, bg=C["surface"])
        haut.pack(fill=tk.X)
        gauche = tk.Frame(haut, bg=C["surface"])
        gauche.pack(side=tk.LEFT)
        tk.Label(gauche, text="ALGORITHME DE CHEMINEMENT", bg=C["surface"],
                 fg=C["muted"], font=FONTS["tiny"], anchor="w").pack(anchor="w")
        self.segment_algo = SegmentedControl(
            gauche, [("HRH", "HRH"), ("SHRH", "SHRH")],
            value=self._algo_par_defaut(), command=self._on_algorithme,
            bg=C["surface"], width=210)
        self.segment_algo.pack(anchor="w", pady=(6, 0))
        self.lbl_algo = tk.Label(gauche, text="", bg=C["surface"],
                                 fg=C["faint"], font=FONTS["tiny"],
                                 anchor="w", justify="left", wraplength=330)
        self.lbl_algo.pack(anchor="w", pady=(6, 0))

        droite = tk.Frame(haut, bg=C["surface"])
        droite.pack(side=tk.RIGHT, anchor="ne")
        tk.Label(droite, text="FIXATIONS EXISTANTES", bg=C["surface"],
                 fg=C["muted"], font=FONTS["tiny"], anchor="e").pack(anchor="e")
        PillButton(droite, "Choisir un dossier de colliers...", kind="plain",
                   bg=C["surface"], command=self.action_choisir_fixations,
                   font=FONTS["small"]).pack(anchor="e", pady=(6, 0))
        self.lbl_fixations = tk.Label(
            droite, text="Aucune fixation : le harnais sera libre",
            bg=C["surface"], fg=C["faint"], font=FONTS["tiny"], anchor="e")
        self.lbl_fixations.pack(anchor="e", pady=(6, 0))

        tk.Frame(corps, bg=C["hairline"], height=1).pack(fill=tk.X, pady=12)
        self.lbl_params = tk.Label(corps, text="", bg=C["surface"],
                                   fg=C["muted"], font=FONTS["tiny"],
                                   justify="left", anchor="w")
        self.lbl_params.pack(fill=tk.X)
        self._rafraichir_parametres()

    # -- carte de calcul ----------------------------------------------
    def _build_calcul(self):
        self.cadre_calcul = SoftCard(self.scene)
        corps = self.cadre_calcul.body
        interieur = tk.Frame(corps, bg=C["surface"])
        interieur.pack(expand=True)

        self.spinner = Spinner(interieur, texte="Preparation")
        self.spinner.pack(pady=(20, 14))
        self.lbl_attente = tk.Label(interieur, text="Cheminement en cours",
                                    bg=C["surface"], fg=C["primary"],
                                    font=FONTS["h2"])
        self.lbl_attente.pack()
        self.lbl_etape = tk.Label(interieur, text="", bg=C["surface"],
                                  fg=C["muted"], font=FONTS["small"],
                                  wraplength=620, justify="center")
        self.lbl_etape.pack(pady=(8, 22))

    # -- carte de resultat + regles HS ---------------------------------
    def _build_fini(self):
        self.cadre_fini = SoftCard(self.scene)
        corps = self.cadre_fini.body

        ligne = tk.Frame(corps, bg=C["surface"])
        ligne.pack(fill=tk.X)
        self.lbl_verdict = tk.Label(ligne, text="Termine", bg=C["surface"],
                                    fg=C["ok"], font=FONTS["h2"], anchor="w")
        self.lbl_verdict.pack(side=tk.LEFT)
        self.chips_momos = tk.Frame(ligne, bg=C["surface"])
        self.chips_momos.pack(side=tk.LEFT, padx=12)

        self.lbl_resume = tk.Label(corps, text="", bg=C["surface"],
                                   fg=C["muted"], font=FONTS["small"],
                                   anchor="w", justify="left")
        self.lbl_resume.pack(fill=tk.X, pady=(4, 10))

        cadre_table = tk.Frame(corps, bg=C["surface"])
        cadre_table.pack(fill=tk.BOTH, expand=True)
        colonnes = ("regle", "mesure", "limite", "detail")
        self.table = ttk.Treeview(cadre_table, columns=colonnes,
                                  show="headings", height=8)
        for cle, titre, largeur, ancre in (
                ("regle", "Regle HS", 330, "w"),
                ("mesure", "Mesure", 105, "e"),
                ("limite", "Limite", 105, "e"),
                ("detail", "Constat", 330, "w")):
            self.table.heading(cle, text=titre)
            self.table.column(cle, width=largeur, anchor=ancre,
                              stretch=(cle == "detail"))
        barre = ttk.Scrollbar(cadre_table, orient="vertical",
                              command=self.table.yview)
        self.table.configure(yscrollcommand=barre.set)
        barre.pack(side=tk.RIGHT, fill=tk.Y)
        self.table.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.table.tag_configure("ok", foreground=C["ok"])
        self.table.tag_configure("warn", foreground=C["warn"],
                                 background=C["warn_soft"])
        self.table.tag_configure("ko", foreground=C["error"],
                                 background=C["error_soft"])
        self.table.tag_configure("na", foreground=C["faint"])

        actions = tk.Frame(corps, bg=C["surface"])
        actions.pack(fill=tk.X, pady=(14, 0))
        self.btn_nouveau = PillButton(actions, "Nouveau routage", kind="filled",
                                      bg=C["surface"],
                                      command=self.action_nouveau_routage)
        self.btn_nouveau.pack(side=tk.LEFT)
        PillButton(actions, "Recalculer les memes points", kind="tinted",
                   bg=C["surface"], command=self.action_recalculer,
                   font=FONTS["small"]).pack(side=tk.LEFT, padx=8)
        self.btn_momos = PillButton(actions, "Vue 3D des regles HS",
                                    kind="plain", bg=C["surface"],
                                    command=self.action_ouvrir_momos,
                                    font=FONTS["small"])
        self.btn_momos.pack(side=tk.LEFT)
        self.lbl_fichiers = tk.Label(actions, text="", bg=C["surface"],
                                     fg=C["faint"], font=FONTS["tiny"],
                                     anchor="e", justify="right")
        self.lbl_fichiers.pack(side=tk.RIGHT)

    # -- pied de page --------------------------------------------------
    def _build_pied(self):
        pied = tk.Frame(self, bg=C["bg"])
        pied.pack(fill=tk.X, padx=20, pady=(0, 14))
        self.console = LogConsole(pied, height=5, collapsed=True)
        self.console.pack(fill=tk.X, pady=(0, 10))

        barre = tk.Frame(pied, bg=C["bg"])
        barre.pack(fill=tk.X)
        self.lbl_touches = tk.Label(barre, text="", bg=C["bg"], fg=C["faint"],
                                    font=FONTS["small"], anchor="w")
        self.lbl_touches.pack(side=tk.LEFT)
        self.btn_action = PillButton(barre, "Valider  (P)", kind="filled",
                                     bg=C["bg"], command=self.action_suivant)
        self.btn_action.pack(side=tk.RIGHT)
        self.btn_retour = PillButton(barre, "Retour", kind="plain", bg=C["bg"],
                                     command=self.action_retour,
                                     font=FONTS["small"])
        self.btn_retour.pack(side=tk.RIGHT, padx=10)

    def _raccourcis(self):
        """
        Raccourcis clavier.

        Les touches simples (P, R) ne doivent pas partir quand l'utilisateur
        tape dans un champ du schema : on verifie donc qui a le focus avant
        d'agir. CTRL + Z, lui, agit partout.
        """
        def libre():
            widget = self.root.focus_get()
            return not isinstance(widget, (tk.Entry, ttk.Entry, tk.Text,
                                           tk.Spinbox, ttk.Combobox))

        for touche in ("p", "P"):
            self.root.bind(f"<KeyPress-{touche}>",
                           lambda _e: libre() and self.action_suivant())
        for touche in ("r", "R"):
            self.root.bind(f"<KeyPress-{touche}>",
                           lambda _e: libre() and self.action_router())
        for sequence in ("<Control-z>", "<Control-Z>"):
            self.root.bind(sequence, lambda _e: self.action_annuler())
        for sequence in ("<Control-y>", "<Control-Y>", "<Control-Shift-Z>"):
            self.root.bind(sequence, lambda _e: self.action_retablir())
        for touche in ("n", "N"):
            self.root.bind(f"<KeyPress-{touche}>",
                           lambda _e: (libre() and self.phase == FINI
                                       and self.action_nouveau_routage()))
        self.root.bind("<Escape>", lambda _e: None)

    # ==================================================================
    # phases
    # ==================================================================
    def _commencer(self):
        self._set_phase(DEPARTS)
        self._lancer_selection(DEPARTS)

    def _set_phase(self, phase):
        self.phase = phase
        titre, aide = CONSIGNES[phase]
        self.entete.set_subtitle(aide)
        self.fil.set_index([k for k, _l in ETAPES].index(phase))

        for cadre in (self.cadre_points, self.cadre_schema, self.cadre_calcul,
                      self.cadre_fini):
            cadre.pack_forget()

        if phase in (DEPARTS, ARRIVEES):
            self.cadre_points.pack(fill=tk.BOTH, expand=True)
            self.lbl_titre.config(text=titre)
            self.lbl_touches.config(
                text="Echap dans CATIA termine la selection  -  P valide  -  "
                     "Ctrl+Z annule le dernier clic")
            self.btn_action.configure(text="Valider  (P)", state=tk.NORMAL)
            self.btn_retour.configure(
                state=tk.NORMAL if phase == ARRIVEES else tk.DISABLED)
            self._rafraichir_liste()
        elif phase == SCHEMA:
            self.cadre_schema.pack(fill=tk.BOTH, expand=True)
            self.schema.redraw()
            self.lbl_touches.config(text="R lance le cheminement  -  "
                                         "Ctrl+Z revient aux arrivees")
            self.btn_action.configure(text="Cheminer  (R)", state=tk.NORMAL)
            self.btn_retour.configure(state=tk.NORMAL)
            self._rafraichir_parametres()
        elif phase == ROUTAGE:
            self.cadre_calcul.pack(fill=tk.BOTH, expand=True)
            self.lbl_touches.config(text="Calcul en cours...")
            self.btn_action.configure(state=tk.DISABLED)
            self.btn_retour.configure(state=tk.DISABLED)
        else:
            self.cadre_fini.pack(fill=tk.BOTH, expand=True)
            self.lbl_touches.config(text="Ctrl+Z revient aux reglages  -  "
                                         "N lance un nouveau routage")
            self.btn_action.configure(text="Nouveau routage", state=tk.NORMAL)
            self.btn_retour.configure(state=tk.NORMAL)

    def _algo_par_defaut(self):
        try:
            from .config import routing_algorithm
            return routing_algorithm()
        except Exception:
            return "HRH"

    def algorithme(self) -> str:
        return (self.segment_algo.get() if hasattr(self, "segment_algo")
                else self._algo_par_defaut())

    def _on_algorithme(self, valeur):
        LOG.info(f"algorithme de cheminement : {valeur}")
        self._rafraichir_parametres()

    def _rafraichir_parametres(self):
        from .config import (RESOLUTION, WEIGHT_BUNDLE, EPS, MAX_SWEEPS,
                             SECURITY_DISTANCE, MINIMAL_BEND_RADIUS, K_FAR,
                             CLIP_SPACING, CONNECTOR_LEAD_IN)

        algo = self.algorithme()
        self.lbl_algo.config(text=(
            "HRH : heuristique de Karlsson et al. (2023). Rapide, c'est le "
            "mode de reference."
            if algo == "HRH" else
            "SHRH : relaxation lagrangienne avant la HRH. Plus long, mais "
            "explore des topologies de tronc que la HRH ne trouve pas."))

        w_l = round(1.0 - WEIGHT_BUNDLE, 2)
        collage = "actif" if K_FAR > 0 else "inactif"
        self.lbl_params.config(text=(
            f"Regles et hyperparametres lus dans config.py\n"
            f"Maille {RESOLUTION} mm   -   w_L / w_B {w_l} / {WEIGHT_BUNDLE}"
            f"   -   A* eps {EPS}   -   {MAX_SWEEPS} passes\n"
            f"Garde {SECURITY_DISTANCE} mm   -   rayon mini "
            f"{MINIMAL_BEND_RADIUS:.0f} mm   -   crabes tous les "
            f"{CLIP_SPACING:.0f} mm   -   amorce connecteur "
            f"{CONNECTOR_LEAD_IN:.0f} mm   -   collage {collage}"))

    # ==================================================================
    # selection des points
    # ==================================================================
    def _lancer_selection(self, phase):
        libelle = "DEPART" if phase == DEPARTS else "ARRIVEE"
        self.controller.start_selection(phase, libelle)

    def points(self, phase=None):
        return [c["xyz"] for c in self.clics[phase or self.phase]]

    def noms(self, phase=None):
        return [c["nom"] for c in self.clics[phase or self.phase]]

    def _rafraichir_liste(self):
        phase = self.phase if self.phase in self.clics else DEPARTS
        clics = self.clics[phase]
        self.liste.delete(0, tk.END)
        if not clics:
            self.liste.insert(tk.END, "   en attente d'un clic dans CATIA...")
        for i, clic in enumerate(clics, start=1):
            x, y, z = clic["xyz"]
            self.liste.insert(
                tk.END, f"  {i:2d}.  {x:9.1f}  {y:9.1f}  {z:9.1f}    "
                        f"{clic['nom']}")
        self.liste.see(tk.END)

        self.chip_points.set(f"{len(clics)} point(s)",
                             "info" if clics else "neutral")
        self.btn_annuler.configure(
            state=tk.NORMAL if clics or phase == ARRIVEES else tk.DISABLED)
        self.btn_retablir.configure(
            state=tk.NORMAL if self.retablir[phase] else tk.DISABLED)
        # Le titre porte le compte : il doit revenir en arriere lui aussi
        # quand on annule, sinon il affiche un nombre de points disparus.
        titre, _aide = CONSIGNES[phase]
        self.lbl_titre.config(
            text=f"{titre}  -  appuyez sur P" if clics else titre,
            fg=C["primary"])

    # -- messages du controleur ----------------------------------------
    def on_point_added(self, phase, xyz, nom):
        if phase not in self.clics:
            return
        self.clics[phase].append(dict(xyz=tuple(float(v) for v in xyz),
                                      nom=nom or ""))
        self.retablir[phase].clear()
        if phase == self.phase:
            self._rafraichir_liste()

    def on_selection_ended(self, phase, points, erreur):
        if erreur:
            messagebox.showerror("Selection dans CATIA", erreur)
            self.lbl_titre.config(text="Selection impossible", fg=C["error"])
            return
        if phase != self.phase:
            return
        titre, _aide = CONSIGNES[phase]
        n = len(self.clics[phase])
        self.lbl_titre.config(text=f"{titre}  -  {n} point(s), appuyez sur P",
                              fg=C["primary"])

    @staticmethod
    def _legende(texte):
        """Legende courte pour le centre du minuteur (le detail est dessous)."""
        court = str(texte).split(" : ")[0].strip()
        return court if len(court) <= 20 else court[:19] + "\u2026"

    def on_step_update(self, texte):
        self.lbl_etape.config(text=texte)
        self.spinner.set_caption(self._legende(texte))
        LOG.info(texte)

    def on_progress_update(self, done, total, texte):
        self.lbl_etape.config(text=texte)
        if total:
            self.spinner.set_progress(done, total)
        else:
            self.spinner.set_progress(None)

    # ==================================================================
    # actions
    # ==================================================================
    def action_choisir_fixations(self):
        dossier = filedialog.askdirectory(
            title="Dossier contenant les colliers (STL)")
        if dossier:
            self.dossier_fixations = dossier
            self.lbl_fixations.config(
                text=f"Fixations : {os.path.basename(dossier)}", fg=C["ok"])

    def action_annuler(self):
        """
        CTRL + Z : revenir sur le dernier clic, ou sur la phase precedente.

        Se tromper de connecteur au premier clic est l'erreur la plus
        frequente du parcours. On enleve donc le dernier point, et quand il
        n'y en a plus, on remonte d'une phase plutot que d'obliger a tout
        reprendre.
        """
        if self.phase in (DEPARTS, ARRIVEES):
            clics = self.clics[self.phase]
            if clics:
                perdu = clics.pop()
                self.retablir[self.phase].append(perdu)
                self._rafraichir_liste()
                LOG.warn(f"clic annule : {perdu['nom'] or 'point'} "
                         f"({perdu['xyz'][0]:.0f}, {perdu['xyz'][1]:.0f}, "
                         f"{perdu['xyz'][2]:.0f})")
                return
            if self.phase == ARRIVEES:
                self.action_retour()
                return
            LOG.info("rien a annuler : aucun depart n'a encore ete clique")
            return
        self.action_retour()

    def action_retablir(self):
        if self.phase not in self.clics:
            return
        pile = self.retablir[self.phase]
        if not pile:
            return
        self.clics[self.phase].append(pile.pop())
        self._rafraichir_liste()
        LOG.info("clic retabli")

    def action_vider(self):
        if self.phase not in self.clics:
            return
        if not self.clics[self.phase]:
            return
        if not messagebox.askyesno(
                "Tout effacer",
                "Effacer les points de cette phase ?\n\n"
                "Les clics deja faits dans CATIA seront oublies."):
            return
        self.retablir[self.phase] = list(reversed(self.clics[self.phase]))
        self.clics[self.phase] = []
        self._rafraichir_liste()
        LOG.warn("points de la phase effaces")

    def action_retour(self):
        """Revient d'une phase, en relancant la selection s'il le faut."""
        if self.phase == ARRIVEES:
            if not self._selection_terminee():
                return
            self._set_phase(DEPARTS)
            self._lancer_selection(DEPARTS)
        elif self.phase == SCHEMA:
            self._set_phase(ARRIVEES)
            self._lancer_selection(ARRIVEES)
        elif self.phase == FINI:
            self._set_phase(SCHEMA)
        elif self.phase == ROUTAGE:
            messagebox.showinfo("Calcul en cours",
                                "Le cheminement est en cours : attendez la fin.")

    def _selection_terminee(self):
        session = self.controller.session
        if session is not None and not session.finished.is_set():
            messagebox.showinfo(
                "Selection en cours",
                "Terminez d'abord la selection dans CATIA (touche Echap).")
            return False
        return True

    def action_suivant(self):
        if self.phase == SCHEMA:
            return self.action_router()
        if self.phase == FINI:
            return self.action_nouveau_routage()
        if self.phase not in (DEPARTS, ARRIVEES):
            return
        if not self._selection_terminee():
            return

        if not self.clics[self.phase]:
            messagebox.showwarning("Aucun point",
                                   "Aucun point n'a ete selectionne.")
            self._lancer_selection(self.phase)
            return

        if self.phase == DEPARTS:
            self._set_phase(ARRIVEES)
            self._lancer_selection(ARRIVEES)
        else:
            self._construire_schema()

    def _construire_schema(self):
        from .controller import appairer
        rows = appairer(self.points(DEPARTS), self.points(ARRIVEES))
        self.schema.set_rows(rows)
        LOG.ok(f"schema construit : {len(self.clics[DEPARTS])} depart(s), "
               f"{len(self.clics[ARRIVEES])} arrivee(s), {len(rows)} cable(s)")
        self._set_phase(SCHEMA)

    # -- lancement du cheminement --------------------------------------
    def action_router(self):
        if self.phase != SCHEMA:
            return
        terminaux = self.schema.rows()
        if not terminaux:
            messagebox.showwarning("Aucun cable",
                                   "Reliez au moins un depart a une arrivee.")
            return

        self._set_phase(ROUTAGE)
        self.spinner.reset()
        self.spinner.start("Preparation")

        from .config import (SECURITY_DISTANCE, IDEAL_DISTANCE_WITH_STRUCTURE,
                             K_FAR, MINIMAL_BEND_RADIUS, CONNECTOR_LEAD_IN,
                             CLIP_SPACING, PASSAGE_MAX_SPAN, RESAMPLE_STEP,
                             SMOOTH_ITERS, SNAP_RADIUS_MM, PASSAGE_RADIUS_MM,
                             FIXATION_ATTRACTIVE, PASSAGE_MAX_DETOUR,
                             RESOLUTION, EPS, TIME_BUDGET_S, MAX_SWEEPS,
                             WEIGHT_BUNDLE, L_MIN_BB, L_MIN_TB)

        params = RoutingParams(
            w_L=round(1.0 - WEIGHT_BUNDLE, 4), w_B=WEIGHT_BUNDLE,
            resolution=RESOLUTION, eps=EPS, max_sweeps=MAX_SWEEPS,
            time_budget_s=TIME_BUDGET_S,
            clearance=SECURITY_DISTANCE,
            d_pref=IDEAL_DISTANCE_WITH_STRUCTURE,
            k_far=K_FAR,
            min_bend_radius=MINIMAL_BEND_RADIUS,
            connector_lead_in=CONNECTOR_LEAD_IN,
            clip_spacing=CLIP_SPACING,
            L_min_BB=L_MIN_BB,
            L_min_TB=L_MIN_TB,
            passage_max_span=PASSAGE_MAX_SPAN,
            passage_reward=FIXATION_ATTRACTIVE,
            passage_max_detour=PASSAGE_MAX_DETOUR,
            resample_step=RESAMPLE_STEP,
            smooth_iters=SMOOTH_ITERS,
            snap_radius_cells=SNAP_RADIUS_MM / RESOLUTION,
            passage_radius_cells=PASSAGE_RADIUS_MM / RESOLUTION,
            snap_passages=bool(self.dossier_fixations),
        )

        poses = self.schema.poses()
        bibliotheque = getattr(self.schema, "library", None)
        models = ({m.name: m for m in bibliotheque.models}
                  if bibliotheque else {})
        diametre = getattr(self.controller, "cable_diameter", 6.0)

        self.controller.start_routing(
            terminaux, params, diametre, self.algorithme(), poses, [], models,
            dossier_fixations=self.dossier_fixations,
            noms=self._noms_des_terminaux(terminaux))

    def _noms_des_terminaux(self, terminaux):
        """
        Nom de la piece CATIA cliquee pour chaque bout de cable.

        C'est ce nom qui permet de retrouver le STL du connecteur, donc de
        calculer sa boite englobante et d'accrocher le cable sur la bonne
        face.
        """
        table = {}
        for phase in (DEPARTS, ARRIVEES):
            for clic in self.clics[phase]:
                table[self._cle(clic["xyz"])] = clic["nom"]
        return [(table.get(self._cle(a), ""), table.get(self._cle(b), ""))
                for a, b in terminaux]

    @staticmethod
    def _cle(point, tol=1.0):
        return tuple(round(float(v) / tol) for v in point)

    # -- relances -------------------------------------------------------
    def action_recalculer(self):
        """Refait le cheminement sur les memes points (autre algorithme,
        autres fixations, autres reglages de config.py)."""
        if self.phase == ROUTAGE:
            return
        self._set_phase(SCHEMA)
        self._rafraichir_parametres()
        self.after(120, self.action_router)

    def action_nouveau_routage(self):
        """Repart de zero : nouveaux departs, nouvelles arrivees."""
        if self.phase == ROUTAGE:
            messagebox.showinfo("Calcul en cours",
                                "Attendez la fin du cheminement.")
            return
        if not messagebox.askyesno(
                "Nouveau routage",
                "Recommencer un cheminement complet ?\n\n"
                "Les points cliques sont oublies ; la maquette deja chargee "
                "est conservee, le nouveau calcul sera donc plus rapide."):
            return
        self.clics = {DEPARTS: [], ARRIVEES: []}
        self.retablir = {DEPARTS: [], ARRIVEES: []}
        self.bilan = None
        self.spinner.reset()
        self.lbl_attente.config(text="Cheminement en cours", fg=C["primary"])
        for ligne in self.table.get_children():
            self.table.delete(ligne)
        self.controller.reset()
        LOG.info("nouveau routage demande")
        self._set_phase(DEPARTS)
        self._lancer_selection(DEPARTS)

    def action_ouvrir_momos(self):
        if not self.bilan:
            return
        self.controller.open_momos_window()

    # ==================================================================
    # fin de cheminement
    # ==================================================================
    def on_routing_done(self, bilan):
        self.bilan = bilan
        self.spinner.set_progress(1, 1)
        self.spinner.stop()
        self.spinner.set_color(C["ok"])

        resume, palier = bilan["resume"], bilan["palier"]
        crabes = len(palier.get("crabs") or [])
        self.lbl_attente.config(text="Termine", fg=C["ok"])
        self.lbl_etape.config(
            text=f"{resume['n_cables']} cable(s), {resume['total_length']:.0f} mm, "
                 f"lissage {palier['level']} %, {crabes} crabe(s)")

        self.lbl_resume.config(text=(
            f"{resume['n_cables']} cable(s)  -  {resume['total_length']:.0f} mm"
            f"  -  tronc commun {100.0 * resume.get('common_ratio', 0.0):.0f} %"
            f"  -  lissage {palier['level']} %  -  {crabes} crabe(s)  -  "
            f"algorithme {bilan.get('algorithme', 'HRH')}\n"
            + "\n".join(bilan.get("messages") or [])))

        fichiers = bilan.get("fichiers") or {}
        self.lbl_fichiers.config(text="\n".join(
            f"{cle} : {os.path.basename(str(chemin))}"
            for cle, chemin in fichiers.items() if chemin))

        self._afficher_momos(bilan.get("momos"))
        self._set_phase(FINI)
        LOG.ok(f"click & root termine : {resume['total_length']:.0f} mm")

    def _afficher_momos(self, rapport):
        for widget in self.chips_momos.winfo_children():
            widget.destroy()
        for ligne in self.table.get_children():
            self.table.delete(ligne)

        if rapport is None:
            self.lbl_verdict.config(text="Termine", fg=C["ok"])
            self.btn_momos.configure(state=tk.DISABLED)
            Chip(self.chips_momos, "MOMOS non execute", kind="neutral",
                 bg=C["surface"]).pack(side=tk.LEFT)
            return

        self.btn_momos.configure(state=tk.NORMAL)
        libelle, ton = VERDICTS.get(rapport.statut, VERDICTS["na"])
        couleur = {"ok": C["ok"], "warn": C["warn"], "error": C["error"],
                   "neutral": C["muted"]}[ton]
        self.lbl_verdict.config(text=f"MOMOS  -  {libelle}", fg=couleur)

        compte = rapport.compte
        for cle, tonalite, texte in (("ok", "ok", "conforme(s)"),
                                     ("warn", "warn", "en limite"),
                                     ("ko", "error", "violee(s)"),
                                     ("na", "neutral", "non evaluee(s)")):
            if compte.get(cle):
                Chip(self.chips_momos, f"{compte[cle]} {texte}", kind=tonalite,
                     bg=C["surface"]).pack(side=tk.LEFT, padx=3)

        for ctrl in rapport.controles:
            mesure = ("-" if ctrl.valeur != ctrl.valeur
                      else f"{ctrl.valeur:.1f} {ctrl.unite}")
            limite = ("-" if ctrl.limite != ctrl.limite
                      else f"{ctrl.sens} {ctrl.limite:.1f}")
            self.table.insert("", tk.END, tags=(ctrl.statut,), values=(
                f"{ctrl.code}   {ctrl.titre}", mesure, limite, ctrl.detail))

    def on_routing_failed(self, erreur):
        self.spinner.stop()
        self.spinner.set_color(C["error"])
        self.lbl_attente.config(text="Le cheminement a echoue", fg=C["error"])
        self.lbl_etape.config(text=erreur)
        self._set_phase(SCHEMA)
        self.lbl_touches.config(text="Corrigez les reglages, puis R pour "
                                     "relancer le cheminement.")
        messagebox.showerror("Cheminement", erreur)

    def on_momos_ready(self, rapport):
        """MOMOS a fini apres coup (relance manuelle du controle)."""
        if self.bilan is not None:
            self.bilan["momos"] = rapport
        self._afficher_momos(rapport)
