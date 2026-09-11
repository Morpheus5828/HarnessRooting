"""
Graphiques de l'application (matplotlib embarque dans Tkinter).

Deux tableaux de bord :
  * `MetricsDashboard` : quatre vues, dont les deux demandees explicitement --
    les noeuds visites par iteration (en direct pendant le calcul) et le
    camembert des interferences (a la fin) ;
  * `Result3D` : le harnais dans son environnement, directement dans la page.
"""

from __future__ import annotations

import numpy as np
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
import tkinter as tk

from .theme import C, STATUS_COLORS

# Echelle des figures, fixee au demarrage d'apres la taille de l'ecran : sur un
# portable court, un graphe de 5,6 pouces (560 px) mange a lui seul la moitie
# de la hauteur utile. La liste tient lieu de variable modifiable de module.
FIG_SCALE = [1.0]


def set_figure_scale(scale):
    """Reduit (ou rend) la taille de tous les graphes crees ensuite."""
    FIG_SCALE[0] = float(scale)

REFRESH_MS = 350          # cadence maximale de redessin pendant le calcul


class _CanvasFrame(tk.Frame):
    """Socle commun : une figure matplotlib dans un frame Tk, redessin regule."""

    def __init__(self, parent, figsize=(7.6, 5.6), dpi=100):
        figsize = (figsize[0] * FIG_SCALE[0], figsize[1] * FIG_SCALE[0])
        super().__init__(parent, bg=C["surface"])
        self.fig = Figure(figsize=figsize, dpi=dpi, facecolor=C["surface"])
        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.get_tk_widget().configure(bg=C["surface"], highlightthickness=0)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self._dirty = False
        self._pending = None

    def request_draw(self):
        """Marque la figure a redessiner ; le dessin reel est groupe."""
        self._dirty = True
        if self._pending is None:
            self._pending = self.after(REFRESH_MS, self._do_draw)

    def _do_draw(self):
        self._pending = None
        if self._dirty:
            self._dirty = False
            try:
                self.canvas.draw_idle()
            except Exception:
                pass

    def draw_now(self):
        self._dirty = False
        try:
            self.canvas.draw_idle()
        except Exception:
            pass

    def save(self, path, dpi=150):
        self.fig.savefig(path, dpi=dpi, facecolor=self.fig.get_facecolor())
        return path


def draw_connector(ax, center, direction, size=(45.0, 26.0, 20.0),
                   color="#2E9E5B"):
    """Pave droit figurant le connecteur, aligne sur la sortie du cable."""
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    d = np.asarray(direction, float)
    n = np.linalg.norm(d)
    d = d / n if n > 1e-9 else np.array([1.0, 0.0, 0.0])
    tmp = np.array([0.0, 0.0, 1.0]) if abs(d[2]) < 0.9 else np.array([0.0, 1.0, 0.0])
    e2 = np.cross(d, tmp)
    e2 /= np.linalg.norm(e2)
    e3 = np.cross(d, e2)
    R = np.column_stack([d, e2, e3]) * (np.asarray(size, float) / 2.0)
    c = np.asarray(center, float) - d * size[0] * 0.5
    corners = np.array([[sx, sy, sz] for sx in (-1, 1) for sy in (-1, 1)
                        for sz in (-1, 1)])
    v = c + corners @ R.T
    faces = [[0, 1, 3, 2], [4, 5, 7, 6], [0, 1, 5, 4],
             [2, 3, 7, 6], [0, 2, 6, 4], [1, 3, 7, 5]]
    ax.add_collection3d(Poly3DCollection([v[f] for f in faces], facecolor=color,
                                         edgecolor="#12233A", linewidths=0.4,
                                         alpha=0.92, zorder=5))


def draw_crab(ax, matrix, dx, dy, height, color="#C77700"):
    """
    Crabe pose : pave a l'assise sur la structure, oriente par son repere.

    On dessine exactement le repere qui a servi au controle de pose -- x le
    long du cable, z depuis la surface -- sans quoi le dessin montrerait
    l'attache ailleurs que la ou sa collision a ete verifiee.
    """
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    M = np.asarray(matrix, float)
    coins = np.array([[sx * dx, sy * dy, sz * height]
                      for sx in (-1, 1) for sy in (-1, 1) for sz in (0, 1)])
    v = coins @ M[:3, :3].T + M[:3, 3]
    faces = [[0, 1, 3, 2], [4, 5, 7, 6], [0, 1, 5, 4],
             [2, 3, 7, 6], [0, 2, 6, 4], [1, 3, 7, 5]]
    ax.add_collection3d(Poly3DCollection([v[f] for f in faces], facecolor=color,
                                         edgecolor="#12233A", linewidths=0.4,
                                         alpha=0.95, zorder=6))


