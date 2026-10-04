import asyncio
import base64
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests
from PIL import Image
from telethon import TelegramClient
from telethon.sessions import StringSession

ENABLED = os.getenv("MEDIA_ENABLED", "0") == "1"            # off by default
CANDIDATES = int(os.getenv("MEDIA_CANDIDATES", "4"))         # downloaded per story
PER_SEGMENT = int(os.getenv("MEDIA_PER_SEGMENT", "2"))       # kept per story after the check
MAX_MB = float(os.getenv("MEDIA_MAX_MB", "40"))
MAX_VIDEO_SEC = float(os.getenv("MEDIA_MAX_VIDEO_SEC", "120"))
SAFETY = os.getenv("MEDIA_SAFETY", "0") == "1"              # Gemini check: safety + logos + relevance
SAFETY_MODELS = [m.strip() for m in os.getenv("GEMINI_SAFETY_MODEL", "gemini-flash-lite-latest").split(",")
                 if m.strip()]

CHECK_PROMPT = """You check images for a family-safe Hebrew YouTube news video.
Each image is listed with the Hebrew headline of the story it is meant to illustrate.
Some images are a strip of 3 frames from one video - judge all 3 frames.

Give each image exactly one verdict:
- "unsafe": blood, gore, dead or injured bodies, wounded people, graphic violence,
  executions, torture, hostages in distress, nudity, close-ups of victims.
- "reject": channel logos or channel branding graphics, greeting cards
  (e.g. שבוע טוב, בוקר טוב, שבת שלום), ads or promotions, screenshots of text or
  social media posts, images that are mostly text, memes, or images clearly
  unrelated to the headline.
- "ok": real news photos or footage that fit the story: soldiers, weapons,
  vehicles, aircraft, explosions or smoke seen from a distance, damaged buildings
  without victims, politicians, press conferences, maps, crowds, protests without injuries.

When in doubt between "ok" and "unsafe", choose "unsafe".
Return ONLY JSON: {"verdicts": {"1": "ok", "2": "reject", "3": "unsafe"}}"""


def env(name):
    v = os.getenv(name)
    if not v:
        sys.exit(f"Missing env var: {name}")
    return v


def latest_day_dir():
    days = sorted(p for p in Path("daily").glob("*") if (p / "script.json").exists())
    if not days:
        sys.exit("No daily/*/script.json found - run script.py first")
    return days[-1]


async def candidate_messages(client, channel, msg_id):
    m = await client.get_messages(channel, ids=msg_id)
    if not m:
        return []
    if m.grouped_id:  # album: media may sit in neighbouring messages
        around = await client.get_messages(channel, ids=list(range(msg_id - 9, msg_id + 10)))
        return [x for x in around if x and x.grouped_id == m.grouped_id]
    return [m]


async def download_all(day_dir, script):
    media_dir = day_dir / "media"
    media_dir.mkdir(exist_ok=True)
    result = {}
    async with TelegramClient(StringSession(env("TG_SESSION")),
                              int(env("TG_API_ID")), env("TG_API_HASH")) as client:
        for i, seg in enumerate(script["segments"], 1):
            seg_id = f"seg{i:02d}"
            items, seen = [], set()
            for sid in seg.get("source_ids", []):
                if len(items) >= CANDIDATES:
                    break
                try:
                    channel, msg_id = sid.split("/")
                    msg_id = int(msg_id)
                except ValueError:
                    continue
                try:
                    msgs = await candidate_messages(client, channel, msg_id)
                except Exception as e:
                    print(f"  [!] {sid}: {e}")
                    continue
                for m in msgs:
                    if len(items) >= CANDIDATES:
                        break
                    if (channel, m.id) in seen:
                        continue
                    seen.add((channel, m.id))
                    kind = "photo" if m.photo else ("video" if m.video else None)
                    if not kind:
                        continue
                    size_mb = (m.file.size or 0) / 1e6 if m.file else 0
                    if size_mb > MAX_MB:
                        print(f"  skip {channel}/{m.id}: {size_mb:.0f} MB")
                        continue
                    if kind == "video" and m.file and (m.file.duration or 0) > MAX_VIDEO_SEC:
                        print(f"  skip {channel}/{m.id}: video too long")
                        continue
                    try:
                        path = await m.download_media(file=str(media_dir / f"{seg_id}_{channel}_{m.id}"))
                    except Exception as e:
                        print(f"  [!] download {channel}/{m.id}: {e}")
                        continue
                    if path:
                        items.append({"file": str(Path(path).relative_to(day_dir)),
                                      "type": kind, "source": f"{channel}/{m.id}"})
            result[seg_id] = items
            print(f"{seg_id}: {len(items)} candidates")
    return result


