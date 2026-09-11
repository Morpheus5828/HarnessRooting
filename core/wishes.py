"""
Souhaits de modification du harnais.

Une fois le cheminement calcule, le concepteur veut la main : ecarter le
faisceau d'une zone de maintenance, ouvrir un coude trop serre, ou refuser que
deux cables se regroupent a tel endroit. Ce module traduit ces souhaits en
modifications geometriques, puis laisse l'algorithme REVALIDER le resultat :
un souhait n'a jamais le droit de creer une interference, de descendre sous la
garde ou de casser un rayon de cintrage.

Deux familles, parce qu'elles ne coutent pas la meme chose :

  * souhaits GEOMETRIQUES (`deplacer`, `rayon`) -- traites ici meme, en
    quelques secondes, par le lisseur : la topologie ne bouge pas.
  * souhaits de TOPOLOGIE (`separer`, `regrouper`) -- ils changent QUI passe
    avec QUI : ils sont traduits en zones remises au cheminement, qui doit
    etre relance. `routing_hints` les prepare.

Le rapport rendu dit, souhait par souhait, ce qui a ete obtenu et ce qui a ete
refuse -- jamais un souhait applique en silence au prix d'une regle metier.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

import numpy as np

from .analysis import interference_breakdown
from .hrh import curvature_radii, polyline_length
from ..log import Logger

LOG = Logger("souhaits")

KINDS = ("deplacer", "rayon", "separer", "regrouper")

# Fractions successives d'un souhait de deplacement : tout, la moitie, le quart.
FRACTIONS = (1.0, 0.5, 0.25)

LABELS = {
    "deplacer": "Deplacer le harnais",
    "rayon": "Ouvrir le rayon de courbure",
    "separer": "Separer les cables (moins de tronc commun)",
    "regrouper": "Regrouper les cables (plus de tronc commun)",
}


@dataclass
class Wish:
    """
    Un souhait de l'utilisateur.

    `point`  : ou il s'applique (mm, repere maquette).
    `radius` : sur quelle etendue il agit -- au-dela, le harnais ne bouge pas.
    `vector` : deplacement demande (souhait `deplacer`).
    `target` : rayon de cintrage vise (souhait `rayon`, en mm).
    """
    kind: str = "deplacer"
    point: tuple = (0.0, 0.0, 0.0)
    radius: float = 150.0
    vector: tuple = (0.0, 0.0, 0.0)
    target: float = 0.0
    note: str = ""

    @property
    def is_geometric(self) -> bool:
        return self.kind in ("deplacer", "rayon")

    def describe(self) -> str:
        p = ", ".join(f"{v:.0f}" for v in self.point)
        base = f"{LABELS.get(self.kind, self.kind)} en ({p}), portee {self.radius:.0f} mm"
        if self.kind == "deplacer":
            v = np.asarray(self.vector, float)
            base += f", de {float(np.linalg.norm(v)):.0f} mm"
        elif self.kind == "rayon":
            base += f", vise {self.target:.0f} mm"
        return base

    def to_dict(self):
        return dict(kind=self.kind, point=list(map(float, self.point)),
                    radius=float(self.radius), vector=list(map(float, self.vector)),
                    target=float(self.target), note=self.note)

    @classmethod
    def from_dict(cls, d):
        return cls(kind=d.get("kind", "deplacer"),
                   point=tuple(d.get("point", (0.0, 0.0, 0.0))),
                   radius=float(d.get("radius", 150.0)),
                   vector=tuple(d.get("vector", (0.0, 0.0, 0.0))),
                   target=float(d.get("target", 0.0)), note=d.get("note", ""))


def _weights(points, centre, radius):
    """Poids d'influence : 1 au centre du souhait, 0 au-dela de sa portee."""
    d = np.linalg.norm(np.asarray(points, float) - np.asarray(centre, float), axis=1)
    t = np.clip(d / max(float(radius), 1e-9), 0.0, 1.0)
    return (1.0 - t) ** 2 * (1.0 + 2.0 * t)          # lisse, derivee nulle aux bords


# ==========================================================================
# souhaits geometriques
# ==========================================================================