def equal_axes(ax, lo, hi, pad=0.04, zoom=1.32):
    """
    Repere orthonorme qui remplit le cadre.

    Une boite cubique (1, 1, 1) conserve les proportions mais laisse enormement
    de vide autour d'un harnais allonge. On donne donc a la boite les
    proportions reelles des donnees : l'echelle reste identique sur les trois
    axes, et le dessin occupe toute la place disponible.
    """
    lo = np.asarray(lo, float)
    hi = np.asarray(hi, float)
    span = hi - lo
    biggest = float(span.max()) or 1.0
    span = np.maximum(span, 0.02 * biggest)       # evite une boite plate
    mid = 0.5 * (lo + hi)
    half = 0.5 * span * (1.0 + pad)
    ax.set_xlim(mid[0] - half[0], mid[0] + half[0])
    ax.set_ylim(mid[1] - half[1], mid[1] + half[1])
    ax.set_zlim(mid[2] - half[2], mid[2] + half[2])
    aspect = tuple(span / span.max())
    try:
        # `zoom` (matplotlib >= 3.6) agrandit le trace dans le cadre : sans lui,
        # une vue 3D laisse un quart de l'image vide.
        ax.set_box_aspect(aspect, zoom=zoom)
    except TypeError:
        ax.set_box_aspect(aspect)
    except Exception:
        pass


def _empty(ax, message):
    ax.text(0.5, 0.5, message, transform=ax.transAxes, ha="center", va="center",
            color=C["muted"], fontsize=9)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.grid(False)
    for spine in ax.spines.values():
        spine.set_visible(False)


