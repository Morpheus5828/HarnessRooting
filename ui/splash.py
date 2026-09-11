"""
Ecran de demarrage : le logo, le nom, la promesse.

Une application qui met dix secondes a ouvrir CATIA et a charger une maquette
ne peut pas demarrer sur une fenetre vide. Le temps que l'environnement se
mette en place, on affiche ce que fait le logiciel -- et un helicoptere dont
le rotor tourne, parce que c'est de cela qu'il s'agit.

Tout est dessine sur un canevas : aucun fichier d'image a livrer, aucune
dependance a Pillow, et le logo reste net quel que soit le facteur d'echelle
de l'ecran. La meme fonction sert d'icone dans le bandeau de l'application.
"""

from __future__ import annotations

import math
import time
import tkinter as tk

from .theme import C, FONTS, apply_theme, mix, round_rect

TITRE = "HarnessOpt"
ACCROCHE = "an AI application help designer for harness rooting."
SOUS_TITRE = ("Cheminement automatique de harnais helicoptere  -  "
              "heuristique HRH / SHRH")


# ==========================================================================
# le logo
# ==========================================================================

def draw_helicopter(canvas, cx, cy, scale=1.0, color=None, accent=None,
                    rotor_angle=0.0, tag="helico"):
    """
    Helicoptere de profil, dessine a la main sur un canevas Tk.

    `rotor_angle` (en degres) fait tourner le rotor principal vu de profil :
    les pales s'allongent et se raccourcissent en suivant le cosinus de
    l'angle, ce qui suffit a donner l'illusion de la rotation sans passer en
    perspective.

    Rend la liste des identifiants crees, pour pouvoir tout effacer d'un bloc.
    """
    color = color or C["primary"]
    accent = accent or C["accent"]
    s = float(scale)
    ids = []

    def X(v):
        return cx + v * s

    def Y(v):
        return cy + v * s

    # --- poutre de queue ---
    ids.append(canvas.create_polygon(
        X(14), Y(-6), X(78), Y(-13), X(84), Y(-9), X(80), Y(-2), X(14), Y(6),
        fill=color, outline="", smooth=False, tags=tag))

    # --- derive (l'aileron vertical de queue) ---
    ids.append(canvas.create_polygon(
        X(70), Y(-11), X(88), Y(-46), X(101), Y(-42), X(97), Y(-30),
        X(86), Y(-8),
        fill=color, outline="", smooth=True, tags=tag))

    # --- stabilisateur horizontal, sur la poutre ---
    ids.append(canvas.create_polygon(
        X(52), Y(-13), X(70), Y(-19), X(72), Y(-15), X(56), Y(-8),
        fill=color, outline="", smooth=True, tags=tag))

    # --- cabine : une goutte, nez bas et verriere haute ---
    ids.append(canvas.create_polygon(
        X(-46), Y(2), X(-40), Y(-14), X(-22), Y(-26), X(2), Y(-28),
        X(20), Y(-20), X(24), Y(-4), X(16), Y(10), X(-8), Y(16),
        X(-34), Y(14), X(-46), Y(6),
        fill=color, outline="", smooth=True, tags=tag))

    # --- verriere ---
    ids.append(canvas.create_polygon(
        X(-40), Y(-6), X(-34), Y(-17), X(-18), Y(-24), X(-4), Y(-24),
        X(-6), Y(-6), X(-24), Y(-2),
        fill=accent, outline="", smooth=True, tags=tag))

    # --- mat du rotor ---
    ids.append(canvas.create_rectangle(X(-6), Y(-38), X(2), Y(-26),
                                       fill=color, outline="", tags=tag))
    ids.append(canvas.create_oval(X(-9), Y(-43), X(5), Y(-35), fill=color,
                                  outline="", tags=tag))

    # --- rotor principal : deux pales vues de profil ---
    a = math.radians(float(rotor_angle))
    for k, phase in enumerate((0.0, math.pi)):
        etendue = math.cos(a + phase)
        longueur = 74.0 * etendue
        hauteur = -39.0 - 3.5 * math.sin(a + phase)
        ids.append(canvas.create_line(
            X(-2), Y(-39), X(-2 + longueur), Y(hauteur),
            width=max(1.0, 3.4 * s), capstyle=tk.ROUND,
            fill=mix(accent, C["surface"], 0.55 if etendue < 0 else 0.0),
            tags=tag))

    # --- rotor anticouple : deux pales, en bout de derive ---
    hx, hy = 96.0, -38.0
    for k in range(2):
        b = math.radians(rotor_angle * 1.7 + 180 * k)
        ids.append(canvas.create_line(
            X(hx), Y(hy), X(hx + 19 * math.cos(b)), Y(hy + 19 * math.sin(b)),
            width=max(1.0, 2.4 * s), fill=mix(accent, C["surface"], 0.3),
            capstyle=tk.ROUND, tags=tag))
    ids.append(canvas.create_oval(X(hx - 4), Y(hy - 4), X(hx + 4), Y(hy + 4),
                                  fill=color, outline="", tags=tag))

    # --- patins ---
    ids.append(canvas.create_line(X(-40), Y(30), X(20), Y(30),
                                  width=max(1.0, 3.6 * s), fill=color,
                                  capstyle=tk.ROUND, tags=tag))
    for x0, x1 in ((-28, -22), (4, 12)):
        ids.append(canvas.create_line(X(x0), Y(14), X(x1), Y(30),
                                      width=max(1.0, 3.0 * s), fill=color,
                                      capstyle=tk.ROUND, tags=tag))
    return ids


