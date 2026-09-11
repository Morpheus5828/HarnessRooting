
"""
HarnessOpt - Implementation de la Harness Routing Heuristic (HRH)
Karlsson, Ablad, Hermansson, Carlson, Tenfalt (arXiv:2311.09061), Algos 1-2-3.

Corrections principales par rapport a la version precedente :
  1. GRILLE UNIQUE PARTAGEE  : tous les cables cherchent dans le meme graphe
     (noeuds = indices entiers). Sans cela deux cables ne peuvent jamais
     partager une arete -> jamais de tronc commun.
  2. CRITERE D'ACCEPTATION GLOBAL (Algorithme 2) : on compare
     f = w_L*f_L + w_B*f_B (f_B = cout de l'UNION des aretes), et non plus le
     cout individuel du cable. C'etait LE bug bloquant : le cout Algo-3 d'un
     cable est toujours >= son cout initial (w_B=0), donc `new_cost < costs[k]`
     n'etait jamais vrai et la boucle sortait apres 1 iteration.
  3. Cout d'arete conforme a l'article : c_e = ||u-v|| * mean(cout_noeud),
     puis w_L*c_e si l'arete est deja empruntee par Phi, (w_L+w_B)*c_e sinon.
  4. Initialisation sequentielle (facon alpha-SPHRH) : le tronc apparait des
     la premiere passe.
  5. LISSAGE topologique : on lisse les SEGMENTS du graphe union (pas les
     cables un par un), donc le tronc commun reste rigoureusement commun.
  6. Metriques + figure avant/apres lissage (longueur, longueur commune,
     rayon de courbure min, clearance min).

Ce module est le coeur algorithmique de l'application : il ne depend ni de
Tkinter, ni de PyVista, ni de CATIA. La progression est publiee via un
`core.events.Reporter`, qui porte aussi le drapeau d'annulation.

Utilisation sans CATIA :  python -m harnessopt.core.hrh --demo
"""

from __future__ import annotations

import argparse
import heapq
import math
import time
from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

from ..log import Logger
from .collider import MeshCollider
from .events import Cancelled

LOG = Logger("routage")

# ==========================================================================
# 1. HYPERPARAMETRES
# ==========================================================================


@dataclass
class RoutingParams:
    # --- grille / environnement ---
    resolution: float = 20.0      # taille de cellule (mm). Cf. Table 1 de l'article
    clearance: float = 15.0       # distance mini axe-toron <-> structure (mm)
    d_pref: float = 60.0          # au-dela, on commence a payer (routage "colle" a la structure)
    k_far: float = 0.4            # penalite max d'eloignement (cout noeud dans [1, 1+k_far])

    # --- objectif bi-critere (article : w_L + w_B = 1) ---
    w_L: float = 0.15
    w_B: float = 0.85

    # --- collision ---
    forbid_crossing: bool = True  # interdit toute arete qui traverse un triangle

    # --- A* / HRH ---
    eps: float = 1.0              # 1.0 = A* optimal ; 1.1-1.3 = beaucoup plus rapide
    max_sweeps: int = 10          # garde-fou sur la boucle Algo 2
    time_budget_s: float = 300.0

    # --- attraction vers les passages de fixation (champ de cout, Fig. 3b) ---
    passage_reward: float = 0.35     # cout relatif dans la zone d'un passage (<1 = attirant)
    pin_passages: bool = True        # epingle le toron EXACTEMENT sur p_in / p_out
    pin_tol: float = 45.0            # distance maxi pour epingler un passage (mm)
    passage_radius_cells: float = 1.5  # rayon d'attraction, en cellules
    anchor_passages: bool = True     # epingler le toron exactement sur les traversees
    anchor_tol: float = 40.0         # distance sous laquelle une traversee est epinglee (mm)
    anchor_blend: float = 0.0        # >0 : repartit l'ecart sur N mm d'arc (degrade l'epinglage)
    clip_spacing: float = 250.0      # espacement maxi entre deux clips
    snap_passages: bool = True       # imposer les fixations proches du trace
    snap_radius_cells: float = 4.0   # rayon de detection, en cellules de grille
    # Garde-fous sur les traversees imposees (cf. core.fixations) : sans eux
    # une piece mal reconnue ou une fixation voisine fabrique des demi-tours.
    passage_max_span: float = 150.0    # largeur maxi d'un vrai passage de cable (mm)
    passage_max_detour: float = 120.0  # rallongement maxi accepte pour desservir une fixation (mm)
    passage_align_cos: float = 0.5     # |cos| mini pour imposer p_in ET p_out (0.5 = 60 deg)
    # Un peigne offre sept a dix encoches : celle du milieu est preferee, et la
    # suite d'encoches est choisie d'un bloc pour que le rail file droit.
    clamp_center_bias: float = 0.5
    # Un collier a un oeil plus petit que la distance de securite : sans
    # derogation locale, le harnais ne pourrait jamais l'enfiler.
    passage_clearance: float = 2.0   # garde admise a l'interieur d'une fixation
    passage_zone_cells: float = 2.0  # rayon de la derogation, en cellules
    clip_tol_cells: float = 1.5      # tolerance pour dire qu'un passage est "emprunte"

    # --- regles de branchement (article, Fig. 13b) ---
    branch_repair: bool = True
    L_min_BB: float = 120.0       # longueur mini entre deux branchements
    L_min_TB: float = 80.0        # longueur mini terminal -> branchement
    # Amorce droite en sortie de connecteur : le toron doit sortir dans l'axe
    # avant de pouvoir tourner, sans quoi il force sur les contacts.
    connector_lead_in: float = 40.0

    # --- lissage (smooth router) ---
    shortcut: bool = True
    keep_cost_field: bool = True   # le lissage ne doit pas quitter le couloir prefere
    cost_tol: float = 0.05
    smooth_iters: int = 150
    smooth_alpha: float = 0.30
    # Tension du tronc commun (0 = aucune). Les troncons partages sont tires
    # vers leur corde, et les branchements suivent ce qui tire le plus fort.
    # Ce n'est pas du lissage : lisser efface les angles, tendre raccourcit.
    trunk_tension: float = 0.5
    min_bend_radius: float = 80.0
    resample_step: float = 12.0

    def check(self):
        if abs(self.w_L + self.w_B - 1.0) > 1e-6:
            raise ValueError("L'article impose w_L + w_B = 1.")
        if self.clearance >= self.d_pref:
            raise ValueError("clearance doit etre < d_pref.")


# ==========================================================================
# 2. GRILLE DE ROUTAGE (graphe partage par tous les cables)
# ==========================================================================

# bornage memoire du cache des aretes deja testees
CROSS_CACHE_MAX = 2_000_000

NEIGHBOURS = [
    (dx, dy, dz)
    for dx in (-1, 0, 1)
    for dy in (-1, 0, 1)
    for dz in (-1, 0, 1)
    if (dx, dy, dz) != (0, 0, 0)
]  # ||u - v||_inf = 1  -> 26 voisins (definition de E dans l'article)


def enclosing_bounds(bounds, *point_groups, margin=0.0):
    """
    Boite englobant a la fois la maquette et tous les points imposes.

    `point_groups` accepte des terminaux (couples de points), des listes de
    points de passage, ou des objets porteurs de p_in / p_out.
    """
    lo = np.array([bounds[0], bounds[2], bounds[4]], float)
    hi = np.array([bounds[1], bounds[3], bounds[5]], float)

    def _absorb(value):
        nonlocal lo, hi
        if value is None:
            return
        if hasattr(value, "p_in") and hasattr(value, "p_out"):
            _absorb(value.p_in)
            _absorb(value.p_out)
            return
        arr = np.asarray(value, dtype=object)
        try:
            pt = np.asarray(value, float)
        except (TypeError, ValueError):
            pt = None
        if pt is not None and pt.shape == (3,):
            lo = np.minimum(lo, pt)
            hi = np.maximum(hi, pt)
            return
        if arr.ndim == 0:
            return
        for item in value:
            _absorb(item)

    for group in point_groups:
        _absorb(group)

    lo -= margin
    hi += margin
    return (float(lo[0]), float(hi[0]), float(lo[1]), float(hi[1]),
            float(lo[2]), float(hi[2]))