class MetricsDashboard(_CanvasFrame):
    """
    Quatre cadrans :
      (haut gauche)  noeuds visites par iteration de recherche      -- en direct
      (haut droite)  convergence de l'objectif f = wL.fL + wB.fB    -- en direct
      (bas gauche)   camembert des interferences                    -- a la fin
      (bas droite)   rayon de cintrage par troncon                  -- a la fin
    """

    def __init__(self, parent):
        super().__init__(parent, figsize=(9.0, 5.6))
        gs = self.fig.add_gridspec(2, 2, hspace=0.62, wspace=0.34,
                                   left=0.09, right=0.91, top=0.91, bottom=0.10)
        self.ax_nodes = self.fig.add_subplot(gs[0, 0])
        self.ax_cum = self.ax_nodes.twinx()
        self.ax_conv = self.fig.add_subplot(gs[0, 1])
        self.ax_pie = self.fig.add_subplot(gs[1, 0])
        self.ax_ladder = self.fig.add_subplot(gs[1, 1])
        self.ax_bend = self.ax_ladder.twinx()
        self.reset()

    # ---------------- cycle de vie ----------------
    def reset(self):
        self.nodes = []          # noeuds developpes par appel A*
        self.found = []          # l'appel a-t-il abouti ?
        self.cumulative = []
        self.stages = []
        self.f_hist = []
        self.fL_hist = []
        self.fB_hist = []
        self._draw_nodes()
        self._draw_conv()
        _empty(self.ax_pie, "Interferences :\ndisponibles a la fin du calcul")
        self.ax_pie.set_title("Interferences avec la structure")
        self.ax_bend.clear()
        self.ax_bend.grid(False)
        self.ax_bend.set_yticks([])
        _empty(self.ax_ladder, "Effet du lissage :\ndisponible apres le cheminement")
        self.ax_ladder.set_title("Interferences selon le lissage")
        self.draw_now()

    # ---------------- alimentation en direct ----------------
    def push_astar(self, rec):
        self.nodes.append(int(rec["nodes"]))
        self.found.append(bool(rec["found"]))
        self.cumulative.append(int(rec["cumulative"]))
        self.stages.append(rec.get("stage", ""))
        self._draw_nodes()
        self.request_draw()

    def push_sweep(self, rec):
        self.f_hist.append(float(rec["f"]))
        self.fL_hist.append(float(rec["f_L"]))
        self.fB_hist.append(float(rec["f_B"]))
        self._draw_conv()
        self.request_draw()

    # ---------------- cadran 1 : noeuds par iteration ----------------
    def _draw_nodes(self):
        ax, ax2 = self.ax_nodes, self.ax_cum
        ax.clear()
        ax2.clear()
        ax.set_title("Noeuds explores par iteration")
        ax.set_xlabel("iteration (recherche de chemin)")
        ax.set_ylabel("noeuds explores")
        ax2.grid(False)
        # Axes.clear() ramene l'axe jumeau a gauche : on le renvoie a droite,
        # sinon son libelle se superpose a celui de l'axe principal.
        ax2.yaxis.set_label_position("right")
        ax2.yaxis.tick_right()

        n = len(self.nodes)
        if n == 0:
            ax.text(0.5, 0.5, "En attente du lancement du cheminement",
                    transform=ax.transAxes, ha="center", va="center",
                    color=C["muted"], fontsize=9)
            ax.set_xlim(0, 1)
            ax.set_ylim(0, 1)
            return

        x = np.arange(1, n + 1)
        y = np.array(self.nodes, float)
        colors = [C["serie1"] if ok else C["error"] for ok in self.found]
        if n <= 150:
            ax.bar(x, y, color=colors, width=0.85, linewidth=0)
        else:
            ax.plot(x, y, color=C["serie1"], lw=1.2)
            bad = np.where(~np.array(self.found))[0]
            if len(bad):
                ax.plot(x[bad], y[bad], "o", color=C["error"], ms=3)
        moy = float(y.mean())
        ax.axhline(moy, color=C["muted"], ls="--", lw=1)
        ax2.plot(x, np.array(self.cumulative, float) / 1000.0,
                 color=C["serie3"], lw=1.4, alpha=0.85)
        ax2.set_ylabel("cumul (milliers)", color=C["serie3"], fontsize=8)
        ax2.tick_params(axis="y", colors=C["serie3"], labelsize=7)

        total = int(self.cumulative[-1])
        ax.legend(handles=[
            Line2D([], [], color=C["serie1"], lw=6, label="chemin trouve"),
            Line2D([], [], color=C["muted"], ls="--", lw=1,
                   label=f"moyenne {moy:,.0f}".replace(",", " ")),
            Line2D([], [], color=C["serie3"], lw=1.6,
                   label=f"cumul {total:,.0f}".replace(",", " ")),
        ], loc="upper left", fontsize=7, framealpha=0.85)
        ax.set_xlim(0.4, max(n, 1) + 0.6)
        ax.set_ylim(0, float(y.max()) * 1.38 or 1.0)   # place pour la legende

    # ---------------- cadran 2 : convergence ----------------
    def _draw_conv(self):
        ax = self.ax_conv
        ax.clear()
        ax.set_title("Convergence de l'objectif")
        ax.set_xlabel("ameliorations acceptees (algorithme 2)")
        ax.set_ylabel("f = wL.fL + wB.fB")
        if not self.f_hist:
            ax.text(0.5, 0.5, "L'objectif apparait des la premiere passe",
                    transform=ax.transAxes, ha="center", va="center",
                    color=C["muted"], fontsize=9)
            ax.set_xlim(0, 1)
            ax.set_ylim(0, 1)
            return
        x = np.arange(len(self.f_hist))
        ax.plot(x, self.f_hist, "o-", color=C["serie2"], ms=4,
                label="f (global)")
        ax.plot(x, self.fB_hist, "-", color=C["serie1"], lw=1.2, alpha=0.8,
                label="fB (torons)")
        if len(self.f_hist) > 1:
            gain = 100.0 * (self.f_hist[0] - self.f_hist[-1]) / max(self.f_hist[0], 1e-9)
            ax.set_title(f"Convergence de l'objectif  (-{gain:.1f} %)")
        ax.legend(loc="best", fontsize=7, framealpha=0.85)
        ax.margins(x=0.05, y=0.15)

    # ---------------- cadrans 3 et 4 : resultats ----------------
    def finalize(self, res, interf, params):
        self._draw_pie(interf)
        self.draw_now()

    def set_ladder(self, records, params, current_level=None):
        """Courbe "interferences en fonction du pourcentage de lissage"."""
        self._draw_ladder(records, params, current_level)
        self.draw_now()

    def _draw_pie(self, interf):
        ax = self.ax_pie
        ax.clear()
        ax.grid(False)
        counts = interf["counts"]
        labels = ["Conforme", "Marge faible", "Interference"]
        keys = ["conforme", "limite", "interference"]
        values = [counts[k] for k in keys]
        colors = [STATUS_COLORS[k] for k in keys]

        keep = [(l, v, c) for l, v, c in zip(labels, values, colors) if v > 0]
        if not keep:
            _empty(ax, "Aucun point de mesure")
            ax.set_title("Interferences avec la structure")
            return
        labels, values, colors = zip(*keep)
        total = float(sum(values))

        wedges, _ = ax.pie(values, colors=colors, startangle=90,
                           counterclock=False,
                           wedgeprops=dict(width=0.42, edgecolor=C["surface"],
                                           linewidth=1.5))
        # le coeur du beignet ne contient que le chiffre : un libelle sur deux
        # lignes deborderait sur l'anneau des que le cadran est petit.
        n_zones = interf["n_zones"]
        ax.text(0, 0.06, f"{n_zones}", ha="center", va="center",
                fontsize=19, fontweight="bold",
                color=C["error"] if n_zones else C["ok"])
        ax.text(0, -0.20, "zones", ha="center", va="center",
                fontsize=7.5, color=C["muted"])
        ax.set_title(f"Zones en interference  "
                     f"(clearance {interf['clearance']:.0f} mm)")

        legend_labels = [f"{l}  {v / total * 100:.1f} %" for l, v in zip(labels, values)]
        sev = interf["severity_counts"]
        if n_zones:
            detail = ", ".join(f"{k} : {v}" for k, v in sev.items() if v)
            legend_labels.append(f"[{detail}]")
            colors = list(colors) + [C["surface"]]
        ax.legend(handles=[Line2D([], [], color=c, lw=7) for c in colors],
                  labels=legend_labels, loc="lower center",
                  bbox_to_anchor=(0.5, -0.28), fontsize=7, ncol=1,
                  handlelength=1.2)

    def _draw_ladder(self, records, params, current=None):
        """
        Le cadran central du nouveau mode de travail : pour chaque pourcentage
        de lissage, le nombre d'interferences (barres) et le rayon de courbure
        minimal obtenu (courbe). L'utilisateur y lit directement jusqu'ou il
        peut lisser sans percer la structure.
        """
        ax, ax2 = self.ax_ladder, self.ax_bend
        ax.clear()
        ax2.clear()
        ax2.grid(False)
        ax.set_title("Interferences selon le lissage")
        ax.set_xlabel("lissage applique (%)")
        ax.set_ylabel("zones en interference")

        if not records:
            _empty(ax, "Effet du lissage :\ndisponible apres le cheminement")
            ax2.set_yticks([])
            return

        x = np.array([r["level"] for r in records], float)
        zones = np.array([r["n_zones"] for r in records], float)
        cross = np.array([r["n_crossings"] for r in records], float)
        rmin = np.array([r["min_bend_radius"] for r in records], float)
        rmin = np.where(np.isfinite(rmin), rmin, np.nan)

        width = max(2.0, (x[1] - x[0]) * 0.7) if len(x) > 1 else 4.0
        colors = [C["error"] if c > 0 else (C["warn"] if z > 0 else C["ok"])
                  for z, c in zip(zones, cross)]
        ax.bar(x, np.maximum(zones, cross), color=colors, width=width, linewidth=0,
               zorder=2)
        ax.set_ylim(0, max(1.0, float(np.max([zones.max(), cross.max()])) * 1.45))

        ax2.plot(x, rmin, "o-", color=C["serie2"], ms=3.5, lw=1.5, zorder=3)
        ax2.set_ylabel("rayon de courbure min (mm)", color=C["serie2"], fontsize=8)
        ax2.tick_params(axis="y", colors=C["serie2"], labelsize=7)
        ax2.yaxis.set_label_position("right")
        ax2.yaxis.tick_right()
        seuil = float(params.min_bend_radius)
        ax2.axhline(seuil, color=C["serie2"], ls="--", lw=1, alpha=0.6)
        finite = rmin[np.isfinite(rmin)]
        if len(finite):
            ax2.set_ylim(0, max(float(finite.max()), seuil) * 1.35)

        if current is not None:
            ax.axvline(current, color=C["accent"], lw=2, alpha=0.85, zorder=1)
            rec = next((r for r in records if r["level"] == current), None)
            if rec is not None:
                ax.text(0.02, 0.95,
                        f"reglage courant : {current} %\n"
                        f"{rec['n_zones']} zone(s), "
                        f"{rec['n_crossings']} traversee(s)\n"
                        f"R min {rec['min_bend_radius']:.0f} mm",
                        transform=ax.transAxes, va="top", fontsize=7.5,
                        color=C["error"] if (rec["n_zones"] or rec["n_crossings"])
                        else C["ok"])
        ax.legend(handles=[
            Line2D([], [], color=C["ok"], lw=6, label="aucune interference"),
            Line2D([], [], color=C["warn"], lw=6, label="sous la garde"),
            Line2D([], [], color=C["error"], lw=6, label="traversee"),
            Line2D([], [], color=C["serie2"], lw=1.5, marker="o", ms=3,
                   label=f"R min (seuil {seuil:.0f} mm)"),
        ], loc="upper right", fontsize=6.5, framealpha=0.85)


