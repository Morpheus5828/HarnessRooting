# ui/controller.py
from __future__ import annotations

import os
import queue
import threading
import time
import traceback
import tkinter as tk
from tkinter import messagebox
import numpy as np

import pythoncom
import win32com.client

from .state import AppState
from .app import HarnessAppView, STEPS, VERSION
from ..catia import bridge, mesh as meshio
from ..catia.picking import PickSession
from ..core import analysis, events, fixations, smoothing, hrh, geometry
from ..core import connectors as conn
from ..core import crabs as crab_mod
from ..core import wishes as wish_mod
from ..core.hrh import run_pipeline
from ..core.shrh import subgradient_harness_routing
from ..core.connectors import ConnectorLibrary
from ..core.collider import MeshCollider
from ..log import Logger
from ..viewer import open_viewer, scene_payload
from .. import log as applog

LOG = Logger("application")


class AppController:
    """Contrôleur principal : gère les threads, la file de messages et les règles métier."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.state = AppState()
        self.view = HarnessAppView(self.root, self, self.state)

        self.q = queue.Queue()
        self._stop = threading.Event()
        self.busy = False
        self._viewer = None
        self._edit_file = None
        self.momos = None            # dernier rapport de regles HS

        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self._check_environment()
        self.show_page("import")
        self.root.after(120, self._drain)

    def show_page(self, key: str):
        if key == "routing" and not self.state.has_scene:
            messagebox.showinfo("Maquette requise", "Charger d'abord la maquette 3D en page 1.")
            return
        self.view.display_page(key)
        self.view.set_status(f"Page : {dict((k, t) for k, _, t, _ in STEPS)[key]}")

    def quit(self):
        routing_page = self.view.pages.get("routing")
        if routing_page and routing_page.is_running:
            if not messagebox.askyesno("Calcul en cours", "Quitter interrompra le calcul.\n\nQuitter ?"):
                return
        LOG.info("fermeture de l'application")
        self.root.destroy()

    def _check_environment(self):
        available = bridge.is_available()
        self.view.set_catia_status(
            "CATIA : pilotage disponible" if available else "CATIA : pilotage indisponible (mode STL)",
            "#8FD6A0" if available else "#F0BE5A"
        )

    def _drain(self):
        try:
            for _ in range(3000):
                msg = self.q.get_nowait()
                self._handle_message(msg)
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    def _handle_message(self, msg):
        kind = msg[0]
        page_import = self.view.pages["import"]
        page_routing = self.view.pages["routing"]

        if kind == "count":
            page_import.on_count_done(msg[1])
            self.busy = False
        elif kind == "extract":
            page_import.on_extract_progress(msg[1], msg[2], msg[3])
        elif kind == "load":
            page_import.on_load_progress(msg[1], msg[2], msg[3])
        elif kind == "scan":
            page_import.on_scan_progress(msg[1], msg[2], msg[3])
        elif kind == "cloud":
            page_import.on_cloud_progress(msg[1])
        elif kind == "scene":
            _, merged, cloud, info = msg
            self.state.set_scene(merged, cloud, info)
            page_import.on_scene_loaded(info)
            self.busy = False
        elif kind == "passages":
            self.state.set_passages(msg[1])
            page_import.on_passages_loaded(msg[1])
            self.busy = False
        elif kind == "cancelled":
            self.busy = False
            page_import.on_cancelled()
        elif kind == "error":
            self.busy = False
            page_import.on_error(msg[1], msg[2])
        elif kind == events.PHASE:
            page_routing.update_phase(msg[1])
        elif kind == events.ASTAR_END:
            page_routing.dashboard.push_astar(msg[1])
        elif kind == events.ASTAR_TICK:
            page_routing.update_astar_tick(msg[1])
        elif kind == events.SWEEP:
            page_routing.dashboard.push_sweep(msg[1])
        elif kind == events.DONE:
            res, interf, summ, params = msg[1]["res"], msg[1]["interf"], msg[1]["summary"], msg[1]["params"]
            self.state.set_result(res, summ, interf, params)
            page_routing.on_routing_done(res, interf, summ, params)
            self.start_ladder_evaluation(res, params, page_routing.crab_model)
        elif kind == events.CANCELLED:
            page_routing.on_routing_cancelled()
        elif kind == events.FAILED:
            page_routing.on_routing_failed(msg[1])
        elif kind == "LIBRARY":
            page_routing.on_library_loaded(msg[1])
        elif kind == "WISHES_DONE":
            page_routing.on_wishes_done(msg[1])
        elif kind == "WISHES_FAILED":
            page_routing.on_wishes_failed(msg[1])
        elif kind == "LADDER_LEVEL":
            self.state.ladder.append(msg[1]["record"])
            page_routing.on_ladder_level(msg[1]["record"], msg[1]["index"], msg[1]["total"])
        elif kind == "LADDER_DONE":
            self.state.set_ladder(msg[1])
            page_routing.on_ladder_done(msg[1])
        elif kind == "LADDER_FAILED":
            page_routing.on_ladder_failed(msg[1])
        elif kind == "PUSH_OK":
            page_routing.on_push_success(msg[1])
        elif kind == "PUSH_KO":
            page_routing.on_push_failed(msg[1])

    def cancel_operation(self):
        self._stop.set()
        LOG.warn("annulation demandée par l'utilisateur")

    def _start_thread(self, target, args, page):
        if self.busy: return
        self._stop.clear()
        self.busy = True
        page.set_busy_state(True)
        threading.Thread(target=target, args=args, daemon=True).start()

    def action_count(self, exclude):
        self._start_thread(self._task_count, (exclude,), self.view.pages["import"])

    def _task_count(self, exclude):
        try:
            self.q.put(("count", bridge.count_parts(exclude)))
        except Exception as exc:
            self.q.put(("error", str(exc), "Analyse impossible"))

    def action_extract(self, exclude):
        self._start_thread(self._task_extract, (exclude,), self.view.pages["import"])

    def _task_extract(self, exclude):
        try:
            folder = bridge.export_stl(exclude, on_progress=lambda d, t, e: self.q.put(("extract", d, t, e)),
                                       should_stop=self._stop.is_set)
            if self._stop.is_set():
                self.q.put(("cancelled", None))
                return
            self._load_folder_task(folder, "CATIA V5")
        except Exception as exc:
            self.q.put(("error", str(exc), "Extraction interrompue"))

    def action_load_folder(self, folder, source="Dossier STL"):
        self._start_thread(self._task_load, (folder, source), self.view.pages["import"])

    def _task_load(self, folder, source):
        try:
            self._load_folder_task(folder, source)
        except Exception as exc:
            self.q.put(("error", str(exc), "Chargement impossible"))

    def _load_folder_task(self, folder, source):
        merged, info = meshio.load_folder(folder, on_progress=lambda d, t, n: self.q.put(("load", d, t, n)),
                                          should_stop=self._stop.is_set)
        if self._stop.is_set():
            self.q.put(("cancelled", None))
            return
        info.source = f"{source} - {folder}"
        self.q.put(("cloud", "construction du nuage"))
        cloud = meshio.collision_cloud(merged, on_progress=lambda msg: self.q.put(("cloud", msg)))
        info.n_cloud = len(cloud)
        self.q.put(("scene", merged, cloud, info))

    def action_scan_clamps(self, folder):
        self._start_thread(self._task_scan, (folder,), self.view.pages["import"])

    def _task_scan(self, folder):
        try:
            scene = self.state.save_scene_stl() if self.state.has_scene else None
            res = fixations.scan_fixations(folder, scene_path=scene, scene_bounds=self.state.bounds,
                                           on_progress=lambda d, t, n: self.q.put(("scan", d, t, n)),
                                           should_stop=self._stop.is_set,
                                           collider=self.state.collider if self.state.has_scene else None)
            self.q.put(("passages", res))
        except Exception as exc:
            self.q.put(("error", str(exc), "Scan impossible"))

    def action_load_passages_file(self, path):
        try:
            result = fixations.load_passages_file(path)
            self.state.set_passages(result)
            self.view.pages["import"].on_passages_loaded(result)
        except Exception as exc:
            messagebox.showerror("Erreur", str(exc))

    def open_view3d_import(self):
        if not self.state.has_scene: return
        scene = self.state.save_scene_stl()
        passages = [(p.p_in, p.p_out) for p in self.state.passage_list()]
        open_viewer(scene_payload(scene_stl=scene, passages=passages, title="HarnessOpt - maquette"),
                    workdir=self.state.cache_dir("work"))

    def load_connector_library(self, folder):
        threading.Thread(target=self._task_library, args=(folder,), daemon=True).start()

    def _task_library(self, folder):
        try:
            lib = ConnectorLibrary.scan(folder, cache_dir=str(self.state.cache_dir("work")))
            self.q.put(("LIBRARY", lib))
        except Exception as exc:
            self.q.put(("LIBRARY", ConnectorLibrary(folder=folder, errors=[(folder, str(exc))])))

    def start_routing(self, terminals, params, cable_diameter, deep, poses, wishes, models):
        # Override `deep` by the configuration setting if available
        try:
            from .config import USE_SHRH
            deep = USE_SHRH
        except ImportError:
            pass

        self.state.cable_diameter = cable_diameter
        self.state.terminals = terminals
        self.view.pages["routing"].reporter = events.Reporter(self.q)
        threading.Thread(target=self._task_route, args=(terminals, params, deep, poses, wishes, models),
                         daemon=True, name="cheminement").start()

    def _task_route(self, terminals, params, deep, poses, wishes, models):
        rep = self.view.pages["routing"].reporter
        try:
            passages, _ = fixations.filter_passages(self.state.passage_list(), params.passage_max_span, LOG)

            att = hrh.corridor_attractors([(pa.p_in, pa.p_out) for pa in passages]) if passages else np.empty((0, 3))
            self.state.attractors = att
            anchors = [np.asarray(v, float) for pa in passages for v in (pa.p_in, pa.p_out)]

            extra, conn_anchors = conn.connector_waypoints(terminals, poses, lead_in=params.connector_lead_in,
                                                           models=models)
            anchors.extend(list(conn_anchors))
            anchor_points = np.asarray(anchors, float).reshape(-1, 3) if anchors else None

            hints = wish_mod.routing_hints(wishes or [])
            no_share = hints["no_share"] if len(hints["no_share"][0]) else None
            if len(hints["attract"][0]):
                att = np.vstack([att, hints["attract"][0]]) if len(att) else hints["attract"][0]

            shrh_res = self._run_shrh(self.state.collider, terminals, params, rep) if deep else None
            res = run_pipeline(self.state.collider, self.state.bounds, terminals, params, reporter=rep,
                               extra_waypoints=extra if any(extra) else None, attractors=att if len(att) else None,
                               anchor_points=anchor_points, snap_passages=passages if params.snap_passages else None,
                               smooth=False, initial_routes=shrh_res.get("routes") if shrh_res else None,
                               no_share_zones=no_share)

            res["shrh"] = shrh_res
            res["poses"] = poses
            res["connector_models"] = models
            res["anchor_points"] = anchor_points
            res["seg_geo_free"] = res["seg_geo"]
            res["smooth"], res["seg_geo"] = self._connector_heads(res["smooth"], res["seg_geo"], params, poses, models,
                                                                  self.state.collider)

            interf = analysis.interference_breakdown(res["grid"], res["smooth"])
            summ = analysis.summary(res, interf, params, self.state.cable_diameter)
            res["connector_report"] = conn.connector_report(res["smooth"], poses, lead_in=params.connector_lead_in,
                                                            models=models, min_radius=params.min_bend_radius)
            rep.done(dict(res=res, interf=interf, summary=summ, params=params))
        except events.Cancelled:
            rep.confirm_cancelled()
        except Exception as exc:
            rep.failed(str(exc))

    def _connector_heads(self, polylines, seg_geo, params, poses, models,
                         collider, logger=LOG):
        """`logger=None` pendant l'echelle de lissage : le meme refus d'amorce
        y serait journalise a chaque palier."""
        if not poses: return polylines, seg_geo
        pts = conn.apply_connector_geometry(polylines, poses, lead_in=params.connector_lead_in, models=models,
                                            min_radius=params.min_bend_radius, step=params.resample_step,
                                            collider=collider, clearance=params.clearance, logger=logger)
        geo = conn.apply_connector_geometry_segments(seg_geo, poses, lead_in=params.connector_lead_in, models=models,
                                                     min_radius=params.min_bend_radius, step=params.resample_step,
                                                     tol=2.0 * params.resolution, collider=collider,
                                                     clearance=params.clearance, logger=logger)
        return pts, geo

    def _run_shrh(self, collider, terminals, params, rep):
        try:
            from .config import SHRH_N_ITER, SHRH_I_HRH, SHRH_DELTA0, SHRH_DELTA_MIN, SHRH_STALL_LIMIT, SHRH_DELTA_MULT
        except ImportError:
            # Sécurité si les paramètres sont absents du fichier config
            SHRH_N_ITER, SHRH_I_HRH, SHRH_DELTA0, SHRH_DELTA_MIN, SHRH_STALL_LIMIT, SHRH_DELTA_MULT = 100, 25, 1.5, 1e-4, 10, 0.8

        rep.phase("Recherche approfondie : relaxation lagrangienne", ratio=0.05)
        grid = hrh.RoutingGrid(collider, self.state.bounds, params)

        return subgradient_harness_routing(
            grid, [[grid.snap(a), grid.snap(b)] for a, b in terminals], reporter=rep,
            n_iter=SHRH_N_ITER, i_hrh=SHRH_I_HRH, delta0=SHRH_DELTA0, delta_min=SHRH_DELTA_MIN,
            stall_limit=SHRH_STALL_LIMIT, delta_mult=SHRH_DELTA_MULT,
            time_budget_s=max(60.0, 0.5 * params.time_budget_s)
        )

    def apply_wishes(self, res, params, geom):
        threading.Thread(target=self._task_wishes, args=(res, params, geom), daemon=True).start()

    def _task_wishes(self, res, params, geom):
        try:
            out = wish_mod.apply_wishes(res, params, geom, logger=LOG)
            if res.get("poses"):
                out["points"], out["seg_geo"] = self._connector_heads(out["points"], out["seg_geo"], params,
                                                                      res["poses"], res.get("connector_models") or {},
                                                                      self.state.collider)
                out["interferences"] = analysis.interference_breakdown(res["grid"], out["points"])
            self.q.put(("WISHES_DONE", out))
        except Exception as exc:
            self.q.put(("WISHES_FAILED", str(exc)))

    def start_ladder_evaluation(self, res, params, crab_model):
        threading.Thread(target=self._task_ladder, args=(res, params, crab_model, self.state.passage_list()),
                         daemon=True).start()

    def _task_ladder(self, res, params, crab_model, passages):
        try:
            poses, models, collider = res.get("poses"), res.get("connector_models") or {}, self.state.collider
            # Modele de crabe : celui choisi dans la page, sinon le STL
            # designe par CRAB_PATH dans config.py.
            crabe = ClickAndRootController._modele_de_crabe(crab_model)

            ancrages = np.asarray([v for pa in passages for v in (pa.p_in, pa.p_out)], float).reshape(-1, 3)

            def poser_crabes(pts, geo):
                tous, ecart_max = [], 0.0
                for poly in pts:
                    c, _e, _d = crab_mod.compute_crabs(poly, collider, crabe, spacing=params.clip_spacing,
                                                       anchors=ancrages)
                    tous.extend(c)
                    ecart, _ok = crab_mod.worst_gap(poly, c, ancrages, params.clip_spacing)
                    ecart_max = max(ecart_max, ecart)
                return dict(crabs=tous, crab_gap=ecart_max, crab_model=crabe.name)

            records = smoothing.build_ladder(
                res["grid"], res["routes"], res["topo"], params, anchor_points=res.get("anchor_points"),
                max_iters=params.smooth_iters,
                on_level=lambda rec, i, t: self.q.put(("LADDER_LEVEL", dict(record=rec, index=i, total=t))),
                should_stop=lambda: self.view.pages["routing"].reporter.cancelled if self.view.pages[
                    "routing"].reporter else False,
                post=(lambda pts, geo: self._connector_heads(pts, geo, params, poses, models,
                                                             collider, logger=None)) if poses else None,
                extras=poser_crabes)
            self.q.put(("LADDER_DONE", records))
            self._controler_regles(res, params, records, passages)
        except Exception as exc:
            self.q.put(("LADDER_FAILED", str(exc)))

    def _controler_regles(self, res, params, records, passages):
        """
        MOMOS : controle des regles HS du palier retenu, puis vue 3D.

        Le controle arrive apres coup : il ne doit jamais faire echouer un
        cheminement reussi, d'ou le filet d'exception.
        """
        try:
            from .. import MOMOS
            palier = smoothing.best_level(records, params.min_bend_radius) \
                or (records[-1] if records else None)
            if palier is None:
                return None
            res = dict(res)
            res["smooth"] = palier["points"]
            res["seg_geo"] = palier["seg_geo"]
            res["crabs"] = palier.get("crabs") or []
            res["connector_report"] = conn.connector_report(
                res["smooth"], res.get("poses"),
                lead_in=params.connector_lead_in,
                models=res.get("connector_models") or {},
                min_radius=params.min_bend_radius)
            rapport, _proc = MOMOS.run(
                res, interf=palier["interferences"], params=params,
                cable_diameter=self.state.cable_diameter, passages=passages,
                crabs=res["crabs"], crab_gap=palier.get("crab_gap"),
                points=res["smooth"],
                scene_stl=self.state.save_scene_stl(),
                harness_stl=self.state.harness_stl,
                terminals=[p for pair in (self.state.terminals or []) for p in pair],
                workdir=str(self.state.cache_dir("work")))
            self.momos = rapport
            return rapport
        except Exception as exc:
            LOG.exception("controle des regles HS impossible", exc)
            return None

    def push_to_catia(self, stl_builder, csv_builder):
        threading.Thread(target=self._task_push, args=(stl_builder, csv_builder), daemon=True).start()

    def _task_push(self, stl_builder, csv_builder):
        errors, stl_path = [], None
        try:
            stl_path = stl_builder()
        except Exception as exc:
            errors.append(f"solide STL : {exc}")
        try:
            csv_path = csv_builder()
            message = bridge.import_native(csv_path)
            self.q.put(("PUSH_OK", dict(path=stl_path, csv=csv_path, message=message, mode="native")))
            return
        except Exception as exc:
            errors.append(f"geometrie native : {exc}")
        try:
            if stl_path is None: raise RuntimeError("aucun solide disponible")
            message = bridge.import_stl(stl_path)
            self.q.put(("PUSH_OK", dict(path=stl_path, csv=self.state.harness_csv, message=message, mode="stl")))
        except Exception as exc:
            errors.append(f"import STL : {exc}")
            self.q.put(("PUSH_KO", dict(error="\n\n".join(errors), path=stl_path, csv=self.state.harness_csv)))


# ==============================================================================
# CONTRÔLEUR CLICK & ROOT (Assistant Automatisé de bout en bout)
# ==============================================================================
def appairer(departs, arrivees):
    """Associe les départs et arrivées selon leur nombre et distance."""
    departs = [tuple(float(v) for v in p) for p in departs]
    arrivees = [tuple(float(v) for v in p) for p in arrivees]
    if not departs or not arrivees: return []
    if len(arrivees) == 1: return [(d, arrivees[0]) for d in departs]
    if len(departs) == len(arrivees): return list(zip(departs, arrivees))
    cibles = np.asarray(arrivees, float)
    return [(d, arrivees[int(np.argmin(np.linalg.norm(cibles - np.asarray(d, float), axis=1)))]) for d in departs]


class ClickAndRootController:
    """
    Controleur du parcours guide Click & Root (mode CATIA reel).

    Il garde la maquette en memoire d'un routage a l'autre : refaire un
    cheminement ne reexporte pas les STL et ne reconstruit pas le detecteur
    de collision, ce qui fait passer une relance de plusieurs minutes a
    quelques secondes.
    """

    def __init__(self, root: tk.Tk, cable_diameter=6.0):
        self.root = root
        self.cable_diameter = float(cable_diameter)
        self.q = queue.Queue()
        self.view = None
        self.session = None
        self.busy = False

        self.maquette = None          # cache : maillage, nuage, collider
        self.dernier = None           # dernier bilan de cheminement
        self.momos = None             # dernier rapport MOMOS

        self.root.after(120, self._drain)

    # ==================================================================
    # file de messages
    # ==================================================================
    def _drain(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if self.view:
                    self._handle_message(msg)
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    def _handle_message(self, msg):
        genre = msg[0]
        if genre == "POINT":
            self.view.on_point_added(msg[1], msg[2], msg[3])
        elif genre == "FIN_SELECTION":
            self.view.on_selection_ended(msg[1], msg[2], msg[3])
        elif genre == "ETAPE":
            self.view.on_step_update(msg[1])
        elif genre == "PROGRESS":
            self.view.on_progress_update(msg[1], msg[2], msg[3])
        elif genre == "FINI":
            self.busy = False
            self.view.on_routing_done(msg[1])
        elif genre == "ECHEC":
            self.busy = False
            self.view.on_routing_failed(msg[1])
        elif genre == "MOMOS":
            self.momos = msg[1]
            if hasattr(self.view, "on_momos_ready"):
                self.view.on_momos_ready(msg[1])

    # ==================================================================
    # selection dans CATIA
    # ==================================================================
    def start_selection(self, phase, libelle):
        def on_point(xyz, nom):
            self.q.put(("POINT", phase, xyz, nom))

        def on_end(points, err):
            self.q.put(("FIN_SELECTION", phase, points, err))

        self.session = PickSession(libelle, on_point=on_point,
                                   on_end=on_end).start()

    # ==================================================================
    # relances
    # ==================================================================
    def reset(self, oublier_maquette=False):
        """
        Prepare un nouveau routage.

        La maquette est conservee par defaut : c'est ce qui rend la relance
        immediate. `oublier_maquette=True` force une reextraction depuis
        CATIA, par exemple apres une modification de la definition.
        """
        self.dernier = None
        self.momos = None
        self.session = None
        if oublier_maquette:
            self.maquette = None
            LOG.info("maquette oubliee : le prochain routage la rechargera")

    def open_momos_window(self):
        """Rouvre la fenetre 3D des regles HS du dernier controle."""
        if self.momos is None:
            LOG.warn("aucun controle MOMOS disponible")
            return None
        from .. import MOMOS
        maquette = self.maquette or {}
        try:
            return MOMOS.open_rules_window(
                self.momos, scene_stl=maquette.get("scene_stl"),
                workdir=str(bridge.BASE_CACHE / "click_and_root"))
        except Exception as exc:
            LOG.exception("fenetre 3D des regles indisponible", exc)
            return None

    # ==================================================================
    # cheminement
    # ==================================================================
    def start_routing(self, terminals, params, cable_diameter, algorithme=None,
                      poses=None, wishes=None, models=None,
                      dossier_fixations=None, crab_model=None, noms=None):
        """
        Lance le cheminement dans un thread de travail.

        `algorithme` vaut "HRH" ou "SHRH" ; un booleen est encore accepte
        (ancienne signature `deep`). Sans rien, on suit `config.py`.
        """
        if self.busy:
            LOG.warn("un cheminement est deja en cours")
            return
        algo = self._algorithme(algorithme)
        self.busy = True
        threading.Thread(
            target=self._task_router,
            args=(terminals, params, cable_diameter, algo, poses or [],
                  wishes or [], models or {}, dossier_fixations, crab_model,
                  noms or []),
            daemon=True, name="cnr_cheminement").start()

    @staticmethod
    def _algorithme(valeur):
        if isinstance(valeur, bool):
            return "SHRH" if valeur else "HRH"
        if valeur:
            return "SHRH" if str(valeur).strip().upper() == "SHRH" else "HRH"
        try:
            from .config import routing_algorithm
            return routing_algorithm()
        except Exception:
            return "HRH"

    # -- la maquette, chargee une seule fois ---------------------------
    def _charger_maquette(self, etape, prog):
        """Maillage, nuage de collision et detecteur, mis en cache."""
        if self.maquette is not None:
            etape("Maquette : deja chargee, on la reutilise")
            return self.maquette

        etape("Maquette : export depuis CATIA")
        dossier = str(bridge.STL_FOLDER)
        if not meshio.list_stl(dossier):
            dossier = str(bridge.export_stl(
                on_progress=lambda d, t, e: prog(d, t,
                                                 f"Export CATIA : {d}/{t} pieces")))

        etape("Maquette : lecture des STL")
        merged, info = meshio.load_folder(
            dossier, on_progress=lambda d, t, n: prog(d, t,
                                                      f"Lecture STL : {d}/{t} - {n}"))

        etape("Construction du nuage de collision")
        cloud = meshio.collision_cloud(merged,
                                       on_progress=lambda m: etape(f"Nuage : {m}"))
        collider = MeshCollider.from_mesh(merged, name="maquette")

        # copie disque de la maquette : la visionneuse des regles en a besoin
        scene_stl = None
        try:
            scene_stl = str(bridge.BASE_CACHE / "scene_click_and_root.stl")
            geometry.save_stl(merged, scene_stl)
        except Exception as exc:
            LOG.warn(f"maquette non ecrite sur disque : {exc}")
            scene_stl = None

        self.maquette = dict(merged=merged, info=info, cloud=cloud,
                             collider=collider, dossier=dossier,
                             scene_stl=scene_stl)
        return self.maquette

    # -- accrochage des connecteurs ------------------------------------
    def _accrocher_connecteurs(self, terminals, noms, poses, maquette, params,
                               etape):
        """
        Ramene chaque extremite de cable sur la BONNE FACE de son connecteur.

        Le clic CATIA rend le centre de gravite de la piece, donc un point
        interieur. On calcule la boite englobante du connecteur, on retient
        la face tournee vers le harnais, et son centre devient le depart (ou
        l'arrivee). La normale de cette face donne la direction de sortie du
        cable : l'amorce droite est alors celle du montage reel.
        """
        try:
            from .config import (CONNECTOR_SNAP_TO_FACE, CONNECTOR_FACE_OFFSET,
                                 CONNECTOR_SEARCH_RADIUS,
                                 CONNECTOR_ORIENTED_BBOX)
        except ImportError:
            CONNECTOR_SNAP_TO_FACE, CONNECTOR_FACE_OFFSET = True, 3.0
            CONNECTOR_SEARCH_RADIUS, CONNECTOR_ORIENTED_BBOX = 400.0, True

        if not CONNECTOR_SNAP_TO_FACE:
            return terminals, poses

        etape("Connecteurs : boite englobante et face la plus proche")
        from ..core import connector_snap as snap_mod

        # Le centre de face est SUR le connecteur : pose tel quel, le terminal
        # se retrouve pile a la distance de securite, et toutes les aretes qui
        # y menent passent sous la garde -- le cheminement declare alors qu'il
        # n'existe aucun chemin. On l'ecarte donc d'au moins une garde plus une
        # demi-maille, ce qui est de toute facon la position reelle de l'axe du
        # toron en sortie de connecteur.
        deport = max(float(CONNECTOR_FACE_OFFSET),
                     float(params.clearance) + 0.5 * float(params.resolution))

        dossiers = [maquette.get("dossier"), str(bridge.STL_FOLDER)]
        terminaux, rapport = snap_mod.snap_terminals(
            terminals, names=noms, folders=dossiers, mesh=maquette["merged"],
            offset=deport, search_radius=CONNECTOR_SEARCH_RADIUS,
            oriented=CONNECTOR_ORIENTED_BBOX, logger=LOG)

        accroches = sum(1 for pair in rapport for s in pair if s is not None)
        if not accroches:
            LOG.warn("aucun connecteur reconnu : les points cliques sont "
                     "conserves tels quels")
            return terminals, poses

        poses = snap_mod.merge_poses(poses, rapport)
        # Une amorce droite plantee dans une cloison rend le probleme
        # insoluble : on garde la plus grande qui degage tous les connecteurs.
        voulu = float(params.connector_lead_in)
        lead = snap_mod.feasible_lead_in(poses, maquette["collider"],
                                         clearance=params.clearance,
                                         wanted=voulu, floor=20.0)
        if lead < voulu:
            LOG.warn(f"amorce connecteur ramenee de {voulu:.0f} a {lead:.0f} mm "
                     f"(la structure est trop proche)")
        params.connector_lead_in = lead
        etape(f"Connecteurs : {accroches} face(s) retenue(s), deport "
              f"{deport:.0f} mm, amorce {lead:.0f} mm")
        return terminaux, poses

    # -- le calcul complet ---------------------------------------------
    def _task_router(self, terminals, params, cable_diameter, algorithme,
                     poses, wishes, models, dossier_fixations, crab_model,
                     noms):
        try:
            def etape(texte):
                self.q.put(("ETAPE", texte))

            def prog(d, t, texte):
                self.q.put(("PROGRESS", d, t, texte))

            maquette = self._charger_maquette(etape, prog)
            merged = maquette["merged"]
            collider = maquette["collider"]
            info, cloud = maquette["info"], maquette["cloud"]

            terminals, poses = self._accrocher_connecteurs(
                terminals, noms, poses, maquette, params, etape)

            hors = np.asarray([p for pair in terminals for p in pair], float)
            x0, x1, y0, y1, z0, z1 = merged.bounds
            p_min = hors.min(axis=0) - 60.0
            p_max = hors.max(axis=0) + 60.0
            bounds = (min(x0, p_min[0]), max(x1, p_max[0]),
                      min(y0, p_min[1]), max(y1, p_max[1]),
                      min(z0, p_min[2]), max(z1, p_max[2]))

            # --- fixations existantes ---
            passages_list = []
            if dossier_fixations and os.path.exists(dossier_fixations):
                etape("Scan des colliers 3D")
                try:
                    res_scan = fixations.scan_fixations(
                        dossier_fixations,
                        scene_path=maquette.get("scene_stl"),
                        scene_bounds=merged.bounds,
                        collider=collider,
                        on_progress=lambda d, t, n: prog(
                            d, t, f"Scan colliers : {d}/{t} - {n}"))
                    passages_list = getattr(res_scan, "passages", []) or []
                except Exception as exc:
                    LOG.warn(f"erreur scan colliers : {exc}")

            passages, _ = fixations.filter_passages(passages_list,
                                                    params.passage_max_span, LOG)
            att = (hrh.corridor_attractors([(pa.p_in, pa.p_out) for pa in passages])
                   if passages else np.empty((0, 3)))

            extra, conn_anchors = conn.connector_waypoints(
                terminals, poses, lead_in=params.connector_lead_in, models=models)
            anchors = list(conn_anchors)
            anchors_passages = [np.asarray(v, float)
                                for pa in passages for v in (pa.p_in, pa.p_out)]
            anchors.extend(anchors_passages)
            anchor_points = (np.asarray(anchors, float).reshape(-1, 3)
                             if anchors else None)

            # --- HRH ou SHRH ---
            etape(f"Cheminement {algorithme}"
                  + (" : relaxation lagrangienne puis HRH"
                     if algorithme == "SHRH" else ""))
            shrh_res = None
            if algorithme == "SHRH":
                shrh_res = self._run_shrh(collider, bounds, terminals, params,
                                          prog)

            res = run_pipeline(collider, bounds, terminals, params, smooth=False,
                               extra_waypoints=extra if any(extra) else None,
                               attractors=att if len(att) else None,
                               anchor_points=anchor_points,
                               snap_passages=passages if params.snap_passages else None,
                               initial_routes=(shrh_res.get("routes")
                                               if shrh_res else None))

            res["seg_geo_free"] = res["seg_geo"]
            res["poses"] = poses
            res["connector_models"] = models
            res["anchor_points"] = anchor_points
            res["shrh"] = shrh_res
            res["smooth"], res["seg_geo"] = self._connector_heads(
                res["smooth"], res["seg_geo"], params, poses, models, collider)

            # --- lissage et pose des crabes ---
            etape("Lissage et pose des crabes")
            crabe = self._modele_de_crabe(crab_model)
            ancrages = (np.asarray(anchors_passages, float).reshape(-1, 3)
                        if anchors_passages else np.empty((0, 3)))

            def poser_crabes(pts, geo):
                tous, ecart_max = [], 0.0
                for polyligne in pts:
                    c, _e, _d = crab_mod.compute_crabs(
                        polyligne, collider, crabe, spacing=params.clip_spacing,
                        anchors=ancrages)
                    tous.extend(c)
                    ecart_max = max(ecart_max, crab_mod.worst_gap(
                        polyligne, c, ancrages, params.clip_spacing)[0])
                return dict(crabs=tous, crab_gap=ecart_max,
                            crab_model=crabe.name)

            paliers = smoothing.build_ladder(
                res["grid"], res["routes"], res["topo"], params,
                anchor_points=anchor_points,
                max_iters=params.smooth_iters,
                on_level=lambda rec, i, n: prog(i, n, f"Lissage {rec['level']} %"),
                post=(lambda pts, geo: self._connector_heads(
                    pts, geo, params, poses, models, collider,
                    logger=None)) if poses else None,
                extras=poser_crabes)

            palier = smoothing.best_level(paliers, params.min_bend_radius) or paliers[-1]
            res["smooth"] = palier["points"]
            res["seg_geo"] = palier["seg_geo"]
            res["crabs"] = palier.get("crabs") or []

            interf = palier["interferences"]
            res["interferences_detail"] = interf
            res["connector_report"] = conn.connector_report(
                res["smooth"], poses, lead_in=params.connector_lead_in,
                models=models, min_radius=params.min_bend_radius)
            resume = analysis.summary(res, interf, params, cable_diameter)
            resume["n_cables"] = len(terminals)

            # --- solides et export ---
            etape("Solides : cables et crabes")
            horodatage = time.strftime("%Y%m%d_%H%M%S")
            dossier_exp = bridge.BASE_CACHE / "click_and_root"
            os.makedirs(dossier_exp, exist_ok=True)
            fichiers = self._exporter(res, crabe, cable_diameter, dossier_exp,
                                      horodatage, anchors_passages)

            etape("Envoi dans CATIA")
            messages = self._pousser(fichiers)

            # --- controle des regles HS ---
            rapport = self._controler_regles(
                res, interf, params, cable_diameter, passages, palier,
                terminals, maquette, fichiers, dossier_exp, etape)

            bilan = dict(res=res, palier=palier, resume=resume,
                         interferences=interf, fichiers=fichiers,
                         messages=messages, info=info, n_cloud=len(cloud),
                         algorithme=algorithme, momos=rapport,
                         terminals=terminals, poses=poses)
            self.dernier = bilan
            self.q.put(("FINI", bilan))
        except Exception as exc:
            traceback.print_exc()
            self.q.put(("ECHEC", str(exc)))

    # -- briques du calcul ---------------------------------------------
    def _run_shrh(self, collider, bounds, terminals, params, prog):
        try:
            from .config import (SHRH_N_ITER, SHRH_I_HRH, SHRH_DELTA0,
                                 SHRH_DELTA_MIN, SHRH_STALL_LIMIT,
                                 SHRH_DELTA_MULT)
        except ImportError:
            SHRH_N_ITER, SHRH_I_HRH, SHRH_DELTA0 = 100, 25, 1.5
            SHRH_DELTA_MIN, SHRH_STALL_LIMIT, SHRH_DELTA_MULT = 1e-4, 10, 0.8

        prog(0, 100, "SHRH : exploration des topologies alternatives")
        grille = hrh.RoutingGrid(collider, bounds, params)
        terminaux = [[grille.snap(a), grille.snap(b)] for a, b in terminals]
        return subgradient_harness_routing(
            grille, terminaux, n_iter=SHRH_N_ITER, i_hrh=SHRH_I_HRH,
            delta0=SHRH_DELTA0, delta_min=SHRH_DELTA_MIN,
            stall_limit=SHRH_STALL_LIMIT, delta_mult=SHRH_DELTA_MULT,
            time_budget_s=max(60.0, 0.5 * params.time_budget_s))

    @staticmethod
    def _modele_de_crabe(crab_model):
        """Modele de crabe : celui fourni, sinon le STL de config.py."""
        crabe = crab_model or crab_mod.default_model()
        chemin = ""
        try:
            from .config import CRAB_PATH
            chemin = CRAB_PATH
        except ImportError:
            try:
                from .config import CRAB_STL_PATH
                chemin = CRAB_STL_PATH
            except ImportError:
                chemin = ""
        if chemin and os.path.isfile(str(chemin)):
            try:
                crabe = crab_mod.load_crab(str(chemin))
                LOG.info(f"crabe charge depuis config.py : {crabe.label()}")
            except Exception as exc:
                LOG.warn(f"STL de crabe illisible ({chemin}) : {exc}")
        elif chemin:
            LOG.warn(f"CRAB_PATH introuvable : {chemin} -- crabe par defaut")
        return crabe

    @staticmethod
    def _exporter(res, crabe, cable_diameter, dossier, horodatage,
                  anchors_passages):
        """Ecrit les solides et le fichier de courbes pour CATIA."""
        import pyvista as pv

        cables = geometry.harness_solid(res, cable_diameter=cable_diameter,
                                        polylines=res["smooth"])
        chemin_cables = str(dossier / f"cables_{horodatage}.stl")
        geometry.save_stl(cables, chemin_cables)

        fichiers = dict(
            cables=chemin_cables, crabes=None, passages=None,
            csv=geometry.export_catia_curves_csv(
                res, str(dossier / f"fibres_{horodatage}.csv"), cable_diameter))

        pieces = crab_mod.crab_meshes(res.get("crabs") or [], crabe)
        if pieces:
            chemin_crabes = str(dossier / f"crabes_{horodatage}.stl")
            geometry.save_stl(pv.merge(pieces), chemin_crabes)
            fichiers["crabes"] = chemin_crabes

        if anchors_passages:
            nuage = pv.PolyData(np.asarray(anchors_passages, float))
            spheres = nuage.glyph(geom=pv.Sphere(radius=4.0), scale=False)
            chemin_passages = str(dossier / f"passages_{horodatage}.stl")
            geometry.save_stl(spheres, chemin_passages)
            fichiers["passages"] = chemin_passages
        return fichiers

    def _controler_regles(self, res, interf, params, cable_diameter, passages,
                          palier, terminals, maquette, fichiers, dossier_exp,
                          etape):
        """
        MOMOS : controle des regles HS, puis fenetre 3D dediee.

        Le controle ne doit jamais faire echouer un cheminement reussi : la
        moindre erreur est journalisee et le bilan repart sans rapport.
        """
        try:
            from .. import MOMOS
        except Exception as exc:
            LOG.warn(f"MOMOS indisponible : {exc}")
            return None
        try:
            etape("MOMOS : controle des regles HS")
            rapport, _proc = MOMOS.run(
                res, interf=interf, params=params,
                cable_diameter=cable_diameter, passages=passages,
                crabs=res.get("crabs"), crab_gap=palier.get("crab_gap"),
                points=res["smooth"], scene_stl=maquette.get("scene_stl"),
                harness_stl=fichiers.get("cables"),
                terminals=[p for pair in terminals for p in pair],
                workdir=str(dossier_exp))
            if rapport is not None:
                etape(f"MOMOS : {rapport.resume()}")
                self.q.put(("MOMOS", rapport))
            return rapport
        except Exception as exc:
            LOG.exception("controle des regles HS impossible", exc)
            return None

    def _connector_heads(self, polylines, seg_geo, params, poses, models,
                         collider, logger=LOG):
        """
        Remet les tetes de cable dans l'axe des connecteurs.

        `logger=None` pendant la construction de l'echelle de lissage : la
        meme reprise y est tentee a chaque palier, et journaliser vingt fois
        le meme refus d'amorce noie le reste du journal.
        """
        if not poses:
            return polylines, seg_geo
        pts = conn.apply_connector_geometry(
            polylines, poses, lead_in=params.connector_lead_in, models=models,
            min_radius=params.min_bend_radius, step=params.resample_step,
            collider=collider, clearance=params.clearance, logger=logger)
        geo = conn.apply_connector_geometry_segments(
            seg_geo, poses, lead_in=params.connector_lead_in, models=models,
            min_radius=params.min_bend_radius, step=params.resample_step,
            tol=2.0 * params.resolution, collider=collider,
            clearance=params.clearance, logger=logger)
        return pts, geo

    def _pousser(self, fichiers):
        try:
            pythoncom.CoInitialize()
            catia = win32com.client.Dispatch("CATIA.Application")
            products = catia.ActiveDocument.Product.Products

            products.AddComponentsFromFiles((fichiers["cables"],), "All")
            if fichiers.get("crabes"):
                products.AddComponentsFromFiles((fichiers["crabes"],), "All")
            if fichiers.get("passages"):
                products.AddComponentsFromFiles((fichiers["passages"],), "All")

            catia.ActiveDocument.Update()
            return ["[OK] Harnais et passages inseres dans CATIA."]
        except Exception as exc:
            return [f"[ECHEC] CATIA COM : {exc}"]
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass


def launch():
    """
    Point d'entree de l'application principale.

    L'ecran de demarrage s'affiche d'abord ; la fenetre n'est construite
    qu'a son effacement, sinon elle apparaitrait derriere le logo.
    """
    applog.banner(VERSION)
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.25)
    except tk.TclError:
        pass

    from .splash import splash_then

    etat = {}

    def construire():
        etat["app"] = AppController(root)

    splash_then(root, construire, duration=2600, version=VERSION,
                message="Verification de l'environnement CATIA...")
    root.mainloop()
