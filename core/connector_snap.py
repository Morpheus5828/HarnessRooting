"""
Accrochage d'un connecteur sur le harnais.

Le probleme
-----------
Dans Click & Root, l'utilisateur ne clique pas un point : il clique une
PIECE (une prise, un boitier). CATIA rend le centre de gravite de cette
piece -- un point situe a l'INTERIEUR du connecteur. Faire partir un cable
de la se voit tout de suite : le toron traverse le corps du connecteur avant
d'en sortir.

Le raisonnement retenu
----------------------
1. on calcule la BOITE ENGLOBANTE du connecteur (alignee sur ses propres
   axes principaux, pas sur X/Y/Z : un connecteur monte de biais garde
   ainsi des faces franches) ;
2. on regarde les six faces de cette boite et on retient celle qui est LA
   PLUS PROCHE du harnais (concretement : de l'autre extremite du cable,
   ou d'un point de reference fourni) ;
3. le CENTRE de cette face devient le depart -- ou l'arrivee -- du cable, et
   sa normale sortante donne la direction de sortie.

C'est exactement ce que fait un integrateur : le cable sort par la face
tournee vers le reste du faisceau.

Le module ne depend que de numpy. La lecture des maillages est deleguee a
`core.connectors`, qui sait lire STL / OBJ / PLY et se rabat sur un lecteur
STL binaire minimal quand PyVista n'est pas installe.
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..log import Logger

LOG = Logger("accrochage")

# Noms des six faces, dans l'ordre (-u, +u) pour chacun des trois axes de la
# boite. `u`, `v`, `w` sont les axes propres du connecteur.
FACE_NAMES = ("-U", "+U", "-V", "+V", "-W", "+W")


# ==========================================================================
# 1. boite englobante
# ==========================================================================

@dataclass
class BBox:
    """
    Boite englobante d'un connecteur, avec son propre repere.

    `axes` sont les trois directions de la boite (matrice 3x3, une direction
    par ligne) ; `half` les demi-dimensions le long de ces directions ;
    `center` le centre de la boite, en coordonnees maquette.
    """
    center: np.ndarray
    axes: np.ndarray
    half: np.ndarray
    n_points: int = 0
    oriented: bool = True

    @property
    def size(self) -> np.ndarray:
        return 2.0 * self.half

    @property
    def volume(self) -> float:
        return float(np.prod(self.size))

    @property
    def diagonal(self) -> float:
        return float(np.linalg.norm(self.size))

    def corners(self) -> np.ndarray:
        """Les huit sommets de la boite, en coordonnees maquette."""
        signs = np.array([[sx, sy, sz]
                          for sx in (-1.0, 1.0)
                          for sy in (-1.0, 1.0)
                          for sz in (-1.0, 1.0)])
        return self.center + (signs * self.half) @ self.axes

    def to_local(self, points) -> np.ndarray:
        """Coordonnees dans le repere de la boite."""
        p = np.asarray(points, float).reshape(-1, 3)
        return (p - self.center) @ self.axes.T

    def to_world(self, local) -> np.ndarray:
        q = np.asarray(local, float).reshape(-1, 3)
        return self.center + q @ self.axes

    def contains(self, point, margin=0.0) -> bool:
        q = np.abs(self.to_local(point)[0])
        return bool(np.all(q <= self.half + float(margin)))

    def faces(self) -> list:
        """
        Les six faces, avec leur centre, leur normale sortante et leur aire.

        Les faces sont rendues dans l'ordre de FACE_NAMES.
        """
        out = []
        for k in range(3):
            other = [i for i in range(3) if i != k]
            area = float(4.0 * self.half[other[0]] * self.half[other[1]])
            for sign in (-1.0, 1.0):
                local = np.zeros(3)
                local[k] = sign * self.half[k]
                normal = sign * self.axes[k]
                out.append(dict(
                    name=FACE_NAMES[2 * k + (1 if sign > 0 else 0)],
                    axis=k, sign=sign, area=area,
                    center=self.to_world(local)[0],
                    normal=normal / max(float(np.linalg.norm(normal)), 1e-12),
                    half=np.array([self.half[other[0]], self.half[other[1]]]),
                ))
        return out

    def as_dict(self):
        return dict(center=[float(v) for v in self.center],
                    axes=[[float(v) for v in row] for row in self.axes],
                    half=[float(v) for v in self.half],
                    size=[float(v) for v in self.size],
                    n_points=int(self.n_points), oriented=bool(self.oriented))


def bbox_from_points(points, oriented=True, min_half=0.5) -> BBox:
    """
    Boite englobante d'un nuage de points.

    `oriented=True` aligne la boite sur les axes principaux du nuage (ACP) :
    c'est ce qu'il faut pour un connecteur monte de biais. `False` rend la
    boite alignee sur X / Y / Z de la maquette.
    """
    pts = np.asarray(points, float).reshape(-1, 3)
    if len(pts) == 0:
        raise ValueError("boite englobante : nuage vide")

    axes = np.eye(3)
    if oriented and len(pts) >= 4:
        c = pts.mean(axis=0)
        q = pts - c
        # ACP sur un echantillon : un connecteur peut peser 200 000 sommets.
        sample = q if len(q) <= 20000 else q[
            np.random.default_rng(0).choice(len(q), 20000, replace=False)]
        try:
            _, _, vt = np.linalg.svd(sample, full_matrices=False)
            if np.all(np.isfinite(vt)):
                axes = vt
        except np.linalg.LinAlgError:
            LOG.debug("ACP impossible : boite alignee sur les axes maquette")
    # repere direct : evite des normales qui pointent a l'envers
    if float(np.linalg.det(axes)) < 0.0:
        axes[2] = -axes[2]

    local = pts @ axes.T
    lo, hi = local.min(axis=0), local.max(axis=0)
    half = np.maximum(0.5 * (hi - lo), float(min_half))
    center = (0.5 * (lo + hi)) @ axes
    return BBox(center=center, axes=axes, half=half, n_points=len(pts),
                oriented=bool(oriented))


# ==========================================================================
# 2. la face la plus proche du harnais
# ==========================================================================

def _distance_to_face(face, target) -> float:
    """
    Distance du point `target` au RECTANGLE de la face (pas a son plan).

    Un connecteur long et fin a une petite face frontale et deux grandes
    faces laterales : comparer les centres suffirait a choisir une face
    laterale alors que le harnais sort par l'avant. On mesure donc la
    distance au rectangle lui-meme.
    """
    t = np.asarray(target, float).reshape(3)
    d = t - face["center"]
    n = float(np.dot(d, face["normal"]))
    # composantes dans le plan de la face : les deux axes de la boite qui ne
    # sont pas la normale
    tangent = d - n * face["normal"]
    a = float(np.dot(tangent, face["u"]))
    b = float(np.dot(tangent, face["v"]))
    da = max(0.0, abs(a) - float(face["half"][0]))
    db = max(0.0, abs(b) - float(face["half"][1]))
    return float(np.sqrt(n * n + da * da + db * db))


def _with_basis(bbox, faces):
    """Ajoute a chaque face les deux axes de son plan."""
    for face in faces:
        other = [i for i in range(3) if i != face["axis"]]
        face["u"] = bbox.axes[other[0]]
        face["v"] = bbox.axes[other[1]]
    return faces


def exit_face_index(bbox: BBox, target) -> int | None:
    """
    Face par laquelle SORT le segment [centre du connecteur -> harnais].

    Methode des dalles : dans le repere de la boite, on cherche l'axe qui
    limite le premier le rayon. C'est la face que le cable franchit ; elle
    reste bien definie meme quand le harnais est tres loin, la ou comparer
    des distances de plusieurs centaines de millimetres ne departage plus
    rien.
    """
    d = bbox.to_local(target)[0]
    n = float(np.linalg.norm(d))
    if n < 1e-9:
        return None
    d = d / n
    t_min, axe = np.inf, None
    for k in range(3):
        if abs(d[k]) < 1e-9:
            continue
        t = float(bbox.half[k] / abs(d[k]))
        if t < t_min:
            t_min, axe = t, k
    if axe is None:
        return None
    return 2 * axe + (1 if d[axe] > 0.0 else 0)


def nearest_face(bbox: BBox, target, prefer_outward=True,
                 exit_tol=0.10) -> dict:
    """
    Face de la boite tournee vers `target`.

    Deux criteres, dans cet ordre :

    1. la distance du point vise au RECTANGLE de la face (et non a son plan
       ni a son centre) : c'est bien "la face la plus proche du harnais" ;
    2. quand plusieurs faces sont a egalite -- le cas des que le harnais
       s'eloigne de quelques diagonales de connecteur --, celle par laquelle
       le segment centre -> harnais sort reellement.

    `prefer_outward` ecarte les faces qui tournent le dos au harnais : une
    face dont la normale s'eloigne du point vise ne peut pas etre la sortie
    du cable. `exit_tol` est l'ecart, en diagonales de boite, sous lequel
    deux faces sont dites a egalite.
    """
    t = np.asarray(target, float).reshape(3)
    faces = _with_basis(bbox, bbox.faces())

    scored = []
    for face in faces:
        d = _distance_to_face(face, t)
        towards = float(np.dot(t - face["center"], face["normal"]))
        scored.append((face, d, towards))

    candidats = [s for s in scored if s[2] > 0.0] if prefer_outward else scored
    if not candidats:                    # le harnais part vers l'interieur ?
        candidats = scored

    face, dist, towards = min(candidats, key=lambda s: s[1])

    sortie = exit_face_index(bbox, t)
    if sortie is not None:
        f_sortie, d_sortie, v_sortie = scored[sortie]
        if d_sortie <= dist + exit_tol * bbox.diagonal:
            face, dist, towards = f_sortie, d_sortie, v_sortie

    face = dict(face)
    face["distance"] = float(dist)
    face["towards"] = float(towards)
    return face


@dataclass
class SnapResult:
    """Point d'accrochage retenu pour un connecteur."""
    point: np.ndarray                   # centre de face (+ deport) : depart/arrivee
    direction: np.ndarray               # normale sortante = direction du cable
    face_center: np.ndarray
    face_name: str = ""
    face_area: float = 0.0
    distance: float = 0.0               # distance face -> point vise
    origin: np.ndarray = field(default_factory=lambda: np.zeros(3))
    bbox: BBox = None
    source: str = ""                    # d'ou vient le maillage
    moved: float = 0.0                  # ecart entre le clic et le point retenu

    def as_dict(self):
        return dict(point=[float(v) for v in self.point],
                    direction=[float(v) for v in self.direction],
                    face=self.face_name, face_area=float(self.face_area),
                    distance=float(self.distance), moved=float(self.moved),
                    source=self.source,
                    bbox=self.bbox.as_dict() if self.bbox else None)


