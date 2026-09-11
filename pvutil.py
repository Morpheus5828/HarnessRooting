"""
Petits adaptateurs PyVista.

L'API de `extract_surface` a change de valeur par defaut selon les versions et
emet un avertissement si l'algorithme n'est pas explicite. On centralise le
choix ici pour ne pas polluer la console de l'utilisateur, tout en restant
compatible avec les versions anterieures qui ignorent ce parametre.
"""

from __future__ import annotations


def surface(mesh, triangulate=True):
    """Surface triangulee d'un maillage, quelle que soit la version de PyVista."""
    try:
        surf = mesh.extract_surface(algorithm="dataset_surface")
    except (TypeError, ValueError):
        surf = mesh.extract_surface()
    return surf.triangulate() if triangulate else surf
