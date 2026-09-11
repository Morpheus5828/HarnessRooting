# harnessopt/ui/config.py

CONNECTOR_STL_PATH = r"C:\Users\a609568\Desktop\STL\Fixations_H160\ECS0792A012A_--D_STD01_CABLE-TIE SUPPORT.1_OFFSET.stl"

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

USE_SHRH = False
SHRH_N_ITER = 100         # istag
SHRH_I_HRH = 25           # iHRH
SHRH_DELTA0 = 1.5         # delta[0]
SHRH_STALL_LIMIT = 10     # iterations avant réduction du pas
SHRH_DELTA_MULT = 0.8     # multiplicateur de réduction
SHRH_DELTA_MIN = 1e-4     # epsilon
