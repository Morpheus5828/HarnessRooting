"""
Schema du harnais : la saisie des cables, dessinee comme une planche.

Le concepteur ne raisonne pas en lignes de tableau mais en ENTREES et en
SORTIES : deux departs a gauche, une arrivee a droite, et entre les deux un
tronc commun. Ce widget dessine exactement cela.

Chaque noeud est une CARTE posee sur le schema, qui porte tout ce qui le
definit :

    [E1]  [silhouette du connecteur]  [ Connecteur v ]
                                      [X][Y][Z] [Sortie v] [Sens]

La silhouette est le connecteur VU DE PROFIL, tracee a partir du fichier STL
lui-meme (contour tranche par tranche, cf. `ConnectorModel.silhouette`) : elle
suit le modele choisi, se retourne avec le sens, et devient un simple tube
quand le connecteur vaut *Aucun*.

Les cables quittent les cartes d'entree, se rejoignent sur un tronc commun --
trace d'autant plus epais qu'il porte de cables -- puis repartent vers les
cartes de sortie. Un clic sur le fond d'une carte la selectionne ; deux clics
sur deux cartes de sens opposes relient ou delient un cable.

Le widget reste la source des cables : `rows()` rend les couples
(depart, arrivee) attendus par le reste de l'application, et `poses()` les
connecteurs a poser.
"""

from __future__ import annotations

import math
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk

import numpy as np

from .layout import plan_cards, thumb_polygon, wire_points
from .theme import C, FONTS
from .widgets import Tooltip
from ..core.connectors import ConnectorPose, auto_direction
from ..log import Logger

LOG = Logger("schema")

AXES = {
    "Auto": None,
    "+X": (1.0, 0.0, 0.0), "-X": (-1.0, 0.0, 0.0),
    "+Y": (0.0, 1.0, 0.0), "-Y": (0.0, -1.0, 0.0),
    "+Z": (0.0, 0.0, 1.0), "-Z": (0.0, 0.0, -1.0),
}
AXIS_NAMES = list(AXES)

NONE_LABEL = "Aucun (tube seul)"

# Largeur d'une carte : la pastille (48) + la vignette (96) + la colonne de
# reglages (~270, mesuree sur les widgets ttk) + les marges. Trop etroite, la
# case "Sens" sort de la carte et se fait rogner.
CARD_W, CARD_H = 440, 116          # encombrement d'une carte, en pixels
CARD_GAP = 30                     # espace vertical entre deux cartes
MARGIN = 26
THUMB_W, THUMB_H = 96, 56         # vignette du connecteur


@dataclass
class SchematicNode:
    """Un connecteur du schema : une entree ou une sortie du harnais."""
    side: str = "in"                      # "in" (gauche) ou "out" (droite)
    point: tuple = (0.0, 0.0, 0.0)
    model: str = ""                       # nom du connecteur choisi
    axis_name: str = "Auto"
    axis: tuple = (1.0, 0.0, 0.0)         # direction personnalisee
    flip: bool = False
    roll: float = 0.0

    @property
    def label(self):
        return "E" if self.side == "in" else "S"

    def direction(self, other=None):
        """Direction de sortie du cable, choisie ou deduite."""
        fixed = AXES.get(self.axis_name)
        if fixed is not None:
            return fixed
        if other is not None:
            return auto_direction(self.point, other)
        return (1.0, 0.0, 0.0)

    def pose(self, other=None) -> ConnectorPose:
        return ConnectorPose(model=self.model, point=tuple(self.point),
                             direction=self.direction(other), flip=self.flip,
                             roll=self.roll)


