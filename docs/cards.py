"""Render the README hero and the GitHub social preview from one design.

    python docs/cards.py            # writes docs/*.svg; with cairosvg installed also docs/*.png

Fonts: Noto Sans (falls back to the system sans-serif).
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent
BG, BG2 = '#0f1720', '#16212d'
INK, MUTED, ACCENT, ACCENT2, LINE = '#eef3f8', '#9fb0c2', '#f08a24', '#3fb6c8', '#2a3a4c'
FONT = "'Noto Sans', 'Helvetica Neue', Arial, sans-serif"


def cube(x, y, s, top, left, right):
    """Isometric cube with its top vertex at (x, y) and edge s."""
    h = s * 0.5
    return (f'<path d="M{x},{y} l{s},{h} l-{s},{h} l-{s},-{h} z" fill="{top}"/>'
            f'<path d="M{x - s},{y + h} l{s},{h} v{s} l-{s},-{h} z" fill="{left}"/>'
            f'<path d="M{x + s},{y + h} l-{s},{h} v{s} l{s},-{h} z" fill="{right}"/>')


def art(ox, oy, k):
    """Cloud of parts flowing into a folder on disk; origin (ox, oy), scale k."""
    parts = []
    # Cloud outline.
    parts.append(f'<g transform="translate({ox},{oy}) scale({k})">'
                 '<path d="M60,120 a50,50 0 0 1 20,-96 a70,70 0 0 1 130,-4 a55,55 0 0 1 70,52 '
                 'a46,46 0 0 1 -14,48 z" fill="none" stroke="%s" stroke-width="5" stroke-linejoin="round"/>' % LINE)
    # Parts in the cloud.
    parts.append(cube(110, 40, 26, '#f6b26b', '#d9731a', '#b85f12'))
    parts.append(cube(170, 30, 20, '#7fd6e2', '#2f9bb0', '#237a8b'))
    parts.append(cube(220, 62, 16, '#f6b26b', '#d9731a', '#b85f12'))
    # Flow arrow.
    parts.append(f'<path d="M165,140 v70" stroke="{ACCENT}" stroke-width="7" stroke-linecap="round" '
                 'stroke-dasharray="2 16"/>')
    parts.append(f'<path d="M147,205 l18,22 l18,-22" fill="none" stroke="{ACCENT}" stroke-width="7" '
                 'stroke-linecap="round" stroke-linejoin="round"/>')
    # Folder.
    parts.append(f'<path d="M70,250 h58 l14,16 h118 a10,10 0 0 1 10,10 v104 a10,10 0 0 1 -10,10 h-180 '
                 f'a10,10 0 0 1 -10,-10 v-120 a10,10 0 0 1 10,-10 z" fill="{BG2}" stroke="{MUTED}" stroke-width="4"/>')
    # Files in the folder.
    for i, (label, color) in enumerate([('.f3d', ACCENT), ('.f3z', ACCENT2), ('.step', INK)]):
        y = 286 + i * 30
        parts.append(f'<rect x="88" y="{y}" width="164" height="22" rx="5" fill="{BG}" stroke="{LINE}" stroke-width="2"/>')
        parts.append(f'<rect x="96" y="{y + 7}" width="60" height="8" rx="4" fill="{LINE}"/>')
        parts.append(f'<text x="244" y="{y + 16}" text-anchor="end" font-family="{FONT}" font-size="14" '
                     f'font-weight="700" fill="{color}">{label}</text>')
    parts.append('</g>')
    return ''.join(parts)


def chips(x, y, labels, size):
    out, cx = [], x
    for label in labels:
        width = int(len(label) * size * 0.5) + size * 2
        out.append(f'<rect x="{cx}" y="{y}" width="{width}" height="{size * 2.2:.0f}" rx="{size * 1.1:.0f}" '
                   f'fill="none" stroke="{LINE}" stroke-width="2"/>')
        out.append(f'<text x="{cx + width / 2:.0f}" y="{y + size * 1.48:.0f}" text-anchor="middle" '
                   f'font-family="{FONT}" font-size="{size}" fill="{MUTED}">{label}</text>')
        cx += width + size
    return ''.join(out)


def card(width, height, title_size, tagline, chip_size, art_x, art_y, art_k, text_x, text_y):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="{BG}"/><stop offset="1" stop-color="{BG2}"/></linearGradient></defs>
<rect width="{width}" height="{height}" fill="url(#g)"/>
<rect x="0" y="0" width="{width}" height="6" fill="{ACCENT}"/>
<text x="{text_x}" y="{text_y}" font-family="{FONT}" font-size="{title_size}" font-weight="800" fill="{INK}">Fusion Bulk Export</text>
{''.join(f'<text x="{text_x}" y="{text_y + title_size * 0.95 + i * title_size * 0.62:.0f}" font-family="{FONT}" font-size="{title_size * 0.42:.0f}" fill="{MUTED}">{line}</text>' for i, line in enumerate(tagline))}
{chips(text_x, int(text_y + title_size * 0.95 + len(tagline) * title_size * 0.62), ['.f3d / .f3z + STEP', 'resumable', 'macOS + Windows', 'MIT'], chip_size)}
{art(art_x, art_y, art_k)}
</svg>
'''


CARDS = {
    # GitHub social preview: 1280x640, keep content inside a 40 px safe margin.
    'social-preview': dict(width=1280, height=640, title_size=76, chip_size=20,
                           tagline=['Every design in your Fusion account,', 'saved to your own disk.'],
                           art_x=840, art_y=110, art_k=1.15, text_x=80, text_y=250),
    # README hero: wide and short.
    'hero': dict(width=1280, height=420, title_size=64, chip_size=18,
                 tagline=['Every design in your Fusion account, saved to your own disk.'],
                 art_x=930, art_y=58, art_k=0.82, text_x=80, text_y=170),
}


def main():
    try:
        import cairosvg
    except ImportError:
        cairosvg = None
    for name, spec in CARDS.items():
        svg = card(**spec)
        (HERE / (name + '.svg')).write_text(svg, encoding='utf-8')
        if cairosvg:
            cairosvg.svg2png(bytestring=svg.encode('utf-8'), write_to=str(HERE / (name + '.png')))
        print(name, 'png' if cairosvg else 'svg only')


if __name__ == '__main__':
    main()
