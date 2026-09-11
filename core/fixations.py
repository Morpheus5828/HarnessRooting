"""
Fixations et passages imposes (peignes, colliers, traversees de cloison).

Un "passage" est un couple de points (p_in -> p_out) que le toron doit
emprunter. Ces passages servent a deux choses dans l'algorithme :
  * ils creent un champ d'attraction (corridor) qui colle le faisceau au
    cheminement prevu par le bureau d'etudes plutot qu'a la ligne droite ;
  * ils fournissent les points d'ancrage exacts du lissage et les positions
    de clips.

Deux sources sont supportees :
  1. le module interne `core.fixation_scan` (detection Open3D des STL de
     colliers dans la scene), s'il est present sur le poste ;
  2. un fichier JSON ou CSV liste a la main / genere par un autre outil.
La seconde source permet d'utiliser la fonctionnalite sans Open3D.
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field

import numpy as np
from scipy.spatial import cKDTree

from ..log import Logger

LOG = Logger("fixations")


@dataclass
class Passage:
    p_in: tuple
    p_out: tuple
    name: str = ""
    fixation: str = ""      # piece d'origine : un peigne porte plusieurs passages
    index: int = 0          # numero de l'encoche dans cette piece

    @property
    def middle(self):
        return 0.5 * (np.asarray(self.p_in, float) + np.asarray(self.p_out, float))

    @property
    def group(self):
        return self.fixation or self.name


@dataclass
class ScanResult:
    """Meme interface que le resultat du scan Open3D interne."""
    ran: bool = False
    passages: list = field(default_factory=list)
    fixations: list = field(default_factory=list)
    source: str = ""
    detail: str = ""

    @property
    def n_passages(self):
        return len(self.passages)

    @property
    def n_fixations(self):
        return len(self.fixations) or len(self.passages)


def _as_xyz(value):
    arr = np.asarray(value, float).ravel()
    if arr.size != 3 or not np.all(np.isfinite(arr)):
        raise ValueError(f"point invalide : {value!r}")
    return tuple(float(v) for v in arr)


def load_passages_file(path) -> ScanResult:
    """
    Lit des passages depuis un fichier.
    """
    path = str(path)
    ext = os.path.splitext(path)[1].lower()
    items = []
    if ext == ".json":
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data = data.get("passages", [])
        for i, entry in enumerate(data):
            if isinstance(entry, dict):
                items.append(Passage(_as_xyz(entry["p_in"]), _as_xyz(entry["p_out"]),
                                     str(entry.get("name", f"passage_{i}"))))
            else:
                items.append(Passage(_as_xyz(entry[0]), _as_xyz(entry[1]),
                                     f"passage_{i}"))
    else:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            sample = fh.read(2048)
            fh.seek(0)
            delim = ";" if sample.count(";") >= sample.count(",") else ","
            for i, row in enumerate(csv.reader(fh, delimiter=delim)):
                row = [c.strip() for c in row if c.strip() != ""]
                if len(row) < 6:
                    continue
                try:
                    nums = [float(c.replace(",", ".")) for c in row[:6]]
                except ValueError:
                    continue                        # ligne d'en-tete
                name = row[6] if len(row) > 6 else f"passage_{i}"
                items.append(Passage(tuple(nums[:3]), tuple(nums[3:6]), name))

    if not items:
        raise ValueError(f"Aucun passage exploitable dans {os.path.basename(path)}.")
    LOG.ok(f"{len(items)} passage(s) charge(s) depuis {os.path.basename(path)}")
    return ScanResult(ran=True, passages=items, fixations=list(items),
                      source=path, detail=f"{len(items)} passage(s) lus dans le fichier")


def scan_fixations(clamps_folder, scene_path=None, scene_bounds=None,
                   on_progress=None, should_stop=None, collider=None,
                   **kw) -> ScanResult:
    """
    Detection des fixations existantes a partir d'un dossier de gabarits STL.
    """
    folder = str(clamps_folder or "")
    if not os.path.isdir(folder):
        return ScanResult(detail=f"Dossier de fixations introuvable : {folder}")

    # NETTOYAGE EFFECTUÉ : On passe directement par le script local fixation_scan.py
    from .fixation_scan import scan_folder

    try:
        raw = scan_folder(folder, scene_path=scene_path, scene_bounds=scene_bounds,
                          on_progress=on_progress, should_stop=should_stop,
                          collider=collider, **kw)
    except Exception as exc:
        LOG.exception("le scan des fixations a echoue", exc)
        return ScanResult(detail=f"Le scan a echoue : {type(exc).__name__} : {exc}")

    items = [Passage(tuple(d["p_in"]), tuple(d["p_out"]), d.get("name", ""),
                     d.get("fixation", ""), int(d.get("index", 0)))
             for d in raw.get("passages", [])]

    return ScanResult(ran=raw.get("ran", False), passages=items,
                      fixations=list(raw.get("fixations", [])), source=folder,
                      detail=raw.get("detail", ""))


def scan_with_open3d(scene_path, clamps_folder) -> ScanResult:
    return scan_fixations(clamps_folder, scene_path=scene_path)


def passages_to_arrays(passages):
    """(milieux, entrees+sorties) : les deux formes utilisees en aval."""
    mids, ends = [], []
    for c in passages or []:
        a = np.asarray(getattr(c, "p_in", None) if hasattr(c, "p_in") else c[0], float)
        b = np.asarray(getattr(c, "p_out", None) if hasattr(c, "p_out") else c[1], float)
        mids.append(0.5 * (a + b))
        ends.extend([a, b])
    return (np.array(mids) if mids else np.empty((0, 3)),
            np.array(ends) if ends else np.empty((0, 3)))


def write_passages_template(path, passages=None):
    """Ecrit un gabarit JSON, pour que l'utilisateur parte d'un exemple valide."""
    data = [dict(name=p.name, p_in=list(p.p_in), p_out=list(p.p_out))
            for p in (passages or [])]
    if not data:
        data = [dict(name="PEIGNE_01", p_in=[3600.0, 1000.0, 1500.0],
                     p_out=[3640.0, 1000.0, 1500.0])]
    with open(str(path), "w", encoding="utf-8") as fh:
        json.dump(dict(passages=data), fh, indent=2)
    return str(path)


# --------------------------------------------------------------------------
# passage OBLIGATOIRE par les fixations proches du cheminement
# --------------------------------------------------------------------------

def passage_span(passage) -> float:
    """Distance p_in -> p_out (largeur de la traversee), en mm."""
    a, b = _passage_points(passage)
    return float(np.linalg.norm(b - a))


def _passage_points(passage):
    a = np.asarray(getattr(passage, "p_in", None) if hasattr(passage, "p_in")
                   else passage[0], float)
    b = np.asarray(getattr(passage, "p_out", None) if hasattr(passage, "p_out")
                   else passage[1], float)
    return a, b


def filter_passages(passages, max_span, logger=None):
    """
    Ecarte les traversees trop larges pour etre un passage de cable.
    """
    kept, dropped = [], []
    for c in passages or []:
        (dropped if max_span and passage_span(c) > float(max_span)
         else kept).append(c)
    if dropped and logger is not None:
        names = sorted({str(getattr(c, "group", "") or getattr(c, "name", "?"))
                        for c in dropped})
        logger.warn(f"{len(dropped)} traversee(s) ecartee(s) : plus larges que "
                    f"{float(max_span):.0f} mm, ce ne sont pas des passages de "
                    f"cable ({', '.join(names[:6])}"
                    f"{', ...' if len(names) > 6 else ''})")
    return kept, dropped


def passages_near_route(grid, path, passages, radius, center_bias=0.5):
    """
    Encoches a emprunter par ce cable, dans l'ordre ou il les rencontre.
    Contient le double verrou de ligne droite et centrage forcé.
    """
    if not passages:
        return []
    world = grid.world_many(path)
    if len(world) < 2:
        return []
    tree = cKDTree(world)

    # Rayon très large pour capter toute la plaque sans rejeter de rangées
    search_radius = max(radius, 400.0)

    groupes = {}
    for k, c in enumerate(passages):
        mid = np.asarray(c.middle if hasattr(c, "middle")
                         else 0.5 * (np.asarray(c[0], float)
                                     + np.asarray(c[1], float)), float)
        d, i = tree.query(mid)

        if float(d) > search_radius:
            continue
        nom = getattr(c, "group", "") or f"__{k}"
        groupes.setdefault(nom, []).append(dict(passage=c, mid=mid,
                                                dist=float(d), pos=int(i)))
    if not groupes:
        return []

    # ordre de rencontre : le rang du point de trace le plus proche
    rails = sorted(groupes.values(),
                   key=lambda cands: min(c["pos"] for c in cands))

    for cands in rails:
        centre = np.mean([c["mid"] for c in cands], axis=0)
        # 1. On trouve la distance minimale au centre absolu pour ce peigne
        min_ecart = min(float(np.linalg.norm(c["mid"] - centre)) for c in cands)
        for c in cands:
            c["ecart_centre"] = float(np.linalg.norm(c["mid"] - centre))
            # 2. On marque l'encoche comme étant "le milieu"
            c["est_au_milieu"] = (c["ecart_centre"] <= min_ecart + 1.0)

    # --- chaine la plus courte : Bellman le long des peignes ---
    cout = []
    for c in rails[0]:
        base_cout = c["dist"] * 0.1
        # VERROU 1 : Mur infranchissable si ce n'est pas l'encoche du milieu
        if not c["est_au_milieu"]:
            base_cout += 10000.0
        cout.append(base_cout)

    venant = [[-1] * len(rails[0])]
    for j in range(1, len(rails)):
        prec, cur = rails[j - 1], rails[j]
        nouveau, source = [], []
        for c in cur:
            base_cout = c["dist"] * 0.1
            # VERROU 1 : Mur infranchissable
            if not c["est_au_milieu"]:
                base_cout += 10000.0

            liens = []
            for a, p in enumerate(prec):
                dist_link = float(np.linalg.norm(c["mid"] - p["mid"]))

                # VERROU 2 : Filer droit (même numéro d'encoche d'un peigne à l'autre)
                penalite_voie = 0.0
                if c["passage"].index != p["passage"].index:
                    penalite_voie = 2000.0

                liens.append(cout[a] + dist_link + penalite_voie)

            a = int(np.argmin(liens))
            nouveau.append(base_cout + liens[a])
            source.append(a)
        cout = nouveau
        venant.append(source)

    # remontee
    j = int(np.argmin(cout))
    choix = [None] * len(rails)
    for etage in range(len(rails) - 1, -1, -1):
        choix[etage] = rails[etage][j]
        j = venant[etage][j]

    return [(c["pos"], c["passage"], c["dist"]) for c in choix]


def oriented_passage(passage, tangent):
    """
    (premier point, second point) de la traversee, dans le sens du cheminement.
    """
    a, b = _passage_points(passage)
    if float(np.dot(b - a, np.asarray(tangent, float))) < 0.0:
        a, b = b, a
    return a, b


def passage_alignment(passage, tangent) -> float:
    """
    |cos| entre la traversee et la direction locale du cheminement.
    """
    a, b = _passage_points(passage)
    u, t = b - a, np.asarray(tangent, float)
    nu, nt = float(np.linalg.norm(u)), float(np.linalg.norm(t))
    if nu < 1e-9 or nt < 1e-9:
        return 1.0
    return abs(float(np.dot(u, t)) / (nu * nt))


def _local_tangent(world, i):
    """Direction du trace autour du point d'indice i (jamais nulle si possible)."""
    n = len(world)
    for span in (1, 2, 4, 8):
        j0, j1 = max(0, i - span), min(n - 1, i + span)
        t = world[j1] - world[j0]
        if float(np.linalg.norm(t)) > 1e-9:
            return t
    return np.array([1.0, 0.0, 0.0])


