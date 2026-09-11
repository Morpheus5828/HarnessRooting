"""
Post-traitement des resultats de cheminement : mise en forme des metriques
attendues par l'interface (interferences, cintrage, bilan de clips) et calcul
du diametre des torons.

Aucune dependance graphique ici : les fonctions renvoient des dictionnaires,
c'est l'interface qui decide comment les tracer.
"""

from __future__ import annotations

import math

import numpy as np

from .hrh import (bundle_polylines, clips_on_passages, curvature_radii,
                  polyline_length, _sample)

# Seuils de severite, exprimes en fraction de la clearance demandee.
SEV_CRITIQUE = 0.50    # penetration > 50 % de la clearance
SEV_MAJEURE = 0.20     # penetration > 20 %

# Au-dela de MARGE_LIMITE * clearance, l'echantillon est juge confortable.
MARGE_LIMITE = 1.25


def interference_breakdown(grid, polylines, step=2.0, tol=1e-3, merge_radius=8.0):
    """
    Analyse EXACTE des interferences, sur le maillage.

    Trois corrections par rapport a la version nuage de points :

    1. la distance est celle a la surface triangulee, pas au point echantillonne
       le plus proche : plus de sous-estimation entre deux points du nuage ;
    2. toute TRAVERSEE du maillage est detectee par intersection segment /
       triangle et rapportee comme faute critique -- c'est le cas qu'un nuage
       de points ne peut structurellement pas voir ;
    3. les zones sont dedoublonnees dans l'espace : un tronc commun a cinq
       cables produisait cinq "interferences" pour un seul point physique.

    `tol` ne sert qu'a absorber le bruit de calcul flottant (1 um par defaut) :
    ce n'est pas une tolerance de conception.
    """
    clearance = float(grid.p.clearance)
    collider = getattr(grid, "collider", None)

    n_ok = n_limite = n_bad = 0
    raw_zones = []
    dmin = math.inf
    n_crossings = 0

    for idx, pts in enumerate(polylines):
        pts = np.asarray(pts, float)
        if len(pts) < 2:
            continue
        chunks = [_sample(a, b, step) for a, b in zip(pts[:-1], pts[1:])]
        sample = np.vstack(chunks)
        d = grid.distance(sample)
        # la garde exigee est reduite a l'interieur des fixations que le harnais
        # doit traverser : les y signaler serait une fausse alerte.
        seuil = grid.clearance_at(sample) if hasattr(grid, "clearance_at") \
            else np.full(len(sample), clearance)
        limite = MARGE_LIMITE * seuil
        dmin = min(dmin, float(d.min()))

        # --- traversees franches du maillage ---
        if collider is not None and collider.exact:
            for i in collider.crossing_indices(pts):
                mid = 0.5 * (pts[i] + pts[i + 1])
                n_crossings += 1
                raw_zones.append(dict(cable=idx, penetration=clearance,
                                      severity="traversee", length=0.0,
                                      position=mid.tolist(), clearance=0.0,
                                      crossing=True))

        bad = d < seuil - tol
        n_bad += int(bad.sum())
        n_limite += int(np.count_nonzero((d >= seuil - tol) & (d < limite)))
        n_ok += int(np.count_nonzero(d >= limite))
        if not bad.any():
            continue

        flags = bad.astype(np.int8)
        edges = np.diff(flags)
        starts = list(np.where(edges == 1)[0] + 1)
        ends = list(np.where(edges == -1)[0] + 1)
        if flags[0]:
            starts.insert(0, 0)
        if flags[-1]:
            ends.append(len(flags))
        for a, b in zip(starts, ends):
            pen = float((seuil[a:b] - d[a:b]).max())
            seg_len = float(np.sum(np.linalg.norm(np.diff(sample[a:b], axis=0), axis=1))) \
                if b - a > 1 else 0.0
            ratio = pen / max(clearance, 1e-9)
            sev = ("critique" if ratio > SEV_CRITIQUE
                   else "majeure" if ratio > SEV_MAJEURE else "mineure")
            raw_zones.append(dict(cable=idx, penetration=pen, severity=sev,
                                  length=seg_len,
                                  position=sample[(a + b) // 2].tolist(),
                                  clearance=float(d[a:b].min()), crossing=False))

    zones = _merge_zones(raw_zones, merge_radius)
    sev_counts = {k: 0 for k in ("traversee", "critique", "majeure", "mineure")}
    for z in zones:
        sev_counts[z["severity"]] = sev_counts.get(z["severity"], 0) + 1

    return dict(
        clearance=clearance,
        exact=bool(collider is not None and collider.exact),
        n_samples=n_ok + n_limite + n_bad,
        counts=dict(conforme=n_ok, limite=n_limite, interference=n_bad),
        zones=zones,
        n_zones=len(zones),
        n_crossings=sum(1 for z in zones if z.get("crossing")),
        n_raw_zones=len(raw_zones),
        severity_counts=sev_counts,
        min_clearance=0.0 if math.isinf(dmin) else dmin,
        max_penetration=max((z["penetration"] for z in zones), default=0.0),
        length_in_interference=sum(z["length"] for z in zones),
    )


def _merge_zones(raw, radius):
    """
    Regroupe les zones distantes de moins de `radius` : sur un tronc commun,
    le meme defaut physique est vu une fois par cable qui l'emprunte.
    """
    if not raw:
        return []
    order = sorted(range(len(raw)), key=lambda i: -raw[i]["penetration"])
    kept = []
    for i in order:
        z = raw[i]
        pos = np.asarray(z["position"], float)
        for k in kept:
            if np.linalg.norm(pos - np.asarray(k["position"], float)) <= radius:
                k["cables"].add(z["cable"])
                k["length"] = max(k["length"], z["length"])
                break
        else:
            z = dict(z)
            z["cables"] = {z["cable"]}
            kept.append(z)
    for k in kept:
        k["n_cables"] = len(k["cables"])
        k["cables"] = sorted(k["cables"])
    return sorted(kept, key=lambda z: -z["penetration"])


def bundle_radius(n_cables, cable_diameter=6.0, fill=0.82):
    """
    Rayon d'un toron de `n_cables` cables ronds.

    Approximation classique : la section utile croit comme le nombre de cables,
    d'ou un rayon en sqrt(n), corrige d'un facteur de foisonnement (les cables
    ne pavent pas parfaitement le disque).
    """
    n = max(1, int(n_cables))
    return 0.5 * float(cable_diameter) * math.sqrt(n) / max(fill, 1e-3)


def bundle_table(res, cable_diameter=6.0):
    """Un enregistrement par troncon de toron : longueur, cables, diametre, R min."""
    rows = []
    bundles = bundle_polylines(res["grid"], res["topo"], res["seg_geo"])
    for sid, (g, n) in enumerate(bundles):
        r = curvature_radii(g)
        r = r[np.isfinite(r)]
        rows.append(dict(
            sid=sid,
            n_cables=int(n),
            length=polyline_length(g),
            diameter=2.0 * bundle_radius(n, cable_diameter),
            rmin=float(r.min()) if len(r) else float("inf"),
            points=np.asarray(g, float),
        ))
    return rows


def clip_plan(res, attractors, params, cable_diameter=6.0):
    """Positions de clips pour l'ensemble du faisceau (voir hrh.clips_on_passages)."""
    on_fix, to_create = [], []
    for row in bundle_table(res, cable_diameter):
        g = row["points"]
        if len(g) < 2:
            continue
        anc, extra = clips_on_passages(
            g, attractors, max_spacing=params.clip_spacing,
            tol=params.clip_tol_cells * params.resolution)
        on_fix.extend(np.asarray(anc, float).reshape(-1, 3))
        to_create.extend(np.asarray(extra, float).reshape(-1, 3))
    return (np.array(on_fix) if on_fix else np.empty((0, 3)),
            np.array(to_create) if to_create else np.empty((0, 3)))


def summary(res, interf, params, cable_diameter=6.0):
    """Indicateurs affiches sur les tuiles de la page 2 (et resumes en CLI)."""
    m_raw, m_smooth = res["metrics_raw"], res["metrics_smooth"]
    rows = bundle_table(res, cable_diameter)
    ko = sum(1 for r in rows if r["rmin"] < params.min_bend_radius)
    common = m_smooth["common_length"] or 0.0
    total = m_smooth["total_length"] or 0.0
    return dict(
        total_length=total,
        common_length=common,
        common_ratio=(common / total) if total > 1e-9 else 0.0,
        length_gain=(m_raw["total_length"] - total),
        n_branch=len(res["topo"]["branch_points"]),
        n_segments=len(res["topo"]["segments"]),
        n_bundles=sum(1 for r in rows if r["n_cables"] > 1),
        max_diameter=max((r["diameter"] for r in rows), default=0.0),
        min_bend_radius=m_smooth["min_bend_radius"],
        bend_ko=ko,
        bend_total=len(rows),
        min_clearance=interf["min_clearance"],
        n_zones=interf["n_zones"],
        max_penetration=interf["max_penetration"],
        t_route=res["t_route"],
        t_smooth=res["t_smooth"],
    )


def verdict(summ, params, interf=None):
    """Feu vert / orange / rouge du bandeau de resultat."""
    crossings = (interf or {}).get("n_crossings", 0)
    if crossings:
        return ("ko", f"{crossings} traversee(s) du maillage : le harnais "
                      "passe au travers de la structure. Corriger avant tout "
                      "autre reglage.")
    if summ["n_zones"] > 0:
        return ("ko", f"{summ['n_zones']} zone(s) sous la distance de securite "
                      f"(penetration max {summ['max_penetration']:.2f} mm)")
    if summ["bend_ko"] > 0:
        return ("warn", f"{summ['bend_ko']} troncon(s) sous le rayon de courbure "
                        f"minimal de {params.min_bend_radius:.0f} mm")
    return ("ok", "Cheminement conforme : aucune interference, aucune traversee, "
                  "rayon de courbure respecte")
