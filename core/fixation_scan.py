"""
Detection des fixations existantes dans la maquette.

Portage du detecteur maison (`core/path_managment/fixation_detection.py`) :
les STL de colliers sont des GABARITS, pas des pieces deja positionnees. Il faut
donc les retrouver dans le maillage par recalage, puis en deduire les couples
(p_in, p_out) par lesquels le harnais doit passer.

Deux etages, dans cet ordre :

  1. **Recalage ICP (Open3D)** -- la methode d'origine, conservee telle quelle
     dans son principe : voxelisation multi-echelle, balayage de 24 rotations
     d'amorce en parallele, ICP grossier puis fin, seuil de correspondance
     stricte, et surtout *peeling* : les points de la scene deja expliques par
     un collier sont retires, ce qui evite qu'un second gabarit se recale au
     meme endroit.

  2. **Detection geometrique des trous traversants** -- repli lorsque Open3D
     n'est pas installe, et utilise aussi pour compter automatiquement les
     encoches d'un peigne quand son nom n'est pas dans la table.

Les couples (p_in, p_out) viennent de `subdiviser_peigne_n_butees`, reprise du
code d'origine : la boite englobante orientee du gabarit est decoupee en
`n_butees` encoches le long de son axe le plus long, et le passage se fait le
long de son axe le plus court.
"""

from __future__ import annotations

import glob
import json
import os
import time

import numpy as np
from scipy import ndimage
from scipy.spatial import cKDTree

from ..log import Logger
from ..pvutil import surface
from .collider import MeshCollider

LOG = Logger("scan-fixations")

# --- reglages du recalage, valeurs du detecteur d'origine ---
VOXEL_FINE = 0.5
VOXEL_COARSE = 2.0
FITNESS_MIN = 0.45          # au-dela, la piece est consideree presente
PEEL_RADIUS = 2.0          # rayon de retrait des points expliques
ICP_COARSE_ITERS = 20
ICP_FINE_ITERS = 100

# 24 rotations d'amorce (un gabarit peut etre pose dans n'importe quel sens)
SEED_ROTATIONS = [
    (0, 0, 0), (90, 0, 0), (180, 0, 0), (270, 0, 0),
    (0, 90, 0), (90, 90, 0), (180, 90, 0), (270, 90, 0),
    (0, 270, 0), (90, 270, 0), (180, 270, 0), (270, 270, 0),
    (0, 0, 90), (90, 0, 90), (180, 0, 90), (270, 0, 90),
    (0, 0, 270), (90, 0, 270), (180, 0, 270), (270, 0, 270),
    (0, 180, 90), (0, 180, 270), (90, 180, 90), (90, 180, 270),
]

# Nombre d'encoches par reference, repris du detecteur d'origine. Surchargeable
# par un fichier "peignes.json" depose dans le dossier des colliers.
DEFAULT_COMB_RULES = {"XA453420": 13}
COMB_PREFIX = "X"          # un nom commencant par X designe un peigne


# ==========================================================================
# 1. couples (p_in, p_out) : decoupe de la boite englobante orientee
# ==========================================================================

def subdiviser_peigne_n_butees(obb_center, obb_extent, obb_R, transform_globale,
                               n_butees, side_ratio=0.85):
    """
    Couples (p_in, p_out) d'un peigne a `n_butees` encoches.

    Reprise du calcul d'origine : l'axe le plus LONG de la boite englobante
    porte la repartition des encoches, l'axe MOYEN sert au decalage vers la
    face ou passent les cables, l'axe le plus COURT est la direction de
    traversee. Le resultat est ramene dans le repere de la maquette par la
    transformation issue du recalage.
    """
    extent = np.asarray(obb_extent, float)
    R = np.asarray(obb_R, float)
    axes_tries = np.argsort(extent)
    idx_longueur, idx_decalage, idx_passage = axes_tries[2], axes_tries[1], axes_tries[0]

    vec_longueur = R[:, idx_longueur]
    vec_decalage = R[:, idx_decalage]
    vec_passage = R[:, idx_passage]

    n_butees = max(1, int(n_butees))
    step = extent[idx_longueur] / n_butees
    start_pos = -(extent[idx_longueur] / 2.0) + (step / 2.0)

    T = np.asarray(transform_globale, float)
    R_glob, T_glob = T[:3, :3], T[:3, 3]

    passages = []
    for i in range(n_butees):
        decalage_l = start_pos + i * step
        decalage_d = -(extent[idx_decalage] / 2.0) * side_ratio
        centre_local = (np.asarray(obb_center, float)
                        + decalage_l * vec_longueur + decalage_d * vec_decalage)
        p_in_local = centre_local - (extent[idx_passage] / 2.0) * vec_passage
        p_out_local = centre_local + (extent[idx_passage] / 2.0) * vec_passage
        passages.append(dict(
            p_in=tuple(float(v) for v in (R_glob @ p_in_local) + T_glob),
            p_out=tuple(float(v) for v in (R_glob @ p_out_local) + T_glob),
            axis=tuple(float(v) for v in (R_glob @ vec_passage)),
            radius=float(0.5 * min(extent[idx_decalage], step)),
        ))
    return passages


# --------------------------------------------------------------------------
# encoches d'un peigne : axes CAO et vides dans la matiere
# --------------------------------------------------------------------------
#
# Portage de `core/agent/tool.py` du depot HarnessOpt (fonctions
# `extraire_geometrie_pure_clamp` et `placer_lignes_dans_encoches_clamp`),
# qui est la reference sur ce point.
#
# La decoupe de la boite englobante (`subdiviser_peigne_n_butees`) ne cherche
# pas les encoches : elle tranche l'encombrement en parts egales et traverse
# selon la plus petite dimension. Les segments obtenus ne sont paralleles ni
# aux dents, ni aux passages reels, et leur nombre est un reglage code en dur
# plutot que ce que porte la piece.
#
# La bonne methode lit la MATIERE du gabarit :
#
#   1. les axes de la CAO sortent de la covariance des NORMALES ponderees par
#      l'aire, C = somme(a . n n^T). Les faces d'une piece usinee sont
#      paralleles a ses plans : leurs normales donnent des axes exacts, la ou
#      une analyse des sommets se laisse tirer par la repartition de matiere ;
#   2. l'axe le plus etendu porte la repartition des encoches, le plus court
#      est le dos de la piece, le troisieme est la hauteur des DENTS ;
#   3. dans la tranche haute (les 60 % superieurs), un histogramme le long de
#      l'axe long fait apparaitre les vides entre les dents : ce sont les
#      encoches ;
#   4. chaque passage est un segment PARALLELE AUX DENTS, centre sur le vide,
#      haut de 90 % de la dent, place 2 mm DEVANT la face du peigne.
#
# C'est bien l'alignement attendu : le cable se pose dans l'encoche en
# glissant le long des dents, il ne traverse pas la piece.

