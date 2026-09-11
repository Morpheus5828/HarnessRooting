"""
Pose des crabes : les fixations que le projet ajoute lui-meme.

Un harnais ne tient pas tout seul entre deux fixations existantes. Quand le
cheminement parcourt une longue distance sans rien rencontrer, il faut poser
une attache -- un *crabe* -- sur la structure. Ce module dit ou, et verifie
que c'est possible.

Portage de `core/agent/tool.py` du depot HarnessOpt (`evaluate_crabe_alignment`,
`is_crabe_clash_free`, `place_crabes_greedy`, `compute_crabes`), adapte au
collider exact de ce projet : la ou la reference interroge une requete de
proximite trimesh, on lit ici la distance et son gradient, qui donnent le
meme couple (point le plus proche, normale de la surface).

Les quatre regles, dans l'ordre ou elles s'appliquent :

  1. **l'embase est strictement parallele a la structure**. Le crabe n'est pas
     pose sur un point mais sur une SURFACE : les quatre coins de son embase
     doivent trouver la meme normale et le meme niveau que son centre. Une
     arete, un conge, une cloison qui tourne : la pose est refusee ;
  2. **le cable est droit** sous le crabe, sur la longueur de l'embase. On ne
     pince pas un toron dans un virage ;
  3. **pas de crabe la ou une fixation existe deja**. A moins de 30 mm d'un
     couple (p_in, p_out) scanne, on ne pose rien -- et le compteur de
     distance repart de la, puisque le harnais y est deja tenu ;
  4. **un crabe des que 250 mm ont ete parcourus** sans le moindre ancrage,
     au premier point qui satisfait les regles precedentes.

Et le corps du crabe ne doit penetrer la structure nulle part : ses sommets
sont controles un a un contre le maillage.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np

from ..log import Logger

LOG = Logger("crabes")

# Valeurs de la reference (core/agent/config.py du depot HarnessOpt), sauf
# les deux premieres : la consigne est que l'embase soit STRICTEMENT parallele
# a la structure. A 0,85 (32 degres) et 6 mm, un crabe se posait sur un tube de
# 40 mm de rayon ; a 0,985 (10 degres) et 1,5 mm, il lui faut un vrai plat. Une
# peau de fuselage, elle, passe largement : sur une embase de 28 mm, un rayon
# de 1 m ne devie que de 0,1 mm et 0,8 degre.
NORMAL_COS = 0.985         # |cos| mini entre la normale des coins et du centre
SURFACE_TOL = 1.5          # mm : denivele admis sous l'embase
STRAIGHT_TOL = 6.0         # mm : ecart admis a la droite, sous l'embase
MIN_SPACING = 250.0        # mm : distance maxi sans aucun ancrage
CLASH_TOL = 1.5            # mm : penetration admise du corps dans la structure
ANCHOR_RADIUS = 30.0       # mm : rayon d'exclusion autour d'une fixation


@dataclass
class CrabModel:
    """
    Le crabe, ramene dans son repere de pose.

    Le fichier est tourne de sorte que sa plus grande face -- son embase --
    regarde -Z, puis translate pour que cette embase soit en z = 0 et centree
    en x et y. Le crabe pose se lit alors directement :  x le long du cable,
    y en travers, z vers le haut depuis la structure.
    """
    path: str = ""
    name: str = ""
    dx: float = 12.0                # demi-longueur de l'embase (le long du cable)
    dy: float = 12.0                # demi-largeur
    height: float = 20.0
    vertices: np.ndarray = field(default_factory=lambda: np.empty((0, 3)))
    faces: np.ndarray = field(default_factory=lambda: np.empty((0, 3), int))
    check_points: np.ndarray = field(default_factory=lambda: np.empty((0, 3)))

    def label(self):
        return (f"{self.name} (embase {2 * self.dx:.0f} x {2 * self.dy:.0f} mm, "
                f"hauteur {self.height:.0f} mm)")


@dataclass
class Crab:
    """Un crabe pose : son assise sur la structure et son repere."""
    index: int = 0
    arc_mm: float = 0.0
    position: tuple = (0.0, 0.0, 0.0)        # point du cable
    seat: tuple = (0.0, 0.0, 0.0)            # point d'appui sur la structure
    x_axis: tuple = (1.0, 0.0, 0.0)          # le long du cable
    y_axis: tuple = (0.0, 1.0, 0.0)
    normal: tuple = (0.0, 0.0, 1.0)          # de la structure vers le cable
    tilt_deg: float = 0.0                    # defaut de parallelisme de l'embase
    clearance: float = 0.0                   # hauteur du cable au-dessus de l'appui

    def matrix(self) -> np.ndarray:
        """Transformation 4x4 qui pose le modele sur la structure."""
        M = np.eye(4)
        M[:3, 0] = self.x_axis
        M[:3, 1] = self.y_axis
        M[:3, 2] = self.normal
        M[:3, 3] = self.seat
        return M


# ==========================================================================
# 1. le modele
# ==========================================================================

def load_crab(path, max_check_points=400) -> CrabModel:
    """
    Lit le STL du crabe et le ramene dans son repere de pose.

    L'embase est la face qui porte la plus grande AIRE cumulee -- pas la plus
    grande facette : une embase est souvent triangulee en dizaines de
    morceaux, quand un flanc peut n'en compter qu'un.
    """
    import pyvista as pv

    from ..pvutil import surface

    mesh = surface(pv.read(str(path)))
    faces = np.asarray(mesh.faces, int).reshape(-1, 4)[:, 1:]
    v = np.asarray(mesh.points, float)
    if len(v) < 4 or len(faces) < 4:
        raise ValueError(f"{os.path.basename(str(path))} : maillage vide.")

    a, b, c = v[faces[:, 0]], v[faces[:, 1]], v[faces[:, 2]]
    cross = np.cross(b - a, c - a)
    aires = 0.5 * np.linalg.norm(cross, axis=1)
    bonnes = aires > 1e-12
    normales = cross[bonnes] / (2.0 * aires[bonnes])[:, None]
    aires = aires[bonnes]

    arrondies = np.round(normales, 4)
    uniques, inverse = np.unique(arrondies, axis=0, return_inverse=True)
    cumul = np.zeros(len(uniques))
    np.add.at(cumul, inverse, aires)
    base = uniques[int(np.argmax(cumul))]

    from .connectors import rotation_between

    R = rotation_between(base, np.array([0.0, 0.0, -1.0]))
    verts = v @ R.T
    lo, hi = verts.min(axis=0), verts.max(axis=0)
    verts = verts - np.array([0.5 * (lo[0] + hi[0]), 0.5 * (lo[1] + hi[1]), lo[2]])
    lo, hi = verts.min(axis=0), verts.max(axis=0)

    check = verts
    if len(check) > max_check_points:
        rng = np.random.default_rng(0)
        check = check[rng.choice(len(check), max_check_points, replace=False)]

    model = CrabModel(path=str(path), name=os.path.splitext(os.path.basename(
        str(path)))[0], dx=float(0.5 * (hi[0] - lo[0])),
        dy=float(0.5 * (hi[1] - lo[1])), height=float(hi[2]),
        vertices=verts, faces=faces, check_points=np.asarray(check, float))
    LOG.ok(f"crabe charge : {model.label()}")
    return model


def default_model(dx=14.0, dy=10.0, height=18.0) -> CrabModel:
    """
    Crabe de substitution, quand aucun STL n'est fourni.

    Un pave : de quoi poser, controler et dessiner quelque chose de juste en
    dimensions, en attendant le vrai modele.
    """
    xs, ys, zs = (-dx, dx), (-dy, dy), (0.0, height)
    verts = np.array([[x, y, z] for x in xs for y in ys for z in zs], float)
    faces = np.array([[0, 1, 3], [0, 3, 2], [4, 6, 7], [4, 7, 5],
                      [0, 4, 5], [0, 5, 1], [2, 3, 7], [2, 7, 6],
                      [0, 2, 6], [0, 6, 4], [1, 5, 7], [1, 7, 3]], int)
    return CrabModel(name="crabe (pave par defaut)", dx=dx, dy=dy, height=height,
                     vertices=verts, faces=faces, check_points=verts)


# ==========================================================================
# 2. ou peut-on poser ?
# ==========================================================================

def _unit(v):
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    return np.divide(v, np.where(n < 1e-12, 1.0, n))


def seat_frames(points, collider, mask=None, distances=None):
    """
    Assise de chaque point du cable : appui, normale et repere local.

    `normal` va de la structure vers le cable, `x_axis` suit le cable projete
    sur la surface -- c'est ce qui aligne le crabe SUR le harnais -- et
    `y_axis` complete le triedre.

    `mask` restreint le calcul de la normale aux points ou un crabe a une
    chance d'aller. Chaque normale coute un appel VTK ; sur un harnais qui
    traverse une soute, la quasi-totalite des points est hors de portee de
    toute attache, et les calculer tous rallongeait le lissage d'autant.
    `distances` evite de redemander une distance deja mesuree.
    """
    pts = np.asarray(points, float).reshape(-1, 3)
    d = np.asarray(distances, float) if distances is not None \
        else collider.distance(pts)
    n = np.zeros_like(pts)
    n[:, 2] = 1.0                      # valeur d'attente, jamais retenue
    idx = np.arange(len(pts)) if mask is None else np.where(mask)[0]
    if len(idx):
        n[idx] = collider.gradient(pts[idx])
    seat = pts - n * d[:, None]

    t = np.zeros_like(pts)
    if len(pts) >= 3:
        t[1:-1] = pts[2:] - pts[:-2]
    if len(pts) >= 2:
        t[0] = pts[1] - pts[0]
        t[-1] = pts[-1] - pts[-2]
    t = _unit(t)
    x = _unit(t - np.einsum("ij,ij->i", t, n)[:, None] * n)
    y = _unit(np.cross(n, x))
    return dict(seat=seat, normal=n, x_axis=x, y_axis=y, clearance=d)


def eligible_points(points, collider, model, normal_cos=NORMAL_COS,
                    surface_tol=SURFACE_TOL, straight_tol=STRAIGHT_TOL,
                    max_clearance=None):
    """
    Ou l'embase du crabe est-elle strictement parallele a la structure, et le
    cable droit ?

    Retourne (eligible, frames, diag). `frames` porte l'assise et le repere,
    `diag` le detail des refus -- c'est ce qui permet de dire a l'utilisateur
    POURQUOI un troncon ne recoit aucun crabe.
    """
    pts = np.asarray(points, float).reshape(-1, 3)
    n_pts = len(pts)
    if max_clearance is None:
        # Un crabe tient le cable a SA hauteur : au-dela, il n'y a rien pour
        # combler, et le poser serait dessiner une attache qui ne touche pas.
        max_clearance = model.height + surface_tol

    # La hauteur se lit en une distance par point ; tout le reste -- normales,
    # quatre coins, droiture -- ne concerne que les points assez pres de la
    # structure pour qu'un crabe y atteigne. Sans ce tri, un harnais qui
    # traverse une soute payait dix appels VTK par point pour un refus certain,
    # a chaque palier de lissage.
    d0 = collider.distance(pts)
    proche = d0 <= max_clearance
    frames = seat_frames(pts, collider, mask=proche, distances=d0)
    if n_pts < 2 or not np.any(proche):
        return (np.zeros(n_pts, bool), frames,
                dict(points=n_pts, hauteur_ok=int(np.sum(proche)), plan_ok=0,
                     droit_ok=0, eligibles=0, tilt_deg=np.full(n_pts, 90.0)))

    n, x, y = frames["normal"], frames["x_axis"], frames["y_axis"]
    vus = np.where(proche)[0]

    # --- 1. l'embase repose a plat : les quatre coins voient la meme surface
    min_cos = np.full(n_pts, -1.0)
    max_dev = np.full(n_pts, np.inf)
    min_cos[vus] = 1.0
    max_dev[vus] = 0.0
    for coin in (x * model.dx, -x * model.dx, y * model.dy, -y * model.dy):
        q = pts[vus] + coin[vus]
        dq = collider.distance(q)
        nq = collider.gradient(q)
        min_cos[vus] = np.minimum(min_cos[vus],
                                  np.einsum("ij,ij->i", n[vus], nq))
        max_dev[vus] = np.maximum(max_dev[vus], np.abs(dq - d0[vus]))
    plan_ok = (min_cos >= normal_cos) & (max_dev < surface_tol) & proche

    # --- 2. le cable est droit sous l'embase
    seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    arc = np.concatenate([[0.0], np.cumsum(seg)])
    droit = np.zeros(n_pts, bool)
    for i in vus:
        a, b = arc[i] - model.dx, arc[i] + model.dx
        if a < 0.0 or b > arc[-1]:
            continue
        reel_a = np.array([np.interp(a, arc, pts[:, k]) for k in range(3)])
        reel_b = np.array([np.interp(b, arc, pts[:, k]) for k in range(3)])
        if (np.linalg.norm(reel_a - (pts[i] - x[i] * model.dx)) < straight_tol
                and np.linalg.norm(reel_b - (pts[i] + x[i] * model.dx))
                < straight_tol):
            droit[i] = True

    eligible = plan_ok & droit
    diag = dict(points=n_pts, hauteur_ok=int(np.sum(proche)),
                plan_ok=int(np.sum((min_cos >= normal_cos)
                                   & (max_dev < surface_tol))),
                droit_ok=int(np.sum(droit)), eligibles=int(np.sum(eligible)),
                tilt_deg=np.degrees(np.arccos(np.clip(min_cos, -1.0, 1.0))))
    return eligible, frames, diag


def clash_free(seat, x_axis, y_axis, normal, model, collider,
               tolerance=CLASH_TOL) -> bool:
    """Le corps du crabe reste-t-il hors de la matiere ?"""
    if model.check_points is None or not len(model.check_points):
        return True
    R = np.stack([x_axis, y_axis, normal], axis=1)
    monde = np.asarray(seat, float) + model.check_points @ R.T
    signee = collider.signed_distance(monde)
    return not bool(np.any(signee < -float(tolerance)))


def place_greedy(points, eligible, spacing=MIN_SPACING, anchors=None,
                 is_valid=None, anchor_radius=ANCHOR_RADIUS):
    """
    Choisit les points porteurs, en marchant le long du cable.

    Le depart compte comme un ancrage. Passer pres d'une fixation existante ne
    pose rien et fait repartir le compteur depuis cette fixation : le harnais y
    est deja tenu, donc les 250 mm se comptent a partir d'elle.
    """
    pts = np.asarray(points, float).reshape(-1, 3)
    seg = np.linalg.norm(np.diff(pts, axis=0), axis=1) if len(pts) > 1 \
        else np.zeros(0)
    arc = np.concatenate([[0.0], np.cumsum(seg)]) if len(seg) else np.zeros(len(pts))
    anchors = np.asarray(anchors, float).reshape(-1, 3) if anchors is not None \
        and len(anchors) else None

    # une fixation tient le harnais a son point de passage au plus pres, pas
    # au bord de sa zone d'exclusion : c'est de la que repart le compteur.
    arc_ancre = None
    if anchors is not None:
        d_ancre = np.linalg.norm(pts[:, None, :] - anchors[None, :, :], axis=2)
        proche = np.argmin(d_ancre, axis=0)
        arc_ancre = {int(k): float(arc[int(j)]) for k, j in enumerate(proche)}

    poses, dernier = [], 0.0
    for i in range(len(pts)):
        if anchors is not None:
            k = int(np.argmin(d_ancre[i]))
            if float(d_ancre[i, k]) < anchor_radius:
                # deja tenu ici : rien a poser, et le compteur repart de
                # l'ancrage lui-meme
                dernier = max(dernier, arc_ancre[k])
                continue
        if eligible[i] and (arc[i] - dernier) >= spacing:
            if is_valid is not None and not is_valid(i):
                continue
            poses.append(i)
            dernier = arc[i]
    return poses, arc


def compute_crabs(points, collider, model=None, spacing=MIN_SPACING,
                  anchors=None, normal_cos=NORMAL_COS, surface_tol=SURFACE_TOL,
                  straight_tol=STRAIGHT_TOL, clash_tol=CLASH_TOL,
                  max_clearance=None, logger=None):
    """
    Pose les crabes le long d'un cable et rend (crabes, eligibles, diagnostic).

    Le diagnostic compte les refus par cause : c'est lui qui explique une
    portion sans crabe -- structure trop loin, surface trop tourmentee, cable
    en virage, ou corps du crabe qui viendrait taper.
    """
    model = model or default_model()
    pts = np.asarray(points, float).reshape(-1, 3)
    if collider is None or len(pts) < 2:
        return [], np.zeros(len(pts), bool), dict(points=len(pts), eligibles=0)

    eligible, frames, diag = eligible_points(
        pts, collider, model, normal_cos=normal_cos, surface_tol=surface_tol,
        straight_tol=straight_tol, max_clearance=max_clearance)
    tilt = diag.pop("tilt_deg", np.zeros(len(pts)))

    refus = [0]

    def _ok(i):
        bon = clash_free(frames["seat"][i], frames["x_axis"][i],
                         frames["y_axis"][i], frames["normal"][i], model,
                         collider, tolerance=clash_tol)
        if not bon:
            refus[0] += 1
        return bon

    poses, arc = place_greedy(pts, eligible, spacing=spacing, anchors=anchors,
                              is_valid=_ok)
    diag["collisions"] = refus[0]
    diag["poses"] = len(poses)

    crabes = [Crab(index=int(i), arc_mm=float(arc[i]),
                   position=tuple(float(v) for v in pts[i]),
                   seat=tuple(float(v) for v in frames["seat"][i]),
                   x_axis=tuple(float(v) for v in frames["x_axis"][i]),
                   y_axis=tuple(float(v) for v in frames["y_axis"][i]),
                   normal=tuple(float(v) for v in frames["normal"][i]),
                   tilt_deg=float(tilt[i]),
                   clearance=float(frames["clearance"][i])) for i in poses]
    if logger:
        logger.info(f"crabes : {len(crabes)} pose(s) sur {diag['points']} points "
                    f"({diag['eligibles']} eligibles, {diag['collisions']} refus "
                    f"pour collision)")
    return crabes, eligible, diag


def worst_gap(points, crabs, anchors=None, spacing=MIN_SPACING):
    """
    Plus longue portion sans aucun ancrage, en millimetres.

    C'est le controle de la regle : au-dela de `spacing`, le harnais pend.
    Retourne (longueur, respectee).
    """
    pts = np.asarray(points, float).reshape(-1, 3)
    if len(pts) < 2:
        return 0.0, True
    seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    arc = np.concatenate([[0.0], np.cumsum(seg)])

    tenus = [0.0, float(arc[-1])]
    tenus.extend(float(c.arc_mm) for c in crabs)
    if anchors is not None and len(anchors):
        anchors = np.asarray(anchors, float).reshape(-1, 3)
        for a in anchors:
            d = np.linalg.norm(pts - a, axis=1)
            i = int(np.argmin(d))
            if float(d[i]) < ANCHOR_RADIUS:
                tenus.append(float(arc[i]))
    tenus = np.array(sorted(tenus))
    ecart = float(np.max(np.diff(tenus))) if len(tenus) > 1 else float(arc[-1])
    return ecart, ecart <= spacing * 1.001


def crab_meshes(crabs, model):
    """Maillages PyVista des crabes poses, pour l'affichage et l'export."""
    import pyvista as pv

    out = []
    if model is None or not len(model.vertices):
        return out
    faces = np.hstack([np.full((len(model.faces), 1), 3), model.faces]).ravel()
    for crabe in crabs:
        mesh = pv.PolyData(np.asarray(model.vertices, float), faces)
        mesh.transform(crabe.matrix(), inplace=True)
        out.append(mesh)
    return out


def crabs_to_rows(crabs):
    """Nomenclature des crabes : une ligne par pose, prete a exporter."""
    return [dict(index=c.index, arc_mm=round(c.arc_mm, 1),
                 x=round(c.position[0], 3), y=round(c.position[1], 3),
                 z=round(c.position[2], 3),
                 appui_x=round(c.seat[0], 3), appui_y=round(c.seat[1], 3),
                 appui_z=round(c.seat[2], 3),
                 hauteur=round(c.clearance, 2),
                 defaut_parallelisme_deg=round(c.tilt_deg, 2)) for c in crabs]