def apply_wishes(res, params, wishes, logger=LOG, passes=6, max_step=None):
    """
    Applique les souhaits geometriques et REVALIDE le harnais.

    Retourne dict(points, seg_geo, report, interferences, min_radius, length).
    `report` contient une ligne par souhait : ce qui a ete obtenu, ce qui a ete
    refuse, et pourquoi.
    """
    from .smoothing import Smoother

    grid = res["grid"]
    geometric = [w for w in wishes if w.is_geometric]
    report = []

    sm = Smoother(grid, res["routes"], res["topo"], params,
                  anchor_points=res.get("anchor_points"))
    # On repart de la geometrie AFFICHEE (palier de lissage courant) quand elle
    # a la meme decoupe, sinon du trace brut re-echantillonne.
    # `seg_geo_free` est la geometrie du palier AVANT les amorces de
    # connecteur : elle a la meme decoupe que le lisseur, donc elle se reprend
    # telle quelle. Les amorces seront reappliquees apres coup par l'appelant.
    cur = res.get("seg_geo_free") or res.get("seg_geo")
    if cur is not None and len(cur) == len(sm.geo) and \
            all(len(a) == len(b) for a, b in zip(cur, sm.geo)):
        sm.geo = [np.asarray(g, float).copy() for g in cur]
        for n, lst in sm.inc.items():
            sid, end = lst[0]
            sm.jpos[n] = sm.geo[sid][end].copy()
            for sid, end in lst:
                sm.geo[sid][end] = sm.jpos[n]
    elif cur is not None and logger:
        logger.debug("geometrie courante non reprise (decoupe differente) : "
                     "les souhaits partent du trace re-echantillonne")

    before = _measure(grid, sm)
    etat = _measure(grid, sm)          # etat courant, souhait apres souhait
    for wish in geometric:
        # Un souhait n'a jamais le droit de creer une interference : c'est la
        # regle annoncee. On mesure donc APRES CHAQUE souhait -- sinon la
        # faute serait imputee au dernier, ou pire, livree en silence. Et
        # plutot que de tout refuser pour un dixieme de millimetre, on rejoue
        # le souhait a une fraction de son amplitude : la moitie du
        # deplacement demande vaut mieux que rien, et l'utilisateur voit dans
        # le rapport ce qu'il a reellement obtenu.
        memoire = [g.copy() for g in sm.geo]
        jmem = {n: p.copy() for n, p in sm.jpos.items()}
        moved, retenu = None, None
        for part in (FRACTIONS if wish.kind == "deplacer" else (1.0,)):
            essai_w = wish if part == 1.0 else replace(
                wish, vector=tuple(part * np.asarray(wish.vector, float)))
            moved = (_apply_move(sm, essai_w, max_step)
                     if wish.kind == "deplacer"
                     else _apply_radius(sm, essai_w, params, passes))
            essai_pts, essai_geo = sm.snapshot(bend_pass=True, repair=True)
            essai = _measure_points(grid, essai_pts, essai_geo)
            if (essai["n_zones"] <= etat["n_zones"]
                    and essai["n_crossings"] <= etat["n_crossings"]):
                retenu, etat = part, essai
                break
            _restore(sm, memoire, jmem)
            memoire = [g.copy() for g in sm.geo]
            jmem = {n: p.copy() for n, p in sm.jpos.items()}

        if retenu is None:
            moved = dict(moved=0,
                         detail="refuse : meme au quart de son amplitude, il "
                                "ferait passer le harnais sous la garde")
        elif retenu < 1.0:
            moved["detail"] += f", applique a {retenu:.0%} (la garde a limite)"
        report.append(dict(wish=wish, **moved))

    points, seg_geo = sm.snapshot(bend_pass=True, repair=True)
    after = _measure_points(grid, points, seg_geo)

    for line in report:
        line["ok"] = line["moved"] > 0
    if logger:
        for line in report:
            w = line["wish"]
            state = "applique" if line["ok"] else "sans effet"
            logger.info(f"{w.describe()} : {state} -- {line['detail']}")
        for z in after["interferences"]["zones"][:3]:
            q = ", ".join(f"{v:.0f}" for v in np.asarray(z["position"], float))
            logger.warn(f"zone residuelle sur le cable {z.get('cable', '?')} en "
                        f"({q}) : {z.get('penetration', 0.0):.2f} mm sous la "
                        f"garde, sur {z.get('length', 0.0):.0f} mm")
        logger.info(f"apres souhaits : {after['n_zones']} zone(s) sous la garde, "
                    f"{after['n_crossings']} traversee(s), rayon mini "
                    f"{after['min_radius']:.0f} mm (avant : {before['min_radius']:.0f} mm), "
                    f"longueur {after['length']:.0f} mm")
    return dict(points=points, seg_geo=seg_geo, report=report,
                interferences=after["interferences"], min_radius=after["min_radius"],
                length=after["length"], before=before, after=after)


