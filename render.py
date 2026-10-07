import json
import os
import re
import subprocess
import sys
from glob import glob
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

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
# Channel background image for parts without media ("" = plain gradient card)
BG_IMAGE = Path(os.getenv("BG_IMAGE", "")) if os.getenv("BG_IMAGE", "").strip() else None
# Stories without media show their text on screen instead of the plain card. Off by default.
TEXT_CARDS = os.getenv("TEXT_CARDS", "0") == "1"
TEXT_SIZE = int(os.getenv("TEXT_CARD_SIZE", "52"))
TEXT_LINES = int(os.getenv("TEXT_CARD_LINES", "5"))
# Click-focused thumbnail: top-story photo, 2-4 huge words, presenter, date badge. Off by default.
THUMB_V2 = os.getenv("THUMB_V2", "0") == "1"
THUMB_TAGLINE = os.getenv("THUMB_TAGLINE", "")            # optional strip at the bottom
THUMB_PRESENTER = Path(os.getenv("THUMB_PRESENTER", "presenter.jpg"))
YELLOW = (255, 214, 0)

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

def load_bg():
    if BG_IMAGE is None:
        return None
    if not BG_IMAGE.exists():
        print(f"[!] background image {BG_IMAGE} not found, using the plain card")
        return None
    try:
        return ImageOps.fit(Image.open(BG_IMAGE).convert("RGB"), (W, H))
    except Exception as e:
        print(f"[!] background image unreadable ({e}), using the plain card")
        return None


def make_card(path):
    """Full-frame branded card for parts without media."""
    bg = load_bg()
    if bg is not None:
        ImageEnhance.Brightness(bg).enhance(0.85).save(path)
        return
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


def make_text_base(path):
    """Blurred, darkened background for the text cards."""
    bg = load_bg()
    if bg is None:
        bg = Image.new("RGB", (W, H))
        d = ImageDraw.Draw(bg)
        for y in range(H):
            t = y / H
            d.line([(0, y), (W, y)],
                   fill=tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)))
    else:
        bg = ImageEnhance.Brightness(bg.filter(ImageFilter.GaussianBlur(18))).enhance(0.45)
    bg.save(path)


# text panel between the top badge and the lower third
PANEL_TOP, PANEL_BOTTOM = 215, H - 320
PANEL_PAD = 45


def text_pages(draw, text, max_w):
    """Split a story into pages of up to TEXT_LINES lines, keeping whole sentences when possible."""
    f = font(TEXT_SIZE)
    pieces = []
    for sent in re.split(r"(?<=[.!?])\s+", text.strip()):
        if not sent:
            continue
        if len(wrap(draw, sent, f, max_w)) <= TEXT_LINES:
            pieces.append(sent)
            continue
        cur = ""                                   # very long sentence: split by words
        for word in sent.split():
            test = f"{cur} {word}".strip()
            if len(wrap(draw, test, f, max_w)) <= TEXT_LINES or not cur:
                cur = test
            else:
                pieces.append(cur)
                cur = word
        if cur:
            pieces.append(cur)
    pages, cur = [], ""
    for piece in pieces:
        test = f"{cur} {piece}".strip()
        if len(wrap(draw, test, f, max_w)) <= TEXT_LINES or not cur:
            cur = test
        else:
            pages.append(cur)
            cur = piece
    if cur:
        pages.append(cur)
    return pages


def make_text_card(path, base, text, panel_left):
    img = Image.open(base).convert("RGBA")
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle([panel_left, PANEL_TOP, TEXT_RIGHT, PANEL_BOTTOM], radius=24,
                        fill=(8, 14, 30, 185))
    d.rectangle([TEXT_RIGHT - 10, PANEL_TOP + 24, TEXT_RIGHT, PANEL_BOTTOM - 24], fill=ACCENT)
    f = font(TEXT_SIZE)
    max_w = TEXT_RIGHT - panel_left - 2 * PANEL_PAD - 10
    lines = wrap(d, text, f, max_w)[:TEXT_LINES]
    step = int(f.size * 1.4)
    y = PANEL_TOP + (PANEL_BOTTOM - PANEL_TOP - step * len(lines)) // 2
    for line in lines:
        draw_rtl(d, line, f, TEXT_RIGHT - PANEL_PAD - 10, y, WHITE)
        y += step
    img.alpha_composite(layer)
    img.convert("RGB").save(path)


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

# ---------------- thumbnail v2 ----------------