NOTCH_BINS = 200          # finesse de l'histogramme des vides
NOTCH_MIN_WIDTH = 0.01    # largeur mini d'un vide, en fraction de la longueur
NOTCH_TOP_BAND = 0.4      # part haute de la piece ou l'on cherche les dents
NOTCH_HEIGHT = 0.90       # hauteur du segment, en fraction de la dent
NOTCH_OFFSET = 2.0        # mm devant la face du peigne


def _mesh_arrays(path):
    """Sommets et triangles d'un fichier, quel que soit le lecteur disponible."""
    try:
        import open3d as o3d

        mesh = o3d.io.read_triangle_mesh(str(path))
        v = np.asarray(mesh.vertices, float)
        f = np.asarray(mesh.triangles, int)
        if len(v) and len(f):
            return v, f
    except Exception:
        pass
    import pyvista as pv

    mesh = surface(pv.read(str(path)))
    faces = mesh.faces.reshape(-1, 4)[:, 1:]
    return np.asarray(mesh.points, float), np.asarray(faces, int)


def _surface_points(vertices, faces, step):
    """
    Nuage couvrant la SURFACE, au pas demande.

    L'histogramme des vides doit voir la matiere, pas les sommets : un STL a
    faces planes n'a de points qu'aux coins, et le milieu d'une dent paraitrait
    vide. On seme donc chaque triangle a pas constant, en barycentrique.
    """
    v = np.asarray(vertices, float)
    f = np.asarray(faces, int)
    step = max(float(step), 1e-6)
    a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    cotes = np.maximum.reduce([np.linalg.norm(b - a, axis=1),
                               np.linalg.norm(c - b, axis=1),
                               np.linalg.norm(a - c, axis=1)])
    out = [v]
    for n in range(1, int(np.clip(np.ceil(cotes.max() / step), 1, 24)) + 1):
        sel = cotes > n * step                # triangles encore trop grands
        if not np.any(sel):
            break
        u = np.linspace(0.0, 1.0, n + 2)[1:-1]
        for i in u:
            for j in u:
                if i + j < 1.0:
                    out.append(a[sel] + i * (b[sel] - a[sel]) + j * (c[sel] - a[sel]))
    pts = np.vstack(out)
    return pts if len(pts) <= 400000 else pts[:: int(np.ceil(len(pts) / 400000))]


