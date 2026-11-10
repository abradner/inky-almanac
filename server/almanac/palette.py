"""The seven inks of the Inky Frame 7.3" (ACeP) panel.

Index order is the panel's pen order (picographics / inky_frame constants), so an
indexed PNG drawn with pngdec's PNG_COPY mode puts each index straight onto the
matching ink.

INK_RGB is roughly what each ink looks like on the glass. The inks are much more
muted than pure RGB, and dithering against what the panel can really show gives
far better pictures than dithering against #ff0000 and friends.
"""

from PIL import Image

BLACK, WHITE, GREEN, BLUE, RED, YELLOW, ORANGE = range(7)

INK_RGB = [
    (26, 24, 30),     # black
    (238, 238, 230),  # white
    (46, 104, 64),    # green
    (44, 54, 128),    # blue
    (176, 52, 44),    # red
    (226, 206, 66),   # yellow
    (208, 112, 52),   # orange
]

# Pure colours for the PNG's own palette. The panel ignores these (PNG_COPY uses
# the index), but a browser shows them, so keep them recognisable.
PNG_RGB = [
    (0, 0, 0),
    (255, 255, 255),
    (0, 160, 0),
    (0, 0, 255),
    (255, 0, 0),
    (255, 255, 0),
    (255, 140, 0),
]


def _flat(colours):
    flat = [c for rgb in colours for c in rgb]
    # Pad by repeating black so Pillow never maps a pixel to an unused entry.
    return flat + list(colours[0]) * (256 - len(colours))


def palette_image(colours=INK_RGB):
    img = Image.new("P", (1, 1))
    img.putpalette(_flat(colours))
    return img


def new_canvas(size, fill=WHITE):
    img = Image.new("P", size, fill)
    img.putpalette(_flat(PNG_RGB))
    return img


def to_preview(canvas):
    """RGB rendering of an index canvas using the inks' real-world colours."""
    preview = canvas.copy()
    preview.putpalette(_flat(INK_RGB))
    return preview.convert("RGB")