def logo_mark(parent, size=44, bg=None, color=None, accent=None):
    """Petit logo carre a coins ronds, pour un bandeau ou une barre d'outils."""
    bg = bg or C["primary"]
    canvas = tk.Canvas(parent, width=size, height=size, bg=bg,
                       highlightthickness=0, bd=0)
    round_rect(canvas, 1, 1, size - 1, size - 1, size * 0.28,
               fill=mix(bg, C["accent"], 0.22), outline="")
    draw_helicopter(canvas, size * 0.5, size * 0.54, scale=size / 210.0,
                    color=color or "#FFFFFF", accent=accent or C["accent"],
                    rotor_angle=18.0)
    return canvas


# ==========================================================================
# l'ecran de demarrage
# ==========================================================================

class SplashScreen(tk.Toplevel):
    """
    Fenetre sans bordure, centree, qui s'efface toute seule.

    `duration` est le temps d'affichage minimal ; l'application peut la
    fermer plus tot avec `close()` une fois prete. Les fondus passent par
    l'attribut `-alpha`, ignore par certains gestionnaires de fenetres : le
    code retombe alors sur un affichage franc, sans jamais echouer.
    """

    LARGEUR = 640
    HAUTEUR = 380

    def __init__(self, master=None, duration=2600, on_close=None, version="",
                 message="Demarrage..."):
        self._maitre_cree = master is None
        if master is None:
            master = tk.Tk()
            master.withdraw()
        super().__init__(master)
        self.master_window = master
        self.duration = float(duration)
        self.on_close = on_close
        self.version = version
        self._t0 = time.monotonic()
        self._job = None
        self._ferme = False
        self._alpha = 0.0

        self.overrideredirect(True)
        try:
            self.attributes("-topmost", True)
            self.attributes("-alpha", 0.0)
        except tk.TclError:
            pass

        larg, haut = self.LARGEUR, self.HAUTEUR
        ecran_l = self.winfo_screenwidth()
        ecran_h = self.winfo_screenheight()
        self.geometry(f"{larg}x{haut}+{(ecran_l - larg) // 2}"
                      f"+{max(0, (ecran_h - haut) // 2 - 40)}")

        self.canvas = tk.Canvas(self, width=larg, height=haut,
                                bg=C["surface"], highlightthickness=0, bd=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self._dessine_fond(larg, haut)

        # --- logo ---
        self._rotor = 0.0
        self._logo_pos = (larg * 0.5, haut * 0.40)

        # --- textes ---
        self.canvas.create_text(larg * 0.5, haut * 0.62, text=TITRE,
                                fill=C["primary"], font=FONTS["h0"])
        self.canvas.create_text(larg * 0.5, haut * 0.72, text=ACCROCHE,
                                fill=C["muted"], font=FONTS["lead"],
                                width=larg - 90)
        self.canvas.create_text(larg * 0.5, haut * 0.79, text=SOUS_TITRE,
                                fill=C["faint"], font=FONTS["tiny"],
                                width=larg - 90)

        # --- barre de progression indeterminee ---
        y = haut * 0.875
        self._piste = round_rect(self.canvas, larg * 0.28, y, larg * 0.72,
                                 y + 4, 2, fill=C["border_soft"], outline="")
        self._barre = round_rect(self.canvas, larg * 0.28, y, larg * 0.36,
                                 y + 4, 2, fill=C["accent"], outline="")
        self._piste_x = (larg * 0.28, larg * 0.72)
        self._piste_y = (y, y + 4)

        self._message = self.canvas.create_text(
            larg * 0.5, haut * 0.93, text=message, fill=C["faint"],
            font=FONTS["tiny"])
        if version:
            self.canvas.create_text(larg - 18, haut - 14, text=f"v{version}",
                                    fill=C["border"], font=FONTS["tiny"],
                                    anchor="e")

        self.bind("<Button-1>", lambda _e: self.close())
        self._anime()

    # -- fond : un degrade tres doux, dans le gout des cartes d'Apple --
    def _dessine_fond(self, larg, haut):
        for i in range(haut):
            t = i / float(haut)
            self.canvas.create_line(
                0, i, larg, i,
                fill=mix(C["surface"], mix(C["accent_soft"], C["bg"], 0.35), t))
        self.canvas.create_rectangle(0, 0, larg - 1, haut - 1, outline=C["border"])
        # liseré d'accent en haut : la seule couleur vive de l'ecran
        self.canvas.create_rectangle(0, 0, larg, 4, fill=C["accent"], outline="")

    # -- animation -----------------------------------------------------
    def _anime(self):
        if self._ferme:
            return
        t = time.monotonic() - self._t0

        # fondu d'entree sur 350 ms
        alpha = min(1.0, t / 0.35)
        if abs(alpha - self._alpha) > 0.01:
            self._alpha = alpha
            try:
                self.attributes("-alpha", alpha)
            except tk.TclError:
                pass

        # rotor : 220 tours par minute, ramene a l'echelle du dessin
        self._rotor = (t * 260.0) % 360.0
        self.canvas.delete("helico")
        draw_helicopter(self.canvas, self._logo_pos[0], self._logo_pos[1],
                        scale=1.28, color=C["primary"], accent=C["accent"],
                        rotor_angle=self._rotor)

        # barre : un segment qui va et vient, adouci aux extremites
        x0, x1 = self._piste_x
        y0, y1 = self._piste_y
        largeur = 0.22 * (x1 - x0)
        phase = 0.5 * (1.0 - math.cos(2.0 * math.pi * (t / 1.9)))
        gauche = x0 + phase * (x1 - x0 - largeur)
        self.canvas.coords(self._barre, *self._points_barre(
            gauche, y0, gauche + largeur, y1))

        try:
            self._job = self.after(16, self._anime)
        except tk.TclError:
            self._job = None

    @staticmethod
    def _points_barre(x0, y0, x1, y1):
        r = (y1 - y0) / 2.0
        return [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
                x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
                x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]

    # -- pilotage ------------------------------------------------------
    def set_message(self, texte):
        try:
            self.canvas.itemconfigure(self._message, text=texte)
            self.update_idletasks()
        except tk.TclError:
            pass

    def reste(self) -> float:
        """Temps d'affichage restant avant la duree minimale, en secondes."""
        return max(0.0, self.duration / 1000.0 - (time.monotonic() - self._t0))

    def close(self, fondu=True):
        """Efface l'ecran, puis rend la main a l'application."""
        if self._ferme:
            return
        self._ferme = True
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

        def fin():
            try:
                self.destroy()
            except tk.TclError:
                pass
            if self.on_close:
                self.on_close()

        if not fondu:
            fin()
            return

        def pas(alpha):
            if alpha <= 0.02:
                fin()
                return
            try:
                self.attributes("-alpha", alpha)
                self.after(16, lambda: pas(alpha - 0.07))
            except tk.TclError:
                fin()

        pas(self._alpha or 1.0)


def show_splash(master=None, duration=2600, version="", message="Demarrage...",
                on_close=None):
    """
    Affiche l'ecran de demarrage et rend la fenetre.

    L'appelant continue son initialisation pendant ce temps, puis appelle
    `splash.close()` : l'ecran s'efface, et `on_close` est declenche.
    """
    try:
        splash = SplashScreen(master, duration=duration, version=version,
                              message=message, on_close=on_close)
        splash.update()
        return splash
    except tk.TclError as exc:                 # pas d'affichage disponible
        from ..log import Logger
        Logger("demarrage").warn(f"ecran de demarrage indisponible : {exc}")
        if on_close:
            on_close()
        return None


def splash_then(root, build, duration=2600, version="", message="Demarrage..."):
    """
    Enchaine ecran de demarrage puis construction de l'application.

    `build()` est appele APRES l'effacement de l'ecran : la fenetre
    principale n'apparait donc jamais derriere le logo, et la duree minimale
    d'affichage est respectee meme quand l'initialisation est instantanee.
    """
    root.withdraw()

    def demarrer():
        try:
            root.deiconify()
        except tk.TclError:
            pass
        build()

    splash = show_splash(root, duration=duration, version=version,
                         message=message, on_close=demarrer)
    if splash is None:
        demarrer()
        return None
    root.after(int(duration), splash.close)
    return splash


if __name__ == "__main__":               # apercu : python -m harnessopt.ui.splash
    racine = tk.Tk()
    apply_theme(racine)
    racine.withdraw()
    ecran = SplashScreen(racine, duration=3000, version="1.1.0",
                         on_close=racine.destroy)
    racine.after(3000, ecran.close)
    racine.mainloop()
