"""
Lissage interactif du harnais.

Le cheminement produit d'abord un trace BRUT : la polyligne des noeuds de
grille, qui respecte par construction la distance de securite et ne traverse
aucun triangle. Ce trace est en escalier, donc inutilisable tel quel, mais il
est sur.

Le lissage est ensuite laisse a la main de l'utilisateur, sous forme d'un
pourcentage de 0 a 100 %. Comme relacher la polyligne la rapproche de la
structure, chaque palier est evalue : l'utilisateur voit la courbe du nombre
d'interferences en fonction du pourcentage et choisit lui-meme son compromis.

Deux points de mise en oeuvre :

  * `Smoother` est INCREMENTAL : passer de 30 % a 40 % ne relance pas le calcul
    depuis zero, il poursuit les iterations. Construire toute l'echelle coute
    donc le prix d'un seul lissage complet.
  * le palier 0 % ne touche a rien du tout : ni raccourci, ni
    reechantillonnage, ni relaxation.
"""

from __future__ import annotations

import time

import numpy as np

from .analysis import interference_breakdown
from .hrh import (_bend_pass, _ekey, _segment_geometry, curvature_radii, dedupe,
                  path_edges, polyline_length)
from ..log import Logger

LOG = Logger("lissage")

# paliers proposes par defaut : fin pres de 0 %, ou tout se joue
DEFAULT_LEVELS = (0, 5, 10, 15, 20, 30, 40, 50, 60, 70, 85, 100)


def _sample_with_owner(pts, step):
    """Echantillonne une polyligne en gardant, pour chaque echantillon,
    l'indice du sommet dont il provient."""
    out, owner = [], []
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        n = max(2, int(np.linalg.norm(b - a) / step) + 1)
        seg = a + np.linspace(0, 1, n)[:, None] * (b - a)
        out.append(seg)
        owner.extend([i] * n)
    if not out:
        return np.atleast_2d(pts), np.zeros(len(np.atleast_2d(pts)), int)
    return np.vstack(out), np.asarray(owner, int)


