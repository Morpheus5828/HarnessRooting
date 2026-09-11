"""
Construction de la geometrie livrable : le harnais est transforme en solide
(tubes par troncon, diametre fonction du nombre de cables) puis exporte en STL
pour etre reinjecte dans l'arbre CATIA.

PyVista n'est importe qu'ici, et seulement a l'appel : l'application reste
utilisable meme si VTK n'est pas installe sur le poste.
"""

from __future__ import annotations

import csv
import os

import numpy as np

from .analysis import bundle_table
from ..log import Logger
from ..pvutil import surface

LOG = Logger("geometry")

MIN_TUBE_RADIUS = 1.5      # mm : en dessous, le tube est invisible dans CATIA


def _spline(points, n_sample=None):
    import pyvista as pv
    pts = np.asarray(points, float)
    n = int(n_sample or max(len(pts), 8))
    return pv.Spline(pts, n)


CONNECTOR_SIZE = (45.0, 26.0, 20.0)      # longueur, largeur, hauteur (mm)


def connector_frames(polylines, min_span=3):
    """
    Repere de pose d'un connecteur a chaque extremite de cable.

    Retourne [(centre, direction sortante)] : la direction est la tangente du
    toron en bout de course, pour que le corps du connecteur soit aligne sur la
    sortie du cable et non plante de travers.
    """
    frames = []
    for pts in polylines:
        q = np.asarray(pts, float)
        if len(q) < 2:
            continue
        j = min(min_span, len(q) - 1)
        frames.append((q[0], q[0] - q[j]))
        frames.append((q[-1], q[-1] - q[max(-1 - min_span, -len(q))]))
    return frames


def harness_solid(res, cable_diameter=6.0, n_sides=16, include_clips=None,
                  clip_radius=6.0, polylines=None, include_connectors=True,
                  connector_size=CONNECTOR_SIZE, connector_parts=None,
                  crab_parts=None):
    """
    Assemble le harnais complet en un seul maillage triangule.

    Chaque troncon devient un tube dont le diametre depend du nombre de cables
    qu'il porte (cf. analysis.bundle_radius) : le tronc commun est visiblement
    plus gros que les antennes, comme sur un harnais reel.
    """
    import pyvista as pv

    rows = bundle_table(res, cable_diameter)
    parts, total_len = [], 0.0
    for row in rows:
        pts = row["points"]
        if len(pts) < 2:
            continue
        radius = max(MIN_TUBE_RADIUS, 0.5 * row["diameter"])
        try:
            tube = _spline(pts).tube(radius=radius, n_sides=n_sides,
                                     capping=True)
        except Exception as exc:                       # troncon degenere
            LOG.warn(f"troncon {row['sid']} ignore ({type(exc).__name__} : {exc})")
            continue
        parts.append(tube)
        total_len += row["length"]

    if include_connectors and connector_parts:
        # Vrais connecteurs choisis dans la bibliotheque : le livrable porte
        # leur maillage, et non plus un pave de substitution.
        parts.extend(connector_parts)
    elif include_connectors and polylines:
        for center, direction in connector_frames(polylines):
            parts.append(connector_box(center, direction, connector_size))

    if crab_parts:
        # Les crabes font partie du livrable : ce sont des pieces a poser, pas
        # une aide a la lecture.
        parts.extend(crab_parts)

    if include_clips is not None and len(include_clips):
        for c in np.asarray(include_clips, float).reshape(-1, 3):
            parts.append(pv.Sphere(radius=clip_radius, center=c,
                                   theta_resolution=12, phi_resolution=12))

    if not parts:
        raise RuntimeError("Aucun troncon exploitable : rien a exporter.")

    mesh = parts[0] if len(parts) == 1 else pv.merge(parts, merge_points=False)
    mesh = surface(mesh)
    LOG.info(f"solide harnais : {mesh.n_points} points, {mesh.n_cells} triangles, "
             f"{len(parts)} corps, longueur cumulee {total_len:.0f} mm")
    return mesh