class RoutingGrid:
    """Graphe cartesien regulier + champ de cout issu de la distance a la structure."""

    def __init__(self, obstacles, bounds, p: RoutingParams, margin=1, attractors=None):
        self.p = p
        self.res = float(p.resolution)
        xmin, xmax, ymin, ymax, zmin, zmax = bounds
        self.origin = np.array([xmin, ymin, zmin], float) - margin * self.res
        self._ox, self._oy, self._oz = (float(v) for v in self.origin)
        span = np.array([xmax, ymax, zmax], float) + margin * self.res - self.origin
        self.dims = np.maximum(np.floor(span / self.res).astype(int) + 1, 1)
        # `obstacles` : MeshCollider, maillage PyVista, ou nuage de points.
        self.collider = MeshCollider.coerce(obstacles)
        self.check_crossing = bool(p.forbid_crossing) and self.collider.exact
        self._dist = {}
        self._cross_cache = {}
        # zones de derogation : l'interieur des fixations que le harnais doit
        # traverser (voir set_passage_zones)
        self.passage_tree = None
        self.passage_radius = 0.0
        # zones ou le regroupement des cables est refuse (souhait "separer")
        self.no_share_tree = None
        self.no_share_radius = 0.0
        self._no_share = {}
        self.passage_clearance = float(p.passage_clearance)
        self._clearance = float(p.clearance)
        self.attractors = None if attractors is None or len(attractors) == 0 \
            else np.asarray(attractors, float)
        self.att_tree = None if self.attractors is None else cKDTree(self.attractors)
        self.att_radius = p.passage_radius_cells * self.res
        # gain = surcout applique LOIN des passages. On penalise l'exterieur plutot
        # que de recompenser l'interieur : le cout noeud reste >= 1, donc
        # l'heuristique A* reste admissible (un simple facteur d'echelle sur f).
        self.att_gain = 1.0 / max(p.passage_reward, 1e-3)
        self._cost = {}

    # ---- conversions ----
    def world(self, n):
        return self.origin + np.asarray(n, float) * self.res

    def world_t(self, n):
        """
        Coordonnees monde d'un noeud, en tuple.

        `world` alloue un tableau numpy a chaque appel : sur un cheminement
        c'etait 7,3 millions d'allocations pour 17 s de calcul. Les chemins
        chauds n'ont besoin que de trois flottants.
        """
        ox, oy, oz = self._ox, self._oy, self._oz
        r = self.res
        return (ox + n[0] * r, oy + n[1] * r, oz + n[2] * r)

    def world_many(self, nodes):
        return self.origin + np.asarray(nodes, float) * self.res

    def index(self, pt):
        return tuple(int(round(v)) for v in (np.asarray(pt, float) - self.origin) / self.res)

    def in_bounds(self, n):
        return 0 <= n[0] < self.dims[0] and 0 <= n[1] < self.dims[1] and 0 <= n[2] < self.dims[2]

    # ---- zones sans regroupement (souhait "separer les cables") ----
    def set_no_share_zones(self, points, radius=None):
        """
        Declare les zones ou les cables ne doivent PAS se regrouper.

        Le regroupement vient de la remise de l'article : une arete deja
        empruntee ne coute que w_L au cable suivant. Dans ces zones, la remise
        est supprimee -- chaque cable paie son passage plein tarif et n'a plus
        aucun interet a suivre le tronc commun.
        """
        pts = np.atleast_2d(np.asarray(points, float)) if points is not None \
            else np.empty((0, 3))
        self._no_share.clear()
        if not len(pts):
            self.no_share_tree = None
            return 0
        self.no_share_tree = cKDTree(pts)
        self.no_share_radius = float(radius if radius is not None else 150.0)
        LOG.info(f"{len(pts)} zone(s) sans regroupement "
                 f"(rayon {self.no_share_radius:.0f} mm)")
        return len(pts)

    def no_share_node(self, n) -> bool:
        """Ce noeud tombe-t-il dans une zone ou le regroupement est refuse ?"""
        if self.no_share_tree is None:
            return False
        hit = self._no_share.get(n)
        if hit is None:
            hit = bool(self.no_share_tree.query(self.world_t(n))[0]
                       <= self.no_share_radius)
            self._no_share[n] = hit
        return hit

    # ---- derogation de garde a l'interieur des fixations ----
    def set_passage_zones(self, points, radius=None, clearance=None):
        """
        Declare les zones ou la garde est relachee.

        Une fixation que le harnais doit emprunter fait partie du maillage : son
        oeil est a quelques millimetres de la matiere, bien en dessous de la
        distance de securite. Sans cette derogation locale, aucun chemin ne
        passerait par un collier impose.
        """
        pts = np.atleast_2d(np.asarray(points, float)) if points is not None \
            else np.empty((0, 3))
        if not len(pts):
            self.passage_tree = None
            return 0
        self.passage_tree = cKDTree(pts)
        self.passage_radius = float(radius if radius is not None
                                    else self.p.passage_zone_cells * self.res)
        self.passage_clearance = float(clearance if clearance is not None
                                       else self.p.passage_clearance)
        self._cost.clear()      # le champ de cout depend de la garde
        LOG.info(f"{len(pts)} zone(s) de derogation de garde "
                 f"(rayon {self.passage_radius:.0f} mm, garde ramenee a "
                 f"{self.passage_clearance:.0f} mm)")
        return len(pts)

    def clearance_at(self, points):
        """Garde exigee en chaque point : reduite a l'interieur des fixations."""
        pts = np.atleast_2d(np.asarray(points, float))
        out = np.full(len(pts), float(self.p.clearance))
        if self.passage_tree is not None:
            inside = self.passage_tree.query(pts)[0] <= self.passage_radius
            out[inside] = self.passage_clearance
        return out

    def clearance_one(self, point):
        if self.passage_tree is None:
            return self._clearance          # cas courant : aucun appel numpy
        d = float(self.passage_tree.query(np.asarray(point, float))[0])
        return self.passage_clearance if d <= self.passage_radius \
            else self._clearance

    # ---- distances a la structure ----
    def distance(self, points):
        """Distance exacte a la surface, pour un lot de points monde."""
        return self.collider.distance(points)

    def distance_one(self, point):
        return self.collider.distance_one(point)

    def node_distance(self, n):
        """Distance du noeud de grille `n` a la structure (memoisee)."""
        d = self._dist.get(n)
        if d is None:
            d = self.collider.distance_one(self.world_t(n))
            self._dist[n] = d
        return d

    # ---- champ de cout ----
    def node_cost(self, n):
        """inf si le noeud viole la clearance (noeud retire du graphe, cf. section 2.1)."""
        c = self._cost.get(n)
        if c is None:
            d = self.node_distance(n)
            if d < (self._clearance if self.passage_tree is None
                    else self.clearance_one(self.world_t(n))):
                c = math.inf
            else:
                far = min(1.0, max(0.0, (d - self.p.d_pref) / max(self.p.d_pref, 1e-9)))
                c = 1.0 + self.p.k_far * far
                if self.att_tree is not None:
                    da = float(self.att_tree.query(self.world(n))[0])
                    t = min(1.0, da / max(self.att_radius, 1e-9))
                    t = t * t * (3 - 2 * t)               # smoothstep
                    c *= 1.0 + (self.att_gain - 1.0) * t  # 1 sur le passage, gain loin
            self._cost[n] = c
        return c

    def cost_at(self, points):
        """Cout du champ evalue directement en coordonnees monde (vectorise)."""
        pts = np.atleast_2d(np.asarray(points, float))
        d = self.collider.distance(pts)
        far = np.clip((d - self.p.d_pref) / max(self.p.d_pref, 1e-9), 0.0, 1.0)
        c = 1.0 + self.p.k_far * far
        if self.att_tree is not None:
            da = self.att_tree.query(pts)[0]
            t = np.clip(da / max(self.att_radius, 1e-9), 0.0, 1.0)
            c = c * (1.0 + (self.att_gain - 1.0) * t * t * (3 - 2 * t))
        c[d < self.clearance_at(pts)] = np.inf
        return c

    def edge_cost(self, u, v):
        """
        Cout d'arete de l'article, plus le controle de non-traversee.

        Une arete dont les deux noeuds respectent la garde peut malgre tout
        passer trop pres de la structure en son milieu, voire percer une cloison
        mince. La garde est donc verifiee sur le SEGMENT entier, et pas
        seulement aux deux noeuds. Le controle n'est declenche que pour les
        aretes reellement a risque (cf. MeshCollider.edge_blocked).
        """
        cu = self.node_cost(u)
        if cu == math.inf:
            return math.inf
        cv = self.node_cost(v)
        if cv == math.inf:
            return math.inf
        d = self.res * math.dist(u, v)

        if self.check_crossing:
            # Sortie rapide EN LIGNE : pour tout point p du segment,
            # d(p) >= min(d(u), d(v)) - longueur/2. Quand cette borne depasse
            # deja la garde, il n'y a ni appel de fonction, ni allocation
            # numpy, ni recherche dans le cache -- et c'est le cas de la quasi
            # totalite des aretes.
            du = self._dist.get(u)
            dv = self._dist.get(v)
            if du is None:
                du = self.node_distance(u)
            if dv is None:
                dv = self.node_distance(v)
            limit = self._clearance
            if self.passage_tree is not None:
                limit = min(self.clearance_one(self.world_t(u)),
                            self.clearance_one(self.world_t(v)))
            if (du if du < dv else dv) - 0.5 * d < limit:
                key = (u, v) if u <= v else (v, u)
                hit = self._cross_cache.get(key)
                if hit is None:
                    hit = self.collider.edge_blocked(
                        self.world_t(u), self.world_t(v), du, dv,
                        clearance=limit, step=max(1.0, 0.25 * limit))
                    if len(self._cross_cache) < CROSS_CACHE_MAX:
                        self._cross_cache[key] = hit
                if hit:
                    return math.inf
        return d * 0.5 * (cu + cv)

    def euclid(self, a, b):
        return self.res * math.dist(a, b)

    # ---- accrochage des terminaux ----
    def snap(self, pt, max_shell=6, label="Terminal"):
        """
        Noeud de grille libre le plus proche d'un point donne.

        En cas d'echec, le diagnostic distingue les deux causes -- point hors
        de la grille, ou point enferme dans la structure -- car elles appellent
        des corrections opposees.
        """
        n0 = self.index(pt)
        best, best_d = None, math.inf
        for r in range(max_shell + 1):
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    for dz in range(-r, r + 1):
                        if max(abs(dx), abs(dy), abs(dz)) != r:
                            continue
                        n = (n0[0] + dx, n0[1] + dy, n0[2] + dz)
                        if not self.in_bounds(n) or math.isinf(self.node_cost(n)):
                            continue
                        d = np.linalg.norm(self.world(n) - np.asarray(pt, float))
                        if d < best_d:
                            best, best_d = n, d
            if best is not None:
                return best
        raise ValueError(self._snap_diagnosis(pt, max_shell, label))

    def _snap_diagnosis(self, pt, max_shell, label):
        """Explique pourquoi aucun noeud n'a pu etre accroche."""
        pt = np.asarray(pt, float)
        lo = self.origin
        hi = self.origin + (self.dims - 1) * self.res
        outside = np.where((pt < lo) | (pt > hi))[0]
        reach = max_shell * self.res
        head = f"{label} ({pt[0]:.0f}, {pt[1]:.0f}, {pt[2]:.0f}) : "

        if len(outside):
            axes = ", ".join("XYZ"[i] for i in outside)
            return (head + "ce point est HORS de la zone de calcul.\n\n"
                    f"Zone couverte : X {lo[0]:.0f} a {hi[0]:.0f}, "
                    f"Y {lo[1]:.0f} a {hi[1]:.0f}, Z {lo[2]:.0f} a {hi[2]:.0f} mm "
                    f"(depassement sur {axes}).\n\n"
                    "Verifier le repere des coordonnees saisies, ou charger la "
                    "partie de maquette qui contient ce connecteur.")

        d = self.distance_one(pt)
        return (head + f"aucun point libre a moins de {reach:.0f} mm "
                f"({max_shell} mailles).\n\n"
                f"Distance de ce point a la structure : {d:.1f} mm, "
                f"distance de securite demandee : {self.p.clearance:.0f} mm.\n\n"
                "Le connecteur est enferme dans la structure. Pistes : reduire "
                "la distance de securite, reduire la taille de maille, ou "
                "deplacer le point vers l'espace libre.")


