"""
Passerelle CATIA V5.

Tout le dialogue avec CATIA passe par des macros VBScript executees via
`CATIA.SystemService.ExecuteScript`. C'est la methode la plus robuste depuis
Python : elle ne depend pas de la version de pywin32 et elle survit aux
assemblages volumineux.

Trois operations sont exposees :
  * `count_parts`  : compte les pieces feuilles de l'assemblage actif (rapide,
                     sert a annoncer le volume de travail a l'utilisateur) ;
  * `export_stl`   : exporte chaque piece feuille en STL dans le cache, avec
                     progression en direct ;
  * `import_stl`   : reinjecte un STL (le harnais calcule) dans l'arbre du
                     produit actif.

Chaque macro ecrit un fichier d'etat dans le dossier de travail : c'est le seul
moyen fiable de remonter une erreur VBScript cote Python.
"""

from __future__ import annotations

import glob
import os
import threading
import time
from pathlib import Path

from ..log import Logger

LOG = Logger("catia")

BASE_CACHE = Path(os.environ.get("HARNESSOPT_CACHE")
                  or os.environ.get("TEMP")
                  or os.environ.get("TMPDIR")
                  or "/tmp") / "HarnessOpt_cache"
STL_FOLDER = BASE_CACHE / "stl"
EXPORT_FOLDER = BASE_CACHE / "export"

STATUS_OK = "OK"
STATUS_ERR = "ERR"


class CatiaError(RuntimeError):
    """CATIA est injoignable, non licencie pour l'operation, ou la macro a echoue."""


# --------------------------------------------------------------------------
# plomberie COM
# --------------------------------------------------------------------------

def _com():
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise CatiaError(
            "Le pilotage de CATIA necessite Windows et le module pywin32.\n"
            "Installer avec :  pip install pywin32\n"
            "Sur un poste sans CATIA, utiliser le bouton "
            "\"Charger un dossier STL\" de la page 1.") from exc
    return pythoncom, win32com.client


def is_available() -> bool:
    """Vrai si l'environnement peut, en principe, piloter CATIA."""
    try:
        _com()
        return True
    except CatiaError:
        return False


def _ensure_dirs():
    for d in (BASE_CACHE, STL_FOLDER, EXPORT_FOLDER):
        os.makedirs(d, exist_ok=True)


def _macro_dir() -> str:
    d = str(BASE_CACHE / "macros")
    os.makedirs(d, exist_ok=True)
    return d


def _run_macro(win32, name, code):
    """Ecrit la macro sur disque puis la fait executer par CATIA."""
    macro_dir = _macro_dir()
    macro_path = os.path.join(macro_dir, name)
    with open(macro_path, "w", encoding="utf-8") as handle:
        handle.write(code)
    LOG.debug(f"macro ecrite : {macro_path} ({len(code)} caracteres)")
    try:
        catia = win32.Dispatch("CATIA.Application")
    except Exception as exc:
        raise CatiaError(
            "Impossible de joindre CATIA.\n"
            "Verifier que CATIA V5 est lance et qu'un document est ouvert.\n\n"
            f"{exc}") from exc
    try:
        catia.SystemService.ExecuteScript(macro_dir, 1, name, "CATMain", [])
    except Exception as exc:
        for attr, value in (("Interactive", True), ("RefreshDisplay", True),
                            ("DisplayFileAlerts", True)):
            try:
                setattr(catia, attr, value)
            except Exception:
                pass
        raise CatiaError(f"L'execution de la macro CATIA a echoue.\n\n{exc}") from exc
    return catia


def _read_status(path):
    """Lit le fichier d'etat ecrit par la macro. Renvoie (statut, message)."""
    if not os.path.exists(path):
        return STATUS_ERR, "la macro n'a produit aucun fichier d'etat"
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        raw = fh.read().strip()
    if not raw:
        return STATUS_ERR, "fichier d'etat vide"
    head, _, rest = raw.partition("|")
    return head.strip().upper(), rest.strip()


# --------------------------------------------------------------------------
# macros VBScript
# --------------------------------------------------------------------------