def thumb_base(day_dir, media):
    """Best picture for the thumbnail: photo of the first story > any photo > video frame > background."""
    order = sorted(media)
    for want in ("photo", "video"):
        for seg in order:
            for it in media[seg]:
                src = day_dir / it["file"]
                if it["type"] != want or not src.exists():
                    continue
                try:
                    if want == "photo":
                        return ImageOps.fit(Image.open(src).convert("RGB"), (W, H))
                    frame = day_dir / "frames" / "thumb_frame.jpg"
                    if run_ok(["ffmpeg", "-y", "-ss", "1", "-i", str(src), "-frames:v", "1", str(frame)]):
                        return ImageOps.fit(Image.open(frame).convert("RGB"), (W, H))
                except Exception:
                    continue
    bg = load_bg()                                    # no media: soften the channel art behind the text
    if bg is None:
        return Image.open(day_dir / "frames" / "card.png").convert("RGB")
    return ImageEnhance.Brightness(bg.filter(ImageFilter.GaussianBlur(14))).enhance(0.6)


def big_lines(draw, text, max_w, max_h):
    """Largest font size where the text fits in at most 2 lines."""
    for size in range(230, 90, -10):
        f = font(size)
        lines = wrap(draw, text, f, max_w)
        if len(lines) <= 2 and len(lines) * size * 1.12 <= max_h:
            return f, lines
    f = font(90)
    return f, wrap(draw, text, f, max_w)[:2]


