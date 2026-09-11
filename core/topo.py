
"""
Recherche locale sur les points de branchement (article, §4.4 et Fig. 13b).

Idee : une fois la topologie figee (quel cable emprunte quel troncon), la
fonction objectif se re-ecrit exactement

        f = sum_arcs (w_L * n_a + w_B) * cout(chemin_a)

ou n_a est le nombre de cables portes par le troncon a. Deplacer un point de
branchement revient donc a chercher le noeud v qui minimise
        sum_i (w_L*n_i + w_B) * dist_i(v)
avec dist_i = plus court chemin depuis le breakpoint voisin n_i, sous les
contraintes de longueur minimale L_min_BB (branchement-branchement) et
L_min_TB (terminal-branchement).

Si l'optimum tombe sur un voisin n_i, le troncon i devient de longueur nulle :
le point de branchement est SUPPRIME (fusionne avec n_i). Relocalisation et
suppression sont donc la meme operation, exactement comme dans l'article.
"""

from __future__ import annotations

import heapq
import math

import numpy as np

from .hrh import NEIGHBOURS, _ekey, path_edges, polyline_length


# --------------------------------------------------------------------------
# graphe reduit : noeuds = terminaux + branchements, arcs = troncons
# --------------------------------------------------------------------------

def build_arc_graph(grid, routes, topo):
    segs = topo["segments"]
    arcs = [dict(u=s[0], v=s[-1], path=list(s),
                 cables=set(topo["owners"][path_edges(s)[0]])) for s in segs]
    cable_seq = []
    for path in routes:
        seq, i = [], 0
        while i < len(path) - 1:
            sid = topo["edge2seg"].get(_ekey(path[i], path[i + 1]))
            if sid is None:
                break
            seg = segs[sid]
            if seg[0] != path[i] and seg[-1] != path[i]:
                break
            seq.append(sid)
            i += len(seg) - 1
        cable_seq.append(seq)
    return dict(arcs=arcs, cable_seq=cable_seq,
                terminals=set(topo["terminals"]),
                cable_ends=[(p[0], p[-1]) for p in routes])


def arc_path_cost(grid, path):
    return sum(grid.edge_cost(a, b) for a, b in zip(path[:-1], path[1:]))


def arc_length(grid, path):
    return polyline_length(grid.world_many(path))


def incident(ag, node):
    return [i for i, a in enumerate(ag["arcs"])
            if a is not None and (a["u"] == node or a["v"] == node)]


def all_nodes(ag):
    s = set()
    for a in ag["arcs"]:
        if a is not None:
            s.add(a["u"])
            s.add(a["v"])
    return s


def routes_from_arcs(ag):
    routes = []
    for k, seq in enumerate(ag["cable_seq"]):
        cur = ag["cable_ends"][k][0]
        path = [cur]
        for aid in seq:
            a = ag["arcs"][aid]
            if a is None:
                continue
            if a["u"] == cur:
                seg = a["path"]
            elif a["v"] == cur:
                seg = a["path"][::-1]
            else:
                return None
            path.extend(seg[1:])
            cur = seg[-1]
        if cur != ag["cable_ends"][k][1]:
            return None
        routes.append(path)
    return routes


# --------------------------------------------------------------------------
# Dijkstra borne (champ de distance depuis un breakpoint voisin)
# --------------------------------------------------------------------------

def dijkstra_field(grid, source, cost_cap=math.inf, max_settled=15000):
    dist = {source: 0.0}
    length = {source: 0.0}
    parent = {}
    settled = {}
    pq = [(0.0, source)]
    while pq:
        d, u = heapq.heappop(pq)
        if u in settled:
            continue
        settled[u] = d
        if d > cost_cap or len(settled) >= max_settled:
            break
        for dx, dy, dz in NEIGHBOURS:
            v = (u[0] + dx, u[1] + dy, u[2] + dz)
            if v in settled or not grid.in_bounds(v):
                continue
            c = grid.edge_cost(u, v)
            if math.isinf(c):
                continue
            nd = d + c
            if nd < dist.get(v, math.inf) - 1e-12:
                dist[v] = nd
                length[v] = length[u] + grid.euclid(u, v)
                parent[v] = u
                heapq.heappush(pq, (nd, v))
    return dict(dist=dist, length=length, parent=parent, settled=settled)