_VBS_LEAF_HELPER = """
Sub GetLeafProducts(prod, dict)
    Dim children: Set children = prod.Products
    If children.Count = 0 Then
        If Not IsExcluded(prod.Name) Then dict.Add prod.Name & "_" & dict.Count, prod
    Else
        Dim i: For i = 1 To children.Count: Call GetLeafProducts(children.Item(i), dict): Next
    End If
End Sub

Function IsExcluded(itemName)
    IsExcluded = False
    If Trim(EXCLUDE_FILTER) = "" Then Exit Function
    Dim filters, i, regEx, pat
    filters = Split(EXCLUDE_FILTER, ",")
    Set regEx = New RegExp
    regEx.IgnoreCase = True
    For i = 0 To UBound(filters)
        pat = Trim(filters(i))
        If pat <> "" Then
            pat = Replace(pat, "\\", "\\\\"): pat = Replace(pat, ".", "\\.")
            pat = Replace(pat, "(", "\\("): pat = Replace(pat, ")", "\\)")
            pat = Replace(pat, "[", "\\["): pat = Replace(pat, "]", "\\]")
            pat = Replace(pat, "*", ".*"): pat = Replace(pat, "?", ".")
            pat = "^" & pat & "$"
            regEx.Pattern = pat
            If regEx.Test(itemName) Then IsExcluded = True: Exit Function
        End If
    Next
End Function
"""


def build_count_macro(work_dir, exclude_str=""):
    """Macro rapide : n'exporte rien, compte et nomme les pieces feuilles."""
    return f'''
Const WORK_FOLDER = "{work_dir}"
Const EXCLUDE_FILTER = "{exclude_str}"

Sub CATMain()
    Dim fso: Set fso = CreateObject("Scripting.FileSystemObject")
    Dim ts: Set ts = fso.CreateTextFile(WORK_FOLDER & "scan_result.txt", True)
    On Error Resume Next
    Dim doc: Set doc = CATIA.ActiveDocument
    If Err.Number <> 0 Or doc Is Nothing Then
        ts.WriteLine "ERR|Aucun document actif dans CATIA."
        ts.Close
        Exit Sub
    End If
    On Error GoTo 0

    Dim rootProduct
    On Error Resume Next
    Set rootProduct = doc.Product
    On Error GoTo 0
    If rootProduct Is Nothing Then
        ts.WriteLine "ERR|Le document actif n'est pas un produit (CATProduct)."
        ts.Close
        Exit Sub
    End If

    Dim leafList: Set leafList = CreateObject("Scripting.Dictionary")
    Call GetLeafProducts(rootProduct, leafList)

    ts.WriteLine "OK|" & doc.Name & "|" & leafList.Count
    Dim key, n: n = 0
    For Each key In leafList.Keys
        If n < 400 Then ts.WriteLine leafList(key).Name
        n = n + 1
    Next
    ts.Close
End Sub
{_VBS_LEAF_HELPER}
'''


