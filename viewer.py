"""
Visionneuse 3D interactive (PyVista), lancee dans un PROCESSUS SEPARE.

VTK ouvre sa propre boucle d'evenements : l'appeler depuis Tkinter fige
l'application, et le faire depuis un thread expose a des plantages du pilote
graphique. On isole donc l'affichage dans un sous-processus, pilote par un
fichier JSON de description de scene.

Usage direct :  python -m harnessopt.viewer scene.json
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import numpy as np

from .log import Logger

LOG = Logger("visionneuse")


# --------------------------------------------------------------------------
# cote application : preparation et lancement
# --------------------------------------------------------------------------

def open_viewer(payload: dict, workdir=None) -> "subprocess.Popen | None":
    """Ecrit la description de scene puis lance la visionneuse en tache de fond."""
    workdir = str(workdir or tempfile.gettempdir())
    os.makedirs(workdir, exist_ok=True)
    path = os.path.join(workdir, "viewer_scene.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [sys.executable, "-m", "harnessopt.viewer", path]
    LOG.info("ouverture de la vue 3D interactive (processus separe)")
    LOG.debug(" ".join(cmd))
    try:
        return subprocess.Popen(cmd, cwd=root, env=env)
    except Exception as exc:
        LOG.exception("impossible de lancer la visionneuse", exc)
        return None


def scene_payload(scene_stl=None, harness_stl=None, polylines=None,
                  branch_points=None, clips_fix=None, clips_new=None,
                  passages=None, terminals=None, connectors=None,
                  connector_meshes=None, handles=None, edit_file=None,
                  reach=150.0, crabs=None, crab_stl=None,
                  title="HarnessOpt - vue 3D"):
    """Construit le dictionnaire attendu par la visionneuse."""
    def _pts(a):
        if a is None:
            return []
        arr = np.asarray(a, float).reshape(-1, 3)
        return arr.tolist()

    return dict(
        title=title,
        scene_stl=str(scene_stl) if scene_stl else None,
        harness_stl=str(harness_stl) if harness_stl else None,
        polylines=[dict(points=np.asarray(p, float).tolist(), n_cables=int(n))
                   for p, n in (polylines or [])],
        branch_points=_pts(branch_points),
        clips_fix=_pts(clips_fix),
        clips_new=_pts(clips_new),
        passages=[[list(map(float, a)), list(map(float, b))]
                  for a, b in (passages or [])],
        terminals=_pts(terminals),
        connectors=[[list(map(float, f[0])), list(map(float, f[1])),
                     list(map(float, f[2])) if len(f) > 2 else None]
                    for f in (connectors or [])],
        # vrais connecteurs : fichier + transformation, charges par la
        # visionneuse elle-meme (le maillage ne traverse pas le tuyau)
        connector_meshes=[[str(path), [float(v) for v in np.asarray(M).ravel()]]
                          for path, M in (connector_meshes or [])],
        # retouche directe : billes saisissables et fichier de retour
        handles=_pts(handles),
        edit_file=str(edit_file) if edit_file else None,
        reach=float(reach),
        # crabes : repere de pose et encombrement, le modele etant charge par
        # la visionneuse elle-meme quand un STL est fourni
        crabs=[[[float(v) for v in np.asarray(M).ravel()],
                float(dx), float(dy), float(h)] for M, dx, dy, h in (crabs or [])],
        crab_stl=str(crab_stl) if crab_stl else None,
        # controle des regles HS (MOMOS) : rempli par `MOMOS.payload`
        rules=[],
    )


# --------------------------------------------------------------------------
# cote sous-processus : affichage
# --------------------------------------------------------------------------

def show(payload: dict):
    import pyvista as pv

    pl = pv.Plotter(title=payload.get("title", "HarnessOpt"))
    pl.set_background("white", top="#DCE6F1")

    scene = payload.get("scene_stl")
    if scene and os.path.exists(scene):
        pl.add_mesh(pv.read(scene), color="#C9D3DE", opacity=0.28,
                    smooth_shading=True, name="structure")

    harness = payload.get("harness_stl")
    if harness and os.path.exists(harness):
        pl.add_mesh(pv.read(harness), color="#0072CE", smooth_shading=True,
                    name="harnais")
    else:
        for item in payload.get("polylines", []):
            pts = np.asarray(item["points"], float)
            if len(pts) < 2:
                continue
            n = max(1, int(item.get("n_cables", 1)))
            radius = 2.0 + 1.6 * np.sqrt(n)
            try:
                tube = pv.Spline(pts, max(len(pts), 8)).tube(radius=radius)
            except Exception:
                continue
            pl.add_mesh(tube, color="#00205B" if n > 1 else "#0072CE")

    for pair in payload.get("passages", []):
        pl.add_lines(np.array(pair, float), color="#7B61FF", width=5)

    from .core.geometry import connector_box
    drawn = 0
    for path, flat in payload.get("connector_meshes", []):
        try:
            mesh = pv.read(path)
            mesh.transform(np.asarray(flat, float).reshape(4, 4), inplace=True)
        except Exception:
            continue
        pl.add_mesh(mesh, color="#2E9E5B", opacity=0.95)
        drawn += 1
    if not drawn:
        for frame in payload.get("connectors", []):
            center, direction = frame[0], frame[1]
            size = frame[2] if len(frame) > 2 and frame[2] else None
            pl.add_mesh(connector_box(center, direction,
                                      size or (45.0, 26.0, 20.0)),
                        color="#2E9E5B", opacity=0.95)

    # --- crabes : le modele pose, ou un pave a ses dimensions ---
    gabarit = None
    if payload.get("crab_stl") and os.path.exists(str(payload["crab_stl"])):
        try:
            from .core.crabs import load_crab

            modele = load_crab(payload["crab_stl"])
            faces = np.hstack([np.full((len(modele.faces), 1), 3),
                               modele.faces]).ravel()
            gabarit = pv.PolyData(np.asarray(modele.vertices, float), faces)
        except Exception as exc:
            LOG.warn(f"modele de crabe non affiche : {exc}")
    for flat, dx, dy, h in payload.get("crabs", []):
        M = np.asarray(flat, float).reshape(4, 4)
        piece = gabarit.copy() if gabarit is not None else pv.Cube(
            center=(0.0, 0.0, 0.5 * h), x_length=2 * dx, y_length=2 * dy,
            z_length=h)
        piece.transform(M, inplace=True)
        pl.add_mesh(piece, color="#C77700", opacity=0.95)

    def _cloud(key, color, size, label):
        pts = np.asarray(payload.get(key) or [], float).reshape(-1, 3)
        if len(pts):
            pl.add_points(pts, color=color, point_size=size,
                          render_points_as_spheres=True, label=label)

    _cloud("terminals", "#2E9E5B", 18, "connecteur")
    _cloud("branch_points", "#12233A", 16, "branchement")
    _cloud("clips_fix", "#E0A03B", 14, "clip sur fixation")
    _cloud("clips_new", "#D2483F", 12, "clip a creer")

    try:
        pl.add_legend(bcolor="white", border=True, size=(0.18, 0.16))
    except Exception:
        pass
    pl.add_axes()

    if payload.get("rules"):
        try:
            show_rules(pl, payload)
        except Exception as exc:
            LOG.warn(f"panneau des regles indisponible "
                     f"({type(exc).__name__} : {exc})")
    else:
        try:
            enable_edit(pl, payload)
        except Exception as exc:             # la retouche est un plus
            LOG.warn(f"mode retouche indisponible ({type(exc).__name__} : "
                     f"{exc}) : la vue 3D reste utilisable")
    pl.show()


# --------------------------------------------------------------------------
# fenetre des regles HS (MOMOS)
# --------------------------------------------------------------------------

STATUT_COULEUR = {"ok": "#2E9E5B", "warn": "#E0A03B", "ko": "#D2483F",
                  "na": "#8A99A8"}


def show_rules(pl, payload):
    """
    Affiche l'INTEGRALITE des regles HS par-dessus la scene.

    A gauche, la liste des regles, une ligne par regle, coloree par son
    verdict. Dans la scene, chaque violation est pointee par une pastille de
    la meme couleur : on lit la regle, on voit ou elle est violee.

    Les touches 1 / 2 / 3 filtrent l'affichage (tout, violations, limites),
    O masque ou rend les pastilles.
    """
    import pyvista as pv

    regles = payload.get("rules") or []
    rayon = float(payload.get("rules_marker_radius") or 12.0)

    # --- pastilles de violation, un acteur par regle ---
    acteurs = {}
    for regle in regles:
        pts = np.asarray(regle.get("points") or [], float).reshape(-1, 3)
        if not len(pts):
            continue
        couleur = regle.get("color") or STATUT_COULEUR.get(regle.get("status"),
                                                           "#8A99A8")
        try:
            boules = pv.PolyData(pts).glyph(geom=pv.Sphere(radius=rayon),
                                            scale=False, orient=False)
            acteurs[regle["code"]] = pl.add_mesh(
                boules, color=couleur, opacity=0.85,
                name=f"regle-{regle['code']}")
        except Exception:
            continue

    # --- panneau de gauche : toutes les regles ---
    etat = dict(filtre="tout", pastilles=True)

    def lignes(filtre):
        titre = payload.get("rules_title") or "REGLES HS"
        resume = payload.get("rules_summary") or ""
        stamp = payload.get("rules_stamp") or ""
        out = [f"{titre}   -   {resume}"]
        if stamp:
            out.append(f"controle du {stamp}")
        out.append("")
        famille = None
        for regle in regles:
            if filtre == "violations" and regle.get("status") != "ko":
                continue
            if filtre == "limites" and regle.get("status") not in ("ko", "warn"):
                continue
            if regle.get("family") != famille:
                famille = regle.get("family")
                out.append(f"-- {famille}")
            out.append("  " + (regle.get("line") or regle.get("code", "")))
            if regle.get("detail") and regle.get("status") in ("ko", "warn"):
                out.append(f"        {regle['detail']}")
        if len(out) <= 3:
            out.append("  aucune regle dans ce filtre.")
        out.append("")
        out.append("1 toutes   2 violations   3 limites   O pastilles")
        return "\n".join(out)

    couleur_globale = STATUT_COULEUR.get(payload.get("rules_status"), "#12233A")
    panneau = pl.add_text(lignes("tout"), position="upper_left", font_size=8,
                          color="#12233A")

    def redessine():
        texte = lignes(etat["filtre"])
        for ecrire in (lambda: panneau.SetText(2, texte),
                       lambda: panneau.SetInput(texte)):
            try:
                ecrire()
                break
            except Exception:
                continue
        try:
            pl.render()
        except Exception:
            pass

    def filtre(nom):
        etat["filtre"] = nom
        redessine()

    def bascule_pastilles():
        etat["pastilles"] = not etat["pastilles"]
        for acteur in acteurs.values():
            try:
                acteur.SetVisibility(etat["pastilles"])
            except Exception:
                pass
        redessine()

    for touche, nom in (("1", "tout"), ("2", "violations"), ("3", "limites")):
        try:
            pl.add_key_event(touche, lambda n=nom: filtre(n))
        except Exception:
            pass
    for touche in ("o", "O"):
        try:
            pl.add_key_event(touche, lambda: bascule_pastilles())
        except Exception:
            pass

    # --- verdict global, en bas ---
    compte = {}
    for regle in regles:
        compte[regle.get("status")] = compte.get(regle.get("status"), 0) + 1
    verdict = {"ok": "CONFORME", "warn": "CONFORME AVEC RESERVES",
               "ko": "NON CONFORME"}.get(payload.get("rules_status"),
                                         "CONTROLE PARTIEL")
    try:
        pl.add_text(f"MOMOS : {verdict}", position="lower_left", font_size=12,
                    color=couleur_globale)
    except Exception:
        pass
    LOG.info(f"regles HS affichees : {len(regles)} regle(s), "
             f"{compte.get('ko', 0)} violee(s)")


# --------------------------------------------------------------------------
# retouche directe : CTRL + clic, on tire la bille, on applique
# --------------------------------------------------------------------------

def enable_edit(pl, payload):
    """
    Mode retouche de la visionneuse.

    Le harnais porte des billes saisissables. CTRL + clic sur l'une d'elles la
    remplace par une poignee que l'on TIRE jusqu'a l'endroit voulu ; on en
    saisit autant qu'on veut. "Appliquer" (ou la touche A) ecrit les
    deplacements dans un fichier que l'application relit aussitot : c'est elle
    qui redresse le harnais et refait respecter les regles d'integration. La
    visionneuse ne deforme rien elle-meme -- elle recueille une intention.
    """
    import pyvista as pv

    handles = np.asarray(payload.get("handles") or [], float).reshape(-1, 3)
    edit_file = payload.get("edit_file")
    if not len(handles) or not edit_file:
        return

    state = dict(reach=float(payload.get("reach") or 150.0), moves={},
                 taken=set())
    span = float(np.linalg.norm(handles.max(axis=0) - handles.min(axis=0)))
    grab = max(8.0, 0.012 * span)           # rayon d'une poignee
    snap = max(25.0, 0.05 * span)           # tolerance du clic

    pl.add_mesh(pv.PolyData(handles), color="#E0A03B", point_size=11,
                render_points_as_spheres=True, name="billes",
                label="bille deplacable")

    banner = pl.add_text("CTRL + clic : saisir une bille   |   glisser : la "
                         "deplacer\nA : appliquer   |   R : tout annuler",
                         position="upper_left", font_size=9, color="#12233A")

    def message(text, color="#12233A"):
        # Selon la position demandee, PyVista rend une annotation de coin
        # (SetText(coin, ...)) ou un acteur de texte (SetInput) : les deux
        # existent, aucun n'a l'API de l'autre.
        for ecrire in (lambda: banner.SetText(2, text),   # 2 = coin haut gauche
                       lambda: banner.SetInput(text)):
            try:
                ecrire()
                break
            except Exception:
                continue
        try:
            banner.GetTextProperty().SetColor(pv.Color(color).float_rgb)
        except Exception:
            pass
        try:
            pl.render()
        except Exception:
            pass

    def moved(index, point):
        state["moves"][index] = np.asarray(point, float)
        d = float(np.linalg.norm(state["moves"][index] - handles[index]))
        message(f"{len(state['moves'])} bille(s) deplacee(s), la derniere de "
                f"{d:.0f} mm.\nA : appliquer   |   R : tout annuler",
                "#C77700")

    def on_pick(point, *_a):
        if not _control_pressed(pl):
            return                       # un clic simple reste un clic de camera
        point = np.asarray(point, float).reshape(3)
        i = int(np.argmin(np.linalg.norm(handles - point, axis=1)))
        if float(np.linalg.norm(handles[i] - point)) > snap:
            message("Aucune bille a cet endroit : viser le harnais.", "#C5221F")
            return
        if i in state["taken"]:
            return
        state["taken"].add(i)
        pl.add_sphere_widget(lambda p, k=i: moved(k, p), center=handles[i],
                             radius=grab, color="#C77700",
                             selected_color="#D2483F", test_callback=False)
        message(f"Bille {i} saisie : la tirer jusqu'a l'endroit voulu.\n"
                f"A : appliquer   |   R : tout annuler")

    def apply():
        if not state["moves"]:
            message("Aucun deplacement a appliquer.", "#C5221F")
            return
        rows = [{"index": int(i), "from": [float(v) for v in handles[i]],
                 "to": [float(v) for v in p]}
                for i, p in sorted(state["moves"].items())]
        try:
            with open(edit_file, "w", encoding="utf-8") as fh:
                json.dump(dict(reach=state["reach"], moves=rows), fh)
        except Exception as exc:
            message(f"Ecriture impossible : {exc}", "#C5221F")
            return
        n = len(rows)
        state["moves"].clear()
        state["taken"].clear()
        try:
            pl.clear_sphere_widgets()
        except Exception:
            pass
        message(f"{n} deplacement(s) transmis a l'application : le harnais est "
                f"redresse la-bas, dans le respect des regles.\n"
                f"CTRL + clic pour continuer.", "#1E8E3E")

    def reset():
        state["moves"].clear()
        state["taken"].clear()
        try:
            pl.clear_sphere_widgets()
        except Exception:
            pass
        message("Deplacements annules.\nCTRL + clic : saisir une bille")

    def set_reach(value):
        state["reach"] = float(value)

    # Chaque commande est armee SEPAREMENT : les versions de PyVista different
    # sur ces API, et un widget indisponible ne doit pas emporter les autres.
    arme = []

    def _arme(nom, action):
        try:
            action()
            arme.append(nom)
        except Exception as exc:
            LOG.warn(f"retouche 3D : {nom} indisponible "
                     f"({type(exc).__name__} : {exc})")

    def _picking():
        try:
            pl.enable_point_picking(callback=on_pick, left_clicking=True,
                                    show_message=False, show_point=False,
                                    use_picker=True, tolerance=0.02)
        except TypeError:                 # signatures plus anciennes
            pl.enable_point_picking(callback=on_pick, left_clicking=True,
                                    show_message=False)

    def _touches():
        # PyVista refuse tout callback de touche portant un parametre sans
        # valeur par defaut -- `*args` compris. D'ou les lambdas sans argument.
        for touche in ("a", "A"):
            pl.add_key_event(touche, lambda: apply())
        for touche in ("r", "R"):
            pl.add_key_event(touche, lambda: reset())

    def _curseur():
        pl.add_slider_widget(set_reach, [30.0, 600.0], value=state["reach"],
                             title="Portee de la retouche (mm)",
                             pointa=(0.62, 0.06), pointb=(0.95, 0.06),
                             style="modern")

    def _bouton():
        pl.add_checkbox_button_widget(lambda _state=False: apply(), value=False,
                                      position=(10.0, 10.0), size=34,
                                      color_on="#1E8E3E", color_off="#1E8E3E")
        pl.add_text("Appliquer", position=(52.0, 18.0), font_size=9,
                    color="#12233A")

    _arme("saisie a la souris", _picking)
    _arme("touches A / R", _touches)
    _arme("curseur de portee", _curseur)
    _arme("bouton Appliquer", _bouton)
    LOG.info(f"mode retouche : {', '.join(arme) if arme else 'aucune commande'}")


def _control_pressed(pl) -> bool:
    """CTRL enfonce ? A defaut de le savoir, on accepte le clic."""
    try:
        return bool(pl.iren.interactor.GetControlKey())
    except Exception:
        return True


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage : python -m harnessopt.viewer <scene.json>")
        return 2
    with open(argv[0], "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    show(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