def backtrack(field, target, source):
    path = [target]
    cur = target
    while cur != source:
        cur = field["parent"][cur]
        path.append(cur)
    return path[::-1]  # source -> target


# --------------------------------------------------------------------------
# recherche locale
# --------------------------------------------------------------------------

def repair_branch_points(grid, routes, topo, w_L, w_B,
                         L_min_BB=120.0, L_min_TB=80.0,
                         max_rounds=6, max_settled=12000, verbose=False):
    """
    Relocalise ou supprime les points de branchement jusqu'a satisfaire
    L_min_BB / L_min_TB en minimisant f. Retourne (routes, rapport).
    """
    ag = build_arc_graph(grid, routes, topo)
    report = dict(n_branch_before=len(topo["branch_points"]),
                  moves=0, collapses=0, violations_before=0, violations_after=0)

    def lmin_for(node):
        return L_min_TB if node in ag["terminals"] else L_min_BB

    def violations():
        out = []
        for aid, a in enumerate(ag["arcs"]):
            if a is None:
                continue
            t_u, t_v = a["u"] in ag["terminals"], a["v"] in ag["terminals"]
            if t_u and t_v:
                continue  # terminal -> terminal : aucun branchement a deplacer
            need = L_min_TB if (t_u or t_v) else L_min_BB
            ln = arc_length(grid, a["path"])
            if ln < need - 1e-6:
                out.append((need - ln, aid))
        return sorted(out, reverse=True)

    report["violations_before"] = len(violations())

    for _ in range(max_rounds):
        changed = False
        nodes = [n for n in all_nodes(ag) if n not in ag["terminals"]]
        # on traite d'abord les branchements impliques dans une violation
        viol_nodes = set()
        for _, aid in violations():
            a = ag["arcs"][aid]
            viol_nodes.update([a["u"], a["v"]])
        nodes.sort(key=lambda n: n not in viol_nodes)

        for b in nodes:
            inc = incident(ag, b)
            if len(inc) < 2:
                continue
            others, weights, cur_cost = [], [], []
            for aid in inc:
                a = ag["arcs"][aid]
                n_i = a["v"] if a["u"] == b else a["u"]
                others.append((aid, n_i))
                weights.append(w_L * len(a["cables"]) + w_B)
                cur_cost.append(arc_path_cost(grid, a["path"]))
            score_now = sum(w * c for w, c in zip(weights, cur_cost))

            infeasible = any(
                arc_length(grid, ag["arcs"][aid]["path"]) < lmin_for(n_i) - 1e-6
                for (aid, n_i) in others)
            if not infeasible and len(inc) < 3:
                continue  # noeud de passage sain : rien a gagner

            fields = []
            for (aid, n_i), w, c in zip(others, weights, cur_cost):
                cap = 1.8 * c + 4.0 * grid.res
                fields.append(dijkstra_field(grid, n_i, cost_cap=cap,
                                             max_settled=max_settled))

            base = min(fields, key=lambda f: len(f["settled"]))
            best_v, best_score = None, (math.inf if infeasible else score_now - 1e-9)
            for v in base["settled"]:
                if v in ag["terminals"]:
                    continue
                s, ok = 0.0, True
                for (aid, n_i), w, f in zip(others, weights, fields):
                    if v == n_i:
                        continue  # troncon supprime
                    d = f["dist"].get(v)
                    if d is None:
                        ok = False
                        break
                    if f["length"][v] < lmin_for(n_i) - 1e-6:
                        ok = False
                        break
                    s += w * d
                    if s >= best_score:
                        ok = False
                        break
                if ok and s < best_score:
                    best_v, best_score = v, s

            if best_v is None and infeasible:
                # dernier recours : fusion avec le voisin le moins couteux,
                # meme si toutes les contraintes ne peuvent pas etre tenues
                cand = []
                for j, ((aid, n_i), f) in enumerate(zip(others, fields)):
                    if n_i in ag["terminals"]:
                        continue
                    if any(jj != j and n_i not in fj["dist"]
                           for jj, fj in enumerate(fields)):
                        continue
                    s = sum(w * fj["dist"][n_i]
                            for jj, (w, fj) in enumerate(zip(weights, fields))
                            if jj != j)
                    cand.append((s, n_i))
                if cand:
                    best_score, best_v = min(cand)

            if best_v is None or best_v == b:
                continue
            if any(best_v != n_i and best_v not in f["dist"]
                   for (aid, n_i), f in zip(others, fields)):
                continue

            snapshot = ([None if a is None else dict(a) for a in ag["arcs"]],
                        [list(s) for s in ag["cable_seq"]])
            collapsed = 0
            for (aid, n_i), f in zip(others, fields):
                if best_v == n_i:
                    ag["arcs"][aid] = None
                    collapsed += 1
                    continue
                p = backtrack(f, best_v, n_i)  # n_i -> best_v
                ag["arcs"][aid] = dict(u=best_v, v=n_i, path=p[::-1],
                                       cables=ag["arcs"][aid]["cables"])
            if collapsed:
                for seq in ag["cable_seq"]:
                    seq[:] = [aid for aid in seq if ag["arcs"][aid] is not None]

            if routes_from_arcs(ag) is None:  # securite : on annule
                ag["arcs"], ag["cable_seq"] = snapshot
                continue

            changed = True
            report["moves"] += 1
            report["collapses"] += collapsed
            if verbose:
                print(f"  branchement {b} -> {best_v} "
                      f"(f_local {score_now:.1f} -> {best_score:.1f}, {collapsed} suppr.)")
        if not changed:
            break

    new_routes = routes_from_arcs(ag)
    report["violations_after"] = len(violations())
    if new_routes is None:
        return routes, report
    return new_routes, report