# ==========================================================================
# 3. ALGORITHME 3 : plus court chemin avec penalite d'arete non partagee
# ==========================================================================


def astar_non_shared_penalty(grid: RoutingGrid, start, goal, shared_nodes,
                             w_L, w_B, budget=None, reporter=None, counter=None,
                             cable=None, stage=""):
    """
    Cout d'arete = w_L*c_e si e est empruntee par un chemin de Phi,
                   (w_L + w_B)*c_e sinon.                       (Algorithme 3)

    `reporter` (core.events.Reporter) recoit le nombre de noeuds developpes :
    c'est la mesure "noeuds visites par iteration" affichee par l'interface.
    """
    p = grid.p
    # borne admissible sur le cout au metre : w_L si du partage est possible,
    # (w_L + w_B) sinon (aucune arete partagee -> toutes payent le plein tarif).
    h_rate = w_L if shared_nodes else (w_L + w_B)
    h_rate *= p.eps

    open_set = [(h_rate * grid.euclid(start, goal), start)]
    came, g = {}, {start: 0.0}
    closed = set()
    t0 = time.time()
    n0 = counter[0] if counter is not None else 0

    def _report(found):
        if reporter is not None:
            n = (counter[0] if counter is not None else 0) - n0
            reporter.astar_end(n, time.time() - t0, found, cable=cable, stage=stage)

    while open_set:
        _, cur = heapq.heappop(open_set)
        if cur in closed:
            continue
        closed.add(cur)
        if counter is not None:
            counter[0] += 1
            if reporter is not None and counter[0] % 2000 == 0:
                reporter.astar_tick(counter[0], time.time() - t0, g[cur])
                if reporter.cancelled:
                    raise Cancelled()
        if cur == goal:
            path = [cur]
            while cur in came:
                cur = came[cur]
                path.append(cur)
            _report(True)
            return path[::-1], g[goal]
        if budget is not None and time.time() > budget:
            break

        cu_shared = cur in shared_nodes
        for dx, dy, dz in NEIGHBOURS:
            nb = (cur[0] + dx, cur[1] + dy, cur[2] + dz)
            if nb in closed or not grid.in_bounds(nb):
                continue
            base = grid.edge_cost(cur, nb)
            if math.isinf(base):
                continue
            shared = (cu_shared and (nb in shared_nodes)
                      and not grid.no_share_node(nb))
            cost = (w_L * base) if shared else ((w_L + w_B) * base)
            tentative = g[cur] + cost
            if tentative < g.get(nb, math.inf) - 1e-12:
                came[nb] = cur
                g[nb] = tentative
                heapq.heappush(open_set, (tentative + h_rate * grid.euclid(nb, goal), nb))

    _report(False)
    return None, math.inf


def route_cable(grid, waypoints, shared_nodes, w_L, w_B, **kw):
    """Chaine les A* entre terminaux et passages imposes (peignes/fixations)."""
    full = []
    for a, b in zip(waypoints[:-1], waypoints[1:]):
        if a == b:
            continue
        seg, _ = astar_non_shared_penalty(grid, a, b, shared_nodes, w_L, w_B, **kw)
        if seg is None:
            return None
        full.extend(seg if not full else seg[1:])
    return full or list(waypoints)


# ==========================================================================
# 4. OBJECTIF (eq. 1-2) ET ALGORITHME 2
# ==========================================================================


def _ekey(a, b):
    return (a, b) if a <= b else (b, a)


def path_edges(path):
    return [_ekey(a, b) for a, b in zip(path[:-1], path[1:]) if a != b]


def objective(grid, routes, w_L, w_B):
    """f = w_L * sum_k sum_e c_e y_e^k  +  w_B * sum_e c_e x_e."""
    f_L, union = 0.0, {}
    for path in routes:
        if path is None:
            return math.inf, math.inf, math.inf
        for e in path_edges(path):
            c = union.get(e)
            if c is None:
                c = grid.edge_cost(*e)
                union[e] = c
            f_L += c
    f_B = sum(union.values())
    return w_L * f_L + w_B * f_B, f_L, f_B


