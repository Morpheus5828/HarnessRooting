"""
Bibliotheque de connecteurs reels.

Jusqu'ici un connecteur etait un pave droit de 45 x 26 x 20 mm. Le concepteur
dispose en realite d'un dossier de modeles (STL ou OBJ exportes de CATIA) : ce
module les inventorie, en deduit tout seul l'axe d'insertion et la face par
laquelle sort le cable, puis les POSE au bout du harnais.

Trois notions, et rien d'autre :

  * `ConnectorModel`   -- un fichier du dossier, avec son repere propre :
                          axe d'insertion, point de sortie du cable, silhouette.
  * `ConnectorPose`    -- ou et comment ce modele est pose : point de sortie du
                          cable, direction du cable, sens, roulis.
  * `ConnectorLibrary` -- le dossier fourni par l'utilisateur.

La pose est contrainte : l'axe du connecteur est STRICTEMENT parallele au
cable qui en sort. L'utilisateur ne choisit donc pas une orientation libre --
il choisit la direction du cable, le sens (le corps part vers l'avant ou vers
l'arriere) et le roulis autour de l'axe.

PyVista n'est importe qu'a l'appel : l'inventaire d'un dossier fonctionne meme
sur un poste sans VTK, en repli sur une lecture STL binaire minimale.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..log import Logger

LOG = Logger("connecteurs")

SUFFIXES = (".stl", ".obj", ".ply", ".vtp")

# Amorce droite imposee en sortie de connecteur quand le modele n'en dicte pas
# d'autre : le cable doit sortir droit avant de pouvoir tourner.
DEFAULT_LEAD_IN = 40.0


def unit(v, fallback=(1.0, 0.0, 0.0)) -> np.ndarray:
    v = np.asarray(v, float).reshape(3)
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-9 else np.asarray(fallback, float)


def rotation_between(a, b) -> np.ndarray:
    """Rotation 3x3 la plus courte qui amene le vecteur `a` sur `b`."""
    a, b = unit(a), unit(b)
    v = np.cross(a, b)
    c = float(np.dot(a, b))
    s = float(np.linalg.norm(v))
    if s < 1e-9:                       # colineaires
        if c > 0.0:
            return np.eye(3)
        # opposes : demi-tour autour d'une perpendiculaire quelconque
        tmp = np.array([1.0, 0.0, 0.0]) if abs(a[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
        axis = unit(np.cross(a, tmp))
        return rotation_about(axis, 180.0)
    K = np.array([[0.0, -v[2], v[1]], [v[2], 0.0, -v[0]], [-v[1], v[0], 0.0]])
    return np.eye(3) + K + K @ K * ((1.0 - c) / (s * s))


def rotation_about(axis, degrees) -> np.ndarray:
    """Rotation de Rodrigues autour d'un axe, en degres."""
    k = unit(axis)
    th = math.radians(float(degrees))
    K = np.array([[0.0, -k[2], k[1]], [k[2], 0.0, -k[0]], [-k[1], k[0], 0.0]])
    return np.eye(3) + math.sin(th) * K + (1.0 - math.cos(th)) * (K @ K)


# ==========================================================================
# 1. modele : un fichier du dossier
# ==========================================================================