class Result3D(_CanvasFrame):
    """
    Vue 3D integree a la page : structure en nuage clair, torons en bleu,
    epaisseur proportionnelle au nombre de cables, connecteurs, branchements,
    clips et zones d'interference.

    C'est une vue matplotlib (et non PyVista) parce qu'elle doit s'afficher
    dans la page sans dependre du pilote OpenGL du poste. Le bouton
    "Vue 3D interactive" ouvre, lui, la fenetre PyVista complete.
    """

    VIEWS = {"Isometrique": (24, -60), "Dessus": (89, -90),
             "Cote": (2, 0), "Avant": (2, -90)}

    def __init__(self, parent):
        super().__init__(parent, figsize=(7.8, 6.2))
        self.ax = self.fig.add_subplot(111, projection="3d")
        self.fig.subplots_adjust(left=0.0, right=1.0, top=1.0, bottom=0.0)
        self._state = None
        self.show_structure = True
        self.show_raw = False
        self.show_clips = True
        self.clear_view("Lancer un cheminement pour afficher le resultat")

    def clear_view(self, message):
        self.ax.clear()
        self.ax.set_axis_off()
        self.ax.text2D(0.5, 0.5, message, transform=self.ax.transAxes,
                       ha="center", va="center", color=C["muted"], fontsize=10)
        self.draw_now()

    def set_result(self, state):
        """`state` : dict(bundles, raw, smooth, branch_points, clips_fix,
        clips_new, cloud, zones, terminals)."""
        self._state = state
        self.redraw()

    def set_view(self, name):
        if name in self.VIEWS and self._state is not None:
            elev, azim = self.VIEWS[name]
            self.ax.view_init(elev=elev, azim=azim)
            self.draw_now()

    def redraw(self):
        st = self._state
        if st is None:
            return
        ax = self.ax
        ax.clear()
        ax.set_axis_off()

        cloud = st.get("cloud")
        if self.show_structure and cloud is not None and len(cloud):
            ax.scatter(cloud[:, 0], cloud[:, 1], cloud[:, 2], s=0.6,
                       c="#B9C6D4", alpha=0.28, linewidths=0, depthshade=False)

        if self.show_raw:
            for pts in st.get("raw", []):
                pts = np.asarray(pts)
                ax.plot(pts[:, 0], pts[:, 1], pts[:, 2], lw=0.9, color=C["muted"],
                        alpha=0.55, ls=":")

        for g, n in st.get("bundles", []):
            g = np.asarray(g)
            if len(g) < 2:
                continue
            lw = 1.6 + 1.5 * np.sqrt(max(n, 1))
            color = C["primary"] if n > 1 else C["serie1"]
            ax.plot(g[:, 0], g[:, 1], g[:, 2], lw=lw, color=color,
                    solid_capstyle="round", alpha=0.95)

        bp = st.get("branch_points")
        if bp is not None and len(bp):
            bp = np.atleast_2d(bp)
            ax.scatter(bp[:, 0], bp[:, 1], bp[:, 2], s=42, c="#12233A",
                       marker="s", depthshade=False, label="branchement")

        # connecteurs : un pave oriente a chaque extremite de cable, comme sur
        # une planche d'installation. C'est ce qui rend la sortie de cable
        # lisible pour un concepteur.
        # Chaque pose peut porter son propre encombrement : deux connecteurs
        # d'un meme harnais n'ont aucune raison d'avoir la meme taille.
        frames = st.get("connectors") or []
        default_size = st.get("connector_size", (45.0, 26.0, 20.0))
        for frame in frames:
            center, direction = frame[0], frame[1]
            size = frame[2] if len(frame) > 2 else default_size
            draw_connector(ax, center, direction, size)
        if frames:
            ax.plot([], [], "s", color="#2E9E5B", label="connecteur")
        term = st.get("terminals")
        if term is not None and len(term) and not frames:
            term = np.atleast_2d(term)
            ax.scatter(term[:, 0], term[:, 1], term[:, 2], s=70, c="#2E9E5B",
                       marker="o", edgecolors="white", linewidths=0.8,
                       depthshade=False, label="connecteur")

        crabes = st.get("crabs") or []
        for M, dx, dy, h in crabes:
            draw_crab(ax, M, dx, dy, h)
        if crabes:
            ax.plot([], [], "s", color="#C77700", label="crabe")

        if self.show_clips:
            cf = st.get("clips_fix")
            if cf is not None and len(cf):
                cf = np.atleast_2d(cf)
                ax.scatter(cf[:, 0], cf[:, 1], cf[:, 2], s=34, c="#E0A03B",
                           marker="D", depthshade=False, label="clip sur fixation")
            cn = st.get("clips_new")
            if cn is not None and len(cn):
                cn = np.atleast_2d(cn)
                ax.scatter(cn[:, 0], cn[:, 1], cn[:, 2], s=28, c="#D2483F",
                           marker="D", depthshade=False, label="clip a creer")

        zones = st.get("zones") or []
        if zones:
            pz = np.array([z["position"] for z in zones], float)
            ax.scatter(pz[:, 0], pz[:, 1], pz[:, 2], s=110, c="#C5221F",
                       marker="x", linewidths=2, depthshade=False,
                       label="interference")

        self._equalize(st)
        ax.legend(loc="upper left", fontsize=7.5, framealpha=0.9,
                  facecolor=C["surface"], edgecolor=C["border"])
        self.draw_now()

    def _equalize(self, st):
        """Repere orthonorme : sans cela un harnais long parait tordu."""
        pts = []
        for g, _ in st.get("bundles", []):
            pts.append(np.asarray(g))
        cloud = st.get("cloud")
        if self.show_structure and cloud is not None and len(cloud):
            pts.append(np.asarray(cloud))
        if not pts:
            return
        allp = np.vstack(pts)
        equal_axes(self.ax, allp.min(axis=0), allp.max(axis=0))