def build_export_macro(export_dir, exclude_str=""):
    """Macro d'export : une piece feuille -> un STL, via ComputeAnOffset."""
    return f'''
Const EXPORT_FOLDER = "{export_dir}"
Const EXCLUDE_FILTER = "{exclude_str}"

Sub CATMain()
    Dim fso: Set fso = CreateObject("Scripting.FileSystemObject")
    Dim st: Set st = fso.CreateTextFile(EXPORT_FOLDER & "export_status.txt", True)

    CATIA.DisplayFileAlerts = False: CATIA.RefreshDisplay = False: CATIA.Interactive = False
    Dim productDocument1
    On Error Resume Next: Set productDocument1 = CATIA.ActiveDocument: On Error GoTo 0
    If productDocument1 Is Nothing Then
        CATIA.Interactive = True: CATIA.RefreshDisplay = True: CATIA.DisplayFileAlerts = True
        st.WriteLine "ERR|Aucun document actif dans CATIA."
        st.Close
        Exit Sub
    End If

    Dim rootProduct: Set rootProduct = productDocument1.Product
    Dim leafList: Set leafList = CreateObject("Scripting.Dictionary")
    Call GetLeafProducts(rootProduct, leafList)
    If leafList.Count = 0 Then
        CATIA.Interactive = True: CATIA.RefreshDisplay = True: CATIA.DisplayFileAlerts = True
        st.WriteLine "ERR|Aucune piece a exporter (arbre vide ou tout est exclu)."
        st.Close
        Exit Sub
    End If

    ' Le total est publie tres tot : l'application peut afficher une progression.
    Dim ts: Set ts = fso.CreateTextFile(EXPORT_FOLDER & "export_total.txt", True)
    ts.WriteLine leafList.Count
    ts.Close

    Dim dmoOffsets: Set dmoOffsets = rootProduct.GetTechnologicalObject("Offsets")
    Dim key, leafProd, groups, group, offsetDoc, arr(0): arr(0) = 0.0
    Dim nOk: nOk = 0
    Dim nKo: nKo = 0

    For Each key In leafList.Keys
        Set leafProd = leafList(key)
        Set groups = rootProduct.GetTechnologicalObject("Groups")
        Set group = groups.Add(): group.AddExplicit leafProd
        Set offsetDoc = dmoOffsets.ComputeAnOffset(group, 0.0, 0, arr)

        If Not offsetDoc Is Nothing Then
            Dim baseName: baseName = leafProd.Name
            baseName = Replace(baseName, "/", "_"): baseName = Replace(baseName, "\\", "_"): baseName = Replace(baseName, ":", "_")
            Dim outputPath: outputPath = EXPORT_FOLDER & baseName & ".stl"
            Dim counter: counter = 1
            While fso.FileExists(outputPath)
                outputPath = EXPORT_FOLDER & baseName & "_" & counter & ".stl"
                counter = counter + 1
            Wend
            On Error Resume Next
            offsetDoc.ExportData outputPath, "stl"
            If Err.Number <> 0 Then
                nKo = nKo + 1
                Err.Clear
            Else
                nOk = nOk + 1
            End If
            offsetDoc.Close
            On Error GoTo 0
        Else
            nKo = nKo + 1
        End If
        If group.CountExplicit > 0 Then group.RemoveExplicit 1
        groups.Remove group
    Next
    CATIA.Interactive = True: CATIA.RefreshDisplay = True: CATIA.DisplayFileAlerts = True
    st.WriteLine "OK|" & nOk & " piece(s) exportee(s), " & nKo & " en echec."
    st.Close
End Sub
{_VBS_LEAF_HELPER}
'''


