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
HALF = W // 2
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
CARD_BG = (10, 16, 30)
MEDIA_PAD = "0x0a101e"
ACCENT = (200, 30, 45)
WHITE = (255, 255, 255)
GREY = (190, 200, 215)

TEXT_RIGHT = W - 80
TEXT_LEFT = HALF + 80
TEXT_W = TEXT_RIGHT - TEXT_LEFT

# media box (16:9) on the right, under the date line
BOX_W, BOX_H = 800, 450
BOX_X = TEXT_RIGHT - BOX_W
BOX_Y = 265
HEAD_Y = BOX_Y + BOX_H + 30          # headline area starts here

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


def fit_lines(draw, text, max_w, max_lines, start_size, min_size=34):
    size = start_size
    while size >= min_size:
        f = font(size)
        lines = wrap(draw, text, f, max_w)
        if len(lines) <= max_lines:
            return f, lines
        size -= 4
    f = font(min_size)
    return f, wrap(draw, text, f, max_w)[:max_lines]


def draw_block(draw, lines, f, center_y, fill, spacing=1.3):
    line_h = int(f.size * spacing)
    y = center_y - (line_h * len(lines)) // 2
    for line in lines:
        draw_rtl(draw, line, f, TEXT_RIGHT, y, fill)
        y += line_h


def run_ok(cmd):
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        print(f"  [!] ffmpeg failed: {r.stderr[-400:]}")
    return r.returncode == 0


# ---------------- static graphics ----------------

def make_background(path, date_text, has_presenter):
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        c = tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3))
        d.line([(0, y), (W, y)], fill=c)

    f_badge = font(40)
    badge_w = int(text_w(d, CHANNEL_NAME, f_badge)) + 60
    d.rectangle([TEXT_RIGHT - badge_w, 50, TEXT_RIGHT, 120], fill=ACCENT)
    draw_rtl(d, CHANNEL_NAME, f_badge, TEXT_RIGHT - 30, 60, WHITE)
    draw_rtl(d, date_text, font(32, bold=False), TEXT_RIGHT, 140, GREY)
    d.line([(TEXT_LEFT, 205), (TEXT_RIGHT, 205)], fill=ACCENT, width=4)

    # frame around the media box
    d.rectangle([BOX_X - 3, BOX_Y - 3, BOX_X + BOX_W + 2, BOX_Y + BOX_H + 2], outline=(60, 80, 120), width=3)

    draw_rtl(d, AI_NOTE, font(24, bold=False), TEXT_RIGHT, H - 55, GREY)

    if not has_presenter:
        d.rectangle([0, 0, HALF, H], fill=CARD_BG)
        f_big = font(90)
        w = text_w(d, CHANNEL_NAME, f_big)
        d.text(((HALF - w) / 2, H / 2 - 60), rtl(CHANNEL_NAME), font=f_big, fill=WHITE)
        d.rectangle([HALF / 2 - 120, H / 2 + 70, HALF / 2 + 120, H / 2 + 78], fill=ACCENT)
    else:
        d.rectangle([HALF - 4, 0, HALF, H], fill=ACCENT)
    img.save(path)


def make_card(path):
    img = Image.new("RGB", (BOX_W, BOX_H), CARD_BG)
    d = ImageDraw.Draw(img)
    for y in range(BOX_H):
        t = y / BOX_H
        c = tuple(int(CARD_BG[i] + (BG_BOTTOM[i] - CARD_BG[i]) * t) for i in range(3))
        d.line([(0, y), (BOX_W, y)], fill=c)
    f = font(64)
    w = text_w(d, CHANNEL_NAME, f)
    d.text(((BOX_W - w) / 2, BOX_H / 2 - 55), rtl(CHANNEL_NAME), font=f, fill=WHITE)
    d.rectangle([BOX_W / 2 - 90, BOX_H / 2 + 40, BOX_W / 2 + 90, BOX_H / 2 + 46], fill=ACCENT)
    img.save(path)


def make_overlay(path, kind, title="", index=0, total=0):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if kind == "intro":
        f, lines = fit_lines(d, title, TEXT_W, 3, 54)
        draw_block(d, lines, f, HEAD_Y + 125, WHITE)
    elif kind == "segment":
        counter = f"סיפור {index} מתוך {total}"
        f_c = font(30, bold=False)
        cw = int(text_w(d, counter, f_c)) + 36
        d.rectangle([TEXT_RIGHT - cw, HEAD_Y, TEXT_RIGHT, HEAD_Y + 44], fill=ACCENT)
        draw_rtl(d, counter, f_c, TEXT_RIGHT - 18, HEAD_Y + 4, WHITE)
        f, lines = fit_lines(d, title, TEXT_W, 3, 52)
        draw_block(d, lines, f, HEAD_Y + 160, WHITE)
    elif kind == "outro":
        draw_block(d, ["תודה שצפיתם"], font(64), HEAD_Y + 70, WHITE)
        draw_block(d, ["הירשמו לערוץ לעדכונים יומיים"], font(40, bold=False), HEAD_Y + 160, GREY)
    img.save(path)


# ---------------- media track ----------------