def _restore(sm, geo, jpos):
    """Remet le lisseur dans l'etat sauvegarde, jonctions comprises."""
    sm.geo = [g.copy() for g in geo]
    sm.jpos = {n: p.copy() for n, p in jpos.items()}
    for n, lst in sm.inc.items():
        for sid, end in lst:
            sm.geo[sid][end] = sm.jpos[n]


def _apply_move(sm, wish, max_step=None):
    """
    Deplacement souhaite, amorti par la garde : ce qui passe est garde.

    Le controle passe par `Smoother._damped_move`, le meme que le lissage :
    il verifie la garde le long des CORDES et pas seulement aux sommets, et
    reduit le pas (100 %, 50 %, 25 %, 10 %) plutot que de refuser tout. Un
    controle sommet par sommet laissait des interferences entre deux points.
    """
    v = np.asarray(wish.vector, float)
    n = float(np.linalg.norm(v))
    if n < 1e-9:
        return dict(moved=0, detail="aucun deplacement demande")
    if max_step:
        v = v * min(1.0, float(max_step) / n)

    moved, refused = 0, 0
    for sid, g in enumerate(sm.geo):
        if len(g) < 3:
            continue
        w = _weights(g, wish.point, wish.radius)
        fx = np.asarray(sm.frozen[sid], bool)
        w[fx] = 0.0                              # traversees imposees
        delta = w[1:-1, None] * v
        asked = np.linalg.norm(delta, axis=1) > 1e-6
        if not np.any(asked):
            continue
        newp = sm._damped_move(g, delta, fx[1:-1])
        done = np.linalg.norm(newp - g[1:-1], axis=1) > 1e-6
        moved += int(np.count_nonzero(done))
        refused += int(np.count_nonzero(asked & ~done))
        out = g.copy()
        out[1:-1] = newp
        sm.geo[sid] = out

    moved += _move_junctions(sm, wish, v)
    detail = f"{moved} point(s) deplace(s)"
    if refused:
        detail += f", {refused} bloque(s) par la garde"
    return dict(moved=moved, detail=detail)


def _move_junctions(sm, wish, v):
    """
    Les branchements suivent le souhait, mais restent partages.

    Un branchement se controle sur ses CORDES, pas seulement sur son sommet :
    il tient trois troncons ou plus, et la passe de reparation ne le touchera
    jamais -- elle ne deplace aucune extremite de troncon, sous peine de
    desolidariser le tronc commun. Un branchement mal pose reste donc mal pose.
    Comme ailleurs, le pas est amorti plutot que refuse : mieux vaut la moitie
    du souhait qu'un branchement fige entre deux voisins qui, eux, ont bouge.
    """
    moved = 0
    for n in list(sm.free_junctions):
        p = sm.jpos[n]
        w = float(_weights(p[None], wish.point, wish.radius)[0])
        if w <= 1e-3:
            continue
        for facteur in sm.DAMPING:
            if sm.place_junction(n, p + facteur * w * v):
                moved += 1
                break
    return moved