@dataclass
class ConnectorModel:
    """
    Un connecteur du dossier, avec le repere qu'on lui a trouve.

    `axis` va de l'ARRIERE (la face d'ou sort le cable) vers l'AVANT (la face
    d'accouplement). `rear` est le point de sortie du cable : c'est lui qu'on
    pose sur le terminal saisi par l'utilisateur.
    """
    path: str
    name: str
    length: float                       # dimension le long de l'axe (mm)
    width: float                        # plus grande dimension transverse (mm)
    height: float
    axis: tuple = (1.0, 0.0, 0.0)       # repere du fichier, arriere -> avant
    rear: tuple = (0.0, 0.0, 0.0)       # point de sortie du cable
    front: tuple = (0.0, 0.0, 0.0)
    profile: tuple = ()                 # silhouette : ((s, demi-largeur), ...)
    n_points: int = 0
    n_cells: int = 0
    source: str = ""                    # comment le repere a ete obtenu

    # -- geometrie utile ------------------------------------------------
    @property
    def lead_in(self) -> float:
        """Amorce droite conseillee : de quoi degager le corps du connecteur."""
        return max(DEFAULT_LEAD_IN, 0.6 * self.length)

    def label(self) -> str:
        return (f"{self.name}  ({self.length:.0f} x {self.width:.0f} x "
                f"{self.height:.0f} mm)")

    def mesh(self):
        """Maillage PyVista du fichier (relu a la demande, jamais garde en RAM)."""
        return _read_mesh(self.path)

    def matrix(self, pose) -> np.ndarray:
        """Transformation 4x4 qui amene le modele a sa pose."""
        u = unit(pose.direction)
        target = u if pose.flip else -u
        R = rotation_about(target, pose.roll) @ rotation_between(self.axis, target)
        M = np.eye(4)
        M[:3, :3] = R
        M[:3, 3] = np.asarray(pose.point, float) - R @ np.asarray(self.rear, float)
        return M

    def placed_mesh(self, pose):
        """Maillage du connecteur, pose au bout du harnais."""
        mesh = self.mesh().copy()
        mesh.transform(self.matrix(pose), inplace=True)
        return mesh

    def silhouette(self, pose=None):
        """
        Contour ferme du connecteur dans le plan (axe, transverse).

        Sert au schema 2D : le corps est dessine STRICTEMENT parallele au cable
        qui en sort, avec la vraie forme du modele et non un rectangle.
        Retourne un tableau (n, 2) en millimetres : x le long de l'axe depuis
        le point de sortie du cable, y transverse.
        """
        prof = np.asarray(self.profile, float) if self.profile is not None \
            else np.empty((0, 3))
        if prof.ndim != 2 or not len(prof):
            half = 0.5 * self.width
            prof = np.array([[0.0, -half, half], [1.0, -half, half]])
        elif prof.shape[1] == 2:               # ancien cache : profil symetrique
            prof = np.column_stack([prof[:, 0], -prof[:, 1], prof[:, 1]])
        s = prof[:, 0] * self.length
        bas, haut = prof[:, 1], prof[:, 2]
        out = np.vstack([np.column_stack([s, haut]),
                         np.column_stack([s[::-1], bas[::-1]])])
        if pose is not None and pose.flip:
            out = out * np.array([-1.0, 1.0])
        return out

    # -- serialisation (cache d'inventaire) -----------------------------
    def to_dict(self):
        d = dict(self.__dict__)
        d["profile"] = [list(map(float, p)) for p in (self.profile or ())]
        return d

    @classmethod
    def from_dict(cls, d):
        d = dict(d)
        d["axis"] = tuple(d["axis"])
        d["rear"] = tuple(d["rear"])
        d["front"] = tuple(d["front"])
        d["profile"] = tuple(tuple(p) for p in d.get("profile", ()))
        return cls(**d)


@dataclass
class ConnectorPose:
    """
    Pose d'un connecteur au bout d'un cable.

    `point`     : point de sortie du cable = le terminal saisi par l'utilisateur.
    `direction` : direction du cable en sortant du connecteur (unitaire).
    `flip`      : sens du corps le long de cet axe.
    `roll`      : rotation propre autour de l'axe (deg), le connecteur n'etant
                  pas de revolution.
    """
    model: str = ""
    point: tuple = (0.0, 0.0, 0.0)
    direction: tuple = (1.0, 0.0, 0.0)
    flip: bool = False
    roll: float = 0.0

    def axis_world(self) -> np.ndarray:
        """Axe du connecteur dans la maquette (arriere -> avant)."""
        u = unit(self.direction)
        return u if self.flip else -u

    def lead_in_point(self, length) -> np.ndarray:
        """Point ou s'acheve l'amorce droite, dans l'axe du connecteur."""
        return np.asarray(self.point, float) + unit(self.direction) * float(length)

    def to_dict(self):
        return dict(model=self.model, point=list(map(float, self.point)),
                    direction=list(map(float, self.direction)),
                    flip=bool(self.flip), roll=float(self.roll))

    @classmethod
    def from_dict(cls, d):
        return cls(model=d.get("model", ""),
                   point=tuple(d.get("point", (0.0, 0.0, 0.0))),
                   direction=tuple(d.get("direction", (1.0, 0.0, 0.0))),
                   flip=bool(d.get("flip", False)), roll=float(d.get("roll", 0.0)))