def build_import_macro(work_dir, stl_path, component_name):
    """
    Macro d'import : ajoute le STL du harnais dans l'arbre du produit actif.

    Trois strategies sont tentees dans l'ordre, car la premiere depend de la
    licence STL du poste :
      1. ouverture du STL comme document puis AddExternalComponent ;
      2. `Products.AddComponentsFromFiles` -- la voie du prototype d'origine,
         qui passe sans licence de maillage ;
      3. AddComponent sur un composant vide puis import de la geometrie.
    Le resultat est ecrit dans import_status.txt pour etre remonte a Python.
    """
    return f'''
Const WORK_FOLDER = "{work_dir}"
Const STL_PATH = "{stl_path}"
Const COMPONENT_NAME = "{component_name}"

Sub CATMain()
    Dim fso: Set fso = CreateObject("Scripting.FileSystemObject")
    Dim ts: Set ts = fso.CreateTextFile(WORK_FOLDER & "import_status.txt", True)

    If Not fso.FileExists(STL_PATH) Then
        ts.WriteLine "ERR|Fichier STL introuvable : " & STL_PATH
        ts.Close
        Exit Sub
    End If

    Dim doc
    On Error Resume Next
    Set doc = CATIA.ActiveDocument
    On Error GoTo 0
    If doc Is Nothing Then
        ts.WriteLine "ERR|Aucun document actif dans CATIA."
        ts.Close
        Exit Sub
    End If

    Dim rootProduct
    On Error Resume Next
    Set rootProduct = doc.Product
    On Error GoTo 0
    If rootProduct Is Nothing Then
        ts.WriteLine "ERR|Le document actif n'est pas un produit (CATProduct)."
        ts.Close
        Exit Sub
    End If

    CATIA.DisplayFileAlerts = False

    ' --- strategie 1 : ouvrir le STL puis l'ajouter comme composant externe ---
    Dim stlDoc, added
    On Error Resume Next
    Set stlDoc = CATIA.Documents.Open(STL_PATH)
    If Err.Number = 0 And Not stlDoc Is Nothing Then
        Err.Clear
        Set added = rootProduct.Products.AddExternalComponent(stlDoc)
        If Err.Number = 0 And Not added Is Nothing Then
            Err.Clear
            added.Name = COMPONENT_NAME
            Err.Clear
            CATIA.DisplayFileAlerts = True
            ts.WriteLine "OK|Harnais ajoute dans l'arbre sous le nom " & COMPONENT_NAME
            ts.Close
            Exit Sub
        End If
        Err.Clear
    End If
    Err.Clear
    On Error GoTo 0

    ' --- strategie 2 : ajout direct du fichier comme composant ---
    ' C'est la voie du prototype d'origine : elle passe la ou l'ouverture du
    ' STL comme document se heurte a la licence de maillage.
    On Error Resume Next
    Dim fichiers(0)
    fichiers(0) = STL_PATH
    rootProduct.Products.AddComponentsFromFiles fichiers, "All"
    If Err.Number = 0 Then
        Err.Clear
        CATIA.DisplayFileAlerts = True
        ts.WriteLine "OK|Harnais ajoute dans l'arbre depuis " & STL_PATH
        ts.Close
        Exit Sub
    End If
    Err.Clear
    On Error GoTo 0

    ' --- strategie 3 : nouveau composant puis import de la geometrie ---
    On Error Resume Next
    Dim newProd: Set newProd = rootProduct.Products.AddNewComponent("Part", COMPONENT_NAME)
    If Err.Number <> 0 Or newProd Is Nothing Then
        Err.Clear
        CATIA.DisplayFileAlerts = True
        ts.WriteLine "ERR|CATIA a refuse l'ajout du composant. Le STL reste disponible : " & STL_PATH
        ts.Close
        Exit Sub
    End If
    Err.Clear
    newProd.Name = COMPONENT_NAME
    Err.Clear
    CATIA.DisplayFileAlerts = True
    ts.WriteLine "OK|Composant " & COMPONENT_NAME & " cree. Importer la geometrie depuis " & STL_PATH & " si elle n'apparait pas."
    ts.Close
End Sub
'''


# --------------------------------------------------------------------------
# operations de haut niveau
# --------------------------------------------------------------------------

