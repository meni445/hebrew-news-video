import json
import os
import subprocess
import sys
from glob import glob
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

try:
    from bidi.algorithm import get_display
except ImportError:
    from bidi import get_display

W, H = 1920, 1080
FPS = int(os.getenv("VIDEO_FPS", "30"))
CRF = os.getenv("VIDEO_CRF", "23")
PRESET = os.getenv("VIDEO_PRESET", "veryfast")
PRESENTER = Path(os.getenv("PRESENTER_VIDEO", "assets/presenter.mp4"))
CHANNEL_NAME = os.getenv("CHANNEL_NAME", "חדשות היום")
AI_NOTE = os.getenv("AI_NOTE", "הסרטון נוצר בסיוע בינה מלאכותית")
FONT_URL = os.getenv(
    "FONT_URL",
    "https://raw.githubusercontent.com/google/fonts/main/ofl/heebo/Heebo%5Bwght%5D.ttf")
FONT_PATH = Path(os.getenv("FONT_PATH", "models/fonts/Heebo.ttf"))

BG_TOP = (14, 22, 40)
BG_BOTTOM = (28, 44, 78)
ACCENT = (200, 30, 45)
WHITE = (255, 255, 255)
GREY = (200, 208, 220)

MARGIN = 60
TEXT_RIGHT = W - MARGIN

# presenter box (picture-in-picture), bottom-left
PIP_W, PIP_H = 400, 500
PIP_X, PIP_Y = MARGIN, H - PIP_H - 50

LAYOUT_BASIC = getattr(getattr(ImageFont, "Layout", None), "BASIC", None)
if LAYOUT_BASIC is None:
    LAYOUT_BASIC = getattr(ImageFont, "LAYOUT_BASIC", 0)


def resolve_fonts():
    if not FONT_PATH.exists():
        try:
            FONT_PATH.parent.mkdir(parents=True, exist_ok=True)
            print(f"Downloading font {FONT_URL} ...")
            r = requests.get(FONT_URL, timeout=120)
            r.raise_for_status()
            FONT_PATH.write_bytes(r.content)
        except Exception as e:
            print(f"[!] font download failed ({e}), falling back to DejaVu")
    if FONT_PATH.exists():
        return str(FONT_PATH), str(FONT_PATH), True
    bold = glob("/usr/share/fonts/**/DejaVuSans-Bold.ttf", recursive=True)
    reg = glob("/usr/share/fonts/**/DejaVuSans.ttf", recursive=True)
    if bold and reg:
        return bold[0], reg[0], False
    sys.exit("No usable Hebrew font found - install fonts-dejavu-core")


FONT_BOLD, FONT_REG, FONT_VARIABLE = resolve_fonts()
_font_cache = {}


def font(size, bold=True):
    key = (size, bold)
    if key not in _font_cache:
        f = ImageFont.truetype(FONT_BOLD if bold else FONT_REG, size, layout_engine=LAYOUT_BASIC)
        if FONT_VARIABLE:
            try:
                f.set_variation_by_axes([700 if bold else 400])
            except Exception:
                pass
        _font_cache[key] = f
    return _font_cache[key]


def rtl(text):
    return get_display(text, base_dir="R")


def text_w(draw, text, f):
    return draw.textlength(rtl(text), font=f)


def draw_rtl(draw, text, f, right_x, y, fill):
    disp = rtl(text)
    w = draw.textlength(disp, font=f)
    draw.text((right_x - w, y), disp, font=f, fill=fill)


def wrap(draw, text, f, max_w):
    lines, cur = [], ""
    for word in text.split():
        test = f"{cur} {word}".strip()
        if text_w(draw, test, f) <= max_w or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def fit_lines(draw, text, max_w, max_lines, start_size, min_size=36):
    size = start_size
    while size >= min_size:
        f = font(size)
        lines = wrap(draw, text, f, max_w)
        if len(lines) <= max_lines:
            return f, lines
        size -= 4
    f = font(min_size)
    return f, wrap(draw, text, f, max_w)[:max_lines]


def draw_lines(draw, lines, f, top_y, fill, spacing=1.25):
    y = top_y
    for line in lines:
        draw_rtl(draw, line, f, TEXT_RIGHT, y, fill)
        y += int(f.size * spacing)


def run_ok(cmd):
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        print(f"  [!] ffmpeg failed: {r.stderr[-400:]}")
    return r.returncode == 0


# ---------------- graphics ----------------

def make_card(path):
    """Full-frame branded card for parts without media."""
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        c = tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3))
        d.line([(0, y), (W, y)], fill=c)
    f = font(120)
    w = text_w(d, CHANNEL_NAME, f)
    d.text(((W - w) / 2, 330), rtl(CHANNEL_NAME), font=f, fill=WHITE)
    d.rectangle([W / 2 - 160, 500, W / 2 + 160, 510], fill=ACCENT)
    img.save(path)


def make_chrome(path, date_text, has_pip, text_left):
    """Static layer on top of the media: gradients, badge, date, AI note, PiP frame."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for y in range(0, 220):                      # top shade
        a = int(150 * (1 - y / 220))
        d.line([(0, y), (W, y)], fill=(0, 0, 0, a))
    for y in range(H - 420, H):                  # bottom shade for the headline
        a = int(215 * (y - (H - 420)) / 420)
        d.line([(0, y), (W, y)], fill=(0, 0, 0, a))

    f_badge = font(40)
    badge_w = int(text_w(d, CHANNEL_NAME, f_badge)) + 60
    d.rectangle([TEXT_RIGHT - badge_w, 40, TEXT_RIGHT, 110], fill=ACCENT)
    draw_rtl(d, CHANNEL_NAME, f_badge, TEXT_RIGHT - 30, 50, WHITE)
    draw_rtl(d, date_text, font(32, bold=False), TEXT_RIGHT, 125, WHITE)

    f_note = font(22, bold=False)
    note = rtl(AI_NOTE)
    d.text((MARGIN, 50), note, font=f_note, fill=GREY)

    if has_pip:
        d.rectangle([PIP_X - 5, PIP_Y - 5, PIP_X + PIP_W + 4, PIP_Y + PIP_H + 4], fill=ACCENT)
    img.save(path)


def make_overlay(path, kind, text_left, title="", index=0, total=0):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    max_w = TEXT_RIGHT - text_left
    tag_y = H - 290
    if kind in ("intro", "segment"):
        tag = "מהדורה יומית" if kind == "intro" else f"סיפור {index} מתוך {total}"
        f_t = font(32)
        tw = int(text_w(d, tag, f_t)) + 40
        d.rectangle([TEXT_RIGHT - tw, tag_y, TEXT_RIGHT, tag_y + 50], fill=ACCENT)
        draw_rtl(d, tag, f_t, TEXT_RIGHT - 20, tag_y + 4, WHITE)
        f, lines = fit_lines(d, title, max_w, 2, 66)
        draw_lines(d, lines, f, tag_y + 75, WHITE)
    elif kind == "outro":
        draw_lines(d, ["תודה שצפיתם"], font(72), H - 260, WHITE)
        draw_lines(d, ["הירשמו לערוץ לעדכונים יומיים"], font(44, bold=False), H - 160, GREY)
    img.save(path)