def comb_geometry(vertices, faces, n_slices=16):
    """
    Axes de la piece et centres des encoches, lus dans la matiere.

    Retourne dict(vec_long, vec_haut, vec_dos, centres, h_center, h_segment,
    d_place, ...) dans le repere du gabarit, ou None si la piece n'a pas de
    niveau peigne. `vec_haut` pointe vers le sommet des dents, et `centres`
    donne l'abscisse du MILIEU DE CHAQUE DENT le long de l'axe long : c'est la
    que le cable se tient, contre la dent qui le retient.

    La reference regarde les 40 % superieurs de la piece. Une fixation reelle
    n'est pas qu'un peigne : elle porte une equerre, des bossages, des vis.
    L'equerre est souvent la plus haute ET couvre toute la longueur : elle
    remplit la tranche, les vides entre les dents disparaissent, et il ne
    reste que les bords de la piece.

    On cherche donc le NIVEAU PEIGNE, en balayant la piece dans les deux axes
    transverses : la bonne tranche est celle ou la matiere ALTERNE le long de
    l'axe long, regulierement, et sans etre ni pleine ni vide. C'est la
    signature d'une rangee de dents, et rien d'autre dans une fixation ne la
    presente.
    """
    v = np.asarray(vertices, float).reshape(-1, 3)
    f = np.asarray(faces, int).reshape(-1, 3)
    if len(v) < 4 or len(f) < 4:
        return None

    a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    cross = np.cross(b - a, c - a)
    areas = 0.5 * np.linalg.norm(cross, axis=1)
    keep = areas > 1e-12
    if not np.any(keep):
        return None
    normals = cross[keep] / (2.0 * areas[keep])[:, None]
    areas = areas[keep]

    # axes de la CAO : covariance des normales ponderees par l'aire
    C = np.einsum("i,ij,ik->jk", areas, normals, normals)
    _, eigenvectors = np.linalg.eigh(C)
    R = eigenvectors

    proj = v @ R
    extents = proj.max(axis=0) - proj.min(axis=0)
    idx_long = int(np.argmax(extents))
    autres = [k for k in range(3) if k != idx_long]
    vec_long = R[:, idx_long]

    coords_L = v @ vec_long
    L_min, L_max = float(coords_L.min()), float(coords_L.max())
    if L_max - L_min < 1e-9:
        return None

    # l'histogramme porte sur la surface semee au pas d'un casier
    dense = _surface_points(v, f, (L_max - L_min) / NOTCH_BINS)
    dense_L = dense @ vec_long

    meilleur = None
    for idx_h, idx_d in ((autres[0], autres[1]), (autres[1], autres[0])):
        H = dense @ R[:, idx_h]
        D = dense @ R[:, idx_d]
        h0, h1 = float(H.min()), float(H.max())
        d0, d1 = float(D.min()), float(D.max())
        if h1 - h0 < 1e-9:
            continue
        # Trois lectures selon l'epaisseur : toute la piece, sa moitie avant,
        # sa moitie arriere. C'est ce qui permet de voir les dents malgre une
        # equerre montee derriere, qui masquerait tout dans une lecture
        # d'ensemble.
        tranches_d = ((d0, d1), (d0, 0.5 * (d0 + d1)), (0.5 * (d0 + d1), d1))
        pas = (h1 - h0) / n_slices
        for dlo, dhi in tranches_d:
            dans_d = (D >= dlo) & (D <= dhi)
            notes = []
            for k in range(n_slices - 1):
                lo, hi = h0 + pas * k, h0 + pas * (k + 2)
                sel = dans_d & (H >= lo) & (H <= hi)
                notes.append(_comb_score(dense_L[sel], L_min, L_max))
            # Des dents se dressent SUR quelque chose : il existe forcement
            # une tranche pleine -- la semelle. Un motif qui se retrouve a
            # toutes les hauteurs n'est pas une rangee de dents, c'est une
            # fente traversante vue dans l'epaisseur : l'axe candidat est
            # alors le dos de la piece, pas la hauteur des dents.
            peignees = sum(1 for n in notes
                           if n is not None and len(n["centres"]) >= 2)
            if peignees >= 0.9 * len(notes):
                continue
            for k, note in enumerate(notes):
                if note is None:
                    continue
                # Une rangee de dents a de la HAUTEUR : son motif se retrouve
                # dans la tranche voisine. Un motif qui n'apparait qu'une fois
                # est un artefact de surface -- une tranche qui frole une face,
                # ou le semis qui laisse des trous la ou il y a de la matiere.
                voisins = [n for n in (notes[k - 1] if k else None,
                                       notes[k + 1] if k + 1 < len(notes) else None)
                           if n is not None]
                if not voisins:
                    continue
                confirme = min(len(note["centres"]),
                               max(len(n["centres"]) for n in voisins))
                if confirme < 2:
                    continue
                # HAUTEUR du motif : de combien de tranches consecutives la
                # meme rangee se retrouve-t-elle ? Vue dans l'epaisseur, une
                # dent ne persiste que sur sa largeur ; vue dans sa hauteur,
                # elle persiste sur toute sa longueur. C'est ce qui distingue
                # l'axe des dents de celui du dos quand les deux montrent le
                # meme motif.
                minimum = max(2, int(0.6 * confirme))
                j = k
                while j > 0 and notes[j - 1] is not None \
                        and len(notes[j - 1]["centres"]) >= minimum:
                    j -= 1
                m = k
                while m + 1 < len(notes) and notes[m + 1] is not None \
                        and len(notes[m + 1]["centres"]) >= minimum:
                    m += 1
                persistance = (m - j + 2) * pas
                score = (confirme, persistance, -note["regularite"])
                if meilleur is None or score > meilleur["score"]:
                    meilleur = dict(idx_h=idx_h, idx_d=idx_d, k=k,
                                    dlo=dlo, dhi=dhi, h0=h0, h1=h1, pas=pas,
                                    H=H, D=D, centres=note["centres"],
                                    regularite=note["regularite"], score=score)
    if meilleur is None:
        return None

    vec_haut, vec_dos = R[:, meilleur["idx_h"]], R[:, meilleur["idx_d"]]
    H, D, pas = meilleur["H"], meilleur["D"], meilleur["pas"]
    dans_d = (D >= meilleur["dlo"]) & (D <= meilleur["dhi"])

    # --- etendue des dents : les tranches voisines qui restent peignees ---
    seuil = max(2, int(0.6 * len(meilleur["centres"])))
    k_lo = k_hi = meilleur["k"]
    while k_lo > 0:
        lo = meilleur["h0"] + pas * (k_lo - 1)
        sel = dans_d & (H >= lo) & (H <= lo + 2 * pas)
        note = _comb_score(dense_L[sel], L_min, L_max)
        if note is None or len(note["centres"]) < seuil:
            break
        k_lo -= 1
    while k_hi < n_slices - 2:
        lo = meilleur["h0"] + pas * (k_hi + 1)
        sel = dans_d & (H >= lo) & (H <= lo + 2 * pas)
        note = _comb_score(dense_L[sel], L_min, L_max)
        if note is None or len(note["centres"]) < seuil:
            break
        k_hi += 1
    dents_lo = meilleur["h0"] + pas * k_lo
    dents_hi = meilleur["h0"] + pas * (k_hi + 2)

    # les dents pointent vers l'exterieur : du corps de la piece vers elles
    if 0.5 * (dents_lo + dents_hi) < 0.5 * (meilleur["h0"] + meilleur["h1"]):
        vec_haut = -vec_haut
        dents_lo, dents_hi = -dents_hi, -dents_lo
        H = -H

    # La face qui recoit le cable est celle des DENTS, du cote oppose au corps
    # de la piece : une equerre montee derriere ne doit pas deplacer le
    # segment devant elle.
    dents = dans_d & (H >= dents_lo) & (H <= dents_hi)
    coords_D = (dense[dents] if np.any(dents) else dense) @ vec_dos
    d_lo, d_hi = float(coords_D.min()), float(coords_D.max())
    milieu_piece = float(np.mean(dense @ vec_dos))
    if 0.5 * (d_lo + d_hi) >= milieu_piece:
        d_place, sens = d_hi + NOTCH_OFFSET, 1.0
    else:
        d_place, sens = d_lo - NOTCH_OFFSET, -1.0

    # --- chaque segment doit etre EN FACE d'un pic ---
    centres = _snap_sur_les_pics(dense[dents] if np.any(dents) else dense,
                                 vec_long, vec_dos, meilleur["centres"],
                                 L_min, L_max, sens)

    return dict(vec_long=vec_long, vec_haut=vec_haut, vec_dos=vec_dos,
                centres=centres,
                h_center=0.5 * (dents_lo + dents_hi),
                h_segment=float(dents_hi - dents_lo) * NOTCH_HEIGHT,
                d_place=float(d_place), d_min=d_lo, d_max=d_hi,
                sens_dos=sens, regularite=meilleur["regularite"])


