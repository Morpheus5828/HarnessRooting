"""
Etat partage entre les deux pages.

Un seul objet transporte la maquette, le nuage de collision, les passages et
le dernier resultat de cheminement. Les pages s'y abonnent : quand la page 1
charge une maquette, la page 2 se met a jour toute seule.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from ..catia.bridge import BASE_CACHE
from ..core.collider import MeshCollider
from ..log import Logger
from ..pvutil import surface

LOG = Logger("etat")


class AppState:
    def __init__(self):
        self.mesh = None              # maillage PyVista fusionne
        self._collider = None         # detection de collision exacte
        self.cloud = None             # nuage de points, pour l'AFFICHAGE seul
        self.bounds = None            # (xmin, xmax, ymin, ymax, zmin, zmax)
        self.scene_info = None        # catia.mesh.SceneInfo
        self.scene_stl = None         # copie disque de la maquette fusionnee
        self.scan_result = None       # passages / fixations
        self.result = None            # dict renvoye par run_pipeline
        self.params = None            # RoutingParams du dernier calcul
        self.summary = None
        self.interferences = None
        self.attractors = np.empty((0, 3))
        self.terminals = []
        self.harness_stl = None       # dernier STL de harnais genere
        self.harness_csv = None       # fibres neutres decimees pour CATIA
        self.cable_diameter = 6.0
        self.ladder = []              # paliers de lissage precalcules
        self.level = 0                # pourcentage de lissage retenu
        self._listeners = {}

    # ---- abonnements ----
    def on(self, event, callback):
        self._listeners.setdefault(event, []).append(callback)

    def emit(self, event, *args):
        for cb in self._listeners.get(event, []):
            try:
                cb(*args)
            except Exception as exc:
                LOG.exception(f"abonne '{event}' en erreur", exc)

    # ---- maquette ----
    @property
    def has_scene(self):
        return self.cloud is not None and len(self.cloud) > 0

    @property
    def has_result(self):
        return self.result is not None

    @property
    def collider(self):
        """
        Detection de collision EXACTE, construite une fois par maquette.

        C'est cet objet, et non le nuage de points, qui sert au routage et au
        controle d'interference : le nuage ne reste utilise que pour l'apercu.
        """
        if self._collider is None and self.mesh is not None:
            self._collider = MeshCollider.from_mesh(self.mesh, name="maquette")
        return self._collider

    def set_scene(self, mesh, cloud, info):
        self.mesh = mesh
        self._collider = None
        self.cloud = np.asarray(cloud, float)
        self.bounds = tuple(float(v) for v in mesh.bounds)
        self.scene_info = info
        self.result = None
        self.ladder = []
        self.level = 0
        self.emit("scene", info)

    def set_passages(self, scan_result):
        self.scan_result = scan_result
        self.emit("passages", scan_result)

    def set_result(self, result, summary, interferences, params):
        self.result = result
        self.summary = summary
        self.interferences = interferences
        self.params = params
        self.ladder = []
        self.level = 0
        self.emit("result", result)

    def set_ladder(self, records):
        self.ladder = list(records)
        self.emit("ladder", self.ladder)

    def level_record(self, level=None):
        """Palier de lissage courant (ou le plus proche)."""
        if not self.ladder:
            return None
        target = self.level if level is None else level
        return min(self.ladder, key=lambda r: abs(r["level"] - target))

    # ---- fichiers de travail ----
    def cache_dir(self, *parts) -> Path:
        d = Path(BASE_CACHE).joinpath(*parts)
        os.makedirs(d, exist_ok=True)
        return d

    def save_scene_stl(self):
        """La maquette fusionnee est ecrite une fois : la visionneuse externe
        et la macro CATIA en ont besoin sous forme de fichier."""
        if self.mesh is None:
            return None
        if self.scene_stl and os.path.exists(self.scene_stl):
            return self.scene_stl
        path = str(self.cache_dir("work") / "scene_fusionnee.stl")
        surface(self.mesh).save(path)
        self.scene_stl = path
        LOG.debug(f"maquette fusionnee ecrite : {path}")
        return path

    def passage_list(self):
        sr = self.scan_result
        if not (sr and getattr(sr, "ran", False)):
            return []
        return list(getattr(sr, "passages", None) or [])
