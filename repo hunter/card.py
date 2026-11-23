"""Draws a 1600x900 post image for a repo, in the Repo Hunter style (dark terminal look)."""
import math
import os
import re
import unicodedata

import requests
from PIL import Image, ImageDraw, ImageFont

import config

W, H, PAD = 1600, 900, 96
BG, DOT = (11, 15, 20), (28, 37, 48)
WHITE, TEXT, MUTED = (230, 237, 243), (201, 209, 217), (139, 152, 165)
CHIP_BG, CHIP_BORDER, FADED = (15, 26, 20), (31, 59, 42), (23, 48, 31)

FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
FONT_URL = "https://github.com/JetBrains/JetBrainsMono/raw/master/fonts/ttf/JetBrainsMono-{}.ttf"
FALLBACKS = {
    "Regular": ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"],
    "Bold": ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf"],
    "ExtraBold": ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
                  "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf"],
}
_paths, _fonts = {}, {}


def _hex(color):
    c = color.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _font_path(weight):
    """JetBrains Mono (downloaded once into fonts/), else a system mono font."""
    if weight in _paths:
        return _paths[weight]
    path = os.path.join(FONT_DIR, f"JetBrainsMono-{weight}.ttf")
    if not os.path.exists(path):
        try:
            r = requests.get(FONT_URL.format(weight), timeout=30)
            r.raise_for_status()
            if not r.content.startswith((b"\x00\x01\x00\x00", b"true", b"OTTO")):
                raise ValueError("download is not a font file")
            os.makedirs(FONT_DIR, exist_ok=True)
            with open(path, "wb") as f:
                f.write(r.content)
        except (requests.RequestException, ValueError, OSError) as e:
            print(f"[warn] JetBrains Mono {weight} unavailable ({e}); using a fallback font")
            path = next((p for p in FALLBACKS[weight] if os.path.exists(p)), None)
    _paths[weight] = path
    return path


def font(weight, size):
    key = (weight, size)
    if key not in _fonts:
        path = _font_path(weight)
        _fonts[key] = ImageFont.truetype(path, size) if path else ImageFont.load_default(size)
    return _fonts[key]


def clean_text(text):
    """Drop emoji, :shortcodes: and symbols the mono font can't draw."""
    text = re.sub(r":[a-z0-9_+-]+:", "", text or "")
    keep = [c for c in text
            if ord(c) <= 0xFFFF and not 0xFE00 <= ord(c) <= 0xFE0F and c != "‍"
            and unicodedata.category(c) not in ("So", "Cs", "Co", "Cn")]
    return re.sub(r"\s+", " ", "".join(keep)).strip()


def _ellipsize(draw, text, fnt, width):
    while text and draw.textlength(text + "…", font=fnt) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def _truncate(draw, text, fnt, width):
    return text if draw.textlength(text, font=fnt) <= width else _ellipsize(draw, text, fnt, width)


def fit_line(draw, text, weight, max_size, min_size, width):
    """Largest font size (down to min_size) that fits one line; truncate beyond that."""
    for size in range(max_size, min_size - 1, -4):
        fnt = font(weight, size)
        if draw.textlength(text, font=fnt) <= width:
            return text, fnt
    fnt = font(weight, min_size)
    return _truncate(draw, text, fnt, width), fnt


def wrap(draw, text, fnt, width, max_lines):
    """Word-wrap into at most max_lines; the last line ends in … if text was cut."""
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=fnt) <= width:
            line = trial
            continue
        if line:
            lines.append(line)
        if len(lines) == max_lines:          # more text left but no room
            lines[-1] = _ellipsize(draw, lines[-1], fnt, width)
            return lines
        line = _truncate(draw, word, fnt, width)
    if line:
        lines.append(line)
    return lines


def _reticle(d, cx, cy, r, color, width, glyph=None):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=width)
    t_out, t_in = r * 1.27, r * 0.81
    for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
        d.line([cx + dx * t_out, cy + dy * t_out, cx + dx * t_in, cy + dy * t_in], fill=color, width=width)
    if glyph:  # git branch in the middle, like the profile photo
        s, gw = r / 118, max(int(width * 0.85), 2)
        x0, ytop, ybot, bx, by = cx - 20 * s, cy - 50 * s, cy + 50 * s, cx + 26 * s, cy - 32 * s
        d.line([x0, ytop, x0, ybot], fill=glyph, width=gw)
        pts = [(bx + (x0 - bx) * t ** 1.6, by + (cy + 32 * s - by) * t) for t in (i / 12 for i in range(13))]
        d.line(pts, fill=glyph, width=gw, joint="curve")
        nr = 15 * s
        for x, y in ((x0, ytop), (x0, ybot)):
            d.ellipse([x - nr, y - nr, x + nr, y + nr], fill=glyph)
        d.ellipse([bx - nr, by - nr, bx + nr, by + nr], fill=BG, outline=color, width=max(int(9 * s), 2))