def _apply_radius(sm, wish, params, passes=6):
    """
    Ouvre le coude : chaque sommet trop cintre est ramene VERS SA CORDE, tant
    que la garde le permet -- c'est ce mouvement, et non l'inverse, qui detend
    un virage.

    Le rayon vise est celui du souhait, a defaut le rayon admissible du projet.
    Le deplacement passe, la aussi, par l'amortisseur du lisseur : detendre un
    coude rapproche fatalement le toron de la paroi du cote interieur.
    """
    target = float(wish.target or getattr(params, "min_bend_radius", 0.0) or 0.0)
    if target <= 0.0:
        return dict(moved=0, detail="aucun rayon vise")

    moved = 0
    r_before = _min_radius_near(sm, wish)
    for _ in range(int(passes)):
        for sid, g in enumerate(sm.geo):
            if len(g) < 3:
                continue
            r = curvature_radii(g)                 # rayon aux points interieurs
            fx = np.asarray(sm.frozen[sid], bool)
            w = _weights(g[1:-1], wish.point, wish.radius)
            w[fx[1:-1]] = 0.0
            bad = (r < target) & (w > 1e-3) & np.isfinite(r)
            if not np.any(bad):
                continue
            # Ouvrir un coude, c'est le RAPPROCHER de sa corde : le sommet
            # d'un V est ce qui serre le rayon, l'ecarter encore le serrerait
            # davantage. C'est le meme mouvement que le lissage, mais cible.
            mid = 0.5 * (g[:-2] + g[2:])
            out = mid - g[1:-1]
            norm = np.linalg.norm(out, axis=1, keepdims=True)
            out = np.divide(out, np.where(norm < 1e-9, 1.0, norm))
            # fleche a rattraper pour atteindre le rayon vise
            chord = 0.5 * (np.linalg.norm(g[1:-1] - g[:-2], axis=1)
                           + np.linalg.norm(g[2:] - g[1:-1], axis=1))
            gain = np.clip(chord ** 2 * (1.0 / np.maximum(r, 1e-6) - 1.0 / target)
                           * 0.5, 0.0, 0.35 * chord)
            delta = np.zeros_like(g[1:-1])
            delta[bad] = (w[bad] * gain[bad])[:, None] * out[bad]
            newp = sm._damped_move(g, delta, fx[1:-1])
            moved += int(np.count_nonzero(
                np.linalg.norm(newp - g[1:-1], axis=1) > 1e-6))
            new_g = g.copy()
            new_g[1:-1] = newp
            sm.geo[sid] = new_g
    r_after = _min_radius_near(sm, wish)
    detail = (f"rayon local {r_before:.0f} -> {r_after:.0f} mm "
              f"(vise {target:.0f} mm), {moved} correction(s)")
    return dict(moved=moved if r_after > r_before + 0.5 else 0, detail=detail,
                r_before=r_before, r_after=r_after, target=target)


def _min_radius_near(sm, wish):
    """Rayon de cintrage le plus serre dans la zone du souhait."""
    best = math.inf
    for g in sm.geo:
        if len(g) < 3:
            continue
        r = curvature_radii(g)
        w = _weights(g[1:-1], wish.point, wish.radius)
        sel = (w > 1e-3) & np.isfinite(r)
        if np.any(sel):
            best = min(best, float(r[sel].min()))
    return best


def _measure(grid, sm):
    points, seg_geo = sm.snapshot(bend_pass=False, repair=False)
    return _measure_points(grid, points, seg_geo)


def _measure_points(grid, points, seg_geo):
    interf = interference_breakdown(grid, points)
    radii = []
    for g in seg_geo:
        r = curvature_radii(g)
        r = r[np.isfinite(r)]
        if len(r):
            radii.append(float(r.min()))
    return dict(interferences=interf, n_zones=interf["n_zones"],
                n_crossings=interf["n_crossings"],
                min_radius=min(radii) if radii else float("inf"),
                length=float(sum(polyline_length(p) for p in points)))


# ==========================================================================
# souhaits de topologie : ils passent par un nouveau cheminement
# ==========================================================================

def routing_hints(wishes):
    """
    Zones a transmettre au prochain cheminement.

    `no_share`  : le regroupement y est desactive -- chaque cable paie son
                  propre passage, donc les cables se separent.
    `attract`   : le regroupement y est au contraire favorise.
    Retourne dict(no_share=(points, rayon), attract=(points, rayon)).
    """
    def collect(kind):
        pts = [w.point for w in wishes if w.kind == kind]
        rad = max([w.radius for w in wishes if w.kind == kind], default=0.0)
        return (np.asarray(pts, float).reshape(-1, 3), float(rad))

    return dict(no_share=collect("separer"), attract=collect("regrouper"))


def needs_reroute(wishes) -> bool:
    return any(not w.is_geometric for w in wishes)