class Smoother:
    """
    Machine de lissage incrementale (smooth router de l'article, §4.4).

    Les points de branchement sont des variables PARTAGEES entre troncons : le
    tronc commun reste rigoureusement commun et la tangente reste continue de
    part et d'autre d'un branchement.
    """

    def __init__(self, grid, routes, topo, p, anchor_points=None):
        self.grid = grid
        self.routes = routes
        self.topo = topo
        self.p = p
        self.done = 0

        segs = topo["segments"]
        self.geo, self.frozen = [], []
        for seg in segs:
            g, fz = _segment_geometry(grid, grid.world_many(seg), anchor_points, p)
            self.geo.append(g)
            self.frozen.append(fz)

        self.inc = {}
        for sid, seg in enumerate(segs):
            for end, node in ((0, seg[0]), (-1, seg[-1])):
                self.inc.setdefault(node, []).append((sid, end))

        # Nombre de cables portes par chaque troncon.
        self.load = []
        for seg in segs:
            aretes = path_edges(seg)
            self.load.append(len(topo["owners"][aretes[0]]) if aretes else 1)

        # MODIFICATION : On applique la tension à TOUS les câbles, même s'ils sont seuls
        self.trunk = [i for i, n in enumerate(self.load)]

        self.jpos = {n: grid.world(n) for n in self.inc}
        self.free_junctions = [n for n in self.inc if n not in topo["terminals"]]
        for n, lst in self.inc.items():
            for sid, end in lst:
                self.geo[sid][end] = self.jpos[n]

        # MODIFICATION : On libère le lissage de la contrainte de coût strict
        self.guard_cost = False

    # ------------------------------------------------------------------
    def iterate(self, n_iter):
        """Poursuit le lissage de `n_iter` passes supplementaires."""
        a = self.p.smooth_alpha
        for _ in range(int(n_iter)):
            for sid, pts in enumerate(self.geo):
                if len(pts) < 3:
                    continue
                prop = pts.copy()
                prop[1:-1] = self._damped_move(
                    pts, a * (0.5 * (pts[:-2] + pts[2:]) - pts[1:-1]),
                    self.frozen[sid][1:-1])
                self._keep_tangent(prop, self.frozen[sid])
                self.geo[sid] = prop
            self._tension_pass()
            self._move_junctions(a)
            self.done += 1
        return self

    # pas d'essai successifs quand le pas complet viole la garde
    DAMPING = (1.0, 0.5, 0.25, 0.1)

    # fractions controlees le long des deux segments adjacents au point deplace
    CHORD_SAMPLES = (0.25, 0.5, 0.75)

    def _damped_move(self, pts, delta, frozen):
        """
        Deplace les points interieurs en respectant la garde SUR LES SEGMENTS,
        pas seulement aux sommets.
        """
        grid = self.grid
        base = pts[1:-1]
        clearance = grid.clearance_at(base)      # relachee dans les fixations
        left, right = pts[:-2], pts[2:]
        out = base.copy()
        pending = ~np.asarray(frozen, bool)
        ref_cost = grid.cost_at(base) if self.guard_cost else None

        for factor in self.DAMPING:
            idx = np.where(pending)[0]
            if not len(idx):
                break
            trial = base[idx] + factor * delta[idx]
            limit = clearance[idx]
            ok = grid.distance(trial) >= limit
            for t in self.CHORD_SAMPLES:
                if not ok.any():
                    break
                ok &= grid.distance(left[idx] + t * (trial - left[idx])) >= limit
                ok &= grid.distance(trial + t * (right[idx] - trial)) >= limit
            if self.guard_cost:
                ok &= grid.cost_at(trial) <= ref_cost[idx] + grid.p.cost_tol
            good = idx[ok]
            out[good] = trial[ok]
            pending[good] = False
        return out                      # les points restants n'ont pas bouge

    def _tension_pass(self):
        """
        Tend le tronc commun : une passe de lissage SUPPLEMENTAIRE sur les
        troncons partages, d'autant plus forte qu'ils portent de cables.
        """
        force = float(getattr(self.p, "trunk_tension", 0.0))
        if force <= 0.0:
            return
        a = self.p.smooth_alpha
        for sid in self.trunk:
            g = self.geo[sid]
            if len(g) < 3:
                continue

            # MODIFICATION : Le câble unique subit maintenant au minimum la
            # moitié de la tension maximale d'un gros faisceau pour le garder droit.
            part = force * min(1.0, max(0.5, self.load[sid] / 3.0))

            prop = g.copy()
            prop[1:-1] = self._damped_move(
                g, a * part * (0.5 * (g[:-2] + g[2:]) - g[1:-1]),
                self.frozen[sid][1:-1])
            self._keep_tangent(prop, self.frozen[sid])
            self.geo[sid] = prop

    def _keep_tangent(self, prop, fx):
        """Tangente continue de part et d'autre d'une traversee epinglee."""
        grid = self.grid
        for i in np.where(fx)[0]:
            if i == 0 or i == len(prop) - 1:
                continue
            t = prop[i + 1] - prop[i - 1]
            nt = np.linalg.norm(t)
            if nt < 1e-9:
                continue
            t = t / nt
            for j, sgn in ((i - 1, -1.0), (i + 1, 1.0)):
                if fx[j]:
                    continue
                cand = prop[i] + sgn * t * np.linalg.norm(prop[j] - prop[i])
                if float(grid.distance(cand[None])[0]) >= grid.clearance_one(cand):
                    prop[j] = cand

    def junction_ok(self, node, cand) -> bool:
        """
        Un branchement peut-il aller la ?
        """
        grid = self.grid
        cand = np.asarray(cand, float)
        if float(grid.distance(cand[None])[0]) < grid.clearance_one(cand):
            return False
        limite = grid.clearance_one(cand)
        for sid, end in self.inc[node]:
            g = self.geo[sid]
            if len(g) < 2:
                continue
            voisin = g[1] if end == 0 else g[-2]
            echant = np.array([cand + t * (voisin - cand)
                               for t in self.CHORD_SAMPLES], float)
            seuil = np.maximum(grid.clearance_at(echant), limite)
            if np.any(grid.distance(echant) < seuil):
                return False
        return True

    def place_junction(self, node, cand) -> bool:
        """Pose le branchement s'il tient, et le partage entre ses troncons."""
        if not self.junction_ok(node, cand):
            return False
        self.jpos[node] = np.asarray(cand, float)
        for sid, end in self.inc[node]:
            self.geo[sid][end] = self.jpos[node]
        return True

    def _move_junctions(self, a):
        """
        Un branchement se place a la moyenne des voisins de TOUS ses troncons.
        """
        force = float(getattr(self.p, "trunk_tension", 0.0))
        for n in self.free_junctions:
            nbrs, charges = [], []
            for sid, end in self.inc[n]:
                if len(self.geo[sid]) < 2:
                    continue
                nbrs.append(self.geo[sid][1] if end == 0 else self.geo[sid][-2])
                charges.append(float(self.load[sid]))
            if not nbrs:
                continue
            cible = (self._fermat(self.jpos[n], nbrs, charges) if force > 0.0
                     else np.mean(nbrs, axis=0))
            self.place_junction(n, self.jpos[n] + a * (cible - self.jpos[n]))

    @staticmethod
    def _fermat(depart, voisins, charges, tours=8):
        """
        Point qui minimise la LONGUEUR de faisceau autour d'un branchement.
        """
        q = np.asarray(voisins, float)
        w = np.asarray(charges, float)
        p = np.asarray(depart, float).copy()
        for _ in range(int(tours)):
            d = np.linalg.norm(q - p, axis=1)
            if np.any(d < 1e-9):
                return q[int(np.argmin(d))]
            lam = w / d
            p = (lam[:, None] * q).sum(axis=0) / lam.sum()
        return p

    # ------------------------------------------------------------------
    def _chord_deficit(self, g, v, pos) -> float:
        """
        De combien les deux cordes du sommet `v` passent-elles sous la garde,
        si ce sommet est en `pos` ? (0 = elles sont saines)
        """
        grid, pire = self.grid, 0.0
        for u in (v - 1, v + 1):
            if u < 0 or u >= len(g):
                continue
            echant = np.array([pos + t * (g[u] - pos)
                               for t in self.CHORD_SAMPLES], float)
            manque = grid.clearance_at(echant) - grid.distance(echant)
            pire = max(pire, float(manque.max()))
        return pire

    def _push_ok(self, g, v, cand, avant) -> bool:
        """
        Le sommet `v` peut-il aller en `cand` ?
        """
        grid = self.grid
        if float(grid.distance(cand[None])[0]) < grid.clearance_one(cand):
            return False
        return self._chord_deficit(g, v, cand) <= avant + 1e-9

    def repair(self, geo, margin=0.05, max_pass=12, step=2.0):
        """
        Ramene la polyligne au-dessus de la garde, partout, y compris ENTRE les
        sommets.
        """
        grid = self.grid
        collider = grid.collider
        for _ in range(max_pass):
            moved = 0
            for sid, g in enumerate(geo):
                if len(g) < 3:
                    continue
                sample, owner = _sample_with_owner(g, step)
                d = grid.distance(sample)
                clearance = grid.clearance_at(sample)
                bad = np.where(d < clearance + margin)[0]
                if not len(bad):
                    continue
                push = (clearance[bad] + margin) - d[bad]
                direction = collider.gradient(sample[bad])
                fixed = np.asarray(self.frozen[sid], bool)
                new_g = g.copy()
                for k, s_i in enumerate(bad):
                    for v in (owner[s_i], min(owner[s_i] + 1, len(g) - 1)):
                        if v in (0, len(g) - 1) or fixed[v]:
                            continue      # extremites et traversees epinglees
                        avant = self._chord_deficit(new_g, v, new_g[v])
                        for facteur in self.DAMPING:
                            cand = new_g[v] + facteur * push[k] * direction[k]
                            if self._push_ok(new_g, v, cand, avant):
                                new_g[v] = cand
                                moved += 1
                                break
                geo[sid] = new_g
            if moved == 0:
                break
        else:
            LOG.warn(f"reparation non convergee apres {max_pass} passes "
                      f"({moved} deplacement(s) a la derniere)")
        return geo

    def snapshot(self, bend_pass=True, repair=True):
        """
        Etat courant, sans consommer la machine : renvoie (polylignes par
        cable, geometrie par troncon).
        """
        geo = [g.copy() for g in self.geo]
        if bend_pass:
            geo = [_bend_pass(self.grid, g, self.p, frozen=fz)
                   for g, fz in zip(geo, self.frozen)]
            for n, lst in self.inc.items():
                for sid, end in lst:
                    geo[sid][end] = self.jpos[n]
        if repair:
            geo = self.repair(geo)
            for n, lst in self.inc.items():   # les jonctions restent partagees
                for sid, end in lst:
                    geo[sid][end] = self.jpos[n]
        return self._assemble(geo), geo

    def _assemble(self, geo):
        """Recompose la polyligne de chaque cable a partir des troncons."""
        segs = self.topo["segments"]
        out = []
        for path in self.routes:
            pts, i, ok = [], 0, True
            while i < len(path) - 1:
                sid = self.topo["edge2seg"].get(_ekey(path[i], path[i + 1]))
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
            out.append(dedupe(np.array(pts)) if ok and pts
                       else self.grid.world_many(path))
        return out