def nodes_of(routes, skip=None):
    s = set()
    for k, path in enumerate(routes):
        if k == skip or path is None:
            continue
        s.update(path)
    return s


def harness_routing_heuristic(grid: RoutingGrid, cable_waypoints, reporter=None,
                              initial_routes=None):
    """
    Algorithme 1 (etape 1) : Algorithme 2 avec acceptation sur l'objectif global.

    `initial_routes` permet de partir d'une solution existante -- solution du
    sous-probleme lagrangien (SHRH, §3.2) ou trace precedent -- au lieu de
    l'initialisation sequentielle.
    """
    p = grid.p
    w_L, w_B = p.w_L, p.w_B
    K = len(cable_waypoints)
    counter = [0]
    budget = time.time() + p.time_budget_s

    def _kw(k, stage):
        return dict(budget=budget, reporter=reporter, counter=counter,
                    cable=k, stage=stage)

    # --- Initialisation : routes fournies, sinon construction sequentielle ---
    if initial_routes is not None and len(initial_routes) == K \
            and all(r for r in initial_routes):
        best = [list(r) for r in initial_routes]
        f_best, fL, fB = objective(grid, best, w_L, w_B)
        history = [f_best]
        if reporter is not None:
            reporter.phase("Reprise d'une solution initiale", ratio=0.35)
            reporter.sweep(0, f_best, fL, fB)
        return _algorithm2(grid, cable_waypoints, best, f_best, history,
                           budget, counter, reporter)

    routes = []
    for k, wps in enumerate(cable_waypoints):
        if reporter is not None:
            reporter.raise_if_cancelled()
            reporter.phase(f"Initialisation : cable {k + 1}/{K}",
                           ratio=0.1 + 0.3 * k / max(K, 1))
        shared = nodes_of(routes)
        path = route_cable(grid, wps, shared, w_L, w_B, **_kw(k, "init"))
        if path is None:  # repli : plus court chemin pur
            path = route_cable(grid, wps, set(), w_L, w_B, **_kw(k, "init-fallback"))
        if path is None:
            return None, []
        routes.append(path)

    best = list(routes)
    f_best, fL, fB = objective(grid, best, w_L, w_B)
    history = [f_best]
    if reporter is not None:
        reporter.sweep(0, f_best, fL, fB)

    return _algorithm2(grid, cable_waypoints, best, f_best, history,
                       budget, counter, reporter)


def _algorithm2(grid, cable_waypoints, best, f_best, history, budget, counter,
                reporter):
    """Algorithme 2 : un cable a la fois, compteur remis a zero si amelioration."""
    p = grid.p
    w_L, w_B = p.w_L, p.w_B
    K = len(cable_waypoints)

    def _kw(k, stage):
        return dict(budget=budget, reporter=reporter, counter=counter,
                    cable=k, stage=stage)

    # --- Algorithme 2 : un cable a la fois, reset du compteur si amelioration ---
    i, k, sweeps = 1, 0, 0
    while i <= K and sweeps < p.max_sweeps * K and time.time() < budget:
        sweeps += 1
        if reporter is not None:
            reporter.raise_if_cancelled()
            reporter.phase(f"Optimisation HRH : passe {sweeps}, cable {k + 1}/{K}",
                           ratio=0.4 + 0.4 * min(1.0, sweeps / float(p.max_sweeps * K)))
        phi = nodes_of(best, skip=k)
        new_path = route_cable(grid, cable_waypoints[k], phi, w_L, w_B,
                               **_kw(k, "hrh"))
        if new_path is not None:
            cand = list(best)
            cand[k] = new_path
            f_cand, fL, fB = objective(grid, cand, w_L, w_B)
            if f_cand < f_best - 1e-9:
                best, f_best = cand, f_cand
                i = 0
                history.append(f_best)
                if reporter is not None:
                    reporter.sweep(len(history) - 1, f_best, fL, fB)
        i += 1
        k = (k + 1) % K

    return best, history


# ==========================================================================
# 5. TOPOLOGIE : segments de toron, points de branchement
# ==========================================================================


def build_topology(routes):
    owners, adj = {}, {}
    for k, path in enumerate(routes):
        for e in path_edges(path):
            owners.setdefault(e, set()).add(k)
    for a, b in owners:
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)

    terminals = set()
    for path in routes:
        terminals.add(path[0])
        terminals.add(path[-1])

    def is_break(n):
        nb = adj[n]
        if len(nb) != 2 or n in terminals:
            return True
        return owners[_ekey(n, nb[0])] != owners[_ekey(n, nb[1])]

    breaks = {n for n in adj if is_break(n)}
    segments, seen = [], set()
    for s in breaks:
        for nb in adj[s]:
            if _ekey(s, nb) in seen:
                continue
            seen.add(_ekey(s, nb))
            seg, prev, cur = [s, nb], s, nb
            while cur not in breaks:
                nxt = [x for x in adj[cur] if x != prev]
                if not nxt:
                    break
                prev, cur = cur, nxt[0]
                seen.add(_ekey(prev, cur))
                seg.append(cur)
            segments.append(seg)

    edge2seg = {}
    for sid, seg in enumerate(segments):
        for e in path_edges(seg):
            edge2seg[e] = sid

    branch_points = [n for n in breaks if len(adj[n]) >= 3]
    return dict(owners=owners, adj=adj, segments=segments, edge2seg=edge2seg,
                breaks=breaks, branch_points=branch_points, terminals=terminals)


# ==========================================================================
# 6. SMOOTH ROUTER (lissage par segment -> le tronc reste commun)
# ==========================================================================


def _clear(grid, pts, margin=0.0):
    pts = np.atleast_2d(pts)
    return bool(np.all(grid.distance(pts) >= grid.clearance_at(pts) - margin))


def _sample(a, b, step):
    n = max(2, int(np.linalg.norm(b - a) / step) + 1)
    return a + np.linspace(0, 1, n)[:, None] * (b - a)


def _mean_cost(grid, pts):
    c = grid.cost_at(pts)
    return float(np.inf) if not np.all(np.isfinite(c)) else float(c.mean())


# Le controle d'interference echantillonne tous les 2 mm : un raccourci valide
# a un pas plus grossier peut plonger sous la garde entre deux echantillons.
SEG_CHECK_STEP = 2.0


def _seg_ok(grid, a, b, ref_cost=None):
    sample = _sample(a, b, min(SEG_CHECK_STEP, 0.4 * grid.res))
    if not _clear(grid, sample):
        return False
    if grid.check_crossing and grid.collider.crosses(a, b):
        return False        # un raccourci ne doit jamais percer une cloison
    if ref_cost is None:
        return True
    # un raccourci geometriquement valide mais qui quitte le couloir prefere
    # (peignes, surfaces clipables) est refuse : sinon le lissage annule le
    # travail du champ de cout et le toron redevient une ligne droite.
    return _mean_cost(grid, sample) <= ref_cost * (1.0 + grid.p.cost_tol)


"""def shortcut_polyline(grid, pts):
    guard = grid.p.keep_cost_field and grid.att_tree is not None
    out, i = [pts[0]], 0
    while i < len(pts) - 1:
        j = len(pts) - 1
        while j > i + 1:
            ref = _mean_cost(grid, _sample_path(pts[i:j + 1], 0.4 * grid.res)) if guard else None
            if _seg_ok(grid, pts[i], pts[j], ref):
                break
            j -= 1
        out.append(pts[j])
        i = j
    return np.array(out)"""

def shortcut_polyline(grid, pts):
    guard = False
    out, i = [pts[0]], 0
    while i < len(pts) - 1:
        j = len(pts) - 1
        while j > i + 1:
            ref = _mean_cost(grid, _sample_path(pts[i:j + 1], 0.4 * grid.res)) if guard else None
            if _seg_ok(grid, pts[i], pts[j], ref):
                break
            j -= 1
        out.append(pts[j])
        i = j
    return np.array(out)


def _sample_path(pts, step):
    out = [_sample(a, b, step) for a, b in zip(pts[:-1], pts[1:])]
    return np.vstack(out) if out else np.atleast_2d(pts)