def _snap_sur_les_pics(points, vec_long, vec_dos, centres, L_min, L_max,
                       sens, fenetre=0.45):
    """
    Recale chaque centre sur le PIC qui lui fait face.

    Un pic est ce qui avance le plus vers le cable : entre deux pics, la
    matiere se retire vers le fond de la piece. On mesure donc, casier par
    casier, jusqu'ou la matiere avance, et l'on glisse chaque centre sur le
    maximum d'avancee de son voisinage -- au plus d'une demi-periode, pour ne
    pas confondre deux pics voisins.

    Quand la piece n'avance nulle part -- une face plane, un peigne dont les
    dents affleurent -- le profil est plat et rien ne bouge : les centres
    trouves dans la matiere restent les bons.
    """
    if len(centres) < 2 or not len(points):
        return centres
    L = points @ vec_long
    D = (points @ vec_dos) * sens          # croissant vers le cable
    casier = np.clip(((L - L_min) / max(L_max - L_min, 1e-9) * NOTCH_BINS)
                     .astype(int), 0, NOTCH_BINS - 1)
    avancee = np.full(NOTCH_BINS, -np.inf)
    np.maximum.at(avancee, casier, -D)     # -D : plus c'est grand, plus c'est devant
    vus = np.isfinite(avancee)
    if vus.sum() < NOTCH_BINS // 4:
        return centres
    relief = float(avancee[vus].max() - np.median(avancee[vus]))
    periode = float(np.mean(np.diff(centres)))
    demi = max(1, int(fenetre * periode / (L_max - L_min) * NOTCH_BINS))
    if relief < 1e-6:
        return centres                     # rien n'avance : profil plat

    recales = []
    for c in centres:
        i = int(np.clip((c - L_min) / max(L_max - L_min, 1e-9) * NOTCH_BINS,
                        0, NOTCH_BINS - 1))
        lo, hi = max(0, i - demi), min(NOTCH_BINS, i + demi + 1)
        fen = avancee[lo:hi]
        if not np.any(np.isfinite(fen)):
            recales.append(c)
            continue
        sommet = float(np.nanmax(np.where(np.isfinite(fen), fen, np.nan)))
        # milieu du plateau : un pic cylindrique avance pareil sur sa largeur
        plateau = np.where(np.isfinite(fen) & (fen >= sommet - 0.05 * relief))[0]
        j = lo + 0.5 * (plateau[0] + plateau[-1])
        recales.append(L_min + ((j + 0.5) / NOTCH_BINS) * (L_max - L_min))
    return np.asarray(recales, float)


def _comb_score(coords_L, L_min, L_max):
    """
    Cette tranche ressemble-t-elle a une rangee de dents ?

    Une rangee de dents alterne matiere et vide REGULIEREMENT, et n'est ni
    pleine (une equerre, une semelle) ni presque vide (une tranche qui frole
    la surface, ou le semis laisse des trous qui n'existent pas). Retourne
    None quand la tranche ne ressemble a rien de tel.
    """
    if len(coords_L) < NOTCH_BINS // 4:
        return None
    hist, _ = np.histogram(coords_L, bins=NOTCH_BINS, range=(L_min, L_max))
    matiere = hist > 0
    remplissage = float(matiere.mean())
    if not 0.04 <= remplissage <= 0.98:
        return None

    # Les DENTS sont les blocs de matiere de cette tranche : c'est sur leur
    # centre que le passage doit tomber. Une dent est etroite ; un bloc qui
    # occupe le quart de la longueur est une semelle ou une equerre, pas une
    # dent.
    centres, largeurs, dans_dent, debut = [], [], False, 0
    for i in range(NOTCH_BINS + 1):
        pleine = matiere[i] if i < NOTCH_BINS else False
        if pleine and not dans_dent:
            dans_dent, debut = True, i
        elif not pleine and dans_dent:
            dans_dent = False
            largeur = i - debut
            if NOTCH_BINS * NOTCH_MIN_WIDTH <= largeur <= NOTCH_BINS * 0.25:
                centres.append(L_min + ((debut + largeur / 2.0) / NOTCH_BINS)
                               * (L_max - L_min))
                largeurs.append(largeur)
    if len(centres) < 2:
        return None

    # Une rangee de dents est REGULIERE, deux fois : ses dents sont egalement
    # espacees, et elles ont la meme largeur. C'est ce qui la separe des
    # accidents de surface d'une equerre ou d'un bossage, qui donnent eux
    # aussi une alternance, mais quelconque.
    ecarts = np.diff(centres)
    regularite = float(np.std(ecarts) / max(np.mean(ecarts), 1e-9))
    dispersion = float(np.std(largeurs) / max(np.mean(largeurs), 1e-9))
    if regularite > 0.20 or dispersion > 0.35:
        return None
    # a nombre de dents egal, la tranche la plus reguliere gagne
    return dict(centres=centres, regularite=regularite,
                score=len(centres) - regularite)


def notch_passages(vertices, faces, transform=None, offset=NOTCH_OFFSET):
    """
    Passages d'un peigne : un segment par DENT, parallele aux dents.

    Le segment est aligne sur le milieu de la dent, haut de 90 % de celle-ci,
    et pose
    `offset` mm devant la face du peigne -- la ou le cable vient se loger. Il
    est ensuite ramene dans le repere de la maquette par la transformation du
    recalage.
    """
    geo = comb_geometry(vertices, faces)
    if geo is None:
        return []
    T = np.eye(4) if transform is None else np.asarray(transform, float)
    R_glob, T_glob = T[:3, :3], T[:3, 3]

    vec_L, vec_H, vec_D = geo["vec_long"], geo["vec_haut"], geo["vec_dos"]
    d_place = geo["d_place"] + (float(offset) - NOTCH_OFFSET) * geo["sens_dos"]
    demi = 0.5 * geo["h_segment"]
    centres = geo["centres"]
    pas = (min(abs(b - a) for a, b in zip(centres[:-1], centres[1:]))
           if len(centres) > 1 else geo["h_segment"])

    axe_global = R_glob @ vec_H
    passages = []
    for pos_L in centres:
        centre = pos_L * vec_L + geo["h_center"] * vec_H + d_place * vec_D
        p_in = (R_glob @ (centre - demi * vec_H)) + T_glob
        p_out = (R_glob @ (centre + demi * vec_H)) + T_glob
        passages.append(dict(
            p_in=tuple(float(x) for x in p_in),
            p_out=tuple(float(x) for x in p_out),
            axis=tuple(float(x) for x in axe_global),
            radius=float(0.5 * min(pas, geo["h_segment"])),
        ))
    return passages