class HarnessSchematic(tk.Frame):
    """Planche de saisie : cartes de connecteurs et tronc commun."""

    def __init__(self, parent, on_change=None, on_place=None, height=380):
        super().__init__(parent, bg=C["surface"])
        self.on_change = on_change
        self.on_place = on_place
        self.library = None
        self.nodes = []
        self.links = []                       # (index entree, index sortie)
        self.cards = []                       # widgets de chaque carte
        self.selected = None
        self._pending = None                  # carte en attente de liaison
        self._pos = {}                        # index -> (x, y) coin haut gauche
        self._quiet = False                   # gele les rappels pendant un remplissage
        self._max_height = int(height)        # plafond ; la planche se tasse si
        self._auto_height = 0                 # elle a moins de cartes a montrer

        self._build(height)
        self.set_counts(1, 1)

    # ==================================================================
    # construction
    # ==================================================================
    def _build(self, height):
        top = tk.Frame(self, bg=C["surface"])
        top.pack(fill=tk.X)
        tk.Label(top, text="Entrees", bg=C["surface"], fg=C["muted"],
                 font=FONTS["small"]).pack(side=tk.LEFT)
        self.var_in = tk.StringVar(value="1")
        sp_in = ttk.Spinbox(top, from_=1, to=12, width=3, textvariable=self.var_in,
                            command=self._on_counts)
        sp_in.pack(side=tk.LEFT, padx=(4, 14))
        # `command` ne se declenche qu'aux fleches : une valeur tapee doit etre
        # prise en compte elle aussi.
        sp_in.bind("<Return>", lambda _e: self._on_counts())
        sp_in.bind("<FocusOut>", lambda _e: self._on_counts())
        Tooltip(sp_in, "Nombre de connecteurs de depart.")
        tk.Label(top, text="Sorties", bg=C["surface"], fg=C["muted"],
                 font=FONTS["small"]).pack(side=tk.LEFT)
        self.var_out = tk.StringVar(value="1")
        sp_out = ttk.Spinbox(top, from_=1, to=12, width=3, textvariable=self.var_out,
                             command=self._on_counts)
        sp_out.pack(side=tk.LEFT, padx=(4, 14))
        sp_out.bind("<Return>", lambda _e: self._on_counts())
        sp_out.bind("<FocusOut>", lambda _e: self._on_counts())
        Tooltip(sp_out, "Nombre de connecteurs d'arrivee.")
        ttk.Button(top, text="Tout relier", style="Ghost.TButton",
                   command=self.action_link_all).pack(side=tk.LEFT)
        ttk.Button(top, text="Tout delier", style="Ghost.TButton",
                   command=self.action_unlink_all).pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="Placer sur la vue 3D", style="Ghost.TButton",
                   command=self.action_place).pack(side=tk.RIGHT)

        self.canvas = tk.Canvas(self, height=height, bg=C["surface_alt"],
                                highlightthickness=1,
                                highlightbackground=C["border"])
        self.canvas.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self.canvas.bind("<Configure>", lambda _e: self.redraw())
        self.canvas.bind("<Button-1>", lambda _e: self._on_background_click())

        self.quick = tk.Frame(self, bg=C["surface"])
        self.quick.pack(fill=tk.X, pady=(8, 0))
        self._build_quick(self.quick)

        self.hint = tk.Label(
            self, text="Renseignez chaque carte : connecteur, coordonnees, sens. "
                       "Un clic sur le fond d'une carte la selectionne ; deux "
                       "cartes de sens opposes relient ou delient un cable.",
            bg=C["surface"], fg=C["muted"], font=FONTS["tiny"], anchor="w",
            justify="left", wraplength=900)
        self.hint.pack(fill=tk.X, pady=(6, 0))

    def _build_quick(self, p):
        """
        Saisie directe d'un cable : depart X, Y, Z puis arrivee X, Y, Z.

        Les cartes savent tout regler, mais taper les six coordonnees d'un
        cable d'affilee reste le geste le plus rapide -- et c'est celui que le
        tableau d'origine permettait. Les points identiques sont regroupes :
        deux cables au meme depart ne font qu'un seul noeud.
        """
        tk.Label(p, text="Ajouter un cable", bg=C["surface"], fg=C["text"],
                 font=FONTS["bold"]).pack(side=tk.LEFT, padx=(0, 10))
        self.var_quick = []
        for label in ("Depart", "Arrivee"):
            tk.Label(p, text=label, bg=C["surface"], fg=C["muted"],
                     font=FONTS["small"]).pack(side=tk.LEFT, padx=(6, 4))
            for axis in "XYZ":
                var = tk.StringVar()
                e = ttk.Entry(p, textvariable=var, width=6, justify="right")
                e.pack(side=tk.LEFT, padx=1)
                e.bind("<Return>", lambda _e: self.action_add_cable())
                Tooltip(e, f"Coordonnee {axis} du point de {label.lower()} (mm).")
                self.var_quick.append(var)
        ttk.Button(p, text="Ajouter le cable",
                   command=self.action_add_cable).pack(side=tk.LEFT, padx=(10, 0))

    # ==================================================================
    # cartes : un jeu de widgets par noeud
    # ==================================================================
    def _rebuild_cards(self):
        """(Re)cree les cartes. Appele quand la liste des noeuds change."""
        for card in self.cards:
            if card["window"] is not None:
                self.canvas.delete(card["window"])
            card["frame"].destroy()
        self.cards = [self._make_card(i, n) for i, n in enumerate(self.nodes)]
        self._fill_cards()

    def _make_card(self, index, node):
        f = tk.Frame(self.canvas, bg=C["surface"], width=CARD_W, height=CARD_H,
                     highlightthickness=0, bd=0)
        f.pack_propagate(False)
        f.bind("<Button-1>", lambda _e, i=index: self._on_card_click(i))

        chip = tk.Label(f, text="", bg=C["primary"], fg=C["on_primary"],
                        font=FONTS["bold"], width=3, height=2)
        chip.place(x=8, y=CARD_H // 2 - 18)
        chip.bind("<Button-1>", lambda _e, i=index: self._on_card_click(i))

        thumb = tk.Canvas(f, width=THUMB_W, height=THUMB_H, bg=C["surface_alt"],
                          highlightthickness=1,
                          highlightbackground=C["border_soft"])
        thumb.place(x=48, y=(CARD_H - THUMB_H) // 2)
        thumb.bind("<Button-1>", lambda _e, i=index: self._on_card_click(i))
        Tooltip(thumb, "Le connecteur choisi, vu de profil, oriente comme le "
                       "cable qui en sort.")

        right = tk.Frame(f, bg=C["surface"])
        right.place(x=48 + THUMB_W + 10, y=12)

        # Trois lignes : le modele, les coordonnees, puis l'orientation.
        # Tout sur une seule ligne demandait 510 px de contenu pour une carte
        # qui en fait 440 : le sens et l'axe finissaient coupes hors du cadre,
        # ce qui rendait le choix du sens simplement inaccessible.
        var_model = tk.StringVar(value=NONE_LABEL)
        cb_model = ttk.Combobox(right, textvariable=var_model, state="readonly",
                                width=16, values=[NONE_LABEL])
        cb_model.grid(row=0, column=0, columnspan=3, sticky="we", pady=(0, 6))
        cb_model.bind("<<ComboboxSelected>>", lambda _e, i=index: self._commit(i))
        Tooltip(cb_model, "Modele pris dans le dossier de connecteurs.\n"
                          f"'{NONE_LABEL}' : le harnais se termine par un simple "
                          "tube, sans corps de connecteur.")

        var_xyz = []
        for c, axis in enumerate("XYZ"):
            holder = tk.Frame(right, bg=C["surface"])
            holder.grid(row=1, column=c, padx=(0, 4))
            tk.Label(holder, text=axis, bg=C["surface"], fg=C["muted"],
                     font=FONTS["tiny"]).pack(side=tk.LEFT)
            var = tk.StringVar()
            e = ttk.Entry(holder, textvariable=var, width=6, justify="right")
            e.pack(side=tk.LEFT)
            e.bind("<Return>", lambda _e, i=index: self._commit(i))
            e.bind("<FocusOut>", lambda _e, i=index: self._commit(i))
            Tooltip(e, f"Coordonnee {axis} du connecteur (mm).")
            var_xyz.append(var)

        var_axis = tk.StringVar(value="Auto")
        cb_axis = ttk.Combobox(right, textvariable=var_axis, state="readonly",
                               width=4, values=AXIS_NAMES)
        cb_axis.grid(row=2, column=0, sticky="w", pady=(6, 0))
        cb_axis.bind("<<ComboboxSelected>>", lambda _e, i=index: self._commit(i))
        Tooltip(cb_axis, "Direction du cable en sortant du connecteur. Le corps "
                         "reste toujours parallele au cable.\n"
                         "'Auto' : vers l'autre extremite du cable.")

        var_flip = tk.BooleanVar(value=False)
        cbf = ttk.Checkbutton(right, text="Sens", variable=var_flip,
                              command=lambda i=index: self._commit(i))
        cbf.grid(row=2, column=1, columnspan=2, sticky="w",
                 padx=(8, 0), pady=(6, 0))
        Tooltip(cbf, "Retourne le corps du connecteur le long de son axe.")

        return dict(frame=f, chip=chip, thumb=thumb, model=cb_model,
                    var_model=var_model, var_xyz=var_xyz, var_axis=var_axis,
                    var_flip=var_flip, window=None)

    def _fill_cards(self):
        """Recopie l'etat des noeuds dans les widgets, sans declencher de rappel."""
        self._quiet = True
        try:
            for i, (node, card) in enumerate(zip(self.nodes, self.cards)):
                rank = sum(1 for m in self.nodes[:i] if m.side == node.side) + 1
                card["chip"].config(text=f"{node.label}{rank}",
                                    bg=C["primary"] if node.side == "in"
                                    else C["accent"])
                card["var_model"].set(node.model or NONE_LABEL)
                for var, value in zip(card["var_xyz"], node.point):
                    var.set(f"{value:g}")
                card["var_axis"].set(node.axis_name)
                card["var_flip"].set(bool(node.flip))
                self._draw_thumb(i)
        finally:
            self._quiet = False

    def _commit(self, index):
        """Ecrit une carte dans son noeud."""
        if self._quiet or index >= len(self.nodes):
            return
        node, card = self.nodes[index], self.cards[index]
        try:
            node.point = tuple(float(v.get().strip().replace(",", ".") or 0.0)
                               for v in card["var_xyz"])
        except ValueError:
            self.hint.config(text="Coordonnees invalides : trois nombres attendus.",
                             fg=C["error"])
            return
        model = card["var_model"].get()
        node.model = "" if model in ("", NONE_LABEL) else model
        node.axis_name = card["var_axis"].get()
        node.flip = bool(card["var_flip"].get())
        self.hint.config(text="", fg=C["muted"])
        self._draw_thumb(index)
        self.redraw()
        self._changed()

    # ------------------------------------------------------------------
    def _draw_thumb(self, index):
        """
        Le connecteur vu de profil, dans la vignette de sa carte.

        Le point de sortie du cable est place du cote du harnais : une entree
        pose son corps a gauche et sort a droite, une sortie fait l'inverse.
        Le sens retourne echange les deux.
        """
        node, card = self.nodes[index], self.cards[index]
        cv = card["thumb"]
        cv.delete("all")
        w, h = THUMB_W, THUMB_H
        model = self.library.get(node.model) if (self.library and node.model) else None

        sil = model.silhouette() if model is not None else np.empty((0, 2))
        pts, sortie, pointe = thumb_polygon(sil, node.side, node.flip, w, h)
        flat = [v for p in pts for v in p]

        if model is None:                            # tube seul
            cv.create_polygon(flat, fill=C["accent_soft"], outline=C["accent"],
                              width=1.2)
            cv.create_text(w / 2, h - 9, text="tube", fill=C["muted"],
                           font=FONTS["tiny"])
        else:
            cv.create_polygon(flat, fill=C["ok_soft"], outline=C["ok"], width=1.3)
            cv.create_text(w / 2, h - 8, text=f"{model.length:.0f} mm",
                           fill=C["muted"], font=FONTS["tiny"])

        # depart du cable : un trait fleche vers le harnais
        cv.create_line(sortie[0], sortie[1], pointe[0], pointe[1],
                       fill=C["primary"], width=2, arrow=tk.LAST,
                       arrowshape=(7, 8, 3))

    # ==================================================================
    # modele
    # ==================================================================
    def set_library(self, library):
        """Renseigne la bibliotheque de connecteurs disponible."""
        self.library = library
        names = [NONE_LABEL] + (library.names() if library else [])
        for card in self.cards:
            card["model"].configure(values=names)
        if library and len(library) and not any(n.model for n in self.nodes):
            first = library.first().name
            for n in self.nodes:
                n.model = first
            self.hint.config(
                text=f"{len(library)} connecteur(s) disponibles : '{first}' a ete "
                     f"applique a tous les noeuds. Changez-le carte par carte, ou "
                     f"choisissez '{NONE_LABEL}' pour un simple tube.",
                fg=C["muted"])
        self._fill_cards()
        self.redraw()

    def counts(self):
        return (sum(1 for n in self.nodes if n.side == "in"),
                sum(1 for n in self.nodes if n.side == "out"))

    def set_counts(self, n_in, n_out, keep=True):
        """Fixe le nombre d'entrees et de sorties, en gardant l'existant."""
        n_in, n_out = max(1, int(n_in)), max(1, int(n_out))
        old_in = [n for n in self.nodes if n.side == "in"]
        old_out = [n for n in self.nodes if n.side == "out"]
        default = self.library.first().name if (self.library and len(self.library)) \
            else ""

        def build(side, count, old):
            out = []
            for i in range(count):
                if keep and i < len(old):
                    out.append(old[i])
                else:
                    out.append(SchematicNode(side=side, model=default,
                                             point=(0.0, 0.0, 0.0)))
            return out

        self.nodes = build("in", n_in, old_in) + build("out", n_out, old_out)
        self.var_in.set(str(n_in))
        self.var_out.set(str(n_out))
        self._default_links()
        self.selected = 0 if self.nodes else None
        self._rebuild_cards()
        self.redraw()
        self._changed()

    def _default_links(self):
        """Chaque noeud sert au moins un cable : appariement en tourniquet."""
        ins = [i for i, n in enumerate(self.nodes) if n.side == "in"]
        outs = [i for i, n in enumerate(self.nodes) if n.side == "out"]
        if not ins or not outs:
            self.links = []
            return
        keep = [(a, b) for a, b in self.links if a in ins and b in outs]
        n = max(len(ins), len(outs))
        auto = [(ins[k % len(ins)], outs[k % len(outs)]) for k in range(n)]
        self.links = keep if len(keep) >= n else sorted(set(keep) | set(auto))

    # ---- passerelle avec le reste de l'application -------------------
    def active_links(self):
        """
        Cables reellement definis.

        Un noeud tout juste cree est a (0, 0, 0) : le cable qui le relie n'a
        pas encore de sens. Le compter reviendrait a proposer un cheminement
        d'un point sur lui-meme -- et a deverrouiller le bouton de lancement
        avant toute saisie.
        """
        out = []
        for a, b in self.links:
            pa = np.asarray(self.nodes[a].point, float)
            pb = np.asarray(self.nodes[b].point, float)
            if float(np.linalg.norm(pa - pb)) > 1.0:
                out.append((a, b))
        return out

    def rows(self):
        """Cables du schema : [(depart, arrivee)] en millimetres."""
        return [(tuple(self.nodes[a].point), tuple(self.nodes[b].point))
                for a, b in self.active_links()]

    def poses(self):
        """Connecteurs poses, dans le meme ordre que `rows()`."""
        out = []
        for a, b in self.active_links():
            na, nb = self.nodes[a], self.nodes[b]
            out.append((na.pose(nb.point), nb.pose(na.point)))
        return out

    def all_poses(self):
        """Une pose par NOEUD (pour l'apercu 3D avant tout cheminement)."""
        out = []
        for i, node in enumerate(self.nodes):
            other = None
            for a, b in self.links:
                if a == i:
                    other = self.nodes[b].point
                elif b == i:
                    other = self.nodes[a].point
                if other is not None:
                    break
            out.append(node.pose(other))
        return out

    def set_rows(self, rows):
        """
        Reconstruit le schema a partir d'une liste de cables.

        Sert a l'import CSV, au jeu d'essai et a la saisie rapide : les points
        identiques sont regroupes en un seul noeud, ce qui fait apparaitre les
        departs communs.
        """
        rows = [(tuple(float(v) for v in a), tuple(float(v) for v in b))
                for a, b in rows]
        if not rows:
            return
        default = self.library.first().name if (self.library and len(self.library)) \
            else ""
        old = {(_key(n.point), n.side): n for n in self.nodes}
        nodes, index = [], {}

        def node_for(point, side):
            k = (_key(point), side)
            if k in index:
                return index[k]
            prev = old.get(k)
            n = prev if prev is not None else SchematicNode(side=side, model=default)
            n.side, n.point = side, point
            nodes.append(n)
            index[k] = len(nodes) - 1
            return index[k]

        links = [(node_for(a, "in"), node_for(b, "out")) for a, b in rows]
        order = sorted(range(len(nodes)), key=lambda i: (nodes[i].side != "in", i))
        remap = {old_i: new_i for new_i, old_i in enumerate(order)}
        self.nodes = [nodes[i] for i in order]
        self.links = [(remap[a], remap[b]) for a, b in links]
        self.var_in.set(str(sum(1 for n in self.nodes if n.side == "in")))
        self.var_out.set(str(sum(1 for n in self.nodes if n.side == "out")))
        self.selected = 0
        self._rebuild_cards()
        self.redraw()
        self._changed()

    # ==================================================================
    # actions
    # ==================================================================
    def _on_counts(self):
        try:
            n_in = int(self.var_in.get())
            n_out = int(self.var_out.get())
        except ValueError:
            return
        self.set_counts(n_in, n_out)

    def action_link_all(self):
        ins = [i for i, n in enumerate(self.nodes) if n.side == "in"]
        outs = [i for i, n in enumerate(self.nodes) if n.side == "out"]
        self.links = [(a, b) for a in ins for b in outs]
        self.redraw()
        self._changed()

    def action_unlink_all(self):
        self.links = []
        self.redraw()
        self._changed()

    def action_place(self):
        for i in range(len(self.nodes)):
            self._commit(i)
        if self.on_place is not None:
            self.on_place()

    def action_add_cable(self):
        """Cree le cable saisi et le fusionne dans le schema."""
        try:
            v = [float(var.get().strip().replace(",", "."))
                 for var in self.var_quick]
        except ValueError:
            self.hint.config(text="Renseigner les six coordonnees du cable "
                                  "(depart X, Y, Z et arrivee X, Y, Z).",
                             fg=C["error"])
            return
        a, b = tuple(v[:3]), tuple(v[3:])
        if float(np.linalg.norm(np.asarray(a) - np.asarray(b))) <= 1.0:
            self.hint.config(text="Le depart et l'arrivee sont au meme endroit.",
                             fg=C["error"])
            return
        self.set_rows(self.rows() + [(a, b)])
        for var in self.var_quick:
            var.set("")
        self.hint.config(text=f"Cable ajoute : ({', '.join(f'{x:g}' for x in a)}) "
                              f"-> ({', '.join(f'{x:g}' for x in b)}).",
                         fg=C["muted"])
        LOG.info(f"cable ajoute : {a} -> {b}")

    def selected_node(self):
        if self.selected is None or self.selected >= len(self.nodes):
            return None
        return self.nodes[self.selected]

    def _on_background_click(self):
        self._pending = None
        self.redraw()

    def _on_card_click(self, index):
        """Selectionne une carte, ou relie/delie deux cartes opposees."""
        if self._pending is None or self._pending == index:
            self._pending = index
            self.selected = index
            self.redraw()
            return
        a, b = self._pending, index
        if self.nodes[a].side == self.nodes[b].side:
            self.hint.config(text="Un cable relie une entree a une sortie.",
                             fg=C["warn"])
            self._pending = index
            self.selected = index
            self.redraw()
            return
        if self.nodes[a].side == "out":
            a, b = b, a
        if (a, b) in self.links:
            self.links.remove((a, b))
        else:
            self.links.append((a, b))
        self._pending = None
        self.selected = index
        self.redraw()
        self._changed()

    def _changed(self):
        if self.on_change is not None:
            self.on_change()

    # ==================================================================
    # dessin
    # ==================================================================
    def redraw(self):
        cv = self.canvas
        cv.delete("deco")
        if not self.nodes or not self.cards:
            return
        plan = plan_cards([n.side for n in self.nodes], cv.winfo_width(),
                          cv.winfo_height(), CARD_W, CARD_H, CARD_GAP, MARGIN)

        # La planche se tasse sur son contenu : deux cartes n'ont pas besoin de
        # 380 px de haut, et cette hauteur vide chassait de l'ecran les
        # colonnes situees dessous. Le plafond reste celui demande a la
        # construction ; en dessous, on rend la place.
        voulu = max(140, min(self._max_height, int(plan["height"])))
        if voulu != self._auto_height and abs(voulu - cv.winfo_height()) > 8:
            self._auto_height = voulu
            cv.configure(height=voulu)
            plan = plan_cards([n.side for n in self.nodes], cv.winfo_width(),
                              voulu, CARD_W, CARD_H, CARD_GAP, MARGIN)
        cv.configure(scrollregion=(0, 0, plan["width"], plan["height"]))
        self._pos = plan["positions"]
        self._plan = plan

        self._draw_wires(cv, plan)
        for i, (x, y) in self._pos.items():
            self._draw_card_frame(cv, i, x, y)
            card = self.cards[i]
            if card["window"] is None:
                card["window"] = cv.create_window(x, y, window=card["frame"],
                                                  anchor="nw")
            else:
                cv.coords(card["window"], x, y)
            cv.tag_raise(card["window"])

    def _draw_card_frame(self, cv, index, x, y):
        """Cadre arrondi et ombre douce sous la carte."""
        selected = (index == self.selected)
        vierge = float(np.linalg.norm(
            np.asarray(self.nodes[index].point, float))) < 1e-9
        _round_rect(cv, x + 3, y + 4, x + CARD_W + 3, y + CARD_H + 4, 12,
                    fill=C["border_soft"], outline="", tags="deco")
        outline = C["accent"] if selected else (C["warn"] if vierge
                                                else C["border"])
        _round_rect(cv, x, y, x + CARD_W, y + CARD_H, 12, fill=C["surface"],
                    outline=outline, width=2.0 if selected else 1.2, tags="deco")
        if vierge:
            cv.create_text(x + CARD_W - 12, y + CARD_H - 9, anchor="e",
                           text="coordonnees a saisir", fill=C["warn"],
                           font=FONTS["tiny"], tags="deco")

    def _draw_wires(self, cv, plan):
        """
        Les cables, et le tronc commun qui les porte.

        Chaque cable quitte sa carte par une courbe, rejoint le tronc, et en
        repart vers son arrivee. Le tronc est trace une seule fois, epais comme
        le nombre de cables qu'il porte : c'est la lecture qu'attend un
        concepteur -- ce qui est commun se voit.
        """
        liens = self.links
        if not liens:
            return
        x0, x1, y_trunk = plan["trunk"]
        if x1 <= x0:
            return

        for a, b in liens:
            if a not in self._pos or b not in self._pos:
                continue
            aller, retour = wire_points(self._pos[a], self._pos[b],
                                        plan["x_left"], plan["x_right"],
                                        plan["trunk"], CARD_H)
            for chemin in (aller, retour):
                cv.create_line([v for p in chemin for v in p], smooth=True,
                               splinesteps=24, fill=C["accent"], width=2.4,
                               capstyle=tk.ROUND, tags="deco")

        n = len(liens)
        cv.create_line(x0, y_trunk, x1, y_trunk, fill=C["primary"],
                       width=2.0 + 1.8 * math.sqrt(n), capstyle=tk.ROUND,
                       tags="deco")
        actifs = len(self.active_links())
        texte = f"tronc commun -- {actifs} cable(s)"
        if n - actifs:
            texte += f", {n - actifs} en attente de coordonnees"
        cv.create_text(0.5 * (x0 + x1), y_trunk - 16, text=texte,
                       fill=C["warn"] if n - actifs else C["muted"],
                       font=FONTS["tiny"], tags="deco")


def _round_rect(cv, x0, y0, x1, y1, r, **kw):
    """Rectangle arrondi : le canvas Tk n'en propose pas."""
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
           x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return cv.create_polygon(pts, smooth=True, splinesteps=12, **kw)


def _key(point, tol=1.0):
    """Cle de regroupement des noeuds : deux points a moins de `tol` mm sont un."""
    return tuple(int(round(float(v) / tol)) for v in point)