def resample(pts, step):
    pts = np.asarray(pts, float)
    if len(pts) < 2:
        return pts
    seglen = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    s = np.concatenate([[0.0], np.cumsum(seglen)])
    if s[-1] < 1e-9:
        return pts
    n = max(3, int(s[-1] / step) + 1)
    t = np.linspace(0, s[-1], n)
    return np.column_stack([np.interp(t, s, pts[:, d]) for d in range(3)])


def curvature_radii(pts):
    """Rayon du cercle circonscrit en chaque point interieur (approx. rayon de cintrage)."""
    pts = np.asarray(pts, float)
    if len(pts) < 3:
        return np.array([np.inf])
    a, b, c = pts[:-2], pts[1:-1], pts[2:]
    ab = np.linalg.norm(b - a, axis=1)
    bc = np.linalg.norm(c - b, axis=1)
    ac = np.linalg.norm(c - a, axis=1)
    cross = np.linalg.norm(np.cross(b - a, c - b), axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(cross > 1e-9, ab * bc * ac / (2 * cross + 1e-12), np.inf)
    return r


"""def _bend_pass(grid, pts, p, n_pass=40, frozen=None):
   
    cur = np.asarray(pts, float).copy()
    if len(cur) < 3:
        return cur
    for _ in range(n_pass):
        r = curvature_radii(cur)
        bad = np.where(r < p.min_bend_radius)[0] + 1
        if frozen is not None and len(frozen) == len(cur):
            bad = bad[~frozen[bad]]
        if len(bad) == 0:
            break
        prop = cur.copy()
        prop[bad] = cur[bad] + 0.5 * (0.5 * (cur[bad - 1] + cur[bad + 1]) - cur[bad])
        keep = grid.distance(prop[bad]) >= grid.clearance_at(prop[bad])
        cur[bad[keep]] = prop[bad[keep]]
    return cur"""


def _bend_pass(grid, pts, p, n_pass=40, frozen=None):
    """Relance localement le lissage la ou le rayon de cintrage est insuffisant."""
    cur = np.asarray(pts, float).copy()
    if len(cur) < 3:
        return cur
    for _ in range(n_pass):
        r = curvature_radii(cur)
        bad = np.where(r < p.min_bend_radius)[0] + 1

        # ON SUPPRIME LE BLOCAGE DES POINTS FIGÉS
        # Les ancrages (peignes/colliers) pourront s'arrondir s'ils violent le
        # rayon de courbure min, évitant ainsi les angles à 90°.
        # if frozen is not None and len(frozen) == len(cur):
        #     bad = bad[~frozen[bad]]

        if len(bad) == 0:
            break

        prop = cur.copy()
        prop[bad] = cur[bad] + 0.5 * (0.5 * (cur[bad - 1] + cur[bad + 1]) - cur[bad])
        keep = grid.distance(prop[bad]) >= grid.clearance_at(prop[bad])
        cur[bad[keep]] = prop[bad[keep]]
    return cur


def dedupe(pts, tol=1e-3):
    pts = np.asarray(pts, float)
    if len(pts) < 2:
        return pts
    keep = np.concatenate([[True], np.linalg.norm(np.diff(pts, axis=0), axis=1) > tol])
    return pts[keep]


def _blend_shift(pts, i, target, blend):
    """
    Amene pts[i] exactement sur `target` en repartissant l'ecart en cosinus sur
    `blend` mm d'arc de part et d'autre. Sans cette repartition, deplacer un
    seul echantillon de 20-40 mm cree un pic de courbure (rayon de quelques mm).
    """
    pts = np.asarray(pts, float).copy()
    seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    s = np.concatenate([[0.0], np.cumsum(seg)])
    room = min(blend, s[i] , s[-1] - s[i])
    if room < 1e-6:
        return pts
    w = np.clip(1.0 - np.abs(s - s[i]) / room, 0.0, 1.0)
    w = 0.5 * (1.0 - np.cos(np.pi * w))
    w[0] = w[-1] = 0.0
    pts += w[:, None] * (np.asarray(target, float) - pts[i])
    pts[i] = target
    return pts


def _segment_geometry(grid, seg_world, anchors, p: RoutingParams):
    """
    Decoupe le troncon aux traversees proches, y insere les coordonnees EXACTES
    du p_in/p_out, lisse chaque sous-portion separement. Retourne (points,
    masque des points figes).
    """
    pts = np.asarray(seg_world, float).copy()
    marks = []
    if p.anchor_passages and anchors is not None and len(anchors) and len(pts) > 2:
        d, idx = cKDTree(pts).query(np.asarray(anchors, float))
        seen = set()
        for k in np.argsort(idx):
            i = int(idx[k])
            if d[k] <= p.anchor_tol and 0 < i < len(pts) - 1 and i not in seen:
                seen.add(i)
                pts = _blend_shift(pts, i, anchors[k], p.anchor_blend)
                marks.append(i)
    cuts = [0] + sorted(marks) + [len(pts) - 1]

    pieces = []
    for a, b in zip(cuts[:-1], cuts[1:]):
        sub = pts[a:b + 1]
        if len(sub) < 2:
            continue
        if p.shortcut and len(sub) > 2:
            sub = shortcut_polyline(grid, sub)
        sub = resample(sub, p.resample_step)
        pieces.append(sub if not pieces else sub[1:])
    if not pieces:
        return np.asarray(seg_world, float), np.ones(len(seg_world), bool)

    g = np.vstack(pieces)
    frozen = np.zeros(len(g), bool)
    frozen[0] = frozen[-1] = True
    cum = len(pieces[0]) - 1
    for piece in pieces[1:]:
        frozen[cum] = True               # point epingle sur la traversee
        cum += len(piece)
    return g, frozen


def smooth_harness(grid, routes, topo, p: RoutingParams, anchor_points=None):
    """
    Smooth router topologique. Les points de branchement sont des variables
    PARTAGEES entre segments : le tronc reste rigoureusement commun et la
    tangente reste continue de part et d'autre d'un branchement.
    Retourne (polylignes par cable, geometrie lissee par segment).
    """
    segs, terminals = topo["segments"], topo["terminals"]

    geo, frozen = [], []
    for seg in segs:
        g, fz = _segment_geometry(grid, grid.world_many(seg), anchor_points, p)
        geo.append(g)
        frozen.append(fz)

    inc = {}
    for sid, seg in enumerate(segs):
        for end, node in ((0, seg[0]), (-1, seg[-1])):
            inc.setdefault(node, []).append((sid, end))
    jpos = {n: grid.world(n) for n in inc}
    free_junctions = [n for n in inc if n not in terminals]
    for n, lst in inc.items():
        for sid, end in lst:
            geo[sid][end] = jpos[n]

    a = p.smooth_alpha
    guard_cost = p.keep_cost_field and grid.att_tree is not None
    for _ in range(p.smooth_iters):
        for sid, pts in enumerate(geo):
            if len(pts) < 3:
                continue
            prop = pts.copy()
            prop[1:-1] = pts[1:-1] + a * (0.5 * (pts[:-2] + pts[2:]) - pts[1:-1])
            bad = grid.distance(prop[1:-1]) < grid.clearance_at(prop[1:-1])
            bad |= frozen[sid][1:-1]        # traversees epinglees : intouchables
            if guard_cost:
                bad |= grid.cost_at(prop[1:-1]) > grid.cost_at(pts[1:-1]) + grid.p.cost_tol
            bad |= frozen[sid][1:-1]
            if np.any(bad):
                prop[1:-1][bad] = pts[1:-1][bad]
            geo[sid] = prop
            fx = frozen[sid]
            for i in np.where(fx)[0]:       # tangente continue de part et d'autre
                if i == 0 or i == len(prop) - 1:
                    continue
                t = prop[i + 1] - prop[i - 1]
                nt = np.linalg.norm(t)
                if nt < 1e-9:
                    continue
                t /= nt
                for j, sgn in ((i - 1, -1.0), (i + 1, 1.0)):
                    if fx[j]:
                        continue
                    cand = prop[i] + sgn * t * np.linalg.norm(prop[j] - prop[i])
                    if float(grid.distance(cand[None])[0]) >= grid.clearance_one(cand):
                        prop[j] = cand
            geo[sid] = prop
        for n in free_junctions:  # branchement = moyenne des voisins de TOUS les segments
            nbrs = [geo[sid][1] if end == 0 else geo[sid][-2]
                    for sid, end in inc[n] if len(geo[sid]) >= 2]
            if not nbrs:
                continue
            newp = jpos[n] + a * (np.mean(nbrs, axis=0) - jpos[n])
            if float(grid.distance(newp[None])[0]) >= grid.clearance_one(newp):
                jpos[n] = newp
                for sid, end in inc[n]:
                    geo[sid][end] = newp

    geo = [_bend_pass(grid, g, p, frozen=fz) for g, fz in zip(geo, frozen)]
    for n, lst in inc.items():  # re-synchronise les extremites apres la passe de cintrage
        for sid, end in lst:
            geo[sid][end] = jpos[n]

    out = []
    for path in routes:
        pts, i, ok = [], 0, True
        while i < len(path) - 1:
            sid = topo["edge2seg"].get(_ekey(path[i], path[i + 1]))
            if sid is None:
                ok = False
                break
            seg, chunk = segs[sid], geo[sid]
            if seg[0] == path[i]:
                pass
            elif seg[-1] == path[i]:
                chunk = chunk[::-1]
            else:
                ok = False
                break
            pts.extend(chunk if not pts else chunk[1:])
            i += len(seg) - 1
        out.append(dedupe(np.array(pts)) if ok and pts else grid.world_many(path))
    return out, geo


# ==========================================================================
# 7. METRIQUES
# ==========================================================================


def passage_midpoints(passages):
    """Milieux exacts des traversees, utilises comme points d'ancrage du toron."""
    out = []
    for c in passages or []:
        a = np.asarray(getattr(c, "p_in", None) if hasattr(c, "p_in") else c[0], float)
        b = np.asarray(getattr(c, "p_out", None) if hasattr(c, "p_out") else c[1], float)
        out.append(0.5 * (a + b))
    return np.array(out) if out else np.empty((0, 3))


def corridor_attractors(passages, step=15.0, max_link=600.0):
    """
    Couloir continu passant par toutes les traversees, et pas seulement des
    bulles isolees autour de chaque peigne : on relie les milieux successifs
    (tries le long de l'axe principal du nuage) tant qu'ils sont a moins de
    `max_link`. Sans ce chainage, l'espace entre deux peignes reste au plein
    tarif et le cable prefere la ligne droite.
    """
    mids, pts = [], []
    for c in passages or []:
        a = np.asarray(getattr(c, "p_in", None) if hasattr(c, "p_in") else c[0], float)
        b = np.asarray(getattr(c, "p_out", None) if hasattr(c, "p_out") else c[1], float)
        mids.append(0.5 * (a + b))
        n = max(2, int(np.linalg.norm(b - a) / step) + 1)
        pts.append(a + np.linspace(0, 1, n)[:, None] * (b - a))
    if not mids:
        return np.empty((0, 3))
    mids = np.array(mids)
    if len(mids) > 1:                      # tri le long de l'axe principal (PCA)
        c0 = mids - mids.mean(0)
        axis = np.linalg.svd(c0, full_matrices=False)[2][0]
        mids = mids[np.argsort(c0 @ axis)]
        for a, b in zip(mids[:-1], mids[1:]):
            d = np.linalg.norm(b - a)
            if d <= max_link:
                n = max(2, int(d / step) + 1)
                pts.append(a + np.linspace(0, 1, n)[:, None] * (b - a))
    return np.vstack(pts)


def passage_attractors(passages, step=8.0):
    """
    Echantillonne les traversees (p_in -> p_out) en points d'attraction pour le
    champ de cout. `passages` : objets avec .p_in/.p_out, ou couples de points.
    """
    pts = []
    for c in passages or []:
        a = np.asarray(getattr(c, "p_in", None) if hasattr(c, "p_in") else c[0], float)
        b = np.asarray(getattr(c, "p_out", None) if hasattr(c, "p_out") else c[1], float)
        n = max(2, int(np.linalg.norm(b - a) / step) + 1)
        pts.append(a + np.linspace(0, 1, n)[:, None] * (b - a))
    return np.vstack(pts) if pts else np.empty((0, 3))


def clips_on_passages(polyline, passage_points, max_spacing=250.0, tol=30.0,
                      min_from_ends=40.0):
    """
    Positions de clips : d'abord les passages REELLEMENT empruntes par le toron
    (a moins de `tol`), puis des clips intercalaires la ou l'ecart depasse
    `max_spacing`. Retourne (clips_sur_passage, clips_ajoutes).
    """
    pts = np.asarray(polyline, float)
    if len(pts) < 2:
        return np.empty((0, 3)), np.empty((0, 3))
    seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    s = np.concatenate([[0.0], np.cumsum(seg)])
    total = s[-1]

    def at(u):
        return np.array([np.interp(u, s, pts[:, d]) for d in range(3)])

    anchored = []
    if passage_points is not None and len(passage_points):
        tree = cKDTree(pts)
        d, idx = tree.query(np.asarray(passage_points, float))
        for dd, ii in zip(d, idx):
            if dd > tol or total < 2.2 * min_from_ends:
                continue
            # un passage tombant sur un branchement : on decale le clip juste
            # apres, on ne le perd pas (regle "clip entre branchement et toron")
            anchored.append(float(np.clip(s[ii], min_from_ends, total - min_from_ends)))
        anchored = sorted(set(np.round(anchored, 1)))
        merged = []
        for u in anchored:  # un seul clip par traversee
            if not merged or u - merged[-1] > tol:
                merged.append(u)
        anchored = merged

    extra = []
    knots = [0.0] + list(anchored) + [total]
    for u0, u1 in zip(knots[:-1], knots[1:]):
        gap = u1 - u0
        if gap > max_spacing:
            n = int(np.ceil(gap / max_spacing)) - 1
            extra.extend(u0 + (gap / (n + 1)) * np.arange(1, n + 1))

    return (np.array([at(u) for u in anchored]) if anchored else np.empty((0, 3)),
            np.array([at(u) for u in extra]) if extra else np.empty((0, 3)))


def polyline_length(pts):
    pts = np.asarray(pts, float)
    return float(np.sum(np.linalg.norm(np.diff(pts, axis=0), axis=1))) if len(pts) > 1 else 0.0


def interference_report(grid, polylines, step=2.0, margin=0.0):
    """
    Compte les interferences avec la structure : on echantillonne les polylignes
    tous les `step` mm et on regarde ou la distance passe sous la clearance.
    Retourne le nombre de zones (interferences distinctes), le nombre
    d'echantillons fautifs, la penetration maximale et la clearance minimale.
    """
    zones, n_bad, worst, dmin = 0, 0, 0.0, math.inf
    for pts in polylines:
        pts = np.asarray(pts, float)
        if len(pts) < 2:
            continue
        sub = [_sample(a, b, step) for a, b in zip(pts[:-1], pts[1:])]
        sample = np.vstack(sub)
        d = grid.distance(sample)
        limit = grid.clearance_at(sample) - margin
        dmin = min(dmin, float(d.min()))
        bad = d < limit
        n_bad += int(bad.sum())
        if bad.any():
            worst = max(worst, float((limit[bad] - d[bad]).max()))
            zones += int(np.count_nonzero(np.diff(bad.astype(int)) == 1) + int(bad[0]))
    return dict(zones=zones, samples=n_bad, max_penetration=worst,
                min_clearance=0.0 if math.isinf(dmin) else dmin)


def bend_report(grid, seg_geo, topo=None):
    """Rayon de cintrage par troncon : min, max, mediane, et nb de tronçons hors specs."""
    rows = []
    for sid, g in enumerate(seg_geo or []):
        r = curvature_radii(g)
        r = r[np.isfinite(r)]
        if len(r) == 0:
            continue
        n = len(topo["owners"][path_edges(topo["segments"][sid])[0]]) if topo else 1
        rows.append(dict(sid=sid, n_cables=n, rmin=float(r.min()),
                         rmax=float(r.max()), rmed=float(np.median(r))))
    return rows


def metrics(grid, routes_pts, topo=None, seg_geo=None):
    total = sum(polyline_length(p) for p in routes_pts)
    radii = np.concatenate([curvature_radii(p) for p in routes_pts])
    radii = radii[np.isfinite(radii)]
    dmin = min(float(grid.distance(np.atleast_2d(p)).min()) for p in routes_pts)
    common, r_seg = None, []
    if topo is not None:
        common = 0.0
        for sid, seg in enumerate(topo["segments"]):
            g = seg_geo[sid] if seg_geo is not None else grid.world_many(seg)
            if len(topo["owners"][path_edges(seg)[0]]) > 1:
                common += polyline_length(g)
            rr = curvature_radii(g)
            r_seg.append(rr[np.isfinite(rr)])
        r_seg = [r for r in r_seg if len(r)]
        r_seg = np.concatenate(r_seg) if r_seg else np.array([])
    # R min par troncon de toron : le "coude" d'un point de branchement est une
    # discontinuite voulue de la topologie, il est reporte a part.
    return dict(total_length=total,
                common_length=common,
                n_branch=len(topo["branch_points"]) if topo else None,
                min_bend_radius=float(r_seg.min()) if len(r_seg) else float("inf"),
                min_bend_radius_paths=float(radii.min()) if len(radii) else float("inf"),
                min_clearance=dmin)


# ==========================================================================
# 8. FIGURE AVANT / APRES LISSAGE
# ==========================================================================


def _draw_connector(ax, center, direction, size=(45.0, 26.0, 20.0), color="limegreen"):
    """Pave droit vert figurant le connecteur, aligne sur la sortie du cable."""
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    d = np.asarray(direction, float)
    n = np.linalg.norm(d)
    d = d / n if n > 1e-9 else np.array([1.0, 0.0, 0.0])
    tmp = np.array([0.0, 0.0, 1.0]) if abs(d[2]) < 0.9 else np.array([0.0, 1.0, 0.0])
    e2 = np.cross(d, tmp); e2 /= np.linalg.norm(e2)
    e3 = np.cross(d, e2)
    R = np.column_stack([d, e2, e3]) * (np.asarray(size, float) / 2.0)
    c = np.asarray(center, float) - d * size[0] * 0.5
    corners = np.array([[sx, sy, sz] for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)])
    v = c + corners @ R.T
    faces = [[0, 1, 3, 2], [4, 5, 7, 6], [0, 1, 5, 4],
             [2, 3, 7, 6], [0, 2, 6, 4], [1, 3, 7, 5]]
    ax.add_collection3d(Poly3DCollection([v[f] for f in faces], facecolor=color,
                                         edgecolor="0.25", linewidths=0.4, alpha=0.9))


