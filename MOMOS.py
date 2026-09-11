"""
MOMOS -- MOniteur des MOntages et des Standards.

L'outil de controle des REGLES HS. Il est lance automatiquement a la fin de
chaque cheminement (Click & Root comme page 2) et fait trois choses :

1. il relit `ui/config.py` -- et rien d'autre : ce fichier EST le referentiel
   des regles. Changer une valeur la-bas change le controle ici, sans
   toucher a une ligne de code ;
2. il verifie chaque regle sur le harnais qui vient d'etre calcule, et note
   ou se trouvent les fautes (coordonnees maquette) ;
3. il ouvre une NOUVELLE FENETRE 3D dediee, ou l'integralite des regles HS
   est affichee -- vertes, oranges ou rouges -- et ou chaque violation est
   pointee sur le harnais.

Aucune dependance graphique n'est importee ici : la fenetre 3D est la
visionneuse de `viewer.py`, lancee dans un processus separe (VTK et Tkinter
ne partagent pas de boucle d'evenements).

Usage direct :
    python -m harnessopt.MOMOS rapport_momos.json     # rouvre la fenetre 3D
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from dataclasses import dataclass, field, asdict

import numpy as np

from .log import Logger

LOG = Logger("MOMOS")

# --------------------------------------------------------------------------
# etats d'une regle
# --------------------------------------------------------------------------
OK = "ok"           # regle respectee
WARN = "warn"       # regle respectee de justesse, ou regle de confort
KO = "ko"           # regle violee
NA = "na"           # regle non evaluable (donnee absente)

COULEURS = {OK: "#2E9E5B", WARN: "#E0A03B", KO: "#D2483F", NA: "#8A99A8"}
GLYPHES = {OK: "OK ", WARN: " ! ", KO: "KO ", NA: " - "}

# Familles, pour regrouper l'affichage.
SECURITE = "Securite"
MECANIQUE = "Mecanique"
MONTAGE = "Montage"
TOPOLOGIE = "Topologie"


# ==========================================================================
# 1. le referentiel : une regle = une ligne
# ==========================================================================

@dataclass
class Regle:
    """Description d'une regle HS, independamment de tout resultat."""
    code: str
    titre: str
    famille: str
    cle_config: str                  # nom du reglage dans ui/config.py
    unite: str = "mm"
    sens: str = ">="                 # ">=" : la valeur doit depasser la limite
    description: str = ""


REFERENTIEL = [
    Regle("HS-01", "Distance de securite a la structure", SECURITE,
          "SECURITY_DISTANCE", "mm", ">=",
          "Aucun point du toron ne s'approche de la structure a moins de la "
          "distance de securite."),
    Regle("HS-02", "Aucune traversee de structure", SECURITE,
          "-", "traversee(s)", "<=",
          "Le harnais ne passe jamais au travers d'une piece : c'est la seule "
          "faute qu'aucun reglage ne rattrape."),
    Regle("HS-03", "Rayon de cintrage minimal", MECANIQUE,
          "MINIMAL_BEND_RADIUS", "mm", ">=",
          "Un toron plie plus court que son rayon admissible casse ses "
          "conducteurs et ouvre ses blindages."),
    Regle("HS-04", "Amorce droite en sortie de connecteur", MONTAGE,
          "CONNECTOR_LEAD_IN", "mm", ">=",
          "Le cable sort DROIT du connecteur avant de pouvoir tourner : sans "
          "cela il force sur les contacts."),
    Regle("HS-05", "Espacement maximal des fixations", MONTAGE,
          "CLIP_SPACING", "mm", "<=",
          "Deux points de maintien consecutifs ne sont jamais plus eloignes "
          "que l'espacement admissible."),
    Regle("HS-06", "Longueur mini entre deux branchements", TOPOLOGIE,
          "L_MIN_BB", "mm", ">=",
          "Deux derivations trop rapprochees ne sont pas realisables en "
          "atelier."),
    Regle("HS-07", "Longueur mini terminal -> branchement", TOPOLOGIE,
          "L_MIN_TB", "mm", ">=",
          "Une derivation collee a un connecteur empeche le montage et la "
          "reprise du harnais."),
    Regle("HS-08", "Distance ideale a la structure", SECURITE,
          "IDEAL_DISTANCE_WITH_STRUCTURE", "mm", ">=",
          "Regle de confort : au-dela de la securite stricte, le toron doit "
          "rester a distance d'integration de la structure."),
    Regle("HS-09", "Diametre maximal d'un toron", MECANIQUE,
          "HS_MAX_BUNDLE_DIAMETER", "mm", "<=",
          "Un toron trop gros ne passe plus dans les fixations et ne se "
          "cintre plus."),
    Regle("HS-10", "Espacement minimal entre deux crabes", MONTAGE,
          "HS_MIN_CLIP_SPACING", "mm", ">=",
          "Deux colliers ne doivent pas se toucher : il faut la place de les "
          "poser et de les reprendre."),
    Regle("HS-11", "Remplissage des passages de fixation", MONTAGE,
          "HS_MAX_FILL_RATIO", "%", "<=",
          "Le toron ne remplit jamais completement l'oeil d'un collier."),
    Regle("HS-12", "Ecart a l'axe du connecteur", MONTAGE,
          "HS_CONNECTOR_AXIS_TOL", "mm", "<=",
          "La tete du cable reste dans l'axe du connecteur sur toute "
          "l'amorce."),
]