def refine_clamp_passages(clamps, logger=LOG, offset=NOTCH_OFFSET):
    """
    Recalcule les passages des peignes sur la geometrie reelle des encoches.

    Travaille sur les dictionnaires rendus par le detecteur -- ceux qui
    portent `file_path` et `transform` -- et remplace leurs `routing_points`.
    Un gabarit illisible garde les points du detecteur : approximatifs vaut
    mieux qu'absents.

    Retourne le nombre de peignes recalcules.
    """
    refaits = 0
    for clamp in clamps or []:
        path = clamp.get("file_path") or clamp.get("path")
        T = clamp.get("transform")
        if not path or T is None or not clamp.get("routing_points"):
            continue
        name = str(clamp.get("name", "?"))
        try:
            v, f = _mesh_arrays(path)
            found = notch_passages(v, f, T, offset=offset)
        except Exception as exc:
            if logger:
                logger.warn(f"{name} : encoches non recalculees "
                            f"({type(exc).__name__} : {exc})")
            continue
        if not found:
            if logger:
                logger.warn(f"{name} : aucune encoche trouvee dans la matiere, "
                            f"les points du detecteur sont conserves")
            continue
        avant = len(clamp["routing_points"]) // 2
        clamp["routing_points"] = [list(p[k]) for p in found
                                   for k in ("p_in", "p_out")]
        refaits += 1
        if logger:
            logger.info(f"{name} : {len(found)} encoche(s) trouvee(s) dans la "
                        f"matiere (le detecteur en annoncait {avant})")
    return refaits


def check_passages_clear(passages, collider, logger=LOG):
    """
    Controle que les segments poses devant les peignes ne percent rien.

    Ils sont censes etre DEVANT la piece : si l'un traverse de la matiere,
    c'est que le recalage ou l'orientation est fausse. On le signale plutot
    que de le corriger en douce.
    """
    if collider is None or not passages:
        return 0
    fautifs = 0
    for c in passages:
        a = np.asarray(c["p_in"] if isinstance(c, dict) else c.p_in, float)
        b = np.asarray(c["p_out"] if isinstance(c, dict) else c.p_out, float)
        if float(np.linalg.norm(b - a)) < 1e-6:
            continue
        if collider.crosses(a, b):
            fautifs += 1
    if fautifs and logger:
        logger.warn(f"{fautifs} passage(s) sur {len(passages)} traversent la "
                    f"matiere : verifier le recalage de ces fixations")
    return fautifs


def _obb_of(path):
    """Boite englobante orientee du gabarit, dans son repere d'origine."""
    import open3d as o3d

    mesh = o3d.io.read_triangle_mesh(path)
    if len(mesh.vertices) == 0:
        return None
    obb = mesh.get_oriented_bounding_box()
    return dict(center=np.asarray(obb.center, float),
                extent=np.asarray(obb.extent, float),
                R=np.asarray(obb.R, float),
                mesh_center=np.asarray(mesh.get_center(), float))


# ==========================================================================
# 2. recalage ICP (detecteur d'origine)
# ==========================================================================

def preprocess_geometry(mesh, voxel_size):
    """Voxelise le nuage des sommets et estime ses normales."""
    import open3d as o3d

    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(np.asarray(mesh.vertices))
    pcd_down = pcd.voxel_down_sample(voxel_size)
    pcd_down.estimate_normals(
        o3d.geometry.KDTreeSearchParamHybrid(radius=voxel_size * 2.5, max_nn=40))
    pcd_down.orient_normals_towards_camera_location(
        pcd_down.get_center() + np.array([0.0, 0.0, 10000.0]))
    return pcd_down


def _test_single_rotation(rx, ry, rz, clamp_pcd_coarse, center, env_pcd_coarse,
                          distance_threshold):
    """Teste UNE rotation d'amorce par un ICP grossier."""
    import open3d as o3d

    pcd_rot = o3d.geometry.PointCloud()
    pcd_rot.points = o3d.utility.Vector3dVector(np.asarray(clamp_pcd_coarse.points))
    pcd_rot.normals = o3d.utility.Vector3dVector(np.asarray(clamp_pcd_coarse.normals))

    T_init = np.eye(4)
    if rx or ry or rz:
        R = pcd_rot.get_rotation_matrix_from_xyz(
            (np.radians(rx), np.radians(ry), np.radians(rz)))
        T_init[:3, :3] = R
        T_init[:3, 3] = center - R @ center
        pcd_rot.transform(T_init)

    try:
        icp = o3d.pipelines.registration.registration_icp(
            pcd_rot, env_pcd_coarse, distance_threshold, np.eye(4),
            o3d.pipelines.registration.TransformationEstimationPointToPlane(),
            o3d.pipelines.registration.ICPConvergenceCriteria(
                max_iteration=ICP_COARSE_ITERS))
        return icp.fitness, icp.transformation @ T_init
    except RuntimeError:
        return -1.0, np.eye(4)