# --------------------------------------------------------------------------
# echelle de lissage
# --------------------------------------------------------------------------

def raw_geometry(grid, routes, topo):
    """Palier 0 % : la polyligne des noeuds de grille, intacte."""
    seg_geo = [grid.world_many(seg) for seg in topo["segments"]]
    return [grid.world_many(r) for r in routes], seg_geo


def segment_load(topo):
    """Nombre de cables portes par chaque troncon de la topologie."""
    out = []
    for seg in topo["segments"]:
        aretes = path_edges(seg)
        out.append(len(topo["owners"][aretes[0]]) if aretes else 1)
    return out


def trunk_tension(seg_geo, load):
    """
    Tension du tronc commun : longueur reelle / distance a vol d'oiseau.
    """
    reelle = droite = 0.0
    for g, n in zip(seg_geo, load):
        g = np.asarray(g, float)
        if n < 2 or len(g) < 2:
            continue
        reelle += float(polyline_length(g))
        droite += float(np.linalg.norm(g[-1] - g[0]))
    if droite < 1e-9:
        return 1.0, reelle, droite
    return reelle / droite, reelle, droite


def _level_record(grid, level, points, seg_geo, topo, load=None):
    interf = interference_breakdown(grid, points)
    radii = []
    for g in seg_geo:
        r = curvature_radii(g)
        r = r[np.isfinite(r)]
        if len(r):
            radii.append(float(r.min()))
    return dict(
        level=int(level),
        points=points,
        seg_geo=seg_geo,
        interferences=interf,
        n_zones=interf["n_zones"],
        n_crossings=interf["n_crossings"],
        max_penetration=interf["max_penetration"],
        min_clearance=interf["min_clearance"],
        total_length=sum(polyline_length(p) for p in points),
        min_bend_radius=min(radii) if radii else float("inf"),
        trunk_tension=(trunk_tension(seg_geo, load)[0] if load else 1.0),
        trunk_length=(trunk_tension(seg_geo, load)[1] if load else 0.0),
    )


