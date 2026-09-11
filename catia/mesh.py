"""
Chargement et preparation de la maquette numerique.

Le routage ne travaille pas sur des triangles mais sur un nuage de points :
la clearance est mesuree par KD-tree. Un STL grossier a de grands triangles
"vides" au milieu -> on ajoute les centres de faces et on subdivise les
triangles trop grands, sans quoi des collisions passent au travers.
"""

from __future__ import annotations

import glob
import os
import time
from dataclasses import dataclass, field

import numpy as np

from ..log import Logger
from ..pvutil import surface

LOG = Logger("maquette")


@dataclass
class SceneInfo:
    """Tout ce que la page 1 affiche apres chargement."""
    source: str = ""
    n_files: int = 0
    n_loaded: int = 0
    n_failed: int = 0
    failed_names: list = field(default_factory=list)
    n_points: int = 0
    n_cells: int = 0
    n_cloud: int = 0
    bounds: tuple = (0, 0, 0, 0, 0, 0)
    seconds: float = 0.0

    @property
    def size_mm(self):
        b = self.bounds
        return (b[1] - b[0], b[3] - b[2], b[5] - b[4])

    def as_rows(self):
        sx, sy, sz = self.size_mm
        return [
            ("Source", self.source),
            ("Pieces chargees", f"{self.n_loaded} / {self.n_files}"
                                + (f"  ({self.n_failed} illisible(s))" if self.n_failed else "")),
            ("Triangles", f"{self.n_cells:,}".replace(",", " ")),
            ("Points de collision", f"{self.n_cloud:,}".replace(",", " ")),
            ("Encombrement (X x Y x Z)", f"{sx:.0f} x {sy:.0f} x {sz:.0f} mm"),
            ("Origine (Xmin, Ymin, Zmin)",
             f"{self.bounds[0]:.0f}, {self.bounds[2]:.0f}, {self.bounds[4]:.0f} mm"),
            ("Duree de chargement", f"{self.seconds:.1f} s"),
        ]


def list_stl(folder):
    files = sorted(glob.glob(os.path.join(str(folder), "*.stl")))
    files += sorted(glob.glob(os.path.join(str(folder), "*.STL")))
    return sorted(set(files))


def load_folder(folder, on_progress=None, should_stop=None):
    """
    Fusionne tous les STL d'un dossier en un seul maillage.

    `on_progress(done, total, name)` permet a l'interface d'afficher la piece
    en cours : sur un helicoptere complet, la fusion peut durer une minute.
    """
    import pyvista as pv

    files = list_stl(folder)
    if not files:
        raise FileNotFoundError(
            f"Aucun fichier .stl dans :\n{folder}\n\n"
            "Verifier le dossier, ou relancer une extraction depuis CATIA.")

    t0 = time.time()
    meshes, failed = [], []
    for i, path in enumerate(files):
        if should_stop is not None and should_stop():
            LOG.warn("chargement interrompu par l'utilisateur")
            break
        name = os.path.basename(path)
        if on_progress:
            on_progress(i, len(files), name)
        try:
            m = pv.read(path)
            if m.n_points:
                meshes.append(m)
            else:
                failed.append(name)
        except Exception as exc:
            LOG.warn(f"{name} illisible ({type(exc).__name__} : {exc})")
            failed.append(name)

    if not meshes:
        raise RuntimeError("Aucun STL exploitable dans le dossier.")

    if on_progress:
        on_progress(len(files), len(files), "fusion des pieces")
    LOG.info(f"fusion de {len(meshes)} maillage(s)...")
    merged = meshes[0] if len(meshes) == 1 else pv.merge(meshes, merge_points=True)

    info = SceneInfo(source=str(folder), n_files=len(files), n_loaded=len(meshes),
                     n_failed=len(failed), failed_names=failed[:20],
                     n_points=int(merged.n_points), n_cells=int(merged.n_cells),
                     bounds=tuple(float(v) for v in merged.bounds),
                     seconds=time.time() - t0)
    LOG.kv("maillage fusionne", {
        "fichiers": f"{len(meshes)}/{len(files)}",
        "points": f"{merged.n_points:,}".replace(",", " "),
        "triangles": f"{merged.n_cells:,}".replace(",", " "),
        "boite": " x ".join(f"{v:.0f}" for v in info.size_mm) + " mm",
        "duree": f"{info.seconds:.1f} s"})
    if failed:
        LOG.warn(f"{len(failed)} fichier(s) ignore(s) : {', '.join(failed[:5])}"
                 + (" ..." if len(failed) > 5 else ""))
    return merged, info


