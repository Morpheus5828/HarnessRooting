"""
Canal de progression entre le thread de calcul et l'interface.

Le coeur algorithmique ne connait ni Tkinter ni matplotlib : il pousse des
evenements (tuples `(TYPE, payload)`) dans une `queue.Queue` que l'interface
depile a intervalle regulier. Le meme objet porte le drapeau d'annulation, ce
qui permet a l'utilisateur d'arreter un cheminement trop long sans tuer le
processus.
"""

from __future__ import annotations

import queue
import threading
import time

# --- types d'evenements ---------------------------------------------------
PHASE = "PHASE"            # changement d'etape (texte affichable)
ASTAR_TICK = "ASTAR_TICK"  # progression a l'interieur d'une recherche A*
ASTAR_END = "ASTAR_END"    # une recherche A* vient de se terminer
SWEEP = "SWEEP"            # amelioration acceptee par l'algorithme 2
DONE = "DONE"              # pipeline termine, payload = dict resultat
FAILED = "FAILED"          # pipeline en echec, payload = message
CANCELLED = "CANCELLED"    # annulation confirmee par le thread de calcul


class Cancelled(Exception):
    """Levee dans le thread de calcul quand l'utilisateur demande l'arret."""


class Reporter:
    """Emetteur d'evenements + drapeau d'annulation (thread-safe)."""

    def __init__(self, q: "queue.Queue | None" = None):
        self.q = q if q is not None else queue.Queue()
        self._stop = threading.Event()
        self.t0 = time.time()
        self.n_astar = 0          # nombre d'appels A* termines
        self.n_nodes = 0          # noeuds developpes, tous appels confondus

    # ---- annulation ----
    def cancel(self):
        self._stop.set()

    @property
    def cancelled(self) -> bool:
        return self._stop.is_set()

    def raise_if_cancelled(self):
        if self._stop.is_set():
            raise Cancelled()

    # ---- emission ----
    def _put(self, kind, payload):
        self.q.put((kind, payload))

    def phase(self, text, ratio=None):
        self._put(PHASE, dict(text=text, ratio=ratio, t=time.time() - self.t0))

    def astar_tick(self, nodes_total, seconds, g_current):
        self._put(ASTAR_TICK, dict(nodes=nodes_total, seconds=seconds, g=g_current))

    def astar_end(self, nodes, seconds, found, cable=None, stage=""):
        self.n_astar += 1
        self.n_nodes += int(nodes)
        self._put(ASTAR_END, dict(index=self.n_astar, nodes=int(nodes),
                                  seconds=seconds, found=bool(found),
                                  cable=cable, stage=stage,
                                  cumulative=self.n_nodes))

    def sweep(self, index, f, f_L, f_B, accepted=True):
        self._put(SWEEP, dict(index=index, f=f, f_L=f_L, f_B=f_B,
                              accepted=accepted, t=time.time() - self.t0))

    def done(self, result):
        self._put(DONE, result)

    def failed(self, message):
        self._put(FAILED, message)

    def confirm_cancelled(self):
        self._put(CANCELLED, None)
