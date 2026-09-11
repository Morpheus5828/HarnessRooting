# harnessopt/click_and_root.py
from __future__ import annotations

import argparse
import sys
import tkinter as tk

from .catia import bridge
from .log import Logger
from .ui.click_and_root import ClickAndRootView
from .ui.controller import ClickAndRootController
from .ui.theme import apply_theme, screen_profile

LOG = Logger("click-and-root")


def main(argv=None):
    parseur = argparse.ArgumentParser(
        prog="click_and_root",
        description="Cheminement de harnais piloté depuis CATIA : on clique "
                    "les départs et les arrivées, P valide, R chemine.")
    parseur.add_argument("--diametre", type=float, default=6.0,
                         help="diamètre d'un câble, en mm (défaut : 6)")
    args = parseur.parse_args(argv)

    if not bridge.is_available():
        print("CATIA ne peut pas être piloté depuis ce poste "
              "(Windows et pywin32 requis).", file=sys.stderr)
        return 2

    root = tk.Tk()
    profil = screen_profile(root)
    apply_theme(root, compact=profil["compact"])
    root.title("HarnessOpt - Click & Root")

    larg = max(760, min(900, profil["width"] - 80))
    haut = max(560, min(760, profil["height"] - 120))
    root.geometry(f"{larg}x{haut}+40+40")
    root.minsize(640, 520)

    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass

    controller = ClickAndRootController(root, cable_diameter=args.diametre)
    view = ClickAndRootView(root, controller)

    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
