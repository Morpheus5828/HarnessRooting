# harnessopt/click_and_root.py
"""
Point d'entree du parcours guide Click & Root.

    python click_and_root_auto.py --diametre 6
"""

from __future__ import annotations

import argparse
import sys
import tkinter as tk

from .catia import bridge
from .log import Logger
from .ui.click_and_root import ClickAndRootView
from .ui.controller import ClickAndRootController
from .ui.splash import splash_then
from .ui.theme import apply_theme, screen_profile

LOG = Logger("click-and-root")

VERSION = "1.1.0"


def main(argv=None):
    parseur = argparse.ArgumentParser(
        prog="click_and_root",
        description="Cheminement de harnais pilote depuis CATIA : on clique "
                    "les departs et les arrivees, P valide, R chemine.")
    parseur.add_argument("--diametre", type=float, default=6.0,
                         help="diametre d'un cable, en mm (defaut : 6)")
    parseur.add_argument("--algo", choices=("HRH", "SHRH"), default=None,
                         help="algorithme de cheminement (defaut : config.py)")
    parseur.add_argument("--sans-splash", action="store_true",
                         help="ouvre directement la fenetre de travail")
    args = parseur.parse_args(argv)

    if not bridge.is_available():
        print("CATIA ne peut pas etre pilote depuis ce poste "
              "(Windows et pywin32 requis).", file=sys.stderr)
        return 2

    root = tk.Tk()
    profil = screen_profile(root)
    apply_theme(root, compact=profil["compact"])
    root.title("HarnessOpt - Click & Root")

    larg = max(880, min(1120, profil["width"] - 80))
    haut = max(620, min(860, profil["height"] - 120))
    root.geometry(f"{larg}x{haut}+40+40")
    root.minsize(720, 560)

    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass

    def construire():
        controleur = ClickAndRootController(root, cable_diameter=args.diametre)
        vue = ClickAndRootView(root, controleur)
        if args.algo:
            vue.segment_algo.set(args.algo)
        LOG.ok(f"Click & Root pret (algorithme "
               f"{args.algo or vue.algorithme()})")

    if args.sans_splash:
        construire()
    else:
        splash_then(root, construire, duration=2400, version=VERSION,
                    message="Connexion a CATIA...")

    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