def _star(d, cx, cy, r, color):
    pts = []
    for i in range(10):
        a = -math.pi / 2 + i * math.pi / 5
        rad = r if i % 2 == 0 else r * 0.45
        pts.append((cx + rad * math.cos(a), cy + rad * math.sin(a)))
    d.polygon(pts, fill=color)


def render_card(repo, path, accent=None, handle=None):
    accent = _hex(accent or config.CARD_ACCENT)
    handle = (config.ACCOUNT_HANDLE if handle is None else handle).lstrip("@")
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    for x in range(16, W, 32):          # dot grid
        for y in range(16, H, 32):
            d.ellipse([x - 1.5, y - 1.5, x + 1.5, y + 1.5], fill=DOT)
    _reticle(d, W + 10, 70, 230, FADED, 8)

    # prompt line
    f_prompt = font("Regular", 30)
    d.text((PAD, PAD), "~/open-source $ ", font=f_prompt, fill=accent)
    d.text((PAD + d.textlength("~/open-source $ ", font=f_prompt), PAD), "repo-hunter --daily",
           font=f_prompt, fill=MUTED)

    # owner / name
    owner, name = repo["full_name"].split("/", 1)
    width = W - 2 * PAD
    y = PAD + 92
    d.text((PAD, y), _truncate(d, f"{owner} /", font("Regular", 40), width), font=font("Regular", 40), fill=MUTED)
    y += 62
    name_text, f_name = fit_line(d, name, "ExtraBold", 104, 56, width)
    d.text((PAD, y), name_text, font=f_name, fill=WHITE)
    y += f_name.size + 44

    # description
    f_desc = font("Regular", 40)
    for line in wrap(d, clean_text(repo.get("description")), f_desc, width, 4):
        d.text((PAD, y), line, font=f_desc, fill=TEXT)
        y += 58

    # chips: stars, language, license
    by, ch = H - PAD - 64, 64
    f_chip = font("Bold", 28)
    chips = [("star", f"{repo.get('stars', 0):,}")]
    if repo.get("language"):
        chips.append(("text", repo["language"]))
    if repo.get("license") and repo["license"] != "NOASSERTION":
        chips.append(("text", repo["license"]))
    x = PAD
    for kind, label in chips:
        icon = 36 if kind == "star" else 0
        w = int(d.textlength(label, font=f_chip)) + 48 + icon
        d.rounded_rectangle([x, by, x + w, by + ch], radius=12, fill=CHIP_BG, outline=CHIP_BORDER, width=2)
        if kind == "star":
            _star(d, x + 24 + 12, by + ch / 2, 14, accent)
        d.text((x + 24 + icon, by + ch / 2), label, font=f_chip, fill=accent, anchor="lm")
        x += w + 16

    # brand, bottom right
    f_brand = font("Bold", 30)
    right = W - PAD
    if handle:
        d.text((right, by + 18), "Repo Hunter", font=f_brand, fill=WHITE, anchor="rm")
        d.text((right, by + 52), f"@{handle}", font=font("Regular", 24), fill=MUTED, anchor="rm")
        text_w = max(d.textlength("Repo Hunter", font=f_brand), d.textlength(f"@{handle}", font=font("Regular", 24)))
    else:
        d.text((right, by + ch / 2), "Repo Hunter", font=f_brand, fill=WHITE, anchor="rm")
        text_w = d.textlength("Repo Hunter", font=f_brand)
    lx, ly, lr = right - text_w - 50, by + ch / 2, 24
    _reticle(d, lx, ly, lr, accent, 5)
    d.ellipse([lx - 7, ly - 7, lx + 7, ly + 7], fill=WHITE)   # small sizes: a dot reads better than the branch

    img.save(path, "PNG", optimize=True)
    return path
