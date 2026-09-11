"""
SHRH - Subgradient Harness Routing Heuristic (article, §3.2, algorithme 4).

Relaxation lagrangienne des contraintes de selection d'arete (3c) du CHRP,
puis maximisation du dual par methode du sous-gradient projete.

Ce que cela apporte, et que la seule HRH ne donne pas :

  * une BORNE INFERIEURE h(lambda) sur l'optimum. L'ecart entre cette borne et
    la meilleure solution trouvee mesure la qualite du cheminement : sans elle,
    on ne sait pas si l'on est a 2 % ou a 40 % de l'optimum ;
  * des solutions initiales TOPOLOGIQUEMENT DIFFERENTES. Chaque iteration duale
    produit un jeu de chemins distinct ; passe a la HRH, il conduit a un autre
    optimum local. C'est le mecanisme des figures 6b-6c de l'article.

Rappel du modele. Les variables duales lambda^k_e verifient (eq. 7)

        somme_k lambda^k_e = w_B * c_e ,      lambda^k_e >= 0 ,

et la fonction duale se reduit alors a une somme de plus courts chemins

        h(lambda) = somme_k  min_{y^k dans P^k}  somme_e (w_L c_e + lambda^k_e) y^k_e .

Le sous-gradient est simplement l'indicatrice des chemins optimaux, et la
projection sur Omega_e est une projection sur le simplexe d'echelle w_B c_e.
"""

from __future__ import annotations

import heapq
import math
import time

import numpy as np

from ..log import Logger
from .events import Cancelled
from .hrh import (NEIGHBOURS, _ekey, harness_routing_heuristic, objective,
                  path_edges)

LOG = Logger("shrh")


# --------------------------------------------------------------------------
# projection sur Omega_e
# --------------------------------------------------------------------------

def project_simplex(v, total):
    """
    Projection euclidienne de `v` sur {x >= 0, somme(x) = total}.

    Algorithme classique par tri (Michelot / Duchi). C'est la projection
    P_Omega_e de l'etape 2 de l'algorithme 4.
    """
    v = np.asarray(v, float)
    if total <= 0:
        return np.zeros_like(v)
    n = v.size
    u = np.sort(v)[::-1]
    css = np.cumsum(u) - total
    idx = np.arange(1, n + 1)
    cond = u - css / idx > 0
    if not np.any(cond):
        return np.full(n, total / n)
    rho = idx[cond][-1]
    theta = css[cond][-1] / rho
    return np.maximum(v - theta, 0.0)


# --------------------------------------------------------------------------
# sous-probleme : plus court chemin a couts lagrangiens
# --------------------------------------------------------------------------

class _DualCosts:
    """
    Couts duaux, stockes de facon creuse.

    lambda^k_e = w_B*c_e/K + delta^k_e, avec somme_k delta^k_e = 0. A
    l'initialisation delta est nul partout : seules les aretes reellement
    empruntees par un chemin finissent dans le dictionnaire, ce qui rend le
    stockage proportionnel au nombre d'aretes visitees et non a |E|.
    """

    def __init__(self, grid, K, w_L, w_B):
        self.grid = grid
        self.K = K
        self.w_L = w_L
        self.w_B = w_B
        self.base_rate = w_L + w_B / K      # cout uniforme initial
        self.delta = {}

    def edge_cost(self, k, u, v):
        c = self.grid.edge_cost(u, v)
        if math.isinf(c):
            return math.inf
        d = self.delta.get(_ekey(u, v))
        return self.base_rate * c + (d[k] if d is not None else 0.0)

    def lam(self, edge, c_e):
        """Vecteur lambda_e complet (K composantes)."""
        d = self.delta.get(edge)
        base = self.w_B * c_e / self.K
        return np.full(self.K, base) if d is None else base + d

    def set_lam(self, edge, lam, c_e):
        base = self.w_B * c_e / self.K
        d = np.asarray(lam, float) - base
        if np.max(np.abs(d)) < 1e-12:
            self.delta.pop(edge, None)
        else:
            self.delta[edge] = d


def _astar_dual(grid, dual, k, start, goal, eps=1.0, budget=None, reporter=None,
                counter=None):
    """A* sur les couts lagrangiens du cable k. h = w_L * distance (admissible :
    le cout d'arete vaut au moins w_L*c_e et le cout de noeud vaut au moins 1)."""
    h_rate = dual.w_L * eps
    open_set = [(h_rate * grid.euclid(start, goal), start)]
    came, g = {}, {start: 0.0}
    closed = set()
    t0 = time.time()
    n0 = counter[0] if counter is not None else 0

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
            if reporter is not None:
                reporter.astar_end((counter[0] if counter else 0) - n0,
                                   time.time() - t0, True, cable=k, stage="shrh")
            return path[::-1], g[goal]
        if budget is not None and time.time() > budget:
            break
        for dx, dy, dz in NEIGHBOURS:
            nb = (cur[0] + dx, cur[1] + dy, cur[2] + dz)
            if nb in closed or not grid.in_bounds(nb):
                continue
            cost = dual.edge_cost(k, cur, nb)
            if math.isinf(cost):
                continue
            tentative = g[cur] + cost
            if tentative < g.get(nb, math.inf) - 1e-12:
                came[nb] = cur
                g[nb] = tentative
                heapq.heappush(open_set, (tentative + h_rate * grid.euclid(nb, goal), nb))

    if reporter is not None:
        reporter.astar_end((counter[0] if counter else 0) - n0,
                           time.time() - t0, False, cable=k, stage="shrh")
    return None, math.inf


