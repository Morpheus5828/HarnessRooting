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

    def _connector_heads(self, polylines, seg_geo, params, poses, models, collider):
        if not poses: return polylines, seg_geo
        pts = conn.apply_connector_geometry(polylines, poses, lead_in=params.connector_lead_in, models=models,
                                            min_radius=params.min_bend_radius, step=params.resample_step,
                                            collider=collider, clearance=params.clearance, logger=LOG)
        geo = conn.apply_connector_geometry_segments(seg_geo, poses, lead_in=params.connector_lead_in, models=models,
                                                     min_radius=params.min_bend_radius, step=params.resample_step,
                                                     tol=2.0 * params.resolution, collider=collider,
                                                     clearance=params.clearance, logger=LOG)
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
            crabe = crab_model or crab_mod.default_model()

            # --- INJECTION DU STL CRABE PERSONNALISE ---
            try:
                from .config import CRAB_STL_PATH
                if CRAB_STL_PATH and os.path.isfile(CRAB_STL_PATH):
                    import pyvista as pv
                    crabe.mesh = pv.read(CRAB_STL_PATH)
            except Exception as e:
                LOG.warn(f"Impossible de charger le STL du crabe personnalisé : {e}")
            # -------------------------------------------

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
                                                             collider)) if poses else None,
                extras=poser_crabes)
            self.q.put(("LADDER_DONE", records))
        except Exception as exc:
            self.q.put(("LADDER_FAILED", str(exc)))

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
    """Contrôleur du parcours guidé Click & Root (Mode CATIA réel exclusif)."""

    def __init__(self, root: tk.Tk, cable_diameter=6.0):
        self.root = root
        self.cable_diameter = float(cable_diameter)
        self.q = queue.Queue()
        self.view = None
        self.session = None

        self.root.after(120, self._drain)

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
            self.view.on_routing_done(msg[1])
        elif genre == "ECHEC":
            self.view.on_routing_failed(msg[1])

    def start_selection(self, phase, libelle):
        def on_point(xyz, nom):
            self.q.put(("POINT", phase, xyz, nom))

        def on_end(points, err):
            self.q.put(("FIN_SELECTION", phase, points, err))

        self.session = PickSession(libelle, on_point=on_point, on_end=on_end).start()

    def start_routing(self, terminals, params, cable_diameter, deep, poses, wishes, models, dossier_fixations=None,
                      crab_model=None):
        # Override the argument if configured globally
        try:
            from .config import USE_SHRH
            deep = USE_SHRH
        except ImportError:
            pass

        threading.Thread(
            target=self._task_router,
            args=(terminals, params, cable_diameter, deep, poses, wishes, models, dossier_fixations, crab_model),
            daemon=True,
            name="cnr_cheminement"
        ).start()

    def _task_router(self, terminals, params, cable_diameter, deep, poses, wishes, models, dossier_fixations,
                     crab_model):
        try:
            def etape(texte):
                self.q.put(("ETAPE", texte))

            def prog(d, t, texte):
                self.q.put(("PROGRESS", d, t, texte))

            etape("Maquette : export depuis CATIA")
            dossier = str(bridge.STL_FOLDER)
            if not meshio.list_stl(dossier):
                dossier = str(bridge.export_stl(
                    on_progress=lambda d, t, e: prog(d, t, f"Export CATIA : {d}/{t} pièces")
                ))

            etape("Maquette : lecture des STL")
            merged, info = meshio.load_folder(dossier,
                                              on_progress=lambda d, t, n: prog(d, t, f"Lecture STL : {d}/{t} - {n}"))

            etape("Construction du nuage de collision...")
            cloud = meshio.collision_cloud(merged, on_progress=lambda msg: etape(f"Nuage : {msg}"))

            collider = MeshCollider.from_mesh(merged, name="maquette")

            hors = np.asarray([p for pair in terminals for p in pair], float)

            # Calcul direct de la boîte englobante
            x0, x1, y0, y1, z0, z1 = merged.bounds
            p_min = hors.min(axis=0) - 60.0
            p_max = hors.max(axis=0) + 60.0
            bounds = (min(x0, p_min[0]), max(x1, p_max[0]),
                      min(y0, p_min[1]), max(y1, p_max[1]),
                      min(z0, p_min[2]), max(z1, p_max[2]))

            etape("Recherche de fixations...")
            passages_list = []

            if dossier_fixations and os.path.exists(dossier_fixations):
                etape("Scan des colliers 3D...")
                try:
                    scene_path = str(bridge.BASE_CACHE / "scene_temp.stl")
                    geometry.save_stl(merged, scene_path)
                    res_scan = fixations.scan_fixations(
                        dossier_fixations,
                        scene_path=scene_path,
                        scene_bounds=merged.bounds,
                        collider=collider,
                        on_progress=lambda d, t, n: prog(d, t, f"Scan colliers : {d}/{t} - {n}")
                    )
                    passages_list = res_scan.passages if hasattr(res_scan, "passages") else []
                except Exception as e:
                    LOG.warn(f"Erreur scan colliers: {e}")

            passages, _ = fixations.filter_passages(passages_list, params.passage_max_span, LOG)
            att = hrh.corridor_attractors([(pa.p_in, pa.p_out) for pa in passages]) if passages else np.empty((0, 3))

            anchors = []
            extra, conn_anchors = conn.connector_waypoints(terminals, poses, lead_in=params.connector_lead_in,
                                                           models=models)
            anchors.extend(list(conn_anchors))

            anchors_passages = [np.asarray(v, float) for pa in passages for v in (pa.p_in, pa.p_out)]
            if anchors_passages:
                anchors.extend(anchors_passages)

            anchor_points = np.asarray(anchors, float).reshape(-1, 3) if anchors else None

            # --- NOUVEAUTÉ ICI : Activation dynamique du SHRH ---
            etape(f"Cheminement {'SHRH (Recherche Lagrangienne)' if deep else 'HRH'}")

            shrh_res = None
            if deep:
                try:
                    from .config import SHRH_N_ITER, SHRH_I_HRH, SHRH_DELTA0, SHRH_DELTA_MIN, SHRH_STALL_LIMIT, \
                        SHRH_DELTA_MULT
                except ImportError:
                    SHRH_N_ITER, SHRH_I_HRH, SHRH_DELTA0, SHRH_DELTA_MIN, SHRH_STALL_LIMIT, SHRH_DELTA_MULT = 100, 25, 1.5, 1e-4, 10, 0.8

                prog(0, 100, "SHRH : Exploration des topologies alternatives...")
                grid_shrh = hrh.RoutingGrid(collider, bounds, params)
                terminaux_grille = [[grid_shrh.snap(a), grid_shrh.snap(b)] for a, b in terminals]

                shrh_res = subgradient_harness_routing(
                    grid_shrh, terminaux_grille,
                    n_iter=SHRH_N_ITER, i_hrh=SHRH_I_HRH, delta0=SHRH_DELTA0, delta_min=SHRH_DELTA_MIN,
                    stall_limit=SHRH_STALL_LIMIT, delta_mult=SHRH_DELTA_MULT,
                    time_budget_s=max(60.0, 0.5 * params.time_budget_s)
                )

            res = run_pipeline(collider, bounds, terminals, params, smooth=False,
                               extra_waypoints=extra if any(extra) else None,
                               attractors=att if len(att) else None,
                               anchor_points=anchor_points,
                               snap_passages=passages if params.snap_passages else None,
                               initial_routes=shrh_res.get("routes") if shrh_res else None)

            res["seg_geo_free"] = res["seg_geo"]

            res["poses"] = poses
            res["connector_models"] = models
            res["anchor_points"] = anchor_points
            res["smooth"], res["seg_geo"] = self._connector_heads(res["smooth"], res["seg_geo"], params, poses, models,
                                                                  collider)

            etape("Lissage et pose des crabes")
            crabe = crab_model or crab_mod.default_model()

            # --- INJECTION DU STL CRABE PERSONNALISE ---
            try:
                from .config import CRAB_STL_PATH
                if CRAB_STL_PATH and os.path.isfile(CRAB_STL_PATH):
                    import pyvista as pv
                    crabe.mesh = pv.read(CRAB_STL_PATH)
            except Exception as e:
                LOG.warn(f"Impossible de charger le STL du crabe personnalisé : {e}")
            # -------------------------------------------

            ancrages = np.asarray(anchors_passages, float).reshape(-1, 3) if anchors_passages else np.empty((0, 3))

            def poser_crabes(pts, geo):
                tous, ecart_max = [], 0.0
                for polyligne in pts:
                    c, _e, _d = crab_mod.compute_crabs(polyligne, collider, crabe, spacing=params.clip_spacing,
                                                       anchors=ancrages)
                    tous.extend(c)
                    ecart_max = max(ecart_max, crab_mod.worst_gap(polyligne, c, ancrages, params.clip_spacing)[0])
                return dict(crabs=tous, crab_gap=ecart_max)

            paliers = smoothing.build_ladder(
                res["grid"],
                res["routes"],
                res["topo"],
                params,
                anchor_points=anchor_points,
                on_level=lambda rec, i, n: prog(i, n, f"Lissage {rec['level']} %"),
                post=(lambda pts, geo: self._connector_heads(pts, geo, params, poses, models,
                                                             collider)) if poses else None,
                extras=poser_crabes
            )

            palier = smoothing.best_level(paliers, params.min_bend_radius) or paliers[-1]
            res["smooth"] = palier["points"]
            res["seg_geo"] = palier["seg_geo"]
            res["crabs"] = palier.get("crabs") or []

            interf = palier["interferences"]
            resume = analysis.summary(res, interf, params, cable_diameter)
            resume["n_cables"] = len(terminals)

            etape("Solides : câbles et crabes")
            horodatage = time.strftime("%Y%m%d_%H%M%S")
            dossier_exp = bridge.BASE_CACHE / "click_and_root"
            os.makedirs(dossier_exp, exist_ok=True)

            cables = geometry.harness_solid(res, cable_diameter=cable_diameter, polylines=res["smooth"])
            chemin_cables = str(dossier_exp / f"cables_{horodatage}.stl")
            geometry.save_stl(cables, chemin_cables)

            fichiers = dict(cables=chemin_cables, crabes=None, passages=None,
                            csv=geometry.export_catia_curves_csv(res, str(dossier_exp / f"fibres_{horodatage}.csv"),
                                                                 cable_diameter))
            pieces = crab_mod.crab_meshes(res.get("crabs") or [], crabe)
            import pyvista as pv
            if pieces:
                chemin_crabes = str(dossier_exp / f"crabes_{horodatage}.stl")
                geometry.save_stl(pv.merge(pieces), chemin_crabes)
                fichiers["crabes"] = chemin_crabes

            if anchors_passages:
                nuage_points = pv.PolyData(np.array(anchors_passages))
                spheres = nuage_points.glyph(geom=pv.Sphere(radius=4.0), scale=False)

                chemin_passages = str(dossier_exp / f"passages_{horodatage}.stl")
                geometry.save_stl(spheres, chemin_passages)
                fichiers["passages"] = chemin_passages

            etape("Envoi dans CATIA")
            messages = self._pousser(fichiers)

            self.q.put(("FINI", dict(res=res, palier=palier, resume=resume, interferences=interf, fichiers=fichiers,
                                     messages=messages, info=info, n_cloud=len(cloud))))
        except Exception as exc:
            traceback.print_exc()
            self.q.put(("ECHEC", str(exc)))

    def _connector_heads(self, polylines, seg_geo, params, poses, models, collider):
        if not poses: return polylines, seg_geo
        pts = conn.apply_connector_geometry(polylines, poses, lead_in=params.connector_lead_in, models=models,
                                            min_radius=params.min_bend_radius, step=params.resample_step,
                                            collider=collider, clearance=params.clearance, logger=LOG)
        geo = conn.apply_connector_geometry_segments(seg_geo, poses, lead_in=params.connector_lead_in, models=models,
                                                     min_radius=params.min_bend_radius, step=params.resample_step,
                                                     tol=2.0 * params.resolution, collider=collider,
                                                     clearance=params.clearance, logger=LOG)
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
            return ["[OK] Harnais et passages insérés dans CATIA."]
        except Exception as e:
            return [f"[ÉCHEC] CATIA COM : {e}"]
        finally:
            try:
                pythoncom.CoUninitialize()
            except:
                pass


def launch():
    """Point d'entrée de l'application UI principale."""
    applog.banner(VERSION)
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.25)
    except tk.TclError:
        pass

    app = AppController(root)
    root.mainloop()