# ==========================================================================
# 2. lecture des fichiers et deduction du repere
# ==========================================================================

def _read_mesh(path):
    import pyvista as pv

    from ..pvutil import surface
    return surface(pv.read(str(path)))


def _read_points(path):
    """Sommets du fichier. Repli sur un lecteur STL binaire si VTK manque."""
    try:
        mesh = _read_mesh(path)
        return np.asarray(mesh.points, float), int(mesh.n_points), int(mesh.n_cells)
    except Exception:
        pts = _read_stl_binary(path)
        if pts is None:
            raise
        return pts, len(pts), len(pts) // 3


def _read_stl_binary(path):
    """Sommets d'un STL binaire, sans VTK (en-tete 84 o, puis 50 o par facette)."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    if len(raw) < 84 or raw[:5].lower() == b"solid":
        return None                    # STL ASCII : laisse la main a VTK
    n_tri = int(np.frombuffer(raw[80:84], dtype="<u4")[0])
    if 84 + 50 * n_tri != len(raw) or n_tri == 0:
        return None
    rec = np.frombuffer(raw[84:], dtype=np.dtype([("n", "<f4", 3),
                                                  ("v", "<f4", (3, 3)),
                                                  ("a", "<u2")]), count=n_tri)
    return rec["v"].reshape(-1, 3).astype(float)


def _frame_from_points(pts, n_slices=24):
    """
    Axe d'insertion, faces avant/arriere et silhouette d'un connecteur.

    L'axe d'insertion est la direction d'allongement du corps (premiere
    composante principale) : un connecteur est toujours plus long que large.
    Reste a savoir de quel cote sort le cable. Le fut arriere est plus fin que
    la face d'accouplement : on compare le rayon moyen des deux moities, et le
    cable sort du cote le plus MINCE.
    """
    pts = np.asarray(pts, float).reshape(-1, 3)
    c = pts.mean(axis=0)
    q = pts - c
    # SVD sur un echantillon : un connecteur peut peser 200 000 sommets.
    sample = q if len(q) <= 20000 else q[np.random.default_rng(0).choice(
        len(q), 20000, replace=False)]
    _, _, vt = np.linalg.svd(sample, full_matrices=False)
    axis, e2, e3 = vt[0], vt[1], vt[2]

    t = q @ axis
    t0, t1 = float(t.min()), float(t.max())
    length = t1 - t0
    radial = np.linalg.norm(q - np.outer(t, axis), axis=1)
    mid = 0.5 * (t0 + t1)
    r_low = float(radial[t <= mid].mean()) if np.any(t <= mid) else 0.0
    r_high = float(radial[t > mid].mean()) if np.any(t > mid) else 0.0
    if r_low > r_high:                 # la moitie basse est la plus epaisse
        axis, e2 = -axis, -e2
        t, t0, t1 = -t, -t1, -t0

    rear = c + axis * t0
    front = c + axis * t1
    width = float(np.ptp(q @ e2))
    height = float(np.ptp(q @ e3))

    # Silhouette : contour vu de profil, tranche par tranche. On garde le bas
    # ET le haut, et non une demi-largeur : un connecteur n'est pas symetrique,
    # et c'est cette forme que le concepteur reconnait au premier coup d'oeil.
    prof = []
    if length > 1e-6:
        s = (t - t0) / length
        edges = np.linspace(0.0, 1.0, max(4, int(n_slices)) + 1)
        w = q @ e2
        idx = np.clip(np.digitize(s, edges) - 1, 0, len(edges) - 2)
        last = None
        for k in range(len(edges) - 1):
            sel = idx == k
            if np.any(sel):
                last = (float(w[sel].min()), float(w[sel].max()))
            if last is None:
                continue
            prof.append((0.5 * (edges[k] + edges[k + 1]), last[0], last[1]))
        if prof:
            prof = ([(0.0, prof[0][1], prof[0][2])] + prof
                    + [(1.0, prof[-1][1], prof[-1][2])])
    return dict(axis=tuple(map(float, axis)), rear=tuple(map(float, rear)),
                front=tuple(map(float, front)), length=float(length),
                width=max(width, 1e-3), height=max(height, 1e-3),
                profile=tuple(tuple(float(v) for v in row) for row in prof))


def load_model(path) -> ConnectorModel:
    """Inventorie un fichier de connecteur."""
    path = str(path)
    pts, n_pts, n_cells = _read_points(path)
    if len(pts) < 4:
        raise ValueError(f"{os.path.basename(path)} : maillage vide.")
    fr = _frame_from_points(pts)
    return ConnectorModel(path=path, name=Path(path).stem, n_points=n_pts,
                          n_cells=n_cells, source="axe principal du maillage",
                          **fr)


# ==========================================================================
# 3. la bibliotheque : le dossier fourni par l'utilisateur
# ==========================================================================

@dataclass
class ConnectorLibrary:
    folder: str = ""
    models: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def __len__(self):
        return len(self.models)

    def names(self):
        return [m.name for m in self.models]

    def get(self, name):
        for m in self.models:
            if m.name == name:
                return m
        return None

    def first(self):
        return self.models[0] if self.models else None

    # ------------------------------------------------------------------
    @classmethod
    def scan(cls, folder, cache_dir=None, logger=LOG) -> "ConnectorLibrary":
        """
        Inventorie un dossier de connecteurs.

        Le repere de chaque modele est mis en cache (cle : chemin, taille,
        date) : relire cinquante STL a chaque demarrage prendrait plusieurs
        secondes pour un resultat identique.
        """
        folder = str(folder)
        files = sorted(p for p in Path(folder).iterdir()
                       if p.suffix.lower() in SUFFIXES) if os.path.isdir(folder) else []
        if not files:
            if logger:
                logger.warn(f"aucun connecteur ({', '.join(SUFFIXES)}) dans {folder}")
            return cls(folder=folder)

        cache, cache_path = _load_cache(cache_dir)
        models, errors, from_cache = [], [], 0
        for p in files:
            key = f"{p.resolve()}|{p.stat().st_size}|{int(p.stat().st_mtime)}"
            hit = cache.get(key)
            if hit is not None:
                try:
                    models.append(ConnectorModel.from_dict(hit))
                    from_cache += 1
                    continue
                except Exception:
                    pass
            try:
                m = load_model(p)
            except Exception as exc:
                errors.append((p.name, f"{type(exc).__name__} : {exc}"))
                continue
            models.append(m)
            cache[key] = m.to_dict()

        _save_cache(cache_path, cache)
        if logger:
            logger.ok(f"{len(models)} connecteur(s) inventorie(s) dans {folder}"
                      f" ({from_cache} depuis le cache)")
            for m in models:
                logger.debug(f"    {m.name:24s} {m.length:6.1f} x {m.width:5.1f} x "
                             f"{m.height:5.1f} mm, sortie cable en "
                             f"({m.rear[0]:.0f}, {m.rear[1]:.0f}, {m.rear[2]:.0f})")
            for name, why in errors:
                logger.warn(f"connecteur illisible : {name} -- {why}")
        return cls(folder=folder, models=models, errors=errors)


def _cache_file(cache_dir):
    if not cache_dir:
        return None
    try:
        os.makedirs(str(cache_dir), exist_ok=True)
    except OSError:
        return None
    return Path(cache_dir) / "connecteurs.json"


def _load_cache(cache_dir):
    path = _cache_file(cache_dir)
    if path and path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8")), path
        except Exception:
            pass
    return {}, path


def _save_cache(path, data):
    if not path:
        return
    try:
        path.write_text(json.dumps(data), encoding="utf-8")
    except Exception as exc:
        LOG.debug(f"cache d'inventaire non ecrit : {exc}")


# ==========================================================================
# 4. poses et cheminement
# ==========================================================================

def auto_direction(point, other, bounds=None) -> tuple:
    """
    Direction de sortie par defaut : vers l'autre extremite du cable, ramenee
    a l'axe principal le plus proche.

    Un connecteur n'est presque jamais monte de biais : proposer +X, -Y... est
    plus proche du montage reel qu'une direction quelconque, et l'utilisateur
    garde la main.
    """
    v = np.asarray(other, float) - np.asarray(point, float)
    if float(np.linalg.norm(v)) < 1e-9:
        return (1.0, 0.0, 0.0)
    k = int(np.argmax(np.abs(v)))
    out = np.zeros(3)
    out[k] = 1.0 if v[k] >= 0 else -1.0
    return tuple(out)


def lead_in_waypoints(poses, length=None, models=None):
    """
    Points d'amorce a imposer au cheminement, un par connecteur pose.

    Retourne [(index du terminal, point)] : le cable doit passer par ce point,
    donc sortir DROIT du connecteur avant de pouvoir tourner.
    """
    out = []
    for i, pose in enumerate(poses):
        if pose is None:
            continue
        model = (models or {}).get(pose.model) if models else None
        L = float(length) if length else (model.lead_in if model else DEFAULT_LEAD_IN)
        if L <= 0.0:
            continue
        out.append((i, pose.lead_in_point(L)))
    return out


def straight_lead_in(points, pose, length, min_radius=None, step=None,
                     tries=6):
    """
    Impose l'amorce droite en tete de polyligne, puis raccorde le reste par un
    ARC TANGENT jamais plus serre que le rayon de cintrage admissible.

    C'est la condition pour que le toron entre proprement dans le connecteur :
    d'abord un troncon rigoureusement dans l'axe -- le cable ne doit pas forcer
    sur les contacts -- puis une courbure admissible.

    Le point de raccordement est recule tant que le rayon obtenu reste sous le
    seuil : raccorder plus loin, c'est tourner plus doucement. Si meme le
    dernier essai ne suffit pas, la meilleure tete est renvoyee -- le lisseur
    et le controle d'interference ont le dernier mot, et le rapport affiche le
    rayon reellement obtenu.
    """
    from .hrh import curvature_radii

    pts = np.asarray(points, float).reshape(-1, 3)
    if pose is None or len(pts) < 2:
        return pts
    p0 = np.asarray(pose.point, float)
    u = unit(pose.direction)
    L = float(length)
    if L <= 0.0:
        return pts

    reverse = float(np.linalg.norm(pts[-1] - p0)) < float(np.linalg.norm(pts[0] - p0))
    if reverse:
        pts = pts[::-1]

    keep = pts[np.linalg.norm(pts - p0, axis=1) > L * 1.001]
    tip = p0 + u * L
    if not len(keep):
        out = np.vstack([p0, tip])
        return out[::-1] if reverse else out

    # L'arc est construit avec 10 % de marge : le raccordement a la suite du
    # trace fait perdre quelques pour cent au rayon reellement mesure.
    asked = float(min_radius) * 1.1 if min_radius else None
    best, best_r = None, -1.0
    for k in range(min(int(tries), len(keep))):
        target = keep[k]
        arc = _tangent_arc(tip, u, target, asked, step)
        head = np.vstack([[p0], [tip], arc]) if len(arc) else np.vstack([[p0], [tip]])
        out = np.vstack([head, keep[k:]])
        r = curvature_radii(out[:len(head) + 6])
        r = r[np.isfinite(r)]
        r_min = float(r.min()) if len(r) else float("inf")
        if r_min > best_r:
            best, best_r = out, r_min
        if not min_radius or r_min >= float(min_radius):
            break
    return best[::-1] if reverse else best


def _tangent_arc(start, tangent, target, radius, step=None):
    """
    Arc circulaire partant de `start`, TANGENT a `tangent`, et rejoignant
    `target`. Retourne les points intermediaires (extremites exclues).

    Le cercle tangent en `start` et passant par `target` a un rayon impose :
    R = d / (2 sin a), avec `a` l'angle entre la tangente et la corde. S'il est
    admissible, on le prend tel quel et l'arc arrive exactement sur `target` :
    aucun angle vif ne subsiste. S'il est trop serre (cible trop proche ou
    virage trop brutal), on ouvre le virage au rayon minimal et le lisseur
    reprend la suite -- mais jamais en dessous du rayon admissible.
    """
    start = np.asarray(start, float)
    v = np.asarray(target, float) - start
    dist = float(np.linalg.norm(v))
    if dist < 1e-6:
        return np.empty((0, 3))
    t = unit(tangent)
    w = v / dist
    cos = float(np.clip(np.dot(t, w), -1.0, 1.0))
    if cos > 0.9995:                      # deja aligne : rien a raccorder
        return np.empty((0, 3))
    normal = np.cross(t, w)
    if float(np.linalg.norm(normal)) < 1e-9:
        return np.empty((0, 3))
    normal = unit(normal)
    alpha = math.acos(cos)
    needed = dist / (2.0 * max(math.sin(alpha), 1e-6))
    R = needed if (not radius or needed >= float(radius)) else float(radius)
    side = unit(np.cross(normal, t))      # vers le centre du virage
    centre = start + side * R
    theta = 2.0 * alpha
    n = max(3, int(math.ceil(theta * R / max(step or 8.0, 1e-3))))
    angles = np.linspace(0.0, theta, n + 1)[1:-1]
    if not len(angles):
        return np.empty((0, 3))
    return np.array([centre + rotation_about(normal, math.degrees(a))
                     @ (start - centre) for a in angles])


def entry_bend_radius(points, pose, length):
    """
    Rayon de courbure minimal RENCONTRE sur l'amorce et son raccordement.

    Sert de controle : c'est le chiffre a comparer au rayon admissible pour
    dire si le toron entre correctement dans le connecteur.
    """
    from .hrh import curvature_radii

    pts = np.asarray(points, float).reshape(-1, 3)
    if pose is None or len(pts) < 3:
        return float("inf")
    p0 = np.asarray(pose.point, float)
    d = np.linalg.norm(pts - p0, axis=1)
    near = np.where(d <= max(float(length) * 2.5, 1.0))[0]
    if len(near) < 3:
        return float("inf")
    r = curvature_radii(pts[near.min():near.max() + 1])
    r = r[np.isfinite(r)]
    return float(r.min()) if len(r) else float("inf")


def lead_in_error(points, pose, length, samples=12):
    """Ecart maximal de l'amorce a l'axe du connecteur (mm) : 0 = parfait."""
    pts = np.asarray(points, float).reshape(-1, 3)
    if pose is None or len(pts) < 2:
        return 0.0
    p0 = np.asarray(pose.point, float)
    u = unit(pose.direction)
    s = np.linspace(0.0, float(length), samples)
    axis_pts = p0 + s[:, None] * u
    # distance de chaque point d'axe a la polyligne
    err = 0.0
    for q in axis_pts:
        seg = pts[1:] - pts[:-1]
        w = q - pts[:-1]
        n2 = np.einsum("ij,ij->i", seg, seg)
        tt = np.clip(np.einsum("ij,ij->i", w, seg) / np.where(n2 < 1e-12, 1.0, n2),
                     0.0, 1.0)
        proj = pts[:-1] + tt[:, None] * seg
        err = max(err, float(np.linalg.norm(proj - q, axis=1).min()))
    return err


def connector_waypoints(terminals, poses, lead_in=None, models=None):
    """
    Points imposes au cheminement par les connecteurs poses.

    Retourne (extra_waypoints, anchor_points) :
      * `extra_waypoints[k]` -- les amorces du cable k, dans l'ordre depart puis
        arrivee : le cheminement doit y passer, donc sortir DROIT du connecteur.
      * `anchor_points` -- terminaux et amorces reunis, a epingler pendant le
        lissage pour que la sortie d'axe ne soit pas rabotee.
    """
    extra, anchors = [], []
    for k, (a, b) in enumerate(terminals):
        pair = poses[k] if poses and k < len(poses) else (None, None)
        wps = []
        for pose, term in zip(pair, (a, b)):
            anchors.append(np.asarray(term, float))
            # Sans connecteur choisi, rien n'est impose : le cheminement reste
            # libre de sortir du terminal dans la direction qui l'arrange.
            if pose is None or not pose.model:
                continue
            model = (models or {}).get(pose.model) if models else None
            L = float(lead_in) if lead_in else (model.lead_in if model
                                                else DEFAULT_LEAD_IN)
            if L <= 0.0:
                continue
            tip = pose.lead_in_point(L)
            wps.append(tip)
            anchors.append(tip)
        # l'amorce d'arrivee doit etre le DERNIER point impose
        extra.append(wps)
    return extra, (np.asarray(anchors, float).reshape(-1, 3) if anchors
                   else np.empty((0, 3)))


def apply_connector_geometry(polylines, poses, lead_in=None, models=None,
                             min_radius=None, step=None, collider=None,
                             clearance=0.0, logger=None):
    """
    Reprend chaque extremite de cable : amorce droite exacte, puis arc tangent.

    Le cheminement passe deja par l'amorce (elle lui a ete imposee), mais ses
    points sont accroches a la grille : quelques millimetres de biais suffisent
    a faire forcer le toron sur les contacts. On remet donc la tete du trace
    rigoureusement dans l'axe.
    """
    out = []
    for k, pts in enumerate(polylines):
        pts = np.asarray(pts, float)
        pair = poses[k] if poses and k < len(poses) else (None, None)
        for end, pose in enumerate(pair):
            if pose is None or not pose.model:
                continue
            model = (models or {}).get(pose.model) if models else None
            L = float(lead_in) if lead_in else (model.lead_in if model
                                                else DEFAULT_LEAD_IN)
            head = pts if end == 0 else pts[::-1]
            new = straight_lead_in(head, pose, L, min_radius=min_radius, step=step)
            # Le raccordement coupe un angle : on refuse toute tete qui
            # descendrait sous la garde ou traverserait la structure. Mieux vaut
            # une entree de connecteur imparfaite qu'une interference.
            if collider is not None and not _head_is_clear(
                    new, L, collider, clearance):
                if logger:
                    logger.warn(f"cable {k + 1} : raccordement de connecteur "
                                f"refuse (il passerait sous la garde), trace "
                                f"d'origine conserve")
                continue
            pts = new if end == 0 else new[::-1]
        out.append(pts)
    return out


def _head_is_clear(points, length, collider, clearance):
    """La tete reconstruite respecte-t-elle garde et non-traversee ?"""
    pts = np.asarray(points, float)
    span = float(length) * 3.0
    d0 = np.linalg.norm(pts - pts[0], axis=1)
    head = pts[d0 <= span]
    if len(head) < 2:
        return True
    if float(collider.distance(head).min()) < float(clearance):
        return False
    return len(collider.crossing_indices(head)) == 0


def apply_connector_geometry_segments(seg_geo, poses, lead_in=None, models=None,
                                      min_radius=None, step=None, tol=50.0,
                                      collider=None, clearance=0.0, logger=None):
    """
    Meme correction, mais sur la geometrie PAR TRONCON.

    C'est elle qui part vers CATIA : le solide, les fibres neutres et le
    tableau des torons sont construits sur les troncons, pas sur les
    polylignes par cable. Sans cette passe, le livrable n'aurait pas l'amorce
    droite qu'on voit a l'ecran.

    Le troncon concerne par un connecteur est celui dont une extremite est la
    plus proche du point de sortie de cable. Ce n'est pas une egalite : le
    troncon commence sur un NOEUD DE GRILLE, a une demi-maille pres du terminal
    saisi -- d'ou la tolerance, qu'il revient a l'appelant de caler sur sa
    taille de maille.

    Un troncon plus court que l'amorce elle-meme est laisse intact : il n'y a
    pas la place. Au-dela, l'amorce est posee et le reste du troncon suit, son
    autre extremite comprise -- un point de branchement ne bouge jamais, il est
    partage avec d'autres troncons.
    """
    flat = [p for pair in (poses or []) for p in pair if p is not None and p.model]
    if not flat or not seg_geo:
        return seg_geo
    geo = [np.asarray(g, float) for g in seg_geo]

    # Appariement INJECTIF connecteur <-> bout de troncon, par distance
    # croissante : un bout de troncon appartient a un seul connecteur, sinon
    # le second ecraserait le travail du premier.
    pairs = []
    for k, pose in enumerate(flat):
        p0 = np.asarray(pose.point, float)
        for i, g in enumerate(geo):
            for at_start, q in ((True, g[0]), (False, g[-1])):
                d = float(np.linalg.norm(q - p0))
                if d <= float(tol):
                    pairs.append((d, k, i, at_start))
    pairs.sort(key=lambda t: t[0])
    todo, taken_pose, taken_end = {}, set(), set()
    for d, k, i, at_start in pairs:
        if k in taken_pose or (i, at_start) in taken_end:
            continue
        taken_pose.add(k)
        taken_end.add((i, at_start))
        todo[(i, at_start)] = [flat[k]]
    if logger and len(taken_pose) < len(flat):
        logger.debug(f"{len(flat) - len(taken_pose)} connecteur(s) sans troncon "
                     f"de bout a moins de {float(tol):.0f} mm")

    for (i, at_start), poses_here in todo.items():
        for pose in poses_here:
            g = geo[i]
            model = (models or {}).get(pose.model)
            L = float(lead_in) if lead_in else (model.lead_in if model
                                                else DEFAULT_LEAD_IN)
            if float(np.sum(np.linalg.norm(np.diff(g, axis=0), axis=1))) < 1.05 * L:
                if logger:
                    logger.warn(f"troncon plus court que l'amorce de "
                                f"{pose.model} : laisse intact")
                continue
            head = g if at_start else g[::-1]
            new = straight_lead_in(head, pose, L, min_radius=min_radius, step=step)
            if collider is not None and not _head_is_clear(new, L, collider,
                                                           clearance):
                if logger:
                    logger.warn(f"amorce refusee sur un troncon ({pose.model}) : "
                                f"elle passerait sous la garde")
                continue
            geo[i] = new if at_start else new[::-1]
    return geo


def connector_report(polylines, poses, lead_in=None, models=None,
                     min_radius=None):
    """
    Controle par connecteur : le toron sort-il vraiment dans l'axe, et avec
    quel rayon de cintrage a l'entree ?
    """
    rows = []
    for k, pts in enumerate(polylines):
        pair = poses[k] if poses and k < len(poses) else (None, None)
        for end, pose in enumerate(pair):
            if pose is None or not pose.model:
                continue
            model = (models or {}).get(pose.model) if models else None
            L = float(lead_in) if lead_in else (model.lead_in if model
                                                else DEFAULT_LEAD_IN)
            head = np.asarray(pts, float)
            head = head if end == 0 else head[::-1]
            r = entry_bend_radius(head, pose, L)
            rows.append(dict(cable=k + 1, end="depart" if end == 0 else "arrivee",
                             model=pose.model, lead_in=L,
                             axis_error=lead_in_error(head, pose, L),
                             bend_radius=r,
                             ok=(r >= float(min_radius)) if min_radius else True))
    return rows