def make_thumbnail_v2(out, base, text, date_text):
    img = ImageEnhance.Contrast(ImageEnhance.Color(base).enhance(1.25)).enhance(1.1).convert("RGBA")

    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(shade)
    for x in range(W):                                  # dark on the right, where the text sits
        a = int(215 * max(0.0, (x - W * 0.25) / (W * 0.75)) ** 0.8)
        d.line([(x, 0), (x, H)], fill=(5, 10, 25, a))
    for y in range(H - 260, H):
        d.line([(0, y), (W, y)], fill=(0, 0, 0, int(160 * (y - (H - 260)) / 260)))
    img.alpha_composite(shade)

    d = ImageDraw.Draw(img)
    has_face = THUMB_PRESENTER.exists()
    text_left = 640 if has_face else 120
    max_w = TEXT_RIGHT - text_left

    # channel badge + date badge (top right)
    f_b = font(64)
    bw = int(text_w(d, CHANNEL_NAME, f_b)) + 70
    d.rectangle([TEXT_RIGHT - bw, 50, TEXT_RIGHT, 150], fill=ACCENT)
    draw_rtl(d, CHANNEL_NAME, f_b, TEXT_RIGHT - 35, 60, WHITE)
    if date_text:
        f_d = font(52)
        dw = int(d.textlength(date_text, font=f_d)) + 50
        d.rectangle([TEXT_RIGHT - dw, 165, TEXT_RIGHT, 245], fill=YELLOW)
        d.text((TEXT_RIGHT - dw + 25, 172), date_text, font=f_d, fill=(10, 10, 10))

    # the big words, first line white, second line yellow, thick outline
    f, lines = big_lines(d, text, max_w, H - 600)
    total_h = int(len(lines) * f.size * 1.12)
    y = 300 + (H - 300 - 230 - total_h) // 2
    for k, line in enumerate(lines):
        disp = rtl(line)
        w = d.textlength(disp, font=f)
        d.text((TEXT_RIGHT - w, y), disp, font=f, fill=WHITE if k == 0 else YELLOW,
               stroke_width=max(6, f.size // 22), stroke_fill=(0, 0, 0))
        y += int(f.size * 1.12)

    # presenter, bottom left, framed
    if has_face:
        try:
            pw, ph = 470, 590
            face = ImageOps.fit(Image.open(THUMB_PRESENTER).convert("RGB"), (pw, ph), centering=(0.5, 0.25))
            px, py = 60, H - ph - 40
            d.rectangle([px - 8, py - 8, px + pw + 7, py + ph + 7], fill=ACCENT)
            img.paste(face, (px, py))
        except Exception as e:
            print(f"  [!] presenter not added to thumbnail ({e})")

    if THUMB_TAGLINE.strip():
        f_t = font(54)
        tw = int(text_w(d, THUMB_TAGLINE, f_t)) + 60
        d.rectangle([TEXT_RIGHT - tw, H - 130, TEXT_RIGHT, H - 50], fill=(0, 0, 0))
        draw_rtl(d, THUMB_TAGLINE, f_t, TEXT_RIGHT - 30, H - 122, WHITE)

    img.convert("RGB").save(out)


# ---------------- media track (full frame) ----------------

FILL_GRAPH = (f"[0:v]split=2[a][b];"
              f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
              f"boxblur=30:3,eq=brightness=-0.15[bg];"
              f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
              f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={FPS},format=yuv420p[v]")


def enc_args():
    return ["-an", "-c:v", "libx264", "-preset", PRESET, "-crf", "21", "-r", str(FPS)]


def media_clip(src, kind, frames, out):
    inp = ["-loop", "1", "-framerate", str(FPS), "-i", str(src)] if kind == "photo" \
        else ["-stream_loop", "-1", "-i", str(src)]
    return run_ok(["ffmpeg", "-y", *inp, "-filter_complex", FILL_GRAPH, "-map", "[v]",
                   "-frames:v", str(frames), *enc_args(), str(out)])


def card_clip(card, frames, out):
    return run_ok(["ffmpeg", "-y", "-loop", "1", "-framerate", str(FPS), "-i", str(card),
                   "-vf", f"setsar=1,fps={FPS},format=yuv420p",
                   "-frames:v", str(frames), *enc_args(), str(out)])


def text_clips(day_dir, p, frames, text_card, card):
    """One clip per text page; page time follows its share of the story's text."""
    base, panel_left = text_card
    frames_dir = day_dir / "frames"
    track_dir = day_dir / "track"
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    max_w = TEXT_RIGHT - panel_left - 2 * PANEL_PAD - 10
    pages = text_pages(probe, p["text"], max_w) or [p["text"]]
    weights = [max(1, len(t)) for t in pages]
    total_w = sum(weights)
    clips, given = [], 0
    for k, (page, wgt) in enumerate(zip(pages, weights)):
        n = frames - given if k == len(pages) - 1 else round(frames * wgt / total_w)
        if n <= 0:
            continue
        given += n
        png = frames_dir / f"{p['id']}_text{k}.png"
        out = track_dir / f"{p['id']}_text{k}.mp4"
        make_text_card(png, base, page, panel_left)
        if not card_clip(png, n, out) and not card_clip(card, n, out):
            sys.exit("Could not build media track")
        clips.append(out)
    print(f"  {p['id']}: no media -> {len(clips)} text page(s)")
    return clips


def build_track(day_dir, parts, total, media, card, text_card=None):
    track_dir = day_dir / "track"
    track_dir.mkdir(exist_ok=True)
    clips, used = [], 0
    for i, p in enumerate(parts):
        start = p["start"]
        end = parts[i + 1]["start"] if i + 1 < len(parts) else total
        frames = max(1, round(end * FPS) - round(start * FPS))
        items = [it for it in media.get(p["id"], []) if (day_dir / it["file"]).exists()]
        if items:
            base, extra = divmod(frames, len(items))
            for k, it in enumerate(items):
                n = base + (1 if k < extra else 0)
                if n <= 0:
                    continue
                out = track_dir / f"{p['id']}_{k}.mp4"
                ok = media_clip(day_dir / it["file"], it["type"], n, out)
                if ok:
                    used += 1
                else:
                    ok = card_clip(card, n, out)
                if not ok:
                    sys.exit("Could not build media track")
                clips.append(out)
        elif text_card and p["id"].startswith("seg") and p.get("text", "").strip():
            clips += text_clips(day_dir, p, frames, text_card, card)
        else:
            out = track_dir / f"{p['id']}_card.mp4"
            if not card_clip(card, frames, out):
                sys.exit("Could not build media track")
            clips.append(out)

    list_file = track_dir / "concat.txt"
    list_file.write_text("".join(f"file '{c.name}'\n" for c in clips), encoding="utf-8")
    track = track_dir / "track.mp4"
    if not run_ok(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
                   "-c", "copy", str(track)]):
        sys.exit("Could not join media track")
    print(f"Media track: {used} media items, {len(clips)} clips")
    return track


def latest_day_dir():
    days = sorted(p for p in Path("daily").glob("*") if (p / "audio.json").exists())
    if not days:
        sys.exit("No daily/*/audio.json found - run tts.py first")
    return days[-1]


def main():
    day_dir = latest_day_dir()
    script = json.loads((day_dir / "script.json").read_text(encoding="utf-8"))
    audio = json.loads((day_dir / "audio.json").read_text(encoding="utf-8"))
    media_file = day_dir / "media.json"
    media = json.loads(media_file.read_text(encoding="utf-8")) if media_file.exists() else {}
    parts = audio["parts"]
    total = audio["total_duration"]
    narration = day_dir / audio["narration"]

    # presenter source: lip-synced avatar for today > looping presenter clip > none
    avatar = day_dir / "avatar.mp4"
    if avatar.exists():
        pip_src, pip_loop = avatar, False
    elif PRESENTER.exists():
        pip_src, pip_loop = PRESENTER, True
    else:
        pip_src, pip_loop = None, False
    has_pip = pip_src is not None
    text_left = (PIP_X + PIP_W + 60) if has_pip else MARGIN

    frames_dir = day_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    print(f"Font: {FONT_BOLD} ({'variable' if FONT_VARIABLE else 'static'})")
    date_text = f"יום {script.get('weekday', '')}, {script.get('date', '')}".strip(", ")

    card = frames_dir / "card.png"
    make_card(card)
    chrome = frames_dir / "chrome.png"
    make_chrome(chrome, date_text, has_pip, text_left)

    seg_parts = [p for p in parts if p["id"].startswith("seg")]
    overlays = []
    for i, p in enumerate(parts):
        png = frames_dir / f"{p['id']}.png"
        if p["id"] == "intro":
            make_overlay(png, "intro", text_left, title=script.get("title", ""))
        elif p["id"] == "outro":
            make_overlay(png, "outro", text_left)
        else:
            make_overlay(png, "segment", text_left, title=p.get("headline", ""),
                         index=seg_parts.index(p) + 1, total=len(seg_parts))
        start = p["start"]
        end = parts[i + 1]["start"] if i + 1 < len(parts) else total
        overlays.append((png, start, end))

    text_card = None
    if TEXT_CARDS:
        text_base = frames_dir / "text_base.png"
        make_text_base(text_base)
        text_card = (text_base, text_left)
    print(f"Background: {BG_IMAGE if load_bg() is not None else 'plain card'}, "
          f"text cards {'on' if TEXT_CARDS else 'off'}")

    track = build_track(day_dir, parts, total, media, card, text_card)

    if THUMB_V2:
        words = (script.get("thumbnail_text") or "").strip()
        if not words:
            first = script.get("segments", [{}])[0].get("headline", "") if script.get("segments") else ""
            words = " ".join(first.split()[:4]) or script.get("title", "")
        try:
            make_thumbnail_v2(day_dir / "thumbnail.png", thumb_base(day_dir, media), words,
                              script.get("date", ""))
            print(f"Thumbnail v2: '{words}'")
        except Exception as e:
            print(f"[!] thumbnail v2 failed ({e}), using the classic one")
            (day_dir / "thumbnail.png").unlink(missing_ok=True)
    # thumbnail: first story photo full-frame (or card) + chrome + intro text
    base = Image.open(card).convert("RGB")
    for seg in sorted(media):
        photos = [it for it in media[seg] if it["type"] == "photo" and (day_dir / it["file"]).exists()]
        if photos:
            try:
                base = ImageOps.fit(Image.open(day_dir / photos[0]["file"]).convert("RGB"), (W, H))
            except Exception:
                pass
            break
    thumb = base.convert("RGBA")
    thumb_chrome = frames_dir / "chrome_thumb.png"
    make_chrome(thumb_chrome, date_text, False, MARGIN)
    thumb.alpha_composite(Image.open(thumb_chrome))
    thumb_title = frames_dir / "intro_thumb.png"
    make_overlay(thumb_title, "intro", MARGIN, title=script.get("title", ""))
    thumb.alpha_composite(Image.open(thumb_title))
    if not THUMB_V2 or not (day_dir / "thumbnail.png").exists():
        thumb.convert("RGB").save(day_dir / "thumbnail.png")

    cmd = ["ffmpeg", "-y", "-i", str(track),
           "-loop", "1", "-framerate", str(FPS), "-i", str(chrome)]
    n = 2
    if has_pip:
        cmd += (["-stream_loop", "-1"] if pip_loop else []) + ["-i", str(pip_src)]
        pip_idx = n
        n += 1
    first_overlay = n
    for png, _, _ in overlays:
        cmd += ["-loop", "1", "-framerate", str(FPS), "-i", str(png)]
        n += 1
    cmd += ["-i", str(narration)]
    audio_idx = n

    filters = [f"[0:v][1:v]overlay=0:0[c0]"]
    cur = "[c0]"
    if has_pip:
        filters.append(f"[{pip_idx}:v]scale={PIP_W}:{PIP_H}:force_original_aspect_ratio=increase,"
                       f"crop={PIP_W}:{PIP_H},setsar=1,fps={FPS}[pip]")
        filters.append(f"{cur}[pip]overlay={PIP_X}:{PIP_Y}:eof_action=repeat[c1]")
        cur = "[c1]"
    for k, (_, start, end) in enumerate(overlays):
        out = f"[o{k}]"
        filters.append(f"{cur}[{first_overlay + k}:v]overlay=0:0:"
                       f"enable='between(t,{start:.3f},{end:.3f})'{out}")
        cur = out

    out_file = day_dir / "video.mp4"
    cmd += ["-filter_complex", ";".join(filters), "-map", cur, "-map", f"{audio_idx}:a",
            "-t", f"{total:.3f}", "-r", str(FPS),
            "-c:v", "libx264", "-preset", PRESET, "-crf", CRF, "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out_file)]

    print(f"Presenter: {pip_src if has_pip else 'none (box hidden)'}")
    print(f"Rendering {total / 60:.1f} min video with {len(overlays)} overlays...")
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        sys.exit(f"ffmpeg failed:\n{r.stderr[-2000:]}")

    size_mb = out_file.stat().st_size / 1e6
    print(f"Saved -> {out_file} ({size_mb:.0f} MB)")
    print(f"Saved -> {day_dir / 'thumbnail.png'}")


if __name__ == "__main__":
    main()