def mandatory_waypoints(grid, routes, cable_waypoints, passages, radius,
                        logger=None, max_span=None, max_detour=None,
                        align_cos=0.5, center_bias=0.5):
    """
    Reconstruit les listes de points de passage en y inserant les traversees imposees.
    """
    if max_span:
        passages, _ = filter_passages(passages, max_span, logger)

    new_wps = []
    report = dict(radius=radius, per_cable=[], total=0, skipped=0, reversed=0,
                  middle_only=0, too_far=0, max_detour=max_detour,
                  max_span=max_span, dropped=[])

    for k, path in enumerate(routes):
        base = list(cable_waypoints[k])
        world = grid.world_many(path)
        tree = cKDTree(world) if len(world) else None
        near = passages_near_route(grid, path, passages, radius,
                                   center_bias=center_bias)

        cands = []
        for order, (i, passage, dist) in enumerate(near):
            name = str(getattr(passage, "group", "")
                       or getattr(passage, "name", "") or f"fixation {i}")

            detour = 2.0 * float(dist)
            # Desactivation du rejet pour "detour" si on veut forcer l'inclusion absolue
            if max_detour is not None and detour > 2000.0:
                report["too_far"] += 1
                report["dropped"].append((name, detour))
                if logger is not None:
                    logger.warn(
                        f"cable {k + 1} : {name} n'est pas imposee, elle "
                        f"rallongerait le harnais d'environ {detour:.0f} mm "
                        f"(detour accepte : {float(max_detour):.0f} mm)")
                continue

            tangent = _local_tangent(world, i)
            a, b = oriented_passage(passage, tangent)
            if not np.allclose(a, _passage_points(passage)[0]):
                report["reversed"] += 1

            if np.allclose(a, b):
                targets = (a,)
            elif passage_alignment(passage, tangent) < float(align_cos):
                targets = (0.5 * (a + b),)
                report["middle_only"] += 1
            else:
                targets = (a, b)

            for rank, q in enumerate(targets):
                try:
                    node = grid.snap(q)
                except ValueError:
                    report["skipped"] += 1
                    continue
                pos = int(tree.query(np.asarray(q, float))[1]) if tree is not None \
                    else i
                cands.append((pos, order, rank, node))

        inserted = []
        for _pos, _order, _rank, node in sorted(cands, key=lambda t: t[:3]):
            if node in (base[0], base[-1]) or (inserted and node == inserted[-1]):
                continue
            inserted.append(node)
        new_wps.append([base[0]] + inserted + [base[-1]])
        report["per_cable"].append(len(inserted))
        report["total"] += len(inserted)

    if logger is not None:
        logger.info(f"fixations imposees : {report['total']} point(s) de passage "
                    f"({report['per_cable']}), {report['reversed']} traversee(s) "
                    f"reorientee(s), {report['middle_only']} par le milieu "
                    f"seulement, {report['too_far']} trop a l'ecart, "
                    f"{report['skipped']} inaccessible(s)")
    return new_wps, report