class ScenePreview(_CanvasFrame):
    """Apercu de la maquette sur la page 1 (nuage de collision sous-echantillonne)."""

    def __init__(self, parent):
        super().__init__(parent, figsize=(6.0, 4.2))
        self.ax = self.fig.add_subplot(111, projection="3d")
        self.fig.subplots_adjust(left=0.0, right=1.0, top=1.0, bottom=0.0)
        self.placeholder("Aucune maquette chargee")

    def placeholder(self, message):
        self.ax.clear()
        self.ax.set_axis_off()
        self.ax.text2D(0.5, 0.5, message, transform=self.ax.transAxes,
                       ha="center", va="center", color=C["muted"], fontsize=10)
        self.draw_now()

    def show_cloud(self, cloud, terminals=None):
        cloud = np.asarray(cloud, float)
        self.ax.clear()
        self.ax.set_axis_off()
        if not len(cloud):
            self.placeholder("Maquette vide")
            return
        z = cloud[:, 2]
        self.ax.scatter(cloud[:, 0], cloud[:, 1], z, s=0.7, c=z, cmap="Blues",
                        alpha=0.55, linewidths=0, depthshade=False)
        if terminals is not None and len(terminals):
            t = np.atleast_2d(terminals)
            self.ax.scatter(t[:, 0], t[:, 1], t[:, 2], s=60, c="#2E9E5B",
                            marker="o", edgecolors="white", depthshade=False)
        equal_axes(self.ax, cloud.min(axis=0), cloud.max(axis=0))
        self.ax.view_init(elev=24, azim=-60)
        self.draw_now()
