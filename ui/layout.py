"""
Geometrie du schema du harnais, sans Tkinter.

Le placement des cartes, le trace du tronc commun et la mise a l'echelle de la
silhouette d'un connecteur sont du calcul, pas du dessin. Les sortir du widget
leur donne deux proprietes utiles : ils se testent sans ouvrir de fenetre, et
la lecture du widget reste centree sur ce qu'il fait vraiment -- poser des
widgets et tracer des traits.
"""

from __future__ import annotations

import numpy as np


def plan_cards(sides, width, height, card_w, card_h, gap, margin):
    """
    Place les cartes : entrees a gauche, sorties a droite.

    `sides` donne le cote de chaque noeud ("in" ou "out"). Retourne
    dict(positions, x_left, x_right, trunk) ou `positions[i]` est le coin haut
    gauche de la carte i, `x_left` le bord droit de la colonne d'entree,
    `x_right` le bord gauche de la colonne de sortie, et `trunk` le segment
    (x0, x1, y) du tronc commun.

    La colonne la plus courte est centree sur l'autre : sans cela, une entree
    unique face a trois sorties se retrouve en haut, et le faisceau parait
    partir de travers.
    """
    ins = [i for i, s in enumerate(sides) if s == "in"]
    outs = [i for i, s in enumerate(sides) if s != "in"]
    rows = max(len(ins), len(outs), 1)

    width = max(int(width), 2 * card_w + 220)
    height = max(int(height), rows * (card_h + gap) + 2 * margin)

    x_in = margin
    x_out = max(width - card_w - margin, x_in + card_w + 180)

    # Le bloc est centre verticalement : avec deux cartes dans une planche
    # haute, les laisser en haut donne un schema qui flotte.
    bloc = rows * (card_h + gap) - gap
    haut = max(margin, (height - bloc) / 2.0)

    positions = {}
    for column, group in ((x_in, ins), (x_out, outs)):
        for k, i in enumerate(group):
            y = haut + k * (card_h + gap)
            if len(group) < rows:
                y += (rows - len(group)) * (card_h + gap) / 2.0
            positions[i] = (float(column), float(y))

    x_left, x_right = x_in + card_w, x_out
    ys = [positions[i][1] + card_h / 2.0 for i in positions]
    trunk = (x_left + 70.0, x_right - 70.0,
             float(np.mean(ys)) if ys else height / 2.0)
    return dict(positions=positions, x_left=float(x_left),
                x_right=float(x_right), trunk=trunk, width=width, height=height)


def wire_points(pos_a, pos_b, x_left, x_right, trunk, card_h):
    """
    Les deux courbes d'un cable : de sa carte au tronc, puis du tronc a l'autre.

    Retourne (aller, retour), chacun une liste de points a passer a un trace
    lisse. Les points intermediaires imposent une tangente horizontale en
    sortie de carte et a l'entree du tronc : c'est ce qui donne la courbe en S
    d'une planche de cablage, au lieu d'une diagonale.
    """
    x0, x1, y_trunk = trunk
    ya = pos_a[1] + card_h / 2.0
    yb = pos_b[1] + card_h / 2.0
    aller = [(x_left, ya), (x_left + 34.0, ya), (x0 - 26.0, y_trunk), (x0, y_trunk)]
    retour = [(x1, y_trunk), (x1 + 26.0, y_trunk), (x_right - 34.0, yb),
              (x_right, yb)]
    return aller, retour


def thumb_polygon(silhouette, side, flip, width, height, pad=18.0, pad_y=18.0):
    """
    Silhouette du connecteur mise a l'echelle de la vignette.

    Le point de sortie du cable est place du cote du harnais -- une entree pose
    son corps a gauche et sort a droite, une sortie fait l'inverse -- et le
    sens retourne echange les deux. Le corps occupe la vignette moins la place
    de la fleche : sans cette reserve, un connecteur long deborde du cadre.

    Retourne (points, sortie, pointe) : le contour en pixels, le point de
    sortie du cable, et l'extremite de la fleche qui montre ou part le toron.
    Une silhouette vide (connecteur *Aucun*) rend le contour d'un simple tube.
    """
    sil = np.asarray(silhouette, float).reshape(-1, 2)
    cy = height / 2.0
    vers_droite = (side == "in")
    if flip:
        vers_droite = not vers_droite

    # zone reservee au corps, la fleche occupant le bord oppose
    if vers_droite:
        x0, tip = width - pad, width - 2.0
        body0, body1 = 8.0, width - pad
    else:
        x0, tip = pad, 2.0
        body0, body1 = pad, width - 8.0
    sign = -1.0 if vers_droite else 1.0

    if not len(sil):                       # tube seul
        demi = 5.0
        pts = [(body0, cy - demi), (body1, cy - demi),
               (body1, cy + demi), (body0, cy + demi)]
        return pts, (x0, cy), (tip, cy)

    span_x = max(float(np.ptp(sil[:, 0])), 1e-6)
    span_y = max(float(np.ptp(sil[:, 1])), 1e-6)
    k = min((body1 - body0) / span_x, (height - pad_y) / span_y)
    pts = [(x0 + sign * float(sx) * k, cy - float(sy) * k) for sx, sy in sil]
    return pts, (x0, cy), (tip, cy)
