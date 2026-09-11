# harnessopt/ui/config.py
"""
Reglages metier de HarnessOpt.

Un seul fichier decrit TOUTES les regles d'integration (les "regles HS") et
les hyperparametres du cheminement. C'est ce fichier que MOMOS relit pour
controler un harnais : changer une valeur ici change le controle, sans
toucher au code.
"""

CONNECTOR_STL_PATH = r"C:\Users\a609568\Desktop\STL\Fixations_H160\ECS0792A012A_--D_STD01_CABLE-TIE SUPPORT.1_OFFSET.stl"

# Modele de crabe (collier) pose automatiquement le long du harnais.
# Laisser la chaine vide pour utiliser le pave par defaut (14 x 10 x 18 mm).
CRAB_PATH = r"C:\Users\a609568\Desktop\STL\Crabes\CRABE_AUTO.stl"
# Ancien nom, conserve pour ne rien casser dans le code existant.
CRAB_STL_PATH = CRAB_PATH

# --- Base ---
SECURITY_DISTANCE = 10
IDEAL_DISTANCE_WITH_STRUCTURE = 15
CABLE_DIAMETER = 6.0
MINIMAL_BEND_RADIUS = 48.0
FIXATION_ATTRACTIVE = 0.2

# --- Regroupement & Tronc ---
WEIGHT_BUNDLE = 0.15
TRUNK_TENSION = 0.5

# --- Securite vis-a-vis de la structure ---
K_FAR = 0.7

# --- Fixations existantes ---
USE_FIXATIONS = True
SNAP_RADIUS_MM = 150.0
CLAMP_CENTER_BIAS = 0.8

# --- Regles metier ---
L_MIN_BB = 120.0
CONNECTOR_LEAD_IN = 500
L_MIN_TB = 80.0
CLIP_SPACING = 250.0

# --- Reglages avances ---
RESOLUTION = 10
EPS = 1.15
MAX_SWEEPS = 10
TIME_BUDGET_S = 300.0
PASSAGE_RADIUS_MM = 300.0
PASSAGE_MAX_DETOUR = 800.0
PASSAGE_MAX_SPAN = 1000.0
RESAMPLE_STEP = 12.0
SMOOTH_ITERS = 100

# ==========================================================================
# Algorithme de cheminement : "HRH" ou "SHRH"
# ==========================================================================
# HRH  : Harness Routing Heuristic (Karlsson et al., 2023). Rapide, c'est le
#        mode par defaut.
# SHRH : HRH precede d'une relaxation lagrangienne par sous-gradient. Plus
#        long, mais explore des topologies de tronc que la HRH seule ne
#        trouve pas.
ROUTING_ALGORITHM = "HRH"

# Les deux noms restent synchronises : le code historique lit USE_SHRH, la
# nouvelle interface lit ROUTING_ALGORITHM.
USE_SHRH = str(ROUTING_ALGORITHM).strip().upper() == "SHRH"

SHRH_N_ITER = 100         # istag
SHRH_I_HRH = 25           # iHRH
SHRH_DELTA0 = 1.5         # delta[0]
SHRH_STALL_LIMIT = 10     # iterations avant réduction du pas
SHRH_DELTA_MULT = 0.8     # multiplicateur de réduction
SHRH_DELTA_MIN = 1e-4     # epsilon


def routing_algorithm(name=None) -> str:
    """Nom normalise de l'algorithme ("HRH" ou "SHRH")."""
    value = str(name if name is not None else ROUTING_ALGORITHM).strip().upper()
    return "SHRH" if value == "SHRH" else "HRH"


def use_shrh(name=None) -> bool:
    """Faut-il lancer la relaxation lagrangienne avant la HRH ?"""
    return routing_algorithm(name) == "SHRH"


# ==========================================================================
# Accrochage des connecteurs sur le harnais (bbox -> face la plus proche)
# ==========================================================================
# Le point clique dans CATIA est le centre de gravite de la piece : il est
# DANS le connecteur. On calcule la boite englobante du connecteur, on
# retient la face tournee vers le harnais, et c'est le CENTRE DE CETTE FACE
# qui devient le depart (ou l'arrivee) du cable.
CONNECTOR_SNAP_TO_FACE = True
# Deport applique au centre de face, le long de la normale sortante (mm).
# C'est un PLANCHER : le cheminement l'eleve au besoin a une distance de
# securite plus une demi-maille, sans quoi le terminal se retrouve pile sur
# la limite de garde et devient inatteignable.
CONNECTOR_FACE_OFFSET = 3.0
# Rayon de recherche du maillage de la piece autour du point clique (mm).
# Sert quand on doit decouper la piece dans la maquette fusionnee.
CONNECTOR_SEARCH_RADIUS = 400.0
# Boite englobante orientee (axes principaux du maillage) plutot qu'alignee
# sur X/Y/Z : un connecteur monte de biais garde alors des faces franches.
CONNECTOR_ORIENTED_BBOX = True

# ==========================================================================
# MOMOS : controle automatique des regles HS
# ==========================================================================
# MOMOS est lance a la fin de chaque cheminement. Il relit ce fichier, verifie
# chaque regle sur le harnais calcule, puis ouvre une fenetre 3D dediee ou
# l'integralite des regles HS est affichee, les violations etant pointees.
MOMOS_ENABLED = True
MOMOS_AUTO_OPEN_3D = True          # ouvrir la fenetre 3D sans rien demander
MOMOS_SHOW_STRUCTURE = True        # afficher la maquette en transparence
MOMOS_MARKER_RADIUS = 12.0         # rayon des pastilles de violation (mm)
MOMOS_MAX_MARKERS = 400            # garde-fou d'affichage

# Regles HS complementaires, controlees par MOMOS seul.
HS_MAX_BUNDLE_DIAMETER = 60.0      # diametre maxi d'un toron (mm)
HS_MIN_CLIP_SPACING = 80.0         # deux crabes ne se touchent pas (mm)
HS_CONNECTOR_AXIS_TOL = 5.0        # ecart maxi a l'amorce droite (mm)
HS_MAX_FILL_RATIO = 0.85           # taux de remplissage maxi d'un passage