def check_and_peel(clamp_path, env_pcd_fine, env_pcd_coarse, voxel_fine,
                   voxel_coarse, fitness_min=FITNESS_MIN, max_workers=None):
    """
    Recale un gabarit dans la scene, puis retire les points expliques.

    Le *peeling* est essentiel : sans lui, deux references voisines se recalent
    sur la meme piece et la meme fixation est comptee deux fois.
    """
    import open3d as o3d
    from concurrent.futures import ThreadPoolExecutor

    clamp_mesh = o3d.io.read_triangle_mesh(clamp_path)
    if len(clamp_mesh.vertices) == 0:
        return dict(present=False, fitness=0.0, transform=np.eye(4),
                    env_fine=env_pcd_fine, env_coarse=env_pcd_coarse, center=None)

    clamp_pcd_fine = preprocess_geometry(clamp_mesh, voxel_fine)
    clamp_pcd_coarse = clamp_pcd_fine.voxel_down_sample(voxel_coarse)
    if len(clamp_pcd_coarse.points) < 5:
        return dict(present=False, fitness=0.0, transform=np.eye(4),
                    env_fine=env_pcd_fine, env_coarse=env_pcd_coarse,
                    center=np.asarray(clamp_mesh.get_center(), float))

    center = clamp_pcd_coarse.get_center()
    threshold_coarse = voxel_coarse * 3.0

    best_fit, best_T = 0.0, np.eye(4)
    workers = max_workers or (os.cpu_count() or 8)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_test_single_rotation, rx, ry, rz,
                                   clamp_pcd_coarse, center, env_pcd_coarse,
                                   threshold_coarse)
                   for rx, ry, rz in SEED_ROTATIONS]
        for future in futures:
            fit, T = future.result()
            if fit > best_fit:
                best_fit, best_T = fit, T

    fine_best = o3d.geometry.PointCloud()
    fine_best.points = o3d.utility.Vector3dVector(np.asarray(clamp_pcd_fine.points))
    fine_best.normals = o3d.utility.Vector3dVector(np.asarray(clamp_pcd_fine.normals))
    fine_best.transform(best_T)

    try:
        icp_fine = o3d.pipelines.registration.registration_icp(
            fine_best, env_pcd_fine, voxel_fine * 3.0, np.eye(4),
            o3d.pipelines.registration.TransformationEstimationPointToPoint(),
            o3d.pipelines.registration.ICPConvergenceCriteria(
                max_iteration=ICP_FINE_ITERS))
        strict = o3d.pipelines.registration.evaluate_registration(
            fine_best, env_pcd_fine, 2.0, icp_fine.transformation)
        final_T = icp_fine.transformation @ best_T
        fitness = strict.fitness
    except RuntimeError:
        final_T, fitness = np.eye(4), 0.0

    present = fitness >= fitness_min
    if present:
        placed = o3d.geometry.PointCloud()
        placed.points = o3d.utility.Vector3dVector(np.asarray(clamp_pcd_fine.points))
        placed.transform(final_T)
        env_pts = np.asarray(env_pcd_fine.points)
        # Retrait de TOUS les points de scene situes dans le rayon, et non du
        # seul plus proche de chaque point du gabarit : quand la scene est plus
        # finement maillee que le gabarit, la version au plus proche voisin
        # laissait assez de matiere pour qu'une seconde reference identique se
        # recale au meme endroit et soit comptee deux fois.
        groups = cKDTree(env_pts).query_ball_point(np.asarray(placed.points),
                                                   r=PEEL_RADIUS)
        to_remove = set()
        for g in groups:
            to_remove.update(g)
        keep = sorted(set(range(len(env_pts))) - to_remove)
        LOG.debug(f"peeling : {len(to_remove)} point(s) de scene retires "
                  f"sur {len(env_pts)}")
        env_pcd_fine = env_pcd_fine.select_by_index(keep)
        env_pcd_coarse = env_pcd_fine.voxel_down_sample(voxel_coarse)

    return dict(present=present, fitness=float(fitness), transform=final_T,
                env_fine=env_pcd_fine, env_coarse=env_pcd_coarse,
                center=np.asarray(clamp_mesh.get_center(), float))


def open3d_available():
    """Open3D est-il installe ? Sans lui, aucun recalage n'est possible."""
    import importlib.util
    return importlib.util.find_spec("open3d") is not None


# ==========================================================================
# 3. comptage des encoches
# ==========================================================================