def snap_to_nearest_face(points, target, origin=None, offset=0.0,
                         oriented=True, source="") -> SnapResult:
    """
    Applique le raisonnement complet a un nuage de points de connecteur.

    `points` : maillage du connecteur (sommets) ;
    `target` : point vers lequel part le cable (l'autre extremite, ou le
               barycentre du harnais) ;
    `origin` : le clic d'origine, seulement pour mesurer le deplacement ;
    `offset` : deport du point retenu le long de la normale sortante (mm).
    """
    bbox = bbox_from_points(points, oriented=oriented)
    face = nearest_face(bbox, target)
    point = face["center"] + face["normal"] * float(offset)
    orig = np.asarray(origin if origin is not None else bbox.center, float).reshape(3)
    return SnapResult(point=point, direction=face["normal"],
                      face_center=face["center"], face_name=face["name"],
                      face_area=float(face["area"]), distance=face["distance"],
                      origin=orig, bbox=bbox, source=source,
                      moved=float(np.linalg.norm(point - orig)))


# ==========================================================================
# 3. retrouver le maillage du connecteur clique
# ==========================================================================

def _normalise(name) -> str:
    """Nom de piece comparable : sans extension, sans ponctuation, en minuscules."""
    stem = Path(str(name)).stem if str(name).lower().endswith(
        (".stl", ".obj", ".ply", ".vtp")) else str(name)
    return re.sub(r"[^a-z0-9]+", "", stem.lower())


