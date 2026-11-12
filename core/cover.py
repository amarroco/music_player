# music_player/core/cover.py
"""Rendu d'une pochette en "demi-blocs" pour un terminal 256 couleurs.

Chaque cellule affiche le caractère ▀ : sa couleur de premier plan est le pixel
du haut, sa couleur de fond le pixel du bas -> 2 pixels carrés par cellule.
Pillow est optionnel : sans lui, render_cover() renvoie None."""
from io import BytesIO
from typing import List, Optional, Tuple

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

# cellules[y][x] = (index xterm-256 du pixel du haut, idem pour le pixel du bas)
Cells = List[List[Tuple[int, int]]]

_CUBE_LEVELS = (0, 95, 135, 175, 215, 255)


def rgb_to_xterm256(r: int, g: int, b: int) -> int:
    """Couleur xterm-256 la plus proche : meilleur choix entre le cube 6x6x6
    (16-231) et la rampe de gris (232-255)."""
    def nearest_level(v):
        return min(range(6), key=lambda i: abs(_CUBE_LEVELS[i] - v))

    ri, gi, bi = nearest_level(r), nearest_level(g), nearest_level(b)
    cube = (_CUBE_LEVELS[ri], _CUBE_LEVELS[gi], _CUBE_LEVELS[bi])
    gray_step = max(0, min(23, round((((r + g + b) / 3) - 8) / 10)))
    gray_val = 8 + 10 * gray_step
    gray = (gray_val, gray_val, gray_val)

    def dist(c):
        return (c[0] - r) ** 2 + (c[1] - g) ** 2 + (c[2] - b) ** 2

    if dist(gray) < dist(cube):
        return 232 + gray_step
    return 16 + 36 * ri + 6 * gi + bi


def render_cover(data: bytes, max_cols: int, max_rows: int) -> Optional[Cells]:
    """Réduit l'image pour tenir dans max_cols x max_rows cellules (ratio conservé)."""
    if Image is None or max_cols < 2 or max_rows < 1:
        return None
    try:
        img = Image.open(BytesIO(data)).convert("RGB")
    except Exception:
        return None

    iw, ih = img.size
    scale = min(max_cols / iw, (max_rows * 2) / ih)
    cols = max(1, int(iw * scale))
    rows = max(1, int(ih * scale) // 2)
    resample = getattr(Image, "Resampling", Image).LANCZOS
    img = img.resize((cols, rows * 2), resample)

    px = img.load()
    return [
        [(rgb_to_xterm256(*px[x, 2 * y]), rgb_to_xterm256(*px[x, 2 * y + 1]))
         for x in range(cols)]
        for y in range(rows)
    ]