def load_comb_rules(clamps_folder):
    """
    Table "reference -> nombre d'encoches".

    Le detecteur d'origine codait 13 encoches pour XA453420. La table reste
    surchargeable par un fichier `peignes.json` depose dans le dossier des
    colliers, pour ne pas avoir a modifier le code a chaque nouvelle reference.
    """
    rules = dict(DEFAULT_COMB_RULES)
    path = os.path.join(str(clamps_folder or ""), "peignes.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                user = json.load(fh)
            rules.update({str(k).upper(): int(v) for k, v in user.items()})
            LOG.info(f"{len(user)} regle(s) d'encoches lues dans peignes.json")
        except Exception as exc:
            LOG.warn(f"peignes.json illisible ({type(exc).__name__}) : "
                     "table par defaut conservee")
    return rules


def notch_count(name, clamp_path, rules, auto=True):
    """
    (nombre d'encoches, explication, est-ce un peigne).

    Regle du detecteur d'origine : seules les references dont le nom commence
    par "X" sont des peignes et donnent des couples entree/sortie ; les autres
    sont des colliers simples, reduits a un point de passage unique. La table
    `peignes.json` permet de fixer le nombre d'encoches d'une reference connue,
    faute de quoi il est compte geometriquement.
    """
    upper = os.path.splitext(os.path.basename(name))[0].upper()
    for key, n in rules.items():
        if key.upper() in upper:
            return int(n), f"table ({key})", True
    if not upper.startswith(COMB_PREFIX):
        return 1, "collier simple", False
    if auto:
        try:
            import pyvista as pv
            holes = count_through_holes(pv.read(clamp_path))
            if holes > 0:
                return holes, "comptage geometrique", True
        except Exception as exc:
            LOG.debug(f"comptage automatique impossible pour {name} "
                      f"({type(exc).__name__})")
    return 1, "peigne, nombre d'encoches inconnu", True


# ==========================================================================
# 4. detection geometrique des trous (repli, et comptage des encoches)
# ==========================================================================

MIN_HOLE_RADIUS = 2.5
MAX_HOLE_RADIUS = 120.0
GRID = 42
MAX_CELLS_FOR_SCAN = 4000


def _lighten(surf):
    """Allege la piece : le cout du lancer de rayons suit le nombre de triangles."""
    if surf.n_cells <= MAX_CELLS_FOR_SCAN:
        return surf
    try:
        light = surf.decimate(1.0 - MAX_CELLS_FOR_SCAN / float(surf.n_cells))
        if light.n_cells >= 4:
            return light
    except Exception as exc:
        LOG.debug(f"decimation impossible ({type(exc).__name__})")
    return surf


def _pca_axes(points):
    c = points.mean(axis=0)
    centred = points - c
    axes = np.linalg.svd(centred, full_matrices=False)[2]
    extents = [float(np.ptp(centred @ a)) for a in axes]
    order = np.argsort(extents)[::-1]
    return c, axes[order]


def _through_holes(collider, centre, axis, ea, eb, span_a, ext_b, ext_c,
                   grid=GRID, pad=0.05):
    """Trous traversants vus le long de `axis` : (u, v, rayon, aire)."""
    lo_b, hi_b = ext_b
    lo_c, hi_c = ext_c
    mb = pad * (hi_b - lo_b) + 1e-6
    mc = pad * (hi_c - lo_c) + 1e-6
    us = np.linspace(lo_b - mb, hi_b + mb, grid)
    vs = np.linspace(lo_c - mc, hi_c + mc, grid)
    t0, t1 = span_a
    half = 0.05 * (t1 - t0) + 1.0

    free = np.zeros((grid, grid), bool)
    for i, u in enumerate(us):
        base = centre + u * ea
        for j, v in enumerate(vs):
            p = base + v * eb
            free[i, j] = not collider.crosses(p + (t0 - half) * axis,
                                              p + (t1 + half) * axis)
    if not free.any():
        return []
    lab, n = ndimage.label(free)
    if n == 0:
        return []
    border = set(lab[0, :]) | set(lab[-1, :]) | set(lab[:, 0]) | set(lab[:, -1])
    border.discard(0)

    cell = (us[1] - us[0]) * (vs[1] - vs[0])
    out = []
    for label in range(1, n + 1):
        if label in border:
            continue                        # zone libre ouverte : l'exterieur
        mask = lab == label
        radius = float(np.sqrt(float(mask.sum()) * cell / np.pi))
        if not (MIN_HOLE_RADIUS <= radius <= MAX_HOLE_RADIUS):
            continue
        ii, jj = np.nonzero(mask)
        out.append((float(us[ii].mean()), float(vs[jj].mean()), radius,
                    float(mask.sum()) * cell))
    return out


def _best_hole_axis(mesh, name="", grid=GRID):
    surf = _lighten(surface(mesh))
    pts = np.asarray(surf.points, float)
    if len(pts) < 4:
        return None
    collider = MeshCollider.from_mesh(surf, name=name)
    centre, axes = _pca_axes(pts)
    local = (pts - centre) @ axes.T

    best = None
    for k in range(3):
        others = [i for i in range(3) if i != k]
        holes = _through_holes(
            collider, centre, axes[k], axes[others[0]], axes[others[1]],
            (float(local[:, k].min()), float(local[:, k].max())),
            (float(local[:, others[0]].min()), float(local[:, others[0]].max())),
            (float(local[:, others[1]].min()), float(local[:, others[1]].max())),
            grid=grid)
        score = sum(h[3] for h in holes)
        if holes and (best is None or score > best["score"]):
            best = dict(score=score, axis=axes[k], ea=axes[others[0]],
                        eb=axes[others[1]],
                        span=(float(local[:, k].min()), float(local[:, k].max())),
                        holes=holes, centre=centre)
    return best


def count_through_holes(mesh, grid=GRID):
    best = _best_hole_axis(mesh, grid=grid)
    return len(best["holes"]) if best else 0


def passages_from_mesh(mesh, name="", grid=GRID):
    """
    Passages d'une piece DEJA POSITIONNEE, par detection de ses trous.

    Sert de repli quand Open3D n'est pas installe : dans ce cas les STL doivent
    porter leurs coordonnees reelles, puisqu'aucun recalage n'est possible.
    """
    best = _best_hole_axis(mesh, name=name, grid=grid)
    if best is None:
        return []
    axis, ea, eb, centre = best["axis"], best["ea"], best["eb"], best["centre"]
    t0, t1 = best["span"]
    out = []
    for idx, (u, v, radius, _a) in enumerate(best["holes"]):
        base = centre + u * ea + v * eb
        out.append(dict(p_in=tuple(float(x) for x in base + t0 * axis),
                        p_out=tuple(float(x) for x in base + t1 * axis),
                        axis=tuple(float(x) for x in axis),
                        radius=radius,
                        name=f"{name}_{idx}" if len(best["holes"]) > 1 else name,
                        fixation=name, index=idx))
    return out


# ==========================================================================
# 5. orchestration
# ==========================================================================

def list_clamp_files(folder):
    return sorted(set(glob.glob(os.path.join(str(folder), "*.stl"))
                      + glob.glob(os.path.join(str(folder), "*.STL"))))


def _scan_icp(files, scene_path, rules, voxel_fine, fitness_min,
              on_progress, should_stop):
    """Recalage de chaque gabarit dans la scene, avec retrait progressif."""
    import open3d as o3d

    o3d.utility.set_verbosity_level(o3d.utility.VerbosityLevel.Error)
    LOG.info(f"recalage Open3D : voxel fin {voxel_fine} mm, "
             f"voxel grossier {VOXEL_COARSE} mm, seuil {fitness_min:.2f}")
    env_mesh = o3d.io.read_triangle_mesh(str(scene_path))
    if len(env_mesh.vertices) == 0:
        raise ValueError(f"Maillage de scene vide ou illisible : {scene_path}")
    env_fine = preprocess_geometry(env_mesh, voxel_fine)
    env_coarse = env_fine.voxel_down_sample(VOXEL_COARSE)
    LOG.info(f"scene voxelisee : {len(env_fine.points)} points fins, "
             f"{len(env_coarse.points)} grossiers")

    passages, fixations = [], []
    n_absent = 0
    for i, path in enumerate(files):
        if should_stop is not None and should_stop():
            LOG.warn("scan interrompu par l'utilisateur")
            break
        name = os.path.splitext(os.path.basename(path))[0]
        if on_progress:
            on_progress(i, len(files), name)

        res = check_and_peel(path, env_fine, env_coarse, voxel_fine,
                             VOXEL_COARSE, fitness_min=fitness_min)
        env_fine, env_coarse = res["env_fine"], res["env_coarse"]
        if not res["present"]:
            n_absent += 1
            LOG.debug(f"{name} : absent (correspondance "
                      f"{100 * res['fitness']:.0f} %)")
            continue

        obb = _obb_of(path)
        if obb is None:
            continue
        n_notch, why, is_comb = notch_count(name, path, rules)
        if is_comb:
            # Les encoches se lisent dans la MATIERE du gabarit : le segment
            # est alors parallele aux dents et pose devant la piece. La
            # decoupe de la boite englobante ne sert que de repli.
            try:
                found = notch_passages(*_mesh_arrays(path), res["transform"])
            except Exception as exc:
                LOG.warn(f"{name} : encoches illisibles ({type(exc).__name__}), "
                         f"repli sur la decoupe de la boite englobante")
                found = []
            if found:
                why = f"{len(found)} encoche(s) trouvee(s) dans la matiere"
            else:
                LOG.warn(f"{name} : aucune encoche dans la matiere, repli sur "
                         f"la decoupe de la boite englobante")
                found = subdiviser_peigne_n_butees(
                    obb["center"], obb["extent"], obb["R"], res["transform"],
                    n_notch)
        else:
            # collier simple : un point de passage unique, son centre recale.
            # La decoupe OBB est reservee aux peignes, comme dans le detecteur
            # d'origine : appliquee a un collier rond, elle placerait le passage
            # sur le bord de la piece et non dans son oeil.
            T = np.asarray(res["transform"], float)
            centre = tuple(float(v) for v in
                           (T[:3, :3] @ obb["mesh_center"]) + T[:3, 3])
            found = [dict(p_in=centre, p_out=centre,
                          axis=(0.0, 0.0, 0.0),
                          radius=float(0.5 * min(obb["extent"])))]
        for idx, d in enumerate(found):
            d["name"] = f"{name}_{idx}" if len(found) > 1 else name
            d["fixation"] = name
            d["index"] = idx
        passages.extend(found)
        fixations.append(dict(name=name, path=path, n_passages=len(found),
                              score=res["fitness"],
                              transform=np.asarray(res["transform"]).tolist()))
        LOG.info(f"{name} : present ({100 * res['fitness']:.0f} %), "
                 f"{len(found)} passage(s) -- {why}")
    return passages, fixations, dict(absent=n_absent)


def _scan_positioned(files, scene_bounds, on_progress, should_stop, rules=None):
    """Repli sans Open3D : les STL doivent porter leurs coordonnees reelles."""
    import pyvista as pv

    passages, fixations = [], []
    n_solid = n_unplaced = n_failed = 0
    for i, path in enumerate(files):
        if should_stop is not None and should_stop():
            LOG.warn("scan interrompu par l'utilisateur")
            break
        name = os.path.splitext(os.path.basename(path))[0]
        if on_progress:
            on_progress(i, len(files), name)
        try:
            mesh = pv.read(path)
        except Exception as exc:
            LOG.warn(f"{name} illisible ({type(exc).__name__})")
            n_failed += 1
            continue
        if mesh.n_points < 4:
            n_failed += 1
            continue
        if scene_bounds is not None and not _inside(mesh.center, scene_bounds):
            n_unplaced += 1
            continue
        # Un peigne deja positionne se lit comme au recalage, transformation
        # identite : ses encoches sont dans sa matiere, pas dans ses trous.
        found = []
        if notch_count(name, path, rules or {})[2]:
            try:
                found = notch_passages(*_mesh_arrays(path), None)
            except Exception as exc:
                LOG.warn(f"{name} : encoches illisibles ({type(exc).__name__})")
            for idx, d in enumerate(found):
                d["name"] = f"{name}_{idx}" if len(found) > 1 else name
                d["fixation"] = name
                d["index"] = idx
        if not found:
            found = passages_from_mesh(mesh, name=name)
        if not found:
            n_solid += 1
            continue
        passages.extend(found)
        fixations.append(dict(name=name, path=path, n_passages=len(found)))
    return passages, fixations, dict(solid=n_solid, unplaced=n_unplaced,
                                     failed=n_failed)


def _inside(centre, bounds, margin=0.02):
    c = np.asarray(centre, float)
    x0, x1, y0, y1, z0, z1 = bounds
    m = margin * max(x1 - x0, y1 - y0, z1 - z0, 1.0)
    return (x0 - m <= c[0] <= x1 + m and y0 - m <= c[1] <= y1 + m
            and z0 - m <= c[2] <= z1 + m)


def scan_folder(clamps_folder, scene_path=None, scene_bounds=None,
                voxel_fine=VOXEL_FINE, fitness_min=FITNESS_MIN,
                on_progress=None, should_stop=None, use_icp=None,
                collider=None, refine_axis=True):
    """
    Analyse un dossier de gabarits de colliers.

    Avec Open3D et une scene, chaque gabarit est RECALE dans le maillage
    (methode d'origine). Sans Open3D, on se rabat sur la detection des trous
    des pieces deja positionnees, et on le signale.
    """
    folder = str(clamps_folder or "")
    files = list_clamp_files(folder)
    if not files:
        return dict(ran=False, passages=[], fixations=[], mode="aucun",
                    detail=f"Aucun fichier .stl dans {folder}")

    rules = load_comb_rules(folder)
    icp = open3d_available() and scene_path is not None if use_icp is None \
        else bool(use_icp)

    t0 = time.time()
    try:
        if icp:
            passages, fixations, stats = _scan_icp(
                files, scene_path, rules, voxel_fine, fitness_min,
                on_progress, should_stop)
            mode = "recalage ICP"
        else:
            passages, fixations, stats = _scan_positioned(
                files, scene_bounds, on_progress, should_stop, rules)
            mode = "geometrique (pieces deja positionnees)"
    except Exception as exc:
        LOG.exception("le scan a echoue", exc)
        return dict(ran=False, passages=[], fixations=[], mode="echec",
                    detail=f"Le scan a echoue : {type(exc).__name__} : {exc}")

    # Les segments poses devant les peignes ne doivent rien percer : c'est le
    # controle qui revele un recalage douteux.
    if refine_axis and passages and collider is not None:
        check_passages_clear(passages, collider)

    seconds = time.time() - t0
    detail = (f"{len(passages)} passage(s) sur {len(fixations)} fixation(s), "
              f"{len(files)} gabarit(s) analyses en {seconds:.1f} s "
              f"[{mode}]")
    notes = []
    if stats.get("absent"):
        notes.append(f"{stats['absent']} gabarit(s) absent(s) de la maquette")
    if stats.get("solid"):
        notes.append(f"{stats['solid']} piece(s) sans trou traversant")
    if stats.get("unplaced"):
        notes.append(f"{stats['unplaced']} piece(s) hors maquette")
    if stats.get("failed"):
        notes.append(f"{stats['failed']} fichier(s) illisible(s)")
    if not icp and open3d_available() is False:
        notes.append("Open3D absent : aucun recalage possible, les STL doivent "
                     "porter leurs coordonnees reelles")
    if notes:
        detail += " -- " + ", ".join(notes)

    (LOG.ok if passages else LOG.warn)(detail)
    return dict(ran=bool(passages), passages=passages, fixations=fixations,
                mode=mode, detail=detail, seconds=seconds, **stats)


# Nom historique du point d'entree, conserve pour l'outillage existant.
def run_detection_for_agent(scene_path, clamps_folder, voxel_fine=VOXEL_FINE):
    res = scan_folder(clamps_folder, scene_path=scene_path, voxel_fine=voxel_fine)
    return res["fixations"]
