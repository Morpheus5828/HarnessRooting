"""
Selection de points directement dans CATIA.

L'utilisateur ne saisit plus de coordonnees : il CLIQUE ses departs et ses
arrivees dans la maquette, et l'application lit les coordonnees. C'est la
seule facon d'etre sur du repere -- une coordonnee recopiee a la main se
trompe d'axe une fois sur trois.

Comment ca marche
-----------------
Une macro VBScript boucle sur `Selection.SelectElement2`, qui rend la main a
chaque clic. Elle ecrit le point retenu dans un fichier AVANT de redemander
la main : le fichier est donc lisible en direct depuis Python, ce qui permet
d'afficher les points au fur et a mesure.

`SelectElement2` est MODAL cote CATIA : tant qu'il attend un clic, rien
d'autre ne s'execute dans CATIA. La seule facon d'en sortir est la touche
**Echap**, qui fait rendre "Cancel" a la fonction et termine la boucle. C'est
donc Echap qui clot une phase de selection, pas une touche cote Python -- une
macro bloquee dans SelectElement2 ne peut pas lire un drapeau que Python
poserait.

Ce qu'on designe est en general une PIECE -- une prise, un boitier, un
equipement -- pas un point construit. La macro lit donc d'abord le centre de
gravite de l'element (`Analyze.GetGravityCenter`, qui marche sur un Product
comme sur un Part sans licence de mesure), et ne se rabat sur l'atelier de
mesure que pour un point, un sommet ou une face. C'est la methode du prototype
d'origine (`click_and_root_prototype.py`), la seule eprouvee sur un vrai poste.
"""

from __future__ import annotations

import os
import threading
import time

from . import bridge
from ..log import Logger

LOG = Logger("selection")

PICK_FOLDER = bridge.BASE_CACHE / "picks"

# Filtre de selection. "Product" et "Part" d'abord : c'est ce que fait le
# prototype eprouve sur un vrai poste (click_and_root_prototype.py), et c'est
# ce qu'on designe naturellement -- une prise, un boitier, un equipement.
FILTRE = ("Product", "Part", "Point", "Vertex", "Face", "Edge")


def build_pick_macro(work_dir, out_path, libelle):
    """
    Macro de selection en boucle.

    Chaque point est ecrit puis le fichier est REFERME : VBScript ne vide pas
    son tampon avant `Close`, et sans cela Python ne verrait les points qu'a
    la toute fin -- l'utilisateur cliquerait dans le vide.
    """
    filtres = ", ".join(f'"{f}"' for f in FILTRE)
    return f'''
Const WORK_FOLDER = "{work_dir}"
Const OUT_PATH = "{out_path}"
Const LIBELLE = "{libelle}"

Sub Ecrire(chemin, ligne)
    Dim fso, ts
    Set fso = CreateObject("Scripting.FileSystemObject")
    Set ts = fso.OpenTextFile(chemin, 8, True)
    ts.WriteLine ligne
    ts.Close
End Sub

Sub CATMain()
    Dim fso: Set fso = CreateObject("Scripting.FileSystemObject")
    Dim st

    Dim doc
    On Error Resume Next
    Set doc = CATIA.ActiveDocument
    On Error GoTo 0
    If doc Is Nothing Then
        Set st = fso.CreateTextFile(WORK_FOLDER & "pick_status.txt", True)
        st.WriteLine "ERR|Aucun document actif dans CATIA."
        st.Close
        Exit Sub
    End If

    Dim sel: Set sel = doc.Selection
    Dim wb, meas
    On Error Resume Next
    Set wb = doc.GetWorkbench("SPAWorkbench")
    On Error GoTo 0

    Dim filtre({len(FILTRE) - 1})
    Dim noms: noms = Array({filtres})
    Dim k
    For k = 0 To UBound(noms)
        filtre(k) = noms(k)
    Next

    ' on repart d'un fichier vide : les points de la phase precedente ne
    ' doivent pas etre relus
    Dim vide: Set vide = fso.CreateTextFile(OUT_PATH, True)
    vide.Close

    Dim etat, n, coords(2), nom, ok, element
    n = 0
    Do
        sel.Clear
        etat = sel.SelectElement2(filtre, "HarnessOpt : cliquez un point de " & _
               LIBELLE & ", ou Echap pour terminer", False)
        If etat <> "Normal" Then Exit Do
        If sel.Count < 1 Then Exit Do

        ok = False
        nom = ""

        ' --- 1. une piece : son centre de gravite ---
        ' `Analyze.GetGravityCenter` marche sur un Product comme sur un Part,
        ' sans passer par l'atelier de mesure ni sa licence.
        On Error Resume Next
        Set element = sel.Item2(1).Value
        If Err.Number = 0 Then
            Err.Clear
            nom = element.Name
            Err.Clear
            element.Analyze.GetGravityCenter coords
            If Err.Number = 0 Then ok = True
            Err.Clear
        End If
        Err.Clear

        ' --- 2. sinon un point, un sommet, une face : l'atelier de mesure ---
        If Not ok And Not wb Is Nothing Then
            Err.Clear
            Set meas = wb.GetMeasurable(sel.Item(1).Reference)
            If Err.Number = 0 Then
                Err.Clear
                meas.GetPoint coords
                If Err.Number = 0 Then ok = True
                Err.Clear
                If Not ok Then
                    meas.GetCOG coords
                    If Err.Number = 0 Then ok = True
                    Err.Clear
                End If
            End If
            Err.Clear
        End If
        On Error GoTo 0

        If ok Then
            ' virgule decimale : CATIA suit la locale du poste, Python non
            Ecrire OUT_PATH, Replace(CStr(coords(0)), ",", ".") & ";" & _
                   Replace(CStr(coords(1)), ",", ".") & ";" & _
                   Replace(CStr(coords(2)), ",", ".") & ";" & nom
            n = n + 1
        Else
            Ecrire OUT_PATH, "REFUSE;;;element sans point mesurable"
        End If
    Loop

    sel.Clear
    Set st = fso.CreateTextFile(WORK_FOLDER & "pick_status.txt", True)
    st.WriteLine "OK|" & n
    st.Close
End Sub
'''