PAR_CODE = {r.code: r for r in REFERENTIEL}


# ==========================================================================
# 2. resultat d'un controle
# ==========================================================================

@dataclass
class Controle:
    """Verdict d'une regle sur un harnais donne."""
    code: str
    titre: str
    famille: str
    statut: str = NA
    valeur: float = float("nan")
    limite: float = float("nan")
    unite: str = "mm"
    sens: str = ">="
    detail: str = ""
    description: str = ""
    n_fautes: int = 0
    points: list = field(default_factory=list)     # ou regarder dans la 3D

    @property
    def couleur(self):
        return COULEURS.get(self.statut, COULEURS[NA])

    def ligne(self) -> str:
        """Une ligne de texte, telle qu'affichee dans la fenetre 3D."""
        if math.isnan(self.valeur):
            mesure = "non evaluee"
        else:
            mesure = f"{self.valeur:.1f} {self.unite}"
            if not math.isnan(self.limite):
                mesure += f"  ({self.sens} {self.limite:.1f} {self.unite})"
        faute = f"  -  {self.n_fautes} faute(s)" if self.n_fautes else ""
        return f"{GLYPHES.get(self.statut, ' - ')} {self.code}  {self.titre} : {mesure}{faute}"

    def as_dict(self):
        d = asdict(self)
        d["couleur"] = self.couleur
        return d


@dataclass
class Rapport:
    """L'ensemble des controles, plus de quoi rouvrir la fenetre 3D."""
    controles: list = field(default_factory=list)
    horodatage: str = ""
    titre: str = "MOMOS - controle des regles HS"
    scene_stl: str = ""
    harness_stl: str = ""
    polylines: list = field(default_factory=list)
    crabs: list = field(default_factory=list)
    terminals: list = field(default_factory=list)
    reglages: dict = field(default_factory=dict)

    # -- synthese ------------------------------------------------------
    @property
    def compte(self):
        c = {OK: 0, WARN: 0, KO: 0, NA: 0}
        for ctrl in self.controles:
            c[ctrl.statut] = c.get(ctrl.statut, 0) + 1
        return c

    @property
    def statut(self):
        c = self.compte
        if c[KO]:
            return KO
        if c[WARN]:
            return WARN
        return OK if c[OK] else NA

    @property
    def n_regles(self):
        return len(self.controles)

    def violations(self):
        return [c for c in self.controles if c.statut == KO]

    def resume(self) -> str:
        c = self.compte
        return (f"{c[OK]} regle(s) conforme(s), {c[WARN]} en limite, "
                f"{c[KO]} violee(s), {c[NA]} non evaluee(s)")

    def texte(self) -> str:
        """Rapport complet en texte, pour la console et le journal."""
        lignes = [f"MOMOS - controle des regles HS  ({self.horodatage})",
                  "=" * 72, self.resume(), ""]
        famille = None
        for ctrl in self.controles:
            if ctrl.famille != famille:
                famille = ctrl.famille
                lignes.append(f"-- {famille} " + "-" * max(0, 66 - len(famille)))
            lignes.append("  " + ctrl.ligne())
            if ctrl.detail:
                lignes.append(f"        {ctrl.detail}")
        return "\n".join(lignes)

    def as_dict(self):
        return dict(horodatage=self.horodatage, titre=self.titre,
                    statut=self.statut, resume=self.resume(),
                    compte=self.compte, reglages=self.reglages,
                    scene_stl=self.scene_stl, harness_stl=self.harness_stl,
                    polylines=self.polylines, crabs=self.crabs,
                    terminals=self.terminals,
                    controles=[c.as_dict() for c in self.controles])


# ==========================================================================
# 3. lecture du referentiel : ui/config.py
# ==========================================================================

