import queue
import threading
from tkinter import messagebox
from ..core import events
from ..catia import bridge

class AppController:
    def __init__(self, root, model, view):
        self.root = root
        self.model = model
        self.view = view

        # Le contrôleur centralise la file de messages et l'arrêt des threads
        self.q = queue.Queue()
        self._stop = threading.Event()
        self.busy = False

        # Démarrage de la boucle d'écoute globale
        self.root.after(120, self._drain)

    # ==================================================================
    # Logique de la Page 1 (Import)
    # ==================================================================
    def start_catia_extraction(self, exclude_str):
        """Remplace la méthode action_extract de ImportPage."""
        if self.busy:
            return

        n = (self.model.scene_info or {}).get("n_parts")
        if n and n > 400:
            if not messagebox.askyesno("Extraction volumineuse", "Continuer ?"):
                return

        self.busy = True
        self._stop.clear()
        self.view.pages["import"].set_busy_state(True, "Démarrage...")

        threading.Thread(
            target=self._task_extract,
            args=(exclude_str,),
            daemon=True
        ).start()

    def _task_extract(self, exclude):
        try:

            def on_progress(done, total, elapsed):
                self.q.put(("extract", done, total, elapsed))

            folder = bridge.export_stl(exclude, on_progress=on_progress,
                                       should_stop=self._stop.is_set)
            if self._stop.is_set():
                self.q.put(("cancelled", None))
                return

            self._load_folder_task(folder, source="CATIA V5")
        except Exception as exc:
            self.q.put(("error", str(exc), "Extraction CATIA interrompue"))

    # ==================================================================
    # Boucle de messages centrale
    # ==================================================================
    def _drain(self):
        try:
            while True:
                msg = self.q.get_nowait()
                self._handle_message(msg)
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    def _handle_message(self, msg):
        kind = msg[0]

        # Routage vers la vue Import
        if kind == "extract":
            done, total, elapsed = msg[1], msg[2], msg[3]
            self.view.pages["import"].update_progress(done, total, elapsed)

        elif kind == "scene":
            # Le contrôleur met à jour le Modèle
            _, merged, cloud, info = msg
            self.model.set_scene(merged, cloud, info)

            self.view.pages["import"].on_scene_loaded(info)
            self.busy = False

        elif kind == "error":
            detail, title = msg[1], msg[2]
            self.busy = False
            self.view.pages["import"].show_error(title, detail)

        # Routage vers la vue Cheminement (page 2)
        elif kind == events.PHASE:
            self.view.pages["routing"].update_phase(msg[1])

        # ... (les autres événements de page_routing.py comme ASTAR_END, SWEEP, etc.)
