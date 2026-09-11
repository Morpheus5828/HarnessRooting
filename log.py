"""
Journalisation HarnessOpt.

Deux destinations :
  * la CLI (stdout), coloree, horodatee, avec le nom du module -> pour le
    debogage cote developpeur ;
  * la console integree a l'application (voir `ui.widgets.LogConsole`), qui
    s'abonne via `add_ui_sink` -> pour l'utilisateur final.

Le module est volontairement sans dependance : il doit pouvoir etre importe
avant numpy / pyvista pour tracer les erreurs d'installation.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from datetime import datetime

# --- niveaux -------------------------------------------------------------
DEBUG, INFO, OK, WARN, ERROR = 10, 20, 25, 30, 40

_NAMES = {DEBUG: "DEBUG", INFO: "INFO ", OK: "OK   ", WARN: "WARN ", ERROR: "ERROR"}

# ANSI : desactive si la sortie n'est pas un terminal (redirection, fichier).
_ANSI = {DEBUG: "\033[90m", INFO: "\033[36m", OK: "\033[32m",
         WARN: "\033[33m", ERROR: "\033[31m"}
_RESET = "\033[0m"
_BOLD = "\033[1m"


def _colors_enabled() -> bool:
    if os.environ.get("HARNESSOPT_NO_COLOR"):
        return False
    if os.name == "nt":
        # Windows 10+ : on tente d'activer le VT100, sinon on retombe en mono.
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
        except Exception:
            return False
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


_USE_COLOR = _colors_enabled()
_LOCK = threading.Lock()
_SINKS = []                       # callables (level, tag, message, timestamp)
_MIN_LEVEL = DEBUG if os.environ.get("HARNESSOPT_DEBUG") else INFO
_T0 = time.time()


def set_level(level: int) -> None:
    global _MIN_LEVEL
    _MIN_LEVEL = level


def add_ui_sink(fn) -> None:
    """Abonne un widget (ou toute fonction) au flux de journalisation."""
    with _LOCK:
        if fn not in _SINKS:
            _SINKS.append(fn)


def remove_ui_sink(fn) -> None:
    with _LOCK:
        if fn in _SINKS:
            _SINKS.remove(fn)


def log(level: int, tag: str, message: str) -> None:
    if level < _MIN_LEVEL:
        return
    now = datetime.now()
    stamp = now.strftime("%H:%M:%S")
    elapsed = time.time() - _T0
    name = _NAMES.get(level, "?????")
    line = f"{stamp} +{elapsed:7.1f}s [{name}] {tag:<12s} {message}"
    with _LOCK:
        if _USE_COLOR:
            head = f"{_ANSI.get(level, '')}{stamp} +{elapsed:7.1f}s [{name}]{_RESET}"
            print(f"{head} {_BOLD}{tag:<12s}{_RESET} {message}", flush=True)
        else:
            print(line, flush=True)
        sinks = list(_SINKS)
    for fn in sinks:
        try:
            fn(level, tag, message, now)
        except Exception:            # une UI morte ne doit jamais tuer le calcul
            pass


class Logger:
    """Petit adaptateur nomme : `LOG = Logger("catia")`."""

    __slots__ = ("tag",)

    def __init__(self, tag: str):
        self.tag = tag

    def debug(self, msg): log(DEBUG, self.tag, msg)
    def info(self, msg): log(INFO, self.tag, msg)
    def ok(self, msg): log(OK, self.tag, msg)
    def warn(self, msg): log(WARN, self.tag, msg)
    def error(self, msg): log(ERROR, self.tag, msg)

    def exception(self, msg, exc: BaseException):
        log(ERROR, self.tag, f"{msg} : {type(exc).__name__} : {exc}")
        for line in traceback.format_exception(type(exc), exc, exc.__traceback__):
            for sub in line.rstrip().split("\n"):
                log(DEBUG, self.tag, "  " + sub)

    def kv(self, title: str, pairs: dict):
        """Bloc cle/valeur aligne, pratique pour resumer une etape en CLI."""
        width = max((len(k) for k in pairs), default=0)
        log(INFO, self.tag, title)
        for k, v in pairs.items():
            log(INFO, self.tag, f"    {k:<{width}s} : {v}")


def banner(version: str) -> None:
    lines = [
        "+--------------------------------------------------------------+",
        "|   HarnessOpt - cheminement automatique de harnais helicoptere |",
        "|   Karlsson, Ablad, Hermansson, Carlson, Tenfalt (2023)        |",
        f"|   version {version:<51s}|",
        "+--------------------------------------------------------------+",
    ]
    for line in lines:
        print((f"{_ANSI[OK]}{line}{_RESET}") if _USE_COLOR else line, flush=True)