def _barycentric_lattice(order):
    """Coordonnees barycentriques d'un maillage regulier d'ordre `order`,
    sommets exclus (ils sont deja dans le nuage)."""
    coords = []
    for i in range(order + 1):
        for j in range(order + 1 - i):
            k = order - i - j
            if max(i, j, k) == order:      # sommet du triangle
                continue
            coords.append((i / order, j / order, k / order))
    return np.asarray(coords, float)


def collision_cloud(mesh, max_edge=15.0, max_points=4_000_000, on_progress=None):
    """
    Nuage de points servant au test de clearance.

    Sommets + centres de faces, completes par un echantillonnage des triangles
    trop grands devant `max_edge`. On echantillonne plutot que de subdiviser le
    maillage : la subdivision adaptative de VTK est recursive, son cout explose
    sur une maquette d'helicoptere et rien ne borne la memoire. Ici le nombre
    de points ajoutes est calcule a l'avance et plafonne par `max_points`.
    """
    t0 = time.time()
    if on_progress:
        on_progress("extraction de la surface")
    surf = surface(mesh)

    pts = [np.asarray(surf.points, float)]
    if on_progress:
        on_progress("calcul des centres de faces")
    try:
        pts.append(np.asarray(surf.cell_centers().points, float))
    except Exception as exc:
        LOG.warn(f"centres de faces indisponibles ({type(exc).__name__})")

    faces = _triangles(surf)
    if faces is not None and len(faces):
        if on_progress:
            on_progress("densification des grandes faces")
        extra = _sample_large_triangles(np.asarray(surf.points, float), faces,
                                        max_edge, max_points)
        if extra is not None and len(extra):
            pts.append(extra)

    cloud = np.vstack(pts)
    LOG.ok(f"nuage de collision : {len(cloud):,} points en {time.time() - t0:.1f} s"
           .replace(",", " "))
    return cloud


def _triangles(surf):
    """Indices des triangles, si le maillage est bien purement triangulaire."""
    try:
        faces = np.asarray(surf.faces).reshape(-1, 4)
    except ValueError:
        LOG.warn("maillage non purement triangulaire : densification ignoree")
        return None
    if len(faces) and not np.all(faces[:, 0] == 3):
        LOG.warn("faces non triangulaires detectees : densification ignoree")
        return None
    return faces[:, 1:]


def _sample_large_triangles(points, faces, max_edge, max_points):
    """
    Ajoute des points a l'interieur des triangles dont le plus grand cote
    depasse `max_edge`. Sans cela, un grand triangle est "vide" au milieu pour
    le KD-tree et une collision passe au travers.
    """
    tri = points[faces]                                   # (T, 3, 3)
    e = np.stack([np.linalg.norm(tri[:, 1] - tri[:, 0], axis=1),
                  np.linalg.norm(tri[:, 2] - tri[:, 1], axis=1),
                  np.linalg.norm(tri[:, 0] - tri[:, 2], axis=1)], axis=1)
    order = np.clip(np.ceil(e.max(axis=1) / max(max_edge, 1e-6)), 1, 12).astype(int)
    todo = order > 1
    if not np.any(todo):
        return None

    # points prevus : (m+1)(m+2)/2 - 3 par triangle d'ordre m
    per = (order + 1) * (order + 2) // 2 - 3
    planned = int(per[todo].sum())
    if planned > max_points:
        shrink = (planned / float(max_points)) ** 0.5
        order = np.clip(np.ceil(order / shrink), 1, 12).astype(int)
        todo = order > 1
        per = (order + 1) * (order + 2) // 2 - 3
        LOG.warn(f"densification plafonnee : {planned:,} points prevus, reduits "
                 f"a {int(per[todo].sum()):,}. Augmenter la taille de cellule "
                 "si des collisions fines sont attendues.".replace(",", " "))
    LOG.info(f"{int(np.count_nonzero(todo)):,} face(s) plus grandes que "
             f"{max_edge:g} mm densifiees".replace(",", " "))

    out = []
    for m in np.unique(order[todo]):
        sel = tri[order == m]
        bary = _barycentric_lattice(int(m))
        if not len(bary):
            continue
        out.append(np.einsum("pk,tkc->tpc", bary, sel).reshape(-1, 3))
    return np.vstack(out) if out else None


def display_cloud(cloud, max_points=40_000, seed=0):
    """Sous-echantillonnage stable du nuage pour les vues matplotlib."""
    cloud = np.asarray(cloud, float)
    if len(cloud) <= max_points:
        return cloud
    rng = np.random.default_rng(seed)
    idx = rng.choice(len(cloud), size=max_points, replace=False)
    return cloud[np.sort(idx)]