def _solve_subproblem(grid, dual, cable_waypoints, eps, budget, reporter, counter):
    """h(lambda) et les chemins optimaux associes (le sous-gradient)."""
    paths, total = [], 0.0
    for k, wps in enumerate(cable_waypoints):
        full, cost = [], 0.0
        for a, b in zip(wps[:-1], wps[1:]):
            if a == b:
                continue
            seg, c = _astar_dual(grid, dual, k, a, b, eps, budget, reporter, counter)
            if seg is None:
                return None, math.inf
            cost += c
            full.extend(seg if not full else seg[1:])
        paths.append(full or list(wps))
        total += cost
    return paths, total


# --------------------------------------------------------------------------
# algorithme 4
# --------------------------------------------------------------------------

def subgradient_harness_routing(grid, cable_waypoints, reporter=None,
                                n_iter=100, i_hrh=25, delta0=1.5, delta_min=1e-4,
                                stall_limit=10, delta_mult=0.8, time_budget_s=None, eps=None):
    """
    Maximise le dual par sous-gradient projete et lance la HRH periodiquement.
    """
    p = grid.p
    w_L, w_B = p.w_L, p.w_B
    K = len(cable_waypoints)
    eps = p.eps if eps is None else eps
    budget = time.time() + (time_budget_s if time_budget_s is not None
                            else p.time_budget_s)
    counter = [0]
    dual = _DualCosts(grid, K, w_L, w_B)

    best_routes, f_best = None, math.inf
    h_best = -math.inf
    history = []  # (iteration, h, f_best)
    candidates = []
    delta = float(delta0)
    stall = 0

    for it in range(int(n_iter)):
        if time.time() > budget:
            LOG.warn(f"SHRH : budget de temps epuise a l'iteration {it}")
            break
        if reporter is not None:
            reporter.raise_if_cancelled()
            reporter.phase(f"SHRH : iteration duale {it + 1}/{n_iter}",
                           ratio=0.1 + 0.5 * it / max(n_iter, 1))

        paths, h = _solve_subproblem(grid, dual, cable_waypoints, eps, budget,
                                     reporter, counter)
        if paths is None:
            LOG.warn("SHRH : sous-probleme sans solution, arret")
            break
        if h > h_best + 1e-9:
            h_best = h
            stall = 0
        else:
            stall += 1
            if stall >= stall_limit:  # regle de Held-Karp avec multiplicateur paramétrable
                delta = max(delta_min, delta_mult * delta)
                stall = 0

        # --- primal : la HRH part de la solution duale courante ---
        if it % max(1, i_hrh) == 0 or it == n_iter - 1:
            routes, _ = harness_routing_heuristic(grid, cable_waypoints, reporter,
                                                  initial_routes=paths)
            if routes is not None:
                f, _, _ = objective(grid, routes, w_L, w_B)
                candidates.append(dict(iteration=it, f=f, routes=routes))
                if f < f_best - 1e-9:
                    best_routes, f_best = routes, f
                    LOG.info(f"SHRH iteration {it} : nouvelle meilleure solution "
                             f"f = {f:.1f}")

        history.append((it, h, f_best))
        gap = (f_best - h) / abs(f_best) if math.isfinite(f_best) and f_best > 0 else 1.0
        LOG.debug(f"iteration {it} : h = {h:.1f}, f = {f_best:.1f}, "
                  f"ecart = {100 * gap:.1f} %, delta = {delta:.2f}")

        # --- pas de Polyak et montee projetee ---
        if not math.isfinite(f_best):
            break
        norm2 = float(sum(len(path_edges(pa)) for pa in paths))
        if norm2 <= 0:
            break
        step = delta * max(f_best - h, 1e-9) / norm2

        used = {}                    # arete -> indicatrice par cable
        for k, pa in enumerate(paths):
            for e in path_edges(pa):
                vec = used.get(e)
                if vec is None:
                    vec = np.zeros(K)
                    used[e] = vec
                vec[k] = 1.0
        for e, xi in used.items():
            c_e = grid.edge_cost(*e)
            if math.isinf(c_e):
                continue
            lam = dual.lam(e, c_e) + step * xi
            dual.set_lam(e, project_simplex(lam, w_B * c_e), c_e)

    gap = ((f_best - h_best) / abs(f_best)) if (math.isfinite(f_best)
                                                and f_best > 0) else None
    if best_routes is not None:
        LOG.ok(f"SHRH : f = {f_best:.1f}, borne inferieure = {h_best:.1f}"
               + (f", ecart <= {100 * gap:.1f} %" if gap is not None else ""))
    return dict(routes=best_routes, f=f_best, lower_bound=h_best, gap=gap,
                history=history, candidates=candidates,
                n_nodes=counter[0], n_dual_iters=len(history))