DEFAUTS = dict(
    SECURITY_DISTANCE=10.0, IDEAL_DISTANCE_WITH_STRUCTURE=15.0,
    MINIMAL_BEND_RADIUS=48.0, CONNECTOR_LEAD_IN=40.0, CLIP_SPACING=250.0,
    L_MIN_BB=120.0, L_MIN_TB=80.0, CABLE_DIAMETER=6.0,
    HS_MAX_BUNDLE_DIAMETER=60.0, HS_MIN_CLIP_SPACING=80.0,
    HS_CONNECTOR_AXIS_TOL=5.0, HS_MAX_FILL_RATIO=0.85,
    MOMOS_ENABLED=True, MOMOS_AUTO_OPEN_3D=True, MOMOS_SHOW_STRUCTURE=True,
    MOMOS_MARKER_RADIUS=12.0, MOMOS_MAX_MARKERS=400,
)


def lire_config() -> dict:
    """
    Les reglages du fichier `ui/config.py`, completes par les valeurs de
    repli. C'est la SEULE source des limites appliquees.
    """
    valeurs = dict(DEFAUTS)
    try:
        from .ui import config as cfg
    except Exception as exc:                      # config absente : on continue
        LOG.warn(f"ui/config.py illisible ({exc}) : limites par defaut")
        return valeurs
    for cle in list(valeurs):
        if hasattr(cfg, cle):
            valeurs[cle] = getattr(cfg, cle)
    return valeurs


# ==========================================================================
# 4. outils de mesure
# ==========================================================================

def _polys(res, points=None):
    """Les polylignes du harnais, en tableaux numpy."""
    brut = points if points is not None else (res or {}).get("smooth") or []
    out = []
    for p in brut:
        arr = np.asarray(p, float).reshape(-1, 3)
        if len(arr) >= 2:
            out.append(arr)
    return out


def _arc(poly) -> np.ndarray:
    """Abscisse curviligne de chaque sommet d'une polyligne."""
    d = np.linalg.norm(np.diff(poly, axis=0), axis=1)
    return np.concatenate([[0.0], np.cumsum(d)])


def _arc_of_points(poly, points, tol=None):
    """
    Abscisses curvilignes des `points` qui tombent sur la polyligne.

    Sert a ordonner les crabes le long d'un cable : on projette chaque crabe
    sur le sommet le plus proche, et on ecarte ceux qui appartiennent a un
    autre cable.
    """
    pts = np.asarray(points, float).reshape(-1, 3)
    if len(pts) == 0 or len(poly) < 2:
        return np.empty(0)
    s = _arc(poly)
    tol = float(tol) if tol is not None else max(25.0, 0.02 * float(s[-1]))
    d = np.linalg.norm(pts[:, None, :] - poly[None, :, :], axis=2)
    i = np.argmin(d, axis=1)
    proche = d[np.arange(len(pts)), i]
    return np.sort(s[i][proche <= tol])


def _echantillonne(poly, pas=4.0):
    """Reechantillonne une polyligne a pas constant (mesures de distance)."""
    s = _arc(poly)
    if s[-1] < 1e-9:
        return poly.copy()
    n = max(2, int(s[-1] / max(pas, 0.5)) + 1)
    cible = np.linspace(0.0, s[-1], n)
    return np.column_stack([np.interp(cible, s, poly[:, k]) for k in range(3)])