def find_part_mesh(name, folders) -> str | None:
    """
    Fichier STL de la piece `name` dans les dossiers d'export CATIA.

    L'export nomme chaque fichier d'apres la piece (`build_export_macro`),
    en remplacant `/`, `\\` et `:`. On compare donc des noms normalises, avec
    un repli sur une correspondance partielle : CATIA suffixe volontiers les
    instances (`.1`, `_OFFSET`).
    """
    if not name:
        return None
    cible = _normalise(name)
    if not cible:
        return None

    fichiers = []
    for folder in folders or []:
        if not folder or not os.path.isdir(str(folder)):
            continue
        for motif in ("*.stl", "*.STL", "*.obj", "*.ply"):
            fichiers.extend(glob.glob(os.path.join(str(folder), motif)))
    if not fichiers:
        return None

    exact = [f for f in fichiers if _normalise(f) == cible]
    if exact:
        return exact[0]
    partiel = [f for f in fichiers
               if cible in _normalise(f) or _normalise(f) in cible]
    if partiel:                           # le plus court : le moins suffixe
        return min(partiel, key=lambda f: len(_normalise(f)))
    return None


def _read_points(path):
    from .connectors import _read_points as lire
    pts, _n_pts, _n_cells = lire(str(path))
    return np.asarray(pts, float).reshape(-1, 3)