def save_stl(mesh, path):
    path = str(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    mesh.save(path)
    size = os.path.getsize(path) / 1024.0
    LOG.ok(f"STL ecrit : {path} ({size:.0f} Ko)")
    return path


def export_centerlines_csv(res, path, cable_diameter=6.0):
    """
    Fibre neutre de chaque troncon, en CSV : c'est ce dont un concepteur a
    besoin pour recreer des courbes exactes dans CATIA (Electrical Harness)
    plutot que de travailler sur un maillage.
    """
    path = str(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    rows = bundle_table(res, cable_diameter)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["troncon", "nb_cables", "diametre_mm", "index", "x", "y", "z"])
        for row in rows:
            for i, p in enumerate(row["points"]):
                w.writerow([row["sid"], row["n_cables"], f"{row['diameter']:.2f}",
                            i, f"{p[0]:.3f}", f"{p[1]:.3f}", f"{p[2]:.3f}"])
    LOG.ok(f"fibres neutres exportees : {path} ({len(rows)} troncon(s))")
    return path


def export_catia_curves_csv(res, path, cable_diameter=6.0, step=40.0):
    """
    Fibres neutres DECIMEES, au format attendu par la macro CATIA.

    Creer un point CATIA coute un appel COM : passer les 500 points d'un
    troncon reechantillonne a 12 mm prendrait plusieurs minutes pour une
    spline qui n'y gagnerait rien. On ne garde donc qu'un point tous les
    `step` mm, extremites comprises.
    """
    path = str(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    rows = bundle_table(res, cable_diameter)
    n_pts = 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["troncon", "nb_cables", "diametre_mm", "x", "y", "z"])
        for row in rows:
            pts = _decimate(row["points"], step)
            n_pts += len(pts)
            for q in pts:
                w.writerow([row["sid"], row["n_cables"], f"{row['diameter']:.3f}",
                            f"{q[0]:.4f}", f"{q[1]:.4f}", f"{q[2]:.4f}"])
    LOG.ok(f"courbes CATIA exportees : {path} ({len(rows)} troncon(s), "
           f"{n_pts} point(s))")
    return path


def _decimate(pts, step):
    """Sous-echantillonne une polyligne a pas d'arc constant, extremites gardees."""
    pts = np.asarray(pts, float)
    if len(pts) < 3:
        return pts
    seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    s = np.concatenate([[0.0], np.cumsum(seg)])
    if s[-1] < step:
        return np.vstack([pts[0], pts[-1]])
    keep, last = [0], 0.0
    for i in range(1, len(pts) - 1):
        if s[i] - last >= step:
            keep.append(i)
            last = s[i]
    keep.append(len(pts) - 1)
    return pts[keep]


def export_clips_csv(on_fixation, to_create, path):
    """Nomenclature des clips : ceux qui tombent sur une fixation existante et
    ceux qu'il faudra creer pour tenir l'espacement maximal."""
    path = str(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["type", "index", "x", "y", "z"])
        for i, p in enumerate(np.asarray(on_fixation, float).reshape(-1, 3)):
            w.writerow(["sur_fixation", i, f"{p[0]:.3f}", f"{p[1]:.3f}", f"{p[2]:.3f}"])
        for i, p in enumerate(np.asarray(to_create, float).reshape(-1, 3)):
            w.writerow(["a_creer", i, f"{p[0]:.3f}", f"{p[1]:.3f}", f"{p[2]:.3f}"])
    LOG.ok(f"clips exportes : {path}")
    return path


def export_crabs_csv(crabs, path):
    """Nomenclature des crabes : ou les poser, et a quel defaut de pose."""
    from .crabs import crabs_to_rows

    path = str(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    rows = crabs_to_rows(crabs)
    colonnes = ["index", "arc_mm", "x", "y", "z", "appui_x", "appui_y",
                "appui_z", "hauteur", "defaut_parallelisme_deg"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(colonnes)
        for row in rows:
            w.writerow([row[c] for c in colonnes])
    LOG.ok(f"crabes exportes : {path} ({len(rows)} pose(s))")
    return path


def connector_box(center, direction, size=CONNECTOR_SIZE):
    """Pave droit representant le connecteur, aligne sur la sortie du cable."""
    import pyvista as pv
    d = np.asarray(direction, float)
    n = np.linalg.norm(d)
    d = d / n if n > 1e-9 else np.array([1.0, 0.0, 0.0])
    tmp = np.array([0.0, 0.0, 1.0]) if abs(d[2]) < 0.9 else np.array([0.0, 1.0, 0.0])
    e2 = np.cross(d, tmp)
    e2 /= np.linalg.norm(e2)
    e3 = np.cross(d, e2)
    M = np.eye(4)
    M[:3, 0], M[:3, 1], M[:3, 2] = d, e2, e3
    M[:3, 3] = np.asarray(center, float) - d * size[0] * 0.5   # corps en retrait
    cube = pv.Cube(x_length=size[0], y_length=size[1], z_length=size[2])
    cube.transform(M, inplace=True)
    return cube


def dedupe_points(pts, tol=25.0):
    out = []
    for p in np.asarray(pts, float).reshape(-1, 3):
        if not out or np.min(np.linalg.norm(np.array(out) - p, axis=1)) > tol:
            out.append(p)
    return np.array(out) if out else np.empty((0, 3))