VF_BOX = (f"scale={BOX_W}:{BOX_H}:force_original_aspect_ratio=decrease,"
          f"pad={BOX_W}:{BOX_H}:(ow-iw)/2:(oh-ih)/2:color={MEDIA_PAD},"
          f"setsar=1,fps={FPS},format=yuv420p")


def enc_args():
    return ["-an", "-c:v", "libx264", "-preset", PRESET, "-crf", "20", "-r", str(FPS)]


def still_clip(img, frames, out):
    return run_ok(["ffmpeg", "-y", "-loop", "1", "-framerate", str(FPS), "-i", str(img),
                   "-vf", VF_BOX, "-frames:v", str(frames), *enc_args(), str(out)])


def video_clip(src, frames, out):
    return run_ok(["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(src),
                   "-vf", VF_BOX, "-frames:v", str(frames), *enc_args(), str(out)])


def build_track(day_dir, parts, total, media):
    track_dir = day_dir / "track"
    track_dir.mkdir(exist_ok=True)
    card = track_dir / "card.png"
    make_card(card)

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
                src = day_dir / it["file"]
                ok = video_clip(src, n, out) if it["type"] == "video" else still_clip(src, n, out)
                if ok:
                    used += 1
                else:
                    ok = still_clip(card, n, out)
                if not ok:
                    sys.exit("Could not build media track")
                clips.append(out)
        else:
            out = track_dir / f"{p['id']}_card.mp4"
            if not still_clip(card, frames, out):
                sys.exit("Could not build media track")
            clips.append(out)

    list_file = track_dir / "concat.txt"
    list_file.write_text("".join(f"file '{c.name}'\n" for c in clips), encoding="utf-8")
    track = track_dir / "track.mp4"
    if not run_ok(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
                   "-c", "copy", str(track)]):
        sys.exit("Could not join media track")
    print(f"Media track: {used} media items, {len(clips)} clips")
    return track, card


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
    has_presenter = PRESENTER.exists()

    frames_dir = day_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    print(f"Font: {FONT_BOLD} ({'variable' if FONT_VARIABLE else 'static'})")
    date_text = f"יום {script.get('weekday', '')}, {script.get('date', '')}".strip(", ")
    bg = frames_dir / "background.png"
    make_background(bg, date_text, has_presenter)

    seg_parts = [p for p in parts if p["id"].startswith("seg")]
    overlays = []
    for i, p in enumerate(parts):
        png = frames_dir / f"{p['id']}.png"
        if p["id"] == "intro":
            make_overlay(png, "intro", title=script.get("title", ""))
        elif p["id"] == "outro":
            make_overlay(png, "outro")
        else:
            make_overlay(png, "segment", title=p.get("headline", ""),
                         index=seg_parts.index(p) + 1, total=len(seg_parts))
        start = p["start"]
        end = parts[i + 1]["start"] if i + 1 < len(parts) else total
        overlays.append((png, start, end))

    track, card = build_track(day_dir, parts, total, media)

    # thumbnail: background + first story photo (or card) + intro text
    thumb = Image.open(bg).convert("RGBA")
    box_img = Image.open(card).convert("RGB")
    for seg in sorted(media):
        photos = [it for it in media[seg] if it["type"] == "photo" and (day_dir / it["file"]).exists()]
        if photos:
            try:
                box_img = Image.open(day_dir / photos[0]["file"]).convert("RGB")
            except Exception:
                pass
            break
    box_img = ImageOps.pad(box_img, (BOX_W, BOX_H), color=CARD_BG)
    thumb.paste(box_img, (BOX_X, BOX_Y))
    thumb.alpha_composite(Image.open(frames_dir / "intro.png"))
    thumb.convert("RGB").save(day_dir / "thumbnail.png")

    cmd = ["ffmpeg", "-y", "-loop", "1", "-framerate", str(FPS), "-i", str(bg)]
    n = 1
    if has_presenter:
        cmd += ["-stream_loop", "-1", "-i", str(PRESENTER)]
        pres_idx = n
        n += 1
    cmd += ["-i", str(track)]
    track_idx = n
    n += 1
    first_overlay = n
    for png, _, _ in overlays:
        cmd += ["-loop", "1", "-framerate", str(FPS), "-i", str(png)]
        n += 1
    cmd += ["-i", str(narration)]
    audio_idx = n

    filters, cur = [], "[0:v]"
    if has_presenter:
        filters.append(f"[{pres_idx}:v]scale={HALF}:{H}:force_original_aspect_ratio=increase,"
                       f"crop={HALF}:{H},setsar=1,fps={FPS}[pres]")
        filters.append(f"{cur}[pres]overlay=0:0[base]")
        cur = "[base]"
    filters.append(f"{cur}[{track_idx}:v]overlay={BOX_X}:{BOX_Y}:eof_action=repeat[trk]")
    cur = "[trk]"
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

    print(f"Presenter clip: {'yes' if has_presenter else 'no (placeholder)'}")
    print(f"Rendering {total / 60:.1f} min video with {len(overlays)} overlays...")
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        sys.exit(f"ffmpeg failed:\n{r.stderr[-2000:]}")

    size_mb = out_file.stat().st_size / 1e6
    print(f"Saved -> {out_file} ({size_mb:.0f} MB)")
    print(f"Saved -> {day_dir / 'thumbnail.png'}")


if __name__ == "__main__":
    main()