def build_ladder(grid, routes, topo, p, anchor_points=None, levels=None,
                 max_iters=None, on_level=None, should_stop=None, post=None,
                 extras=None):
    """
    Construit l'echelle de lissage, palier par palier.
    """
    levels = sorted(set(int(v) for v in (levels or DEFAULT_LEVELS)))
    max_iters = int(max_iters if max_iters is not None else p.smooth_iters)
    t0 = time.time()
    records = []
    smoother = None
    charge = segment_load(topo)

    for i, lv in enumerate(levels):
        if should_stop is not None and should_stop():
            LOG.warn("construction de l'echelle de lissage interrompue")
            break
        if lv <= 0:
            pts, geo = raw_geometry(grid, routes, topo)
            free = geo
            if post is not None:
                pts, geo = post(pts, geo)
            rec = _level_record(grid, 0, pts, geo, topo, charge)
            rec["seg_geo_free"] = free
            if extras is not None:
                rec.update(extras(pts, geo))
        else:
            if smoother is None:
                smoother = Smoother(grid, routes, topo, p,
                                    anchor_points=anchor_points)
            target = int(round(max_iters * lv / 100.0))
            smoother.iterate(max(0, target - smoother.done))
            pts, geo = smoother.snapshot()
            free = geo
            if post is not None:
                pts, geo = post(pts, geo)
            rec = _level_record(grid, lv, pts, geo, topo, charge)
            rec["seg_geo_free"] = free
            if extras is not None:
                rec.update(extras(pts, geo))
        records.append(rec)
        crabes = rec.get("crabs")
        LOG.info(f"lissage {lv:3d} % : {rec['n_zones']} zone(s), "
                 f"{rec['n_crossings']} traversee(s), "
                 f"L {rec['total_length']:.0f} mm, "
                 f"R min {rec['min_bend_radius']:.0f} mm, "
                 f"tronc {rec['trunk_tension']:.3f}"
                 + (f", {len(crabes)} crabe(s)" if crabes is not None else ""))
        if on_level is not None:
            on_level(rec, i, len(levels))

    LOG.ok(f"echelle de lissage : {len(records)} palier(s) en "
           f"{time.time() - t0:.1f} s")
    return records


def best_level(records, min_bend_radius=None):
    """
    Palier recommande : le plus lisse qui reste a zero interference.
    """
    if not records:
        return None
    clean = [r for r in records if r["n_zones"] == 0 and r["n_crossings"] == 0]
    if min_bend_radius is not None:
        strict = [r for r in clean if r["min_bend_radius"] >= min_bend_radius]
        if strict:
            clean = strict
    if clean:
        return max(clean, key=lambda r: r["level"])
    return min(records, key=lambda r: (r["n_crossings"], r["n_zones"],
                                       r["max_penetration"]))