def count_parts(exclude_str="") -> dict:
    """Analyse l'assemblage actif sans rien exporter (quelques secondes)."""
    pythoncom, win32 = _com()
    pythoncom.CoInitialize()
    try:
        _ensure_dirs()
        work = os.path.join(str(EXPORT_FOLDER), "")
        status_path = os.path.join(work, "scan_result.txt")
        if os.path.exists(status_path):
            os.remove(status_path)

        LOG.info("analyse de l'arbre CATIA (macro de comptage)...")
        t0 = time.time()
        _run_macro(win32, "HarnessOpt_Count.catvbs",
                   build_count_macro(work, exclude_str))

        if not os.path.exists(status_path):
            raise CatiaError("La macro de comptage n'a produit aucun resultat.")
        with open(status_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = [ln.rstrip("\n") for ln in fh]
        head = lines[0] if lines else ""
        if not head.startswith(STATUS_OK):
            raise CatiaError(head.split("|", 1)[-1] or "analyse impossible")
        _, doc_name, count = (head.split("|") + ["", "0"])[:3]
        info = dict(document=doc_name, n_parts=int(count or 0),
                    names=[ln for ln in lines[1:] if ln.strip()],
                    seconds=time.time() - t0)
        LOG.kv("assemblage actif", {"document": info["document"],
                                    "pieces feuilles": info["n_parts"],
                                    "duree": f"{info['seconds']:.1f} s"})
        return info
    finally:
        pythoncom.CoUninitialize()


def export_stl(exclude_str="", on_progress=None, should_stop=None, poll=0.4) -> Path:
    """
    Exporte l'assemblage actif en STL (une piece = un fichier).

    `on_progress(done, total, elapsed)` est appele regulierement : c'est ce qui
    alimente la barre de progression et l'estimation de temps restant de la
    page 1. La macro tourne dans un thread dedie car ExecuteScript est bloquant.
    """
    pythoncom, win32 = _com()
    pythoncom.CoInitialize()
    try:
        _ensure_dirs()
        for filepath in glob.glob(os.path.join(str(STL_FOLDER), "*")):
            try:
                os.remove(filepath)
            except OSError:
                pass
        export_dir = os.path.join(str(STL_FOLDER), "")
        vba_code = build_export_macro(export_dir, exclude_str)

        macro_exception = []

        def _worker():
            pythoncom.CoInitialize()      # chaque thread initialise COM
            try:
                _run_macro(win32, "HarnessOpt_Export.catvbs", vba_code)
            except Exception as exc:
                macro_exception.append(exc)
            finally:
                pythoncom.CoUninitialize()

        LOG.info("lancement de la macro d'export STL...")
        t0 = time.time()
        thread = threading.Thread(target=_worker, name="catia-export", daemon=True)
        thread.start()

        total_file = os.path.join(export_dir, "export_total.txt")
        total_parts, done = 0, 0
        while thread.is_alive() and not os.path.exists(total_file):
            if should_stop is not None and should_stop():
                LOG.warn("annulation demandee pendant l'analyse de l'arbre")
                break
            if on_progress:
                on_progress(0, 0, time.time() - t0)
            time.sleep(poll)

        if os.path.exists(total_file):
            try:
                with open(total_file, "r") as fh:
                    total_parts = int(fh.read().strip())
            except (ValueError, OSError):
                total_parts = 0
        LOG.info(f"{total_parts} piece(s) a extraire")

        while thread.is_alive():
            done = len(glob.glob(os.path.join(export_dir, "*.stl")))
            if on_progress:
                on_progress(done, total_parts, time.time() - t0)
            if should_stop is not None and should_stop():
                LOG.warn("annulation demandee : la macro CATIA va terminer la piece "
                         "en cours puis s'arreter")
                break
            time.sleep(poll)

        thread.join(timeout=0.0 if (should_stop and should_stop()) else None)
        done = len(glob.glob(os.path.join(export_dir, "*.stl")))
        if on_progress:
            on_progress(done, total_parts, time.time() - t0)

        if macro_exception:
            raise macro_exception[0]

        status, message = _read_status(os.path.join(export_dir, "export_status.txt"))
        if status == STATUS_ERR:
            raise CatiaError(message)
        if message:
            LOG.info(f"macro : {message}")

        if not glob.glob(os.path.join(export_dir, "*.stl")):
            raise CatiaError("La macro s'est executee mais n'a produit aucun STL.\n"
                             "Verifier que le document actif contient des pieces "
                             "et que le filtre d'exclusion n'est pas trop large.")
        LOG.ok(f"{done} STL extraits en {time.time() - t0:.1f} s -> {STL_FOLDER}")
        return STL_FOLDER
    finally:
        pythoncom.CoUninitialize()


def import_stl(stl_path, component_name=None) -> str:
    """Reinjecte le STL du harnais dans l'arbre du produit actif."""
    pythoncom, win32 = _com()
    pythoncom.CoInitialize()
    try:
        _ensure_dirs()
        work = os.path.join(str(EXPORT_FOLDER), "")
        status_path = os.path.join(work, "import_status.txt")
        if os.path.exists(status_path):
            os.remove(status_path)
        name = component_name or f"HARNESS_OPT_{time.strftime('%Y%m%d_%H%M%S')}"
        LOG.info(f"import du harnais dans CATIA sous le nom {name}")
        _run_macro(win32, "HarnessOpt_Import.catvbs",
                   build_import_macro(work, str(stl_path), name))
        status, message = _read_status(status_path)
        if status != STATUS_OK:
            raise CatiaError(message)
        LOG.ok(message)
        return message
    finally:
        pythoncom.CoUninitialize()


# --------------------------------------------------------------------------
# insertion de GEOMETRIE CATIA NATIVE (sans licence de maillage)
# --------------------------------------------------------------------------

def build_native_macro(work_dir, csv_path, component_name, make_solid=True):
    """
    Macro d'insertion de geometrie CATIA native.

    L'import d'un STL passe par le Digitized Shape Editor : sur un poste sans
    cette licence, `Documents.Open` sur un .stl echoue silencieusement, et c'est
    la raison la plus frequente pour laquelle le harnais n'apparaissait pas dans
    l'arbre.

    Cette macro ne depend d'aucune licence de maillage : elle relit les fibres
    neutres depuis un CSV et construit, dans un CATPart neuf, une spline par
    troncon -- puis, si Generative Shape Design est disponible, un balayage
    circulaire au diametre du toron. Le CATPart est ensuite ajoute comme
    composant du produit actif. Une spline est de surcroit bien plus
    exploitable qu'un maillage pour la suite de la conception (Electrical
    Harness Installation).
    """
    solid_flag = "1" if make_solid else "0"
    return f'''
Const WORK_FOLDER = "{work_dir}"
Const CSV_PATH = "{csv_path}"
Const COMPONENT_NAME = "{component_name}"
Const MAKE_SOLID = {solid_flag}

Dim gSolidCount

Sub CATMain()
    Dim fso: Set fso = CreateObject("Scripting.FileSystemObject")
    Dim ts: Set ts = fso.CreateTextFile(WORK_FOLDER & "import_status.txt", True)
    gSolidCount = 0

    If Not fso.FileExists(CSV_PATH) Then
        ts.WriteLine "ERR|Fichier de courbes introuvable : " & CSV_PATH
        ts.Close
        Exit Sub
    End If

    Dim doc, rootProduct
    On Error Resume Next
    Set doc = CATIA.ActiveDocument
    On Error GoTo 0
    If doc Is Nothing Then
        ts.WriteLine "ERR|Aucun document actif dans CATIA."
        ts.Close
        Exit Sub
    End If
    On Error Resume Next
    Set rootProduct = doc.Product
    On Error GoTo 0
    If rootProduct Is Nothing Then
        ts.WriteLine "ERR|Le document actif n'est pas un produit (CATProduct)."
        ts.Close
        Exit Sub
    End If

    CATIA.DisplayFileAlerts = False
    CATIA.RefreshDisplay = False

    Dim partDoc, part, hsf, bodies, hb
    Set partDoc = CATIA.Documents.Add("Part")
    Set part = partDoc.Part
    Set hsf = part.HybridShapeFactory
    Set bodies = part.HybridBodies
    Set hb = bodies.Add()
    hb.Name = "HARNESS_OPT"

    Dim stream: Set stream = fso.OpenTextFile(CSV_PATH, 1)
    Dim line, cols
    Dim curSeg: curSeg = ""
    Dim curDia: curDia = 0
    Dim pts(4000)
    Dim nPts: nPts = 0
    Dim nSeg: nSeg = 0

    Do Until stream.AtEndOfStream
        line = stream.ReadLine
        If Trim(line) <> "" Then
            cols = Split(line, ";")
            If UBound(cols) >= 5 Then
                If IsNumeric(Replace(cols(3), ".", ",")) Then
                    If cols(0) <> curSeg Then
                        If nPts >= 2 Then nSeg = nSeg + BuildSegment(hsf, hb, pts, nPts, curDia)
                        curSeg = cols(0)
                        curDia = CDbl(Replace(cols(2), ".", ","))
                        nPts = 0
                    End If
                    If nPts <= 4000 Then
                        Set pts(nPts) = hsf.AddNewPointCoord( _
                            CDbl(Replace(cols(3), ".", ",")), _
                            CDbl(Replace(cols(4), ".", ",")), _
                            CDbl(Replace(cols(5), ".", ",")))
                        hb.AppendHybridShape pts(nPts)
                        nPts = nPts + 1
                    End If
                End If
            End If
        End If
    Loop
    stream.Close
    If nPts >= 2 Then nSeg = nSeg + BuildSegment(hsf, hb, pts, nPts, curDia)

    part.Update
    CATIA.RefreshDisplay = True

    Dim savePath: savePath = WORK_FOLDER & COMPONENT_NAME & ".CATPart"
    On Error Resume Next
    partDoc.SaveAs savePath
    If Err.Number <> 0 Then
        Err.Clear
        savePath = "(non enregistre)"
    End If
    Err.Clear

    Dim added
    Set added = rootProduct.Products.AddExternalComponent(partDoc)
    If Err.Number <> 0 Or added Is Nothing Then
        Err.Clear
        CATIA.DisplayFileAlerts = True
        ts.WriteLine "ERR|Le CATPart a ete cree (" & savePath & ") mais CATIA a refuse de l'ajouter au produit. L'inserer par Insertion > Composant existant."
        ts.Close
        Exit Sub
    End If
    added.Name = COMPONENT_NAME
    Err.Clear
    On Error GoTo 0

    CATIA.DisplayFileAlerts = True
    ts.WriteLine "OK|" & nSeg & " courbe(s) et " & gSolidCount & " solide(s) inseres dans l'arbre sous " & COMPONENT_NAME
    ts.Close
End Sub

Function BuildSegment(hsf, hb, pts, nPts, dia)
    BuildSegment = 0
    Dim spl, i
    On Error Resume Next
    Set spl = hsf.AddNewSpline()
    spl.SetSplineType 0
    spl.SetClosing 0
    For i = 0 To nPts - 1
        spl.AddPointWithConstraintExplicit pts(i), Nothing, -1#, 1, Nothing, 0#
    Next
    If Err.Number <> 0 Then
        Err.Clear
        On Error GoTo 0
        Exit Function
    End If
    hb.AppendHybridShape spl
    BuildSegment = 1

    ' balayage circulaire : necessite Generative Shape Design, donc facultatif
    If MAKE_SOLID = 1 And dia > 0 Then
        Dim swp
        Set swp = hsf.AddNewSweepCircle(spl)
        If Err.Number = 0 And Not swp Is Nothing Then
            swp.SetSweepType 1
            swp.CenterCurve = spl
            swp.SetRadius 1, dia / 2#
            If Err.Number = 0 Then
                hb.AppendHybridShape swp
                gSolidCount = gSolidCount + 1
            End If
        End If
        Err.Clear
    End If
    On Error GoTo 0
End Function
'''


def import_native(csv_path, component_name=None, make_solid=True) -> str:
    """
    Insere le harnais dans l'arbre CATIA sous forme de geometrie native
    (splines + balayages), a partir du CSV des fibres neutres.

    Voie a privilegier : aucune licence de maillage requise, et le resultat est
    une courbe reutilisable plutot qu'un decor triangule.
    """
    pythoncom, win32 = _com()
    pythoncom.CoInitialize()
    try:
        _ensure_dirs()
        work = os.path.join(str(EXPORT_FOLDER), "")
        status_path = os.path.join(work, "import_status.txt")
        if os.path.exists(status_path):
            os.remove(status_path)
        name = component_name or f"HARNESS_OPT_{time.strftime('%Y%m%d_%H%M%S')}"
        LOG.info(f"insertion de la geometrie native dans CATIA sous {name}")
        _run_macro(win32, "HarnessOpt_Native.catvbs",
                   build_native_macro(work, str(csv_path), name, make_solid))
        status, message = _read_status(status_path)
        if status != STATUS_OK:
            raise CatiaError(message)
        LOG.ok(message)
        return message
    finally:
        pythoncom.CoUninitialize()