def points_around(mesh, center, radius):
    """
    Sommets de la maquette fusionnee autour d'un point.

    Repli quand la piece cliquee n'a pas de fichier STL a elle : on decoupe
    une bulle dans la maquette. Le rayon est reduit tant que la bulle attrape
    manifestement plus que le connecteur.
    """
    if mesh is None:
        return np.empty((0, 3))
    pts = np.asarray(getattr(mesh, "points", mesh), float).reshape(-1, 3)
    if len(pts) == 0:
        return np.empty((0, 3))
    c = np.asarray(center, float).reshape(3)
    d = np.linalg.norm(pts - c, axis=1)
    for r in (float(radius), 0.5 * float(radius), 0.25 * float(radius)):
        sel = pts[d <= r]
        if 12 <= len(sel) <= 200000:
            return sel
    sel = pts[d <= float(radius)]
    return sel if len(sel) >= 4 else np.empty((0, 3))


def connector_points(point, name=None, folders=None, mesh=None,
                     search_radius=400.0):
    """
    Nuage de points du connecteur clique, et d'ou il vient.

    Trois sources, dans l'ordre de fiabilite :
      1. le STL de la piece, retrouve par son nom dans les dossiers d'export ;
      2. une bulle decoupee dans la maquette fusionnee autour du clic ;
      3. rien -- l'appelant gardera le point clique.
    """
    path = find_part_mesh(name, folders)
    if path:
        try:
            pts = _read_points(path)
            if len(pts) >= 4:
                return pts, f"STL de la piece ({os.path.basename(path)})"
        except Exception as exc:
            LOG.warn(f"{os.path.basename(str(path))} illisible : {exc}")

    pts = points_around(mesh, point, search_radius)
    if len(pts) >= 4:
        return pts, "decoupe locale de la maquette"
    return np.empty((0, 3)), ""


def snap_terminal(point, target, name=None, folders=None, mesh=None,
                  offset=0.0, search_radius=400.0, oriented=True):
    """
    Point de depart (ou d'arrivee) reel d'un cable sur un connecteur clique.

    Rend `None` quand le connecteur n'a pas pu etre retrouve : l'appelant
    conserve alors le point clique, sans rien casser.
    """
    pts, source = connector_points(point, name=name, folders=folders,
                                   mesh=mesh, search_radius=search_radius)
    if len(pts) < 4:
        return None
    try:
        return snap_to_nearest_face(pts, target, origin=point, offset=offset,
                                    oriented=oriented, source=source)
    except Exception as exc:
        LOG.warn(f"accrochage du connecteur impossible : {exc}")
        return None


def snap_terminals(terminals, names=None, folders=None, mesh=None, offset=0.0,
                   search_radius=400.0, oriented=True, logger=None):
    """
    Applique l'accrochage aux deux bouts de chaque cable.

    `terminals` : [(depart, arrivee), ...] ; `names` : [(nom, nom), ...],
    les noms des pieces cliquees dans CATIA (facultatifs).

    Rend (terminaux corriges, poses) ou `poses` decrit, pour chaque bout,
    la face retenue et la direction de sortie -- de quoi poser le connecteur
    et tracer l'amorce droite.
    """
    log = logger or LOG
    out, rapport = [], []
    for k, (a, b) in enumerate(terminals):
        na, nb = (names[k] if names and k < len(names) else (None, None))
        a = np.asarray(a, float).reshape(3)
        b = np.asarray(b, float).reshape(3)

        # Chaque connecteur est accroche en visant l'AUTRE extremite : c'est
        # la direction dans laquelle le harnais part.
        sa = snap_terminal(a, b, name=na, folders=folders, mesh=mesh,
                           offset=offset, search_radius=search_radius,
                           oriented=oriented)
        sb = snap_terminal(b, a, name=nb, folders=folders, mesh=mesh,
                           offset=offset, search_radius=search_radius,
                           oriented=oriented)
        pa = sa.point if sa else a
        pb = sb.point if sb else b
        out.append((tuple(float(v) for v in pa), tuple(float(v) for v in pb)))
        rapport.append((sa, sb))

        for bout, snap in (("depart", sa), ("arrivee", sb)):
            if snap is None:
                continue
            log.info(f"cable {k + 1} / {bout} : face {snap.face_name} "
                     f"({snap.face_area:.0f} mm2), point deplace de "
                     f"{snap.moved:.0f} mm [{snap.source}]")
    return out, rapport