def video_duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def preview_jpeg(day_dir, item):
    """Photo -> the photo. Video -> strip of 3 frames (10%, 50%, 90%)."""
    src = day_dir / item["file"]
    if item["type"] == "photo":
        img = Image.open(src).convert("RGB")
        img.thumbnail((640, 640))
    else:
        dur = video_duration(src)
        frames = []
        for k, frac in enumerate((0.1, 0.5, 0.9)):
            ts = max(0.0, dur * frac) if dur else k
            out = src.with_name(f"{src.stem}.f{k}.jpg")
            r = subprocess.run(["ffmpeg", "-y", "-ss", f"{ts:.2f}", "-i", str(src),
                                "-frames:v", "1", str(out)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r.returncode == 0 and out.exists():
                f = Image.open(out).convert("RGB")
                f.thumbnail((360, 360))
                frames.append(f)
        if not frames:
            raise RuntimeError("no frames extracted")
        w = sum(f.width for f in frames) + 10 * (len(frames) - 1)
        h = max(f.height for f in frames)
        img = Image.new("RGB", (w, h), (0, 0, 0))
        x = 0
        for f in frames:
            img.paste(f, (x, 0))
            x += f.width + 10
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode()


def check_media(day_dir, script, media):
    headlines = {f"seg{i:02d}": s.get("headline", "") for i, s in enumerate(script["segments"], 1)}
    flat = [(seg, it) for seg, items in media.items() for it in items]
    if not flat:
        return media
    parts = [{"text": CHECK_PROMPT}]
    for n, (seg, it) in enumerate(flat, 1):
        it["_n"] = n
        try:
            data = preview_jpeg(day_dir, it)
        except Exception as e:
            print(f"  [!] preview failed for {it['source']}: {e}")
            data = None
        it["_has_preview"] = data is not None
        if data:
            parts.append({"text": f"Image {n} - headline: {headlines.get(seg, '')}"})
            parts.append({"inlineData": {"mimeType": "image/jpeg", "data": data}})

    headers = {"x-goog-api-key": env("GEMINI_API_KEY"), "Content-Type": "application/json"}
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}
    verdicts = None
    for model in SAFETY_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(3):
            try:
                r = requests.post(url, headers=headers, json=body, timeout=240)
            except requests.RequestException as e:
                print(f"  check network error: {e}")
                time.sleep(15)
                continue
            if r.status_code == 200:
                try:
                    text = "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"])
                    text = text[text.find("{"): text.rfind("}") + 1]
                    verdicts = {str(k): str(v).lower() for k, v in json.loads(text).get("verdicts", {}).items()}
                except Exception as e:
                    print(f"  check parse error: {e}")
                break
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(30 * (attempt + 1))
                continue
            print(f"  check {model} HTTP {r.status_code}")
            break
        if verdicts is not None:
            print(f"Media check by {model}")
            break

    if verdicts is None:
        print("[!] Media check failed - dropping ALL media for today")
        return {seg: [] for seg in media}

    cleaned, stats = {}, {"ok": 0, "reject": 0, "unsafe": 0}
    for seg, items in media.items():
        keep = []
        for it in items:
            v = verdicts.get(str(it["_n"]), "reject") if it["_has_preview"] else "reject"
            if v not in stats:
                v = "reject"
            stats[v] += 1
            if v == "ok" and len(keep) < PER_SEGMENT:
                keep.append({k: val for k, val in it.items() if not k.startswith("_")})
            elif v != "ok":
                print(f"  {v}: {it['source']} ({seg})")
        cleaned[seg] = keep
    print(f"Verdicts: {stats}")
    return cleaned


def main():
    if not ENABLED:
        print("MEDIA_ENABLED is off - skipping")
        return
    day_dir = latest_day_dir()
    script = json.loads((day_dir / "script.json").read_text(encoding="utf-8"))
    media = asyncio.run(download_all(day_dir, script))
    if SAFETY:
        media = check_media(day_dir, script, media)
    else:
        print("[!] MEDIA_SAFETY is off - media is NOT checked")
        media = {seg: items[:PER_SEGMENT] for seg, items in media.items()}
    (day_dir / "media.json").write_text(json.dumps(media, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(len(v) for v in media.values())
    print(f"Saved {total} media items -> {day_dir / 'media.json'}")


if __name__ == "__main__":
    main()