def _limite_points(points, maxi):
    """Garde au plus `maxi` marqueurs, repartis regulierement."""
    pts = [list(map(float, p)) for p in points]
    if len(pts) <= maxi:
        return pts
    pas = max(1, len(pts) // int(maxi))
    return pts[::pas][:int(maxi)]


def _statut(valeur, limite, sens, marge=0.10):
    """
    Verdict d'une mesure face a sa limite.

    `marge` definit la bande "en limite" : a 10 %, une valeur qui respecte la
    regle a moins d'un dixieme pres passe en orange plutot qu'en vert. Ce
    n'est pas une faute -- c'est une alerte de conception.
    """
    if valeur is None or math.isnan(valeur) or limite is None or math.isnan(limite):
        return NA
    if sens == ">=":
        if valeur < limite:
            return KO
        return WARN if valeur < limite * (1.0 + marge) else OK
    if valeur > limite:
        return KO
    return WARN if valeur > limite * (1.0 - marge) else OK


# ==========================================================================
# 5. les controles, regle par regle
# ==========================================================================

def _ctrl(code, statut=NA, valeur=float("nan"), limite=float("nan"),
          detail="", points=None, n_fautes=0) -> Controle:
    regle = PAR_CODE[code]
    pts = [list(map(float, p)) for p in (points or [])]
    return Controle(code=regle.code, titre=regle.titre, famille=regle.famille,
                    statut=statut, valeur=float(valeur), limite=float(limite),
                    unite=regle.unite, sens=regle.sens, detail=detail,
                    description=regle.description,
                    n_fautes=int(n_fautes or len(pts)), points=pts)


def regle_clearance(interf, cfg) -> Controle:
    limite = float(cfg["SECURITY_DISTANCE"])
    if not interf:
        return _ctrl("HS-01", NA, detail="analyse d'interference indisponible",
                     limite=limite)
    zones = [z for z in interf.get("zones") or [] if not z.get("crossing")]
    valeur = float(interf.get("min_clearance", float("nan")))
    statut = KO if zones else _statut(valeur, limite, ">=")
    detail = (f"{len(zones)} zone(s) sous la garde, penetration maxi "
              f"{interf.get('max_penetration', 0.0):.1f} mm"
              if zones else "aucune zone sous la distance de securite")
    return _ctrl("HS-01", statut, valeur, limite, detail,
                 [z["position"] for z in zones], len(zones))


def regle_traversee(interf, cfg) -> Controle:
    if not interf:
        return _ctrl("HS-02", NA, detail="analyse d'interference indisponible",
                     limite=0.0)
    zones = [z for z in interf.get("zones") or [] if z.get("crossing")]
    n = int(interf.get("n_crossings", len(zones)))
    detail = ("le harnais traverse la structure : a corriger avant tout autre "
              "reglage" if n else "aucune traversee du maillage")
    return _ctrl("HS-02", KO if n else OK, float(n), 0.0, detail,
                 [z["position"] for z in zones], n)


def regle_cintrage(polys, cfg) -> Controle:
    from .core.hrh import curvature_radii

    limite = float(cfg["MINIMAL_BEND_RADIUS"])
    if not polys:
        return _ctrl("HS-03", NA, detail="aucune polyligne", limite=limite)
    pire, fautes = math.inf, []
    for poly in polys:
        r = curvature_radii(poly)
        fini = np.isfinite(r)
        if not fini.any():
            continue
        pire = min(pire, float(r[fini].min()))
        mauvais = np.where(fini & (r < limite))[0] + 1
        fautes.extend(poly[mauvais])
    if math.isinf(pire):
        return _ctrl("HS-03", NA, detail="rayon non mesurable", limite=limite)
    detail = (f"{len(fautes)} sommet(s) sous le rayon admissible"
              if fautes else "cintrage conforme sur tout le faisceau")
    return _ctrl("HS-03", _statut(pire, limite, ">="), pire, limite, detail,
                 fautes, len(fautes))


def regle_amorce(res, cfg) -> Controle:
    """
    L'amorce droite est tenue si, APRES elle, le cable se raccorde au
    faisceau sans plier plus court que le rayon admissible. C'est ce que
    mesure `core.connectors.entry_bend_radius`, et c'est ce qui se voit sur
    un montage : une amorce trop courte se paie par un coude en sortie de
    connecteur.
    """
    amorce = float(cfg["CONNECTOR_LEAD_IN"])
    rayon = float(cfg["MINIMAL_BEND_RADIUS"])
    rows = (res or {}).get("connector_report") or []
    if not rows:
        return _ctrl("HS-04", NA, limite=rayon,
                     detail="aucun connecteur pose : regle sans objet")
    polys = _polys(res)
    fautes, pire = [], math.inf
    for row in rows:
        r = float(row.get("bend_radius", float("nan")))
        if math.isfinite(r):
            pire = min(pire, r)
        if row.get("ok", True):
            continue
        k = int(row.get("cable", 1)) - 1
        if 0 <= k < len(polys):
            fautes.append(polys[k][0] if row.get("end") == "depart"
                          else polys[k][-1])
    if not math.isfinite(pire):
        return _ctrl("HS-04", NA, limite=rayon,
                     detail="rayon d'entree non mesurable")
    detail = (f"{len(fautes)} connecteur(s) dont la tete de cable plie sous "
              f"{rayon:.0f} mm" if fautes else
              f"{len(rows)} connecteur(s) : amorce de {amorce:.0f} mm tenue, "
              f"rayon d'entree mini {pire:.0f} mm")
    return _ctrl("HS-04", _statut(pire, rayon, ">="), pire, rayon, detail,
                 fautes, len(fautes))


def regle_axe_connecteur(res, cfg) -> Controle:
    limite = float(cfg["HS_CONNECTOR_AXIS_TOL"])
    rows = (res or {}).get("connector_report") or []
    if not rows:
        return _ctrl("HS-12", NA, limite=limite,
                     detail="aucun connecteur pose : regle sans objet")
    polys = _polys(res)
    pire, fautes = 0.0, []
    for row in rows:
        err = float(row.get("axis_error", 0.0) or 0.0)
        pire = max(pire, err)
        if err > limite:
            k = int(row.get("cable", 1)) - 1
            if 0 <= k < len(polys):
                fautes.append(polys[k][0] if row.get("end") == "depart"
                              else polys[k][-1])
    detail = (f"ecart maxi {pire:.1f} mm sur {len(rows)} connecteur(s)")
    return _ctrl("HS-12", _statut(pire, limite, "<="), pire, limite, detail,
                 fautes, len(fautes))


def regle_espacement_fixations(polys, crabs, cfg, crab_gap=None) -> Controle:
    limite = float(cfg["CLIP_SPACING"])
    positions = [np.asarray(getattr(c, "position", c), float).reshape(3)
                 for c in (crabs or [])]
    if not positions:
        if crab_gap is None:
            return _ctrl("HS-05", NA, limite=limite,
                         detail="aucun point de maintien pose")
        return _ctrl("HS-05", _statut(float(crab_gap), limite, "<="),
                     float(crab_gap), limite,
                     "ecart mesure par la pose automatique des crabes")

    pire, fautes = 0.0, []
    for poly in polys:
        s = _arc(poly)
        arcs = _arc_of_points(poly, positions)
        # Les deux extremites comptent comme des points tenus : un cable est
        # maintenu par son connecteur.
        bornes = np.concatenate([[0.0], arcs, [s[-1]]])
        for a, b in zip(bornes[:-1], bornes[1:]):
            ecart = float(b - a)
            pire = max(pire, ecart)
            if ecart > limite:
                milieu = 0.5 * (a + b)
                fautes.append([float(np.interp(milieu, s, poly[:, k]))
                               for k in range(3)])
    if crab_gap is not None:
        pire = max(pire, float(crab_gap))
    detail = (f"{len(fautes)} portee(s) au-dela de {limite:.0f} mm, "
              f"{len(positions)} crabe(s) pose(s)")
    return _ctrl("HS-05", _statut(pire, limite, "<="), pire, limite, detail,
                 fautes, len(fautes))


def regle_ecart_crabes(crabs, cfg) -> Controle:
    limite = float(cfg["HS_MIN_CLIP_SPACING"])
    seats = [np.asarray(getattr(c, "seat", getattr(c, "position", c)), float).reshape(3)
             for c in (crabs or [])]
    if len(seats) < 2:
        return _ctrl("HS-10", NA, limite=limite,
                     detail="moins de deux crabes : regle sans objet")
    P = np.asarray(seats, float)
    d = np.linalg.norm(P[:, None, :] - P[None, :, :], axis=2)
    np.fill_diagonal(d, np.inf)
    mini = float(d.min())
    i, j = np.unravel_index(np.argmin(d), d.shape)
    fautes = [0.5 * (P[a] + P[b])
              for a in range(len(P)) for b in range(a + 1, len(P))
              if d[a, b] < limite]
    detail = (f"{len(fautes)} paire(s) de crabes trop proches (mini "
              f"{mini:.0f} mm entre {i} et {j})" if fautes
              else f"ecart mini {mini:.0f} mm entre deux crabes")
    return _ctrl("HS-10", _statut(mini, limite, ">="), mini, limite, detail,
                 fautes, len(fautes))


def _longueurs_segments(res):
    """
    Longueur de chaque troncon, et nature de ses deux extremites.

    Rend [(longueur, n_extremites_de_branchement, point_milieu)].
    """
    topo = (res or {}).get("topo")
    seg_geo = (res or {}).get("seg_geo")
    if not topo or not seg_geo:
        return []
    branchements = set(topo.get("branch_points") or [])
    out = []
    for sid, seg in enumerate(topo.get("segments") or []):
        if sid >= len(seg_geo):
            break
        g = np.asarray(seg_geo[sid], float).reshape(-1, 3)
        if len(g) < 2:
            continue
        longueur = float(np.sum(np.linalg.norm(np.diff(g, axis=0), axis=1)))
        n_br = sum(1 for bout in (seg[0], seg[-1]) if bout in branchements)
        out.append((longueur, n_br, g[len(g) // 2]))
    return out


def regle_branchements(res, cfg) -> Controle:
    limite = float(cfg["L_MIN_BB"])
    segments = [(L, n, m) for L, n, m in _longueurs_segments(res) if n == 2]
    if not segments:
        return _ctrl("HS-06", NA, limite=limite,
                     detail="aucun troncon entre deux branchements")
    mini = min(L for L, _n, _m in segments)
    fautes = [m for L, _n, m in segments if L < limite]
    detail = (f"{len(fautes)} troncon(s) trop courts sur {len(segments)} "
              f"entre branchements")
    return _ctrl("HS-06", _statut(mini, limite, ">="), mini, limite, detail,
                 fautes, len(fautes))


def regle_terminal_branchement(res, cfg) -> Controle:
    limite = float(cfg["L_MIN_TB"])
    segments = [(L, n, m) for L, n, m in _longueurs_segments(res) if n == 1]
    if not segments:
        return _ctrl("HS-07", NA, limite=limite,
                     detail="aucun troncon terminal -> branchement")
    mini = min(L for L, _n, _m in segments)
    fautes = [m for L, _n, m in segments if L < limite]
    detail = (f"{len(fautes)} troncon(s) trop courts sur {len(segments)} "
              f"depuis un terminal")
    return _ctrl("HS-07", _statut(mini, limite, ">="), mini, limite, detail,
                 fautes, len(fautes))


def regle_distance_ideale(res, polys, cfg) -> Controle:
    limite = float(cfg["IDEAL_DISTANCE_WITH_STRUCTURE"])
    grid = (res or {}).get("grid")
    if grid is None or not polys:
        return _ctrl("HS-08", NA, limite=limite,
                     detail="grille de cheminement indisponible")
    pire, fautes, n_tot, n_bas = math.inf, [], 0, 0
    for poly in polys:
        ech = _echantillonne(poly, pas=6.0)
        try:
            d = np.asarray(grid.distance(ech), float)
        except Exception as exc:
            LOG.debug(f"distance a la structure indisponible : {exc}")
            return _ctrl("HS-08", NA, limite=limite,
                         detail="distance a la structure non mesurable")
        n_tot += len(d)
        bas = d < limite
        n_bas += int(bas.sum())
        pire = min(pire, float(d.min()))
        fautes.extend(ech[bas])
    if math.isinf(pire):
        return _ctrl("HS-08", NA, limite=limite, detail="mesure impossible")
    part = (100.0 * n_bas / n_tot) if n_tot else 0.0
    # Regle de CONFORT : une violation reste orange, jamais rouge -- la faute
    # de securite, elle, est deja portee par HS-01.
    statut = OK if pire >= limite else WARN
    detail = (f"{part:.1f} % du faisceau sous la distance d'integration "
              f"({limite:.0f} mm)")
    return _ctrl("HS-08", statut, pire, limite, detail,
                 fautes, int(n_bas))


def regle_diametre_toron(res, cfg, cable_diameter=6.0) -> Controle:
    limite = float(cfg["HS_MAX_BUNDLE_DIAMETER"])
    try:
        from .core.analysis import bundle_table
        rows = bundle_table(res, cable_diameter)
    except Exception as exc:
        LOG.debug(f"table des torons indisponible : {exc}")
        rows = []
    if not rows:
        return _ctrl("HS-09", NA, limite=limite, detail="torons non mesurables")
    pire = max(r["diameter"] for r in rows)
    fautes = [r["points"][len(r["points"]) // 2] for r in rows
              if r["diameter"] > limite and len(r["points"])]
    detail = (f"{len(fautes)} toron(s) au-dela de {limite:.0f} mm sur "
              f"{len(rows)} troncon(s)")
    return _ctrl("HS-09", _statut(pire, limite, "<="), pire, limite, detail,
                 fautes, len(fautes))


def regle_remplissage(res, passages, cfg, cable_diameter=6.0) -> Controle:
    limite = 100.0 * float(cfg["HS_MAX_FILL_RATIO"])
    if not passages:
        return _ctrl("HS-11", NA, limite=limite,
                     detail="aucun passage de fixation : regle sans objet")
    try:
        from .core.analysis import bundle_table
        rows = bundle_table(res, cable_diameter)
    except Exception:
        rows = []
    if not rows:
        return _ctrl("HS-11", NA, limite=limite, detail="torons non mesurables")

    pire, fautes = 0.0, []
    for pa in passages:
        p_in = np.asarray(getattr(pa, "p_in", (0, 0, 0)), float).reshape(3)
        p_out = np.asarray(getattr(pa, "p_out", (0, 0, 0)), float).reshape(3)
        milieu = 0.5 * (p_in + p_out)
        ouverture = float(np.linalg.norm(p_out - p_in))
        if ouverture < 1e-6:
            continue
        # le toron qui emprunte reellement ce passage : le plus proche
        proche, dmin = None, math.inf
        for row in rows:
            pts = np.asarray(row["points"], float).reshape(-1, 3)
            if not len(pts):
                continue
            d = float(np.min(np.linalg.norm(pts - milieu, axis=1)))
            if d < dmin:
                proche, dmin = row, d
        if proche is None or dmin > 0.5 * ouverture + 25.0:
            continue                        # passage non emprunte
        taux = 100.0 * float(proche["diameter"]) / ouverture
        pire = max(pire, taux)
        if taux > limite:
            fautes.append(milieu)
    if pire <= 0.0:
        return _ctrl("HS-11", NA, limite=limite,
                     detail="aucun passage emprunte par le faisceau")
    detail = (f"{len(fautes)} passage(s) trop remplis, taux maxi "
              f"{pire:.0f} %")
    return _ctrl("HS-11", _statut(pire, limite, "<="), pire, limite, detail,
                 fautes, len(fautes))


# ==========================================================================
# 6. le controle complet
# ==========================================================================

def check(res, interf=None, params=None, cable_diameter=None, passages=None,
          crabs=None, crab_gap=None, points=None, config=None) -> Rapport:
    """
    Passe TOUTES les regles HS sur un resultat de cheminement.

    `res`        : dictionnaire rendu par `core.hrh.run_pipeline` ;
    `interf`     : sortie de `core.analysis.interference_breakdown` ;
    `passages`   : passages de fixation detectes (facultatif) ;
    `crabs`      : crabes poses (facultatif) ;
    `crab_gap`   : plus grand ecart entre deux maintiens, deja mesure ;
    `points`     : polylignes a controler, si differentes de `res["smooth"]`.

    Les limites viennent de `ui/config.py` -- `config` ne sert qu'aux tests.
    """
    cfg = dict(config) if config else lire_config()
    if cable_diameter is None:
        cable_diameter = float(cfg.get("CABLE_DIAMETER", 6.0))
    if interf is None:
        interf = (res or {}).get("interferences_detail") or (res or {}).get("interf")
    if crabs is None:
        crabs = (res or {}).get("crabs") or []
    polys = _polys(res, points)

    controles = [
        regle_clearance(interf, cfg),
        regle_traversee(interf, cfg),
        regle_distance_ideale(res, polys, cfg),
        regle_cintrage(polys, cfg),
        regle_diametre_toron(res, cfg, cable_diameter),
        regle_amorce(res, cfg),
        regle_axe_connecteur(res, cfg),
        regle_espacement_fixations(polys, crabs, cfg, crab_gap),
        regle_ecart_crabes(crabs, cfg),
        regle_remplissage(res, passages, cfg, cable_diameter),
        regle_branchements(res, cfg),
        regle_terminal_branchement(res, cfg),
    ]
    # ordre d'affichage : par famille, puis par code
    ordre = {SECURITE: 0, MECANIQUE: 1, MONTAGE: 2, TOPOLOGIE: 3}
    controles.sort(key=lambda c: (ordre.get(c.famille, 9), c.code))

    maxi = int(cfg.get("MOMOS_MAX_MARKERS", 400))
    for ctrl in controles:
        ctrl.points = _limite_points(ctrl.points, maxi)

    rapport = Rapport(
        controles=controles,
        horodatage=time.strftime("%Y-%m-%d %H:%M:%S"),
        polylines=[np.asarray(p, float).tolist() for p in polys],
        crabs=[list(map(float, np.asarray(getattr(c, "position", c), float).reshape(3)))
               for c in (crabs or [])],
        reglages={r.cle_config: cfg.get(r.cle_config)
                  for r in REFERENTIEL if r.cle_config in cfg},
    )
    LOG.info(f"MOMOS : {rapport.resume()}")
    for ctrl in rapport.violations():
        LOG.warn(f"{ctrl.code} {ctrl.titre} : {ctrl.detail}")
    return rapport


# ==========================================================================
# 7. la fenetre 3D des regles
# ==========================================================================

def payload(rapport: Rapport, scene_stl=None, harness_stl=None,
            terminals=None, config=None):
    """Description de scene pour la visionneuse, enrichie des regles."""
    from .viewer import scene_payload

    cfg = dict(config) if config else lire_config()
    montrer_structure = bool(cfg.get("MOMOS_SHOW_STRUCTURE", True))
    scene = scene_stl or rapport.scene_stl
    lignes = [(np.asarray(p, float), 1) for p in rapport.polylines
              if len(p) >= 2]

    data = scene_payload(
        scene_stl=scene if montrer_structure else None,
        harness_stl=harness_stl or rapport.harness_stl or None,
        polylines=lignes,
        terminals=terminals or rapport.terminals or None,
        title=f"MOMOS - regles HS  ({rapport.resume()})")

    data["rules"] = [dict(code=c.code, title=c.titre, family=c.famille,
                          status=c.statut, color=c.couleur, line=c.ligne(),
                          detail=c.detail, description=c.description,
                          value=None if math.isnan(c.valeur) else float(c.valeur),
                          limit=None if math.isnan(c.limite) else float(c.limite),
                          unit=c.unite, sense=c.sens, n_faults=int(c.n_fautes),
                          points=[list(map(float, p)) for p in c.points])
                     for c in rapport.controles]
    data["rules_title"] = "REGLES HS"
    data["rules_summary"] = rapport.resume()
    data["rules_status"] = rapport.statut
    data["rules_stamp"] = rapport.horodatage
    data["rules_marker_radius"] = float(cfg.get("MOMOS_MARKER_RADIUS", 12.0))
    return data


def open_rules_window(rapport: Rapport, scene_stl=None, harness_stl=None,
                      terminals=None, workdir=None, config=None):
    """
    Ouvre la fenetre 3D des regles HS (processus separe).

    Rend le `Popen` de la visionneuse, ou `None` si elle n'a pas pu demarrer.
    """
    from .viewer import open_viewer

    data = payload(rapport, scene_stl=scene_stl, harness_stl=harness_stl,
                   terminals=terminals, config=config)
    LOG.info("ouverture de la fenetre 3D des regles HS")
    return open_viewer(data, workdir=workdir)


def run(res, interf=None, params=None, cable_diameter=None, passages=None,
        crabs=None, crab_gap=None, points=None, scene_stl=None,
        harness_stl=None, terminals=None, workdir=None, open_3d=None,
        config=None):
    """
    Le point d'entree utilise par l'application : controle puis fenetre 3D.

    Rend (rapport, processus de la visionneuse). Le controle n'echoue jamais
    l'application : une exception est journalisee et rendue dans le rapport.
    """
    cfg = dict(config) if config else lire_config()
    if not cfg.get("MOMOS_ENABLED", True):
        LOG.info("MOMOS desactive dans config.py")
        return None, None

    rapport = check(res, interf=interf, params=params,
                    cable_diameter=cable_diameter, passages=passages,
                    crabs=crabs, crab_gap=crab_gap, points=points, config=cfg)
    rapport.scene_stl = str(scene_stl or "")
    rapport.harness_stl = str(harness_stl or "")
    rapport.terminals = [list(map(float, p)) for p in (terminals or [])]

    if workdir:
        try:
            save(rapport, os.path.join(str(workdir), "momos_rapport.json"))
        except Exception as exc:
            LOG.warn(f"rapport MOMOS non enregistre : {exc}")

    ouvrir = cfg.get("MOMOS_AUTO_OPEN_3D", True) if open_3d is None else open_3d
    proc = None
    if ouvrir:
        try:
            proc = open_rules_window(rapport, scene_stl=scene_stl,
                                     harness_stl=harness_stl,
                                     terminals=terminals, workdir=workdir,
                                     config=cfg)
        except Exception as exc:
            LOG.exception("fenetre 3D des regles indisponible", exc)
    return rapport, proc


# ==========================================================================
# 8. lecture / ecriture d'un rapport
# ==========================================================================

def save(rapport: Rapport, path) -> str:
    os.makedirs(os.path.dirname(str(path)) or ".", exist_ok=True)
    with open(str(path), "w", encoding="utf-8") as fh:
        json.dump(rapport.as_dict(), fh, ensure_ascii=False, indent=1)
    LOG.debug(f"rapport MOMOS ecrit : {path}")
    return str(path)


def load(path) -> Rapport:
    with open(str(path), "r", encoding="utf-8") as fh:
        d = json.load(fh)
    controles = []
    for c in d.get("controles", []):
        c = dict(c)
        c.pop("couleur", None)
        controles.append(Controle(**c))
    return Rapport(controles=controles, horodatage=d.get("horodatage", ""),
                   titre=d.get("titre", "MOMOS"),
                   scene_stl=d.get("scene_stl", ""),
                   harness_stl=d.get("harness_stl", ""),
                   polylines=d.get("polylines", []), crabs=d.get("crabs", []),
                   terminals=d.get("terminals", []),
                   reglages=d.get("reglages", {}))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage : python -m harnessopt.MOMOS <rapport_momos.json>")
        return 2
    rapport = load(argv[0])
    print(rapport.texte())
    proc = open_rules_window(rapport)
    if proc is not None:
        proc.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
