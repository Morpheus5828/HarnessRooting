"""
Detection de collision EXACTE, sur le maillage et non sur un nuage de points.

Un nuage de points ne peut pas garantir l'absence de traversee : entre deux
points echantillonnes il y a du vide, et un toron peut passer au travers d'une
cloison sans qu'aucun point ne soit viole. Deux primitives VTK reglent le
probleme :

  * `vtkImplicitPolyDataDistance` : distance exacte d'un point a la surface
    triangulee (~2 us par point) ;
  * `vtkOBBTree.IntersectWithLine` : intersection exacte d'un segment avec les
    triangles (~2 us par segment).

La regle appliquee dans tout le solveur devient donc :
    une arete est interdite si l'un de ses noeuds viole la garde
    OU si le segment traverse un triangle.

Le second terme est celui qui garantit "le harnais ne traverse jamais le
maillage", quelle que soit la finesse de la grille.
"""

from __future__ import annotations

import math
import time

import numpy as np

from ..log import Logger
from ..pvutil import surface

LOG = Logger("collision")

class MeshCollider:
    """
    Interroge la geometrie reelle.

    Deux modes :
      * `from_mesh` : maillage triangule -> distances et traversees exactes ;
      * `from_points` : nuage seul -> mode degrade par KD-tree, conserve pour
        les cas ou aucun maillage n'est disponible (jeux de test, demo).
        Le mode degrade est signale : il ne garantit pas la non-traversee.
    """

    def __init__(self, mesh=None, points=None, name=""):
        if mesh is None and points is None:
            raise ValueError("MeshCollider : fournir un maillage ou un nuage.")
        self.name = name
        self.mesh = None
        self.exact = mesh is not None
        self._imp = None
        self._obb = None
        self._kdtree = None
        self.n_cross_tests = 0
        self.n_cross_hits = 0

        if mesh is not None:
            self._build_from_mesh(mesh)
        else:
            from scipy.spatial import cKDTree
            pts = np.asarray(points, float).reshape(-1, 3)
            self._kdtree = cKDTree(pts)
            self._bounds = tuple(np.column_stack([pts.min(0), pts.max(0)]).ravel())
            LOG.warn("collider en mode degrade (nuage de points) : la "
                     "non-traversee du maillage n'est pas garantie")

    # ------------------------------------------------------------------
    @classmethod
    def from_mesh(cls, mesh, name=""):
        return cls(mesh=mesh, name=name)

    @classmethod
    def from_points(cls, points, name=""):
        return cls(points=points, name=name)

    @classmethod
    def coerce(cls, obstacles):
        """Accepte un MeshCollider, un maillage PyVista, ou un nuage."""
        if isinstance(obstacles, MeshCollider):
            return obstacles
        if hasattr(obstacles, "extract_surface") or hasattr(obstacles, "n_cells"):
            return cls.from_mesh(obstacles)
        return cls.from_points(obstacles)

    # ------------------------------------------------------------------
    def _build_from_mesh(self, mesh):
        import vtk

        t0 = time.time()
        surf = surface(mesh)
        if surf.n_cells == 0:
            raise ValueError("Maillage vide : aucune surface a eviter.")
        self.mesh = surf
        poly = surf if not hasattr(surf, "GetNumberOfCells") else surf

        self._imp = vtk.vtkImplicitPolyDataDistance()
        self._imp.SetInput(poly)

        self._obb = vtk.vtkOBBTree()
        self._obb.SetDataSet(poly)
        self._obb.BuildLocator()

        self._bounds = tuple(float(v) for v in surf.bounds)
        self._vtk_points = vtk.vtkPoints
        LOG.ok(f"collider exact construit sur {surf.n_cells:,} triangles "
               f"en {time.time() - t0:.1f} s".replace(",", " "))

    # ------------------------------------------------------------------
    @property
    def bounds(self):
        return self._bounds

    def distance_one(self, point) -> float:
        """Distance (non signee) d'un point a la surface."""
        if self._imp is not None:
            return abs(float(self._imp.EvaluateFunction(
                float(point[0]), float(point[1]), float(point[2]))))
        return float(self._kdtree.query(np.asarray(point, float))[0])

    def distance(self, points) -> np.ndarray:
        """
        Distances (non signees) d'un lot de points.

        On interroge l'objet VTK deja construit, point par point. La voie
        "vectorisee" de PyVista (`compute_implicit_distance`) est un piege :
        elle reconstruit la fonction de distance sur TOUT le maillage a chaque
        appel. Sur un profil de cheminement, ce seul `SetInput` representait
        317 s sur 585 s -- pour des lots d'une quinzaine de points, la boucle
        est plusieurs centaines de fois plus rapide.
        """
        pts = np.atleast_2d(np.asarray(points, float))
        if self._imp is None:
            return self._kdtree.query(pts)[0]
        evaluate = self._imp.EvaluateFunction
        out = np.empty(len(pts), float)
        for i, q in enumerate(pts):
            out[i] = evaluate(q[0], q[1], q[2])
        return np.abs(out)

    # ------------------------------------------------------------------
    def signed_distance(self, points) -> np.ndarray:
        """
        Distance SIGNEE : negative a l'interieur de la matiere.

        La distance non signee suffit a garder ses distances ; pour savoir si
        un corps pose contre la structure la penetre, il faut le signe. Sans
        VTK, le signe est indecidable et la distance non signee est rendue --
        l'appelant doit alors se contenter d'un controle approche.
        """
        pts = np.atleast_2d(np.asarray(points, float))
        if self._imp is None:
            return self._kdtree.query(pts)[0]
        evaluate = self._imp.EvaluateFunction
        out = np.empty(len(pts), float)
        for i, q in enumerate(pts):
            out[i] = evaluate(q[0], q[1], q[2])
        return out

    def any_closer_than(self, points, limit) -> bool:
        """Un des points est-il a moins de `limit` de la surface ?"""
        return bool((self.distance(points) < limit).any())

    def gradient(self, points) -> np.ndarray:
        """
        Direction unitaire d'eloignement de la surface, en chaque point.

        `vtkImplicitPolyDataDistance.EvaluateGradient` donne le gradient de la
        distance signee : c'est exactement la direction dans laquelle pousser un
        point pour regagner de la garde. En mode degrade, on retombe sur la
        direction depuis le point de nuage le plus proche.
        """
        pts = np.atleast_2d(np.asarray(points, float))
        out = np.zeros_like(pts)
        if self._imp is not None:
            g = [0.0, 0.0, 0.0]
            for i, q in enumerate(pts):
                self._imp.EvaluateGradient((float(q[0]), float(q[1]), float(q[2])), g)
                out[i] = g
        else:
            _, idx = self._kdtree.query(pts)
            out = pts - self._kdtree.data[idx]
        n = np.linalg.norm(out, axis=1, keepdims=True)
        return np.divide(out, np.where(n < 1e-12, 1.0, n))

    # ------------------------------------------------------------------
    def crosses(self, a, b) -> bool:
        """Le segment [a, b] traverse-t-il un triangle du maillage ?"""
        if self._obb is None:
            return False                      # mode degrade : indecidable
        self.n_cross_tests += 1
        pts = self._vtk_points()
        code = self._obb.IntersectWithLine(
            (float(a[0]), float(a[1]), float(a[2])),
            (float(b[0]), float(b[1]), float(b[2])), pts, None)
        hit = (code != 0) or (pts.GetNumberOfPoints() > 0)
        if hit:
            self.n_cross_hits += 1
        return bool(hit)

    def edge_blocked(self, a, b, da=None, db=None, clearance=0.0,
                     step=2.0) -> bool:
        """
        Une arete est interdite si le SEGMENT descend sous la garde, ou s'il
        traverse un triangle.

        Controler seulement les deux extremites ne suffit pas : avec une maille
        de 30 mm, la diagonale mesure 52 mm et son milieu peut passer beaucoup
        plus pres de la structure que ses extremites. C'est ce qui laissait des
        interferences residuelles sur le trace brut.

        Sortie rapide : pour tout point p du segment,
            d(p) >= min(d(a), d(b)) - longueur/2 ,
        donc si cette borne depasse deja la garde, aucun echantillonnage n'est
        necessaire. C'est ce qui rend le controle exact abordable dans l'A*.
        """
        ax, ay, az = float(a[0]), float(a[1]), float(a[2])
        bx, by, bz = float(b[0]), float(b[1]), float(b[2])
        length = math.dist((ax, ay, az), (bx, by, bz))
        if da is None:
            da = self.distance_one(a)
        if db is None:
            db = self.distance_one(b)
        if min(da, db) - 0.5 * length >= clearance:
            return False
        a = np.array((ax, ay, az))
        b = np.array((bx, by, bz))

        n = max(3, int(length / max(step, 1e-6)) + 1)
        pts = a + np.linspace(0.0, 1.0, n)[:, None] * (b - a)
        if self.any_closer_than(pts, clearance):
            return True
        if self._obb is None:
            return False
        return self.crosses(a, b)

    def crossing_indices(self, polyline) -> np.ndarray:
        """Indices des segments d'une polyligne qui traversent le maillage."""
        pts = np.asarray(polyline, float).reshape(-1, 3)
        if self._obb is None or len(pts) < 2:
            return np.empty(0, int)
        d = self.distance(pts)
        seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
        risky = np.minimum(d[:-1], d[1:]) <= seg
        out = [i for i in np.where(risky)[0] if self.crosses(pts[i], pts[i + 1])]
        return np.asarray(out, int)

    def stats(self):
        return dict(exact=self.exact, cross_tests=self.n_cross_tests,
                    cross_hits=self.n_cross_hits,
                    triangles=int(self.mesh.n_cells) if self.mesh is not None else 0)