def bundle_polylines(grid, topo, seg_geo=None):
    """[(polyligne, nb de cables)] pour visualiser le tronc commun."""
    out = []
    for sid, seg in enumerate(topo["segments"]):
        g = seg_geo[sid] if seg_geo is not None else grid.world_many(seg)
        out.append((np.asarray(g), len(topo["owners"][path_edges(seg)[0]])))
    return out


def figure_before_after(routes_raw, routes_smooth, m_raw, m_smooth,
                        obstacle_points=None, history=None, savepath=None,
                        bundles_raw=None, bundles_smooth=None):
    import matplotlib
    if savepath is not None:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(15, 5.5))
    colors = plt.cm.tab10.colors

    for idx, (routes, title, m, bundles) in enumerate(
        [(routes_raw, "Avant lissage (topologie A*/HRH)", m_raw, bundles_raw),
         (routes_smooth, "Apres lissage (smooth router)", m_smooth, bundles_smooth)]
    ):
        ax = fig.add_subplot(1, 3, idx + 1, projection="3d")
        if obstacle_points is not None and len(obstacle_points):
            sub = obstacle_points[:: max(1, len(obstacle_points) // 3000)]
            ax.scatter(sub[:, 0], sub[:, 1], sub[:, 2], s=1, c="0.8", alpha=0.35)
        if bundles:  # tronc commun : epaisseur proportionnelle au nb de cables
            for g, n in bundles:
                if n > 1:
                    ax.plot(g[:, 0], g[:, 1], g[:, 2], lw=2 + 3.0 * n,
                            color="0.35", alpha=0.55, solid_capstyle="round", zorder=1)
        for k, pts in enumerate(routes):
            pts = np.asarray(pts)
            ax.plot(pts[:, 0], pts[:, 1], pts[:, 2], lw=1.6, alpha=0.95,
                    color=colors[k % 10], label=f"cable {k}", zorder=3)
            _draw_connector(ax, pts[0], pts[0] - pts[min(3, len(pts) - 1)])
            _draw_connector(ax, pts[-1], pts[-1] - pts[max(-4, -len(pts))])
        ax.set_title(f"{title}\nL={m['total_length']:.0f} mm | "
                     f"commun={0 if m['common_length'] is None else m['common_length']:.0f} mm | "
                     f"Rmin(troncon)={m['min_bend_radius']:.0f} mm", fontsize=9)
        ax.set_box_aspect((1, 1, 0.6))
        ax.tick_params(labelsize=6)

    ax3 = fig.add_subplot(1, 3, 3)
    if history:
        ax3.plot(history, "o-", color="tab:blue")
        ax3.set_xlabel("ameliorations acceptees (Algo 2)")
        ax3.set_ylabel("f = w_L f_L + w_B f_B")
        ax3.set_title("Convergence de la HRH", fontsize=9)
        ax3.grid(alpha=0.3)
    else:
        for k, pts in enumerate(routes_smooth):
            r = curvature_radii(pts)
            ax3.plot(r[np.isfinite(r)], color=colors[k % 10])
        ax3.set_yscale("log")
        ax3.set_title("Rayon de courbure apres lissage")

    fig.tight_layout()
    if savepath:
        fig.savefig(savepath, dpi=130)
    return fig


# ==========================================================================
# 9. PIPELINE COMPLET
# ==========================================================================


def run_pipeline(obstacles, bounds, terminals, p: RoutingParams,
                 extra_waypoints=None, reporter=None, attractors=None,
                 anchor_points=None, snap_passages=None, smooth=True,
                 initial_routes=None, no_share_zones=None):
    """
    terminals : liste de (start_xyz, goal_xyz)
    extra_waypoints : liste (meme longueur) de listes de points imposes (peignes)
    Retour : dict avec routes brutes (mm), routes lissees (mm), topologie, metriques.
    """
    p.check()
    if reporter is not None:
        reporter.phase("Construction de la grille de routage", ratio=0.02)

    # L'article discretise "within a bounding box enclosing the terminals" :
    # la boite de la maquette ne suffit pas, un connecteur se trouve souvent
    # en dehors de l'encombrement de la structure (et une piece plane donne
    # meme une boite d'epaisseur nulle).
    bounds = enclosing_bounds(bounds, terminals, extra_waypoints,
                              snap_passages, margin=3.0 * p.resolution)
    grid = RoutingGrid(obstacles, bounds, p, attractors=attractors)
    if no_share_zones is not None:
        pts, radius = no_share_zones
        grid.set_no_share_zones(pts, radius or None)

    cable_wps = []
    for i, (s, g) in enumerate(terminals):
        wps = [grid.snap(s, label=f"Depart du cable {i + 1}")]
        if extra_waypoints:
            for j, w in enumerate(extra_waypoints[i]):
                wps.append(grid.snap(w, label=f"Passage impose {j + 1} "
                                              f"du cable {i + 1}"))
        wps.append(grid.snap(g, label=f"Arrivee du cable {i + 1}"))
        cable_wps.append(wps)

    t0 = time.time()
    routes, history = harness_routing_heuristic(grid, cable_wps, reporter,
                                                initial_routes=initial_routes)
    if routes is None:
        # Distinguer les deux causes : un budget epuise et un chemin
        # reellement inexistant demandent des corrections opposees, et le
        # message generique envoyait l'utilisateur sur la mauvaise piste.
        if time.time() - t0 >= 0.95 * p.time_budget_s:
            raise RuntimeError(
                f"Temps de calcul maximal atteint ({p.time_budget_s:.0f} s) "
                "avant d'avoir trouve un chemin complet.\n\n"
                "Pistes : augmenter le temps de calcul, augmenter la taille de "
                "maille, ou choisir un profil plus rapide.")
        raise RuntimeError(
            "Aucun chemin ne relie les terminaux dans l'espace disponible.\n\n"
            "Pistes : reduire la distance de securite, verifier que les "
            "connecteurs ne sont pas noyes dans la structure, ou affiner la "
            "taille de maille pour passer dans les ouvertures etroites.")

    # --- passage OBLIGATOIRE par les fixations proches du trace ---
    # Premiere passe libre, puis re-routage en imposant les traversees que le
    # faisceau ne faisait que froler : une fixation existante situee sur le
    # chemin doit etre empruntee, pas contournee.
    snap_report = None
    if snap_passages and p.snap_passages:
        # Les pieces trop larges pour etre un passage de cable sont ecartees
        # AVANT tout usage : elles ne doivent ni attirer le faisceau, ni
        # relacher la garde, ni etre imposees.
        from .fixations import filter_passages
        snap_passages, _dropped = filter_passages(
            snap_passages, p.passage_max_span, LOG)
    if snap_passages and p.snap_passages:
        # derogation de garde a l'interieur des fixations a emprunter
        zone_pts = []
        for c in snap_passages:
            a = np.asarray(getattr(c, "p_in", None) if hasattr(c, "p_in") else c[0],
                           float)
            b = np.asarray(getattr(c, "p_out", None) if hasattr(c, "p_out") else c[1],
                           float)
            zone_pts.extend([a, b, 0.5 * (a + b)])
        grid.set_passage_zones(np.array(zone_pts) if zone_pts else None)
        from .fixations import mandatory_waypoints
        if reporter is not None:
            reporter.raise_if_cancelled()
            reporter.phase("Passage par les fixations existantes", ratio=0.78)
        radius = p.snap_radius_cells * p.resolution
        forced, snap_report = mandatory_waypoints(
            grid, routes, cable_wps, snap_passages, radius, LOG,
            max_span=p.passage_max_span, max_detour=p.passage_max_detour,
            align_cos=p.passage_align_cos,
            center_bias=p.clamp_center_bias)
        if snap_report["total"]:
            routes2, history2 = harness_routing_heuristic(grid, forced, reporter)
            if routes2 is not None:
                routes, cable_wps = routes2, forced
                history = history + history2
            else:
                LOG.warn("re-routage impose impossible : trace libre conserve")
    t_route = time.time() - t0

    topo = build_topology(routes)
    branch_report = None
    if p.branch_repair:
        if reporter is not None:
            reporter.raise_if_cancelled()
            reporter.phase("Reprise des points de branchement (Lmin)", ratio=0.82)
        from .topo import repair_branch_points
        routes, branch_report = repair_branch_points(
            grid, routes, topo, p.w_L, p.w_B,
            L_min_BB=p.L_min_BB, L_min_TB=p.L_min_TB)
        topo = build_topology(routes)
    raw_pts = [grid.world_many(r) for r in routes]

    t1 = time.time()
    if smooth:
        if reporter is not None:
            reporter.raise_if_cancelled()
            reporter.phase("Lissage du toron (smooth router)", ratio=0.88)
        smooth_pts, seg_geo = smooth_harness(grid, routes, topo, p,
                                             anchor_points=anchor_points)
    else:
        # Le cheminement s'arrete au trace brut : le lissage est ensuite pilote
        # par l'utilisateur, via le curseur 0-100 %.
        smooth_pts = [np.asarray(x, float) for x in raw_pts]
        seg_geo = [grid.world_many(seg) for seg in topo["segments"]]
    t_smooth = time.time() - t1

    if reporter is not None:
        reporter.phase("Calcul des metriques", ratio=0.96)
    m_raw = metrics(grid, raw_pts, topo)
    interf = interference_report(grid, smooth_pts)
    bends = bend_report(grid, seg_geo, topo)
    m_smooth = metrics(grid, smooth_pts, topo, seg_geo)
    return dict(grid=grid, routes=routes, topo=topo, history=history,
                branch_report=branch_report, snap_report=snap_report,
                cable_waypoints=cable_wps, anchor_points=anchor_points,
                raw=raw_pts, smooth=smooth_pts, seg_geo=seg_geo,
                interferences=interf, bends=bends,
                metrics_raw=m_raw, metrics_smooth=m_smooth,
                t_route=t_route, t_smooth=t_smooth)


# ==========================================================================
# 10. DEMO (sans CATIA ni PyVista) : valide que le tronc commun apparait
# ==========================================================================


def demo_environment():
    pts = []
    # sol
    xs, ys = np.meshgrid(np.arange(0, 1001, 20.0), np.arange(0, 601, 20.0))
    pts.append(np.column_stack([xs.ravel(), ys.ravel(), np.zeros(xs.size)]))
    # plafond
    pts.append(np.column_stack([xs.ravel(), ys.ravel(), np.full(xs.size, 400.0)]))
    # cloison percee en x = 430, trou autour de (y=300, z=200)
    yy, zz = np.meshgrid(np.arange(0, 601, 15.0), np.arange(0, 401, 15.0))
    yy, zz = yy.ravel(), zz.ravel()
    hole = (np.abs(yy - 300) < 90) & (np.abs(zz - 200) < 90)
    wall = np.column_stack([np.full((~hole).sum(), 430.0), yy[~hole], zz[~hole]])
    pts.append(wall)
    return np.vstack(pts), (0.0, 1000.0, 0.0, 600.0, 0.0, 400.0)


def main_demo(savepath="harness_before_after.png"):
    obst, bounds = demo_environment()
    terminals = [((60, 80, 120), (940, 320, 300)),
                 ((60, 520, 120), (940, 360, 280)),
                 ((60, 300, 340), (940, 260, 150)),
                 ((60, 140, 320), (940, 200, 320)),
                 ((60, 460, 300), (940, 420, 120))]
    p = RoutingParams(resolution=20.0, clearance=25.0, d_pref=120.0, k_far=0.3,
                      w_L=0.15, w_B=0.85, eps=1.15, min_bend_radius=80.0)
    # traversees fictives (p_in -> p_out) : le faisceau doit venir s'y accrocher
    passages = [((410, 300, 200), (450, 300, 200)),
                ((410, 250, 260), (450, 250, 260))]
    att = passage_attractors(passages)
    res = run_pipeline(obst, bounds, terminals, p, attractors=att)
    br = res["branch_report"]
    print(f"routage {res['t_route']:.1f}s | lissage {res['t_smooth']:.1f}s | "
          f"{len(res['topo']['branch_points'])} point(s) de branchement")
    if br:
        print(f"branchements {br['n_branch_before']} -> {len(res['topo']['branch_points'])} "
              f"({br['moves']} deplacement(s), {br['collapses']} suppression(s)) | "
              f"violations Lmin {br['violations_before']} -> {br['violations_after']}")
    for name in ("metrics_raw", "metrics_smooth"):
        m = res[name]
        print(f"{name:14s} L={m['total_length']:8.0f} mm  commun={m['common_length']:7.0f} mm  "
              f"Rmin(troncon)={m['min_bend_radius']:6.0f} mm  "
              f"Rmin(coude branchement)={m['min_bend_radius_paths']:5.0f} mm  "
              f"clearance={m['min_clearance']:5.0f} mm")
    for g, n in bundle_polylines(res["grid"], res["topo"], res["seg_geo"]):
        if n > 1:
            anc, extra = clips_on_passages(g, att, max_spacing=p.clip_spacing,
                                           tol=p.clip_tol_cells * p.resolution)
            print(f"tronc n={n} : {len(anc)} clip(s) sur passage, {len(extra)} intercalaire(s)")
    figure_before_after(res["raw"], res["smooth"], res["metrics_raw"], res["metrics_smooth"],
                        obstacle_points=obst, history=res["history"], savepath=savepath,
                        bundles_raw=bundle_polylines(res["grid"], res["topo"]),
                        bundles_smooth=bundle_polylines(res["grid"], res["topo"], res["seg_geo"]))
    print("figure ->", savepath)
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--out", default="harness_before_after.png")
    a = ap.parse_args()
    if a.demo:
        main_demo(a.out)
    else:
        print(__doc__)
