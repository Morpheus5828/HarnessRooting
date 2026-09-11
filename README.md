# HarnessOpt

*an AI application help designer for harness rooting.*

Cheminement automatique de harnais helicoptere, pilote depuis CATIA V5.
L'application calcule le trace d'un faisceau (heuristique **HRH**, Karlsson et
al. 2023, ou sa variante lagrangienne **SHRH**), le lisse, pose les crabes,
renvoie le tout dans CATIA, puis **controle les regles HS**.

---

## Les deux points d'entree

| Commande | Ce qu'elle ouvre |
|---|---|
| `python run_harnessopt.py` | l'application complete : page 1 maquette, page 2 cheminement |
| `python click_and_root_auto.py` | le parcours guide **Click & Root** |

Options utiles de Click & Root :

```bash
python click_and_root_auto.py --diametre 6      # diametre d'un cable (mm)
python click_and_root_auto.py --algo SHRH       # force l'algorithme
python click_and_root_auto.py --sans-splash     # saute l'ecran de demarrage
```

Les deux ouvrent d'abord un ecran de demarrage (logo, nom, accroche), puis la
fenetre de travail.

---

## Click & Root, etape par etape

1. **Departs** - vous cliquez les connecteurs de depart *dans CATIA*.
   `Echap` (dans CATIA) termine la selection, `P` valide.
2. **Arrivees** - idem.
3. **Schema** - verification des liaisons, choix de l'**algorithme**
   (HRH / SHRH), dossier de colliers facultatif. `R` lance le calcul.
4. **Cheminement** - minuteur, etapes, puis export automatique vers CATIA.
5. **Regles HS** - MOMOS affiche son verdict et ouvre la vue 3D des regles.

### Se tromper de connecteur : `Ctrl+Z`

`Ctrl+Z` retire le dernier clic. Quand il n'y a plus de point, il remonte a la
phase precedente (arrivees -> departs, schema -> arrivees, resultat ->
reglages). `Ctrl+Y` retablit. Les boutons *Annuler le dernier clic*,
*Retablir* et *Tout effacer* font la meme chose a la souris.

### Refaire un rooting

Depuis l'ecran de resultat :

* **Recalculer les memes points** - garde les terminaux, reprend les reglages
  de `config.py` et l'algorithme choisi. Sert a comparer HRH et SHRH, ou a
  ajouter un dossier de fixations.
* **Nouveau routage** - repart des clics.

Dans les deux cas la maquette reste en memoire : la relance ne reexporte pas
les STL et ne reconstruit pas le detecteur de collision.

---

## Accrochage des connecteurs (bbox -> face la plus proche)

Un clic dans CATIA rend le **centre de gravite** de la piece, donc un point
situe *a l'interieur* du connecteur. Le cable en partirait en traversant son
propre boitier. HarnessOpt applique donc le raisonnement suivant
(`core/connector_snap.py`) :

1. le STL de la piece cliquee est retrouve par son nom dans le dossier
   d'export (a defaut, une bulle est decoupee dans la maquette) ;
2. sa **boite englobante** est calculee sur ses axes propres -- un connecteur
   monte de biais garde ainsi des faces franches ;
3. la **face la plus proche du harnais** est retenue : distance au rectangle
   de la face, departagee par la face que traverse le segment
   connecteur -> harnais ;
4. le **centre de cette face** devient le depart (ou l'arrivee), deporte le
   long de la normale sortante, et cette normale donne la direction de sortie
   du cable -- donc son amorce droite.

L'amorce demandee (`CONNECTOR_LEAD_IN`) est reduite automatiquement si le
point qu'elle impose tombe dans la structure.

---

## MOMOS : le controle des regles HS

`MOMOS.py` est lance **automatiquement a la fin de chaque cheminement**. Il
relit `ui/config.py` -- ce fichier *est* le referentiel -- verifie chaque
regle, puis ouvre une **fenetre 3D dediee** ou l'integralite des regles est
affichee et chaque violation pointee sur le harnais.

| Code | Regle | Reglage |
|---|---|---|
| HS-01 | Distance de securite a la structure | `SECURITY_DISTANCE` |
| HS-02 | Aucune traversee de structure | - |
| HS-03 | Rayon de cintrage minimal | `MINIMAL_BEND_RADIUS` |
| HS-04 | Amorce droite en sortie de connecteur | `CONNECTOR_LEAD_IN` |
| HS-05 | Espacement maximal des fixations | `CLIP_SPACING` |
| HS-06 | Longueur mini entre deux branchements | `L_MIN_BB` |
| HS-07 | Longueur mini terminal -> branchement | `L_MIN_TB` |
| HS-08 | Distance ideale a la structure | `IDEAL_DISTANCE_WITH_STRUCTURE` |
| HS-09 | Diametre maximal d'un toron | `HS_MAX_BUNDLE_DIAMETER` |
| HS-10 | Espacement minimal entre deux crabes | `HS_MIN_CLIP_SPACING` |
| HS-11 | Remplissage des passages de fixation | `HS_MAX_FILL_RATIO` |
| HS-12 | Ecart a l'axe du connecteur | `HS_CONNECTOR_AXIS_TOL` |

Dans la fenetre 3D : `1` toutes les regles, `2` les violations, `3` les
violations et les limites, `O` masque les pastilles.

Le rapport est ecrit a cote des exports (`momos_rapport.json`) et peut etre
rouvert seul :

```bash
python -m harnessopt.MOMOS momos_rapport.json
```

---

## Reglages (`ui/config.py`)

Tout est dans ce fichier. Les ajouts recents :

```python
CRAB_PATH = r"...\CRABE_AUTO.stl"   # modele de crabe pose automatiquement
ROUTING_ALGORITHM = "HRH"           # ou "SHRH" (USE_SHRH reste synchronise)

CONNECTOR_SNAP_TO_FACE = True       # accrochage bbox -> face la plus proche
CONNECTOR_FACE_OFFSET = 3.0         # deport mini du centre de face (mm)
CONNECTOR_ORIENTED_BBOX = True      # boite sur les axes propres du connecteur

MOMOS_ENABLED = True                # controle des regles HS en fin de calcul
MOMOS_AUTO_OPEN_3D = True           # ouverture de la fenetre 3D des regles
```

`CRAB_PATH` vide (ou fichier absent) : le crabe par defaut est utilise.

---

## Organisation

```
click_and_root.py      parcours guide : point d'entree
run_harnessopt.py      application complete : point d'entree
MOMOS.py               controle des regles HS + fenetre 3D
viewer.py              visionneuse 3D (processus separe)
catia/                 pilotage CATIA : export STL, macros, selection au clic
core/                  cheminement : HRH, SHRH, lissage, crabes, connecteurs
  connector_snap.py    boite englobante et face la plus proche
ui/                    interface : theme, widgets, pages, splash, config
```

## Prerequis

Windows, CATIA V5 et `pywin32` pour le pilotage ; `numpy`, `scipy`,
`pyvista` et `matplotlib` pour le calcul et l'affichage.
