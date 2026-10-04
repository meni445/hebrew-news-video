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
PER_SEGMENT = int(os.getenv("MEDIA_PER_SEGMENT", "2"))
MAX_MB = float(os.getenv("MEDIA_MAX_MB", "40"))
MAX_VIDEO_SEC = float(os.getenv("MEDIA_MAX_VIDEO_SEC", "120"))
SAFETY = os.getenv("MEDIA_SAFETY", "0") == "1"              # Gemini check for graphic content
SAFETY_MODELS = [m.strip() for m in os.getenv("GEMINI_SAFETY_MODEL", "gemini-flash-lite-latest").split(",")
                 if m.strip()]

SAFETY_PROMPT = """You review images for a family-safe YouTube news channel.
For each numbered image, decide if it is UNSAFE to show.

UNSAFE: blood, gore, dead or injured bodies, wounded people, graphic violence,
executions, torture, hostages in distress, nudity, close-ups of victims.

SAFE: soldiers, weapons, military vehicles, aircraft, explosions or smoke seen
from a distance, damaged buildings without victims, politicians, press
conferences, maps, text graphics, crowds, protests without injuries.

When in doubt, mark it UNSAFE.
Return ONLY JSON: {"unsafe": [list of image numbers]}"""


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
                if len(items) >= PER_SEGMENT:
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
                    if len(items) >= PER_SEGMENT:
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
            print(f"{seg_id}: {len(items)} media")
    return result


def preview_jpeg(day_dir, item):
    src = day_dir / item["file"]
    if item["type"] == "video":
        frame = src.with_suffix(".preview.jpg")
        for ss in ("1", "0"):
            r = subprocess.run(["ffmpeg", "-y", "-ss", ss, "-i", str(src), "-frames:v", "1", str(frame)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r.returncode == 0 and frame.exists():
                break
        src = frame
    img = Image.open(src).convert("RGB")
    img.thumbnail((512, 512))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode()


def safety_filter(day_dir, media):
    flat = [(seg, it) for seg, items in media.items() for it in items]
    if not flat:
        return media
    parts = [{"text": SAFETY_PROMPT}]
    for n, (_, it) in enumerate(flat, 1):
        try:
            data = preview_jpeg(day_dir, it)
        except Exception as e:
            print(f"  [!] preview failed for {it['source']}: {e} - marking unsafe")
            data = None
        it["_n"] = n
        it["_ok_preview"] = data is not None
        if data:
            parts.append({"text": f"Image {n}:"})
            parts.append({"inlineData": {"mimeType": "image/jpeg", "data": data}})

    headers = {"x-goog-api-key": env("GEMINI_API_KEY"), "Content-Type": "application/json"}
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}
    verdict = None
    for model in SAFETY_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(3):
            try:
                r = requests.post(url, headers=headers, json=body, timeout=180)
            except requests.RequestException as e:
                print(f"  safety network error: {e}")
                time.sleep(15)
                continue
            if r.status_code == 200:
                try:
                    text = "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"])
                    text = text[text.find("{"): text.rfind("}") + 1]
                    verdict = set(int(x) for x in json.loads(text).get("unsafe", []))
                except Exception as e:
                    print(f"  safety parse error: {e}")
                break
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(30 * (attempt + 1))
                continue
            print(f"  safety {model} HTTP {r.status_code}")
            break
        if verdict is not None:
            print(f"Safety check by {model}: {len(verdict)} of {len(flat)} marked unsafe")
            break

    if verdict is None:
        print("[!] Safety check failed - dropping ALL media for today")
        return {seg: [] for seg in media}

    cleaned = {}
    for seg, items in media.items():
        keep = []
        for it in items:
            unsafe = (it["_n"] in verdict) or not it["_ok_preview"]
            if unsafe:
                print(f"  removed {it['source']} ({seg})")
            else:
                keep.append({k: v for k, v in it.items() if not k.startswith("_")})
        cleaned[seg] = keep
    return cleaned


def main():
    if not ENABLED:
        print("MEDIA_ENABLED is off - skipping")
        return
    day_dir = latest_day_dir()
    script = json.loads((day_dir / "script.json").read_text(encoding="utf-8"))
    media = asyncio.run(download_all(day_dir, script))
    if SAFETY:
        media = safety_filter(day_dir, media)
    else:
        print("[!] MEDIA_SAFETY is off - media is NOT checked for graphic content")
    (day_dir / "media.json").write_text(json.dumps(media, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(len(v) for v in media.values())
    print(f"Saved {total} media items -> {day_dir / 'media.json'}")


if __name__ == "__main__":
    main()