# Nom de modele donne aux poses deduites d'une boite englobante. Il doit
# etre NON VIDE : `core.connectors` ignore une pose sans modele, et c'est
# justement l'amorce droite qu'on veut imposer ici.
MODELE_FACE = "face"


def poses_from_snaps(rapport, model=MODELE_FACE):
    """
    Poses de connecteurs deduites de l'accrochage.

    La direction du cable est la normale SORTANTE de la face retenue : le
    toron sort droit du connecteur, comme sur un montage reel.
    """
    from .connectors import ConnectorPose

    poses = []
    for sa, sb in rapport:
        pair = []
        for snap in (sa, sb):
            if snap is None:
                pair.append(None)
                continue
            pair.append(ConnectorPose(
                model=model,
                point=tuple(float(v) for v in snap.point),
                direction=tuple(float(v) for v in snap.direction),
                flip=False, roll=0.0))
        poses.append(tuple(pair))
    return poses


def merge_poses(poses, rapport, model=MODELE_FACE):
    """
    Combine les poses choisies dans l'interface et l'accrochage geometrique.

    Le modele de connecteur, son sens et son roulis restent ceux que
    l'utilisateur a choisis ; le POINT et la DIRECTION viennent de la face
    retenue sur la boite englobante -- c'est la mesure, pas une preference.
    """
    from .connectors import ConnectorPose

    sortie = []
    for k, (sa, sb) in enumerate(rapport or []):
        base = poses[k] if poses and k < len(poses) else (None, None)
        pair = []
        for pose, snap in zip(base, (sa, sb)):
            if snap is None:
                pair.append(pose)
                continue
            pair.append(ConnectorPose(
                model=(pose.model if pose is not None and pose.model else model),
                point=tuple(float(v) for v in snap.point),
                direction=tuple(float(v) for v in snap.direction),
                flip=bool(pose.flip) if pose is not None else False,
                roll=float(pose.roll) if pose is not None else 0.0))
        sortie.append(tuple(pair))
    return sortie


def feasible_lead_in(poses, collider, clearance=0.0, wanted=40.0, floor=15.0,
                     steps=(1.0, 0.6, 0.35, 0.2, 0.1)):
    """
    Plus grande amorce droite realisable pour TOUS les connecteurs poses.

    L'amorce est un point impose au cheminement, a `L` millimetres du
    connecteur, dans l'axe de sortie. Rien ne garantit que ce point soit
    libre : une amorce de 500 mm plantee dans une cloison rend le probleme
    insoluble et fait echouer le calcul au lieu de l'aider.

    On essaie donc l'amorce demandee, puis des fractions decroissantes, et on
    garde la premiere qui degage tous les connecteurs. Rend 0 si aucune ne
    convient : le cheminement repart alors libre, ce qui reste preferable a
    un echec.
    """
    directions = []
    for pair in poses or []:
        for pose in (pair if isinstance(pair, (tuple, list)) else (pair,)):
            if pose is None:
                continue
            directions.append((np.asarray(pose.point, float).reshape(3),
                               np.asarray(pose.direction, float).reshape(3)))
    if not directions or collider is None:
        return float(wanted)

    for facteur in steps:
        L = float(wanted) * float(facteur)
        if L < float(floor):
            break
        pts = np.asarray([p + d * L for p, d in directions], float)
        try:
            if float(np.min(collider.distance(pts))) >= float(clearance):
                return L
        except Exception as exc:
            LOG.debug(f"amorce non verifiable ({exc}) : valeur demandee gardee")
            return float(wanted)
    return 0.0