def _lire(path, deja):
    """Points ecrits par la macro depuis le dernier appel."""
    if not os.path.exists(path):
        return [], deja
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lignes = [ligne.strip() for ligne in fh if ligne.strip()]
    except OSError:
        return [], deja
    nouveaux = []
    for ligne in lignes[deja:]:
        champs = ligne.split(";")
        if champs[0] == "REFUSE":
            LOG.warn("element sans point mesurable : clic ignore")
            continue
        try:
            xyz = tuple(float(v.replace(",", ".")) for v in champs[:3])
        except ValueError:
            continue
        nom = champs[3] if len(champs) > 3 else ""
        nouveaux.append((xyz, nom))
    return nouveaux, len(lignes)


class PickSession:
    """
    Une phase de selection : la macro tourne, les points arrivent au fil de l'eau.

    `on_point(xyz, nom)` est appele depuis un THREAD DE TRAVAIL : l'appelant
    Tkinter doit passer par une file, jamais toucher un widget ici.
    """

    def __init__(self, libelle, on_point=None, on_end=None, poll=0.25):
        self.libelle = libelle
        self.on_point = on_point
        self.on_end = on_end
        self.poll = float(poll)
        self.points = []
        self.error = None
        self.finished = threading.Event()
        self._thread = None
        self._path = None

    def start(self):
        os.makedirs(PICK_FOLDER, exist_ok=True)
        self._path = os.path.join(str(PICK_FOLDER),
                                  f"picks_{self.libelle.lower()}.txt")
        if os.path.exists(self._path):
            os.remove(self._path)
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"selection-{self.libelle}")
        self._thread.start()
        return self

    # -- thread de travail --------------------------------------------
    def _run(self):
        lecteur = threading.Thread(target=self._suivre, daemon=True,
                                   name="lecture-selection")
        lecteur.start()
        try:
            pythoncom, win32 = bridge._com()
            pythoncom.CoInitialize()
            try:
                work = os.path.join(str(PICK_FOLDER), "")
                statut = os.path.join(work, "pick_status.txt")
                if os.path.exists(statut):
                    os.remove(statut)
                LOG.info(f"selection des {self.libelle.lower()} : cliquez dans "
                         f"CATIA, Echap pour terminer")
                bridge._run_macro(win32, "HarnessOpt_Pick.catvbs",
                                  build_pick_macro(work, self._path, self.libelle))
                etat, message = bridge._read_status(statut)
                if etat != bridge.STATUS_OK:
                    self.error = message
            finally:
                pythoncom.CoUninitialize()
        except Exception as exc:                     # CATIA absent, macro refusee
            self.error = str(exc)
            LOG.exception("selection dans CATIA impossible", exc)
        finally:
            self.finished.set()
            lecteur.join(timeout=2.0)
            if self.on_end is not None:
                self.on_end(list(self.points), self.error)

    def _suivre(self):
        """Relit le fichier de points tant que la macro tourne."""
        deja = 0
        while not self.finished.is_set():
            nouveaux, deja = _lire(self._path, deja)
            for xyz, nom in nouveaux:
                self.points.append((xyz, nom))
                LOG.ok(f"{self.libelle} {len(self.points)} : "
                       f"({xyz[0]:.0f}, {xyz[1]:.0f}, {xyz[2]:.0f}) {nom}")
                if self.on_point is not None:
                    self.on_point(xyz, nom)
            time.sleep(self.poll)
        nouveaux, deja = _lire(self._path, deja)     # dernier passage
        for xyz, nom in nouveaux:
            self.points.append((xyz, nom))
            if self.on_point is not None:
                self.on_point(xyz, nom)


def pick_points(libelle, on_point=None):
    """Selection bloquante : rend la liste des points cliques."""
    session = PickSession(libelle, on_point=on_point).start()
    session.finished.wait()
    if session.error:
        raise bridge.CatiaError(session.error)
    return [xyz for xyz, _nom in session.points]