# --------------------------------------------------------------------------
# mutualisation des traversees de fixations (peignes)
# --------------------------------------------------------------------------

def shared_crossing_waypoints(crossings, terminals, mode="midpoint", margin=0.05):
    """
    Une seule sequence de traversees pour TOUT le faisceau.

    `crossings` : liste d'objets/paires ayant .entry/.exit (ou (entry, exit)),
    obtenue une seule fois sur l'axe barycentrique des terminaux. Chaque cable
    ne recoit que les traversees comprises entre son depart et son arrivee
    (projection sur l'axe), ce qui evite que deux cables se voient attribuer
    des passages differents sur le meme peigne -> plus de tronc dedouble.

    mode="midpoint" : un seul waypoint par traversee (moins de serpentin).
    mode="inout"    : entree + sortie imposees (comportement precedent).
    """
    def ends(c):
        if hasattr(c, "entry"):
            return np.asarray(c.entry, float), np.asarray(c.exit, float)
        a, b = c
        return np.asarray(a, float), np.asarray(b, float)

    starts = np.array([t[0] for t in terminals], float)
    goals = np.array([t[1] for t in terminals], float)
    axis_a, axis_b = starts.mean(0), goals.mean(0)
    d = axis_b - axis_a
    n2 = float(d @ d) or 1.0

    def proj(p):
        return float((np.asarray(p, float) - axis_a) @ d) / n2

    items = []
    for c in crossings:
        e, x = ends(c)
        items.append((proj(0.5 * (e + x)), e, x))
    items.sort(key=lambda it: it[0])

    out = []
    for s, g in terminals:
        t0, t1 = sorted((proj(s), proj(g)))
        wps = []
        for t, e, x in items:
            if t0 - margin <= t <= t1 + margin:
                if mode == "inout":
                    wps.extend([tuple(e), tuple(x)])
                else:
                    wps.append(tuple(0.5 * (e + x)))
        out.append(wps)
    return out
