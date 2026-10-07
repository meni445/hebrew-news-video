import io
import json
import os
import sys
import time
from pathlib import Path

import requests
from PIL import Image

UPLOAD = os.getenv("YT_UPLOAD", "0") == "1"                 # off by default
PRIVACY = os.getenv("YT_PRIVACY", "private")                 # private | unlisted | public
CATEGORY = os.getenv("YT_CATEGORY", "25")                    # 25 = News & Politics
SET_THUMBNAIL = os.getenv("YT_THUMBNAIL", "1") == "1"

# Richer metadata: top story in the title, chapters, subscribe link, hashtags, base tags. Off by default.
RICH = os.getenv("YT_RICH", "0") == "1"
CHANNEL_NAME = os.getenv("CHANNEL_NAME", "חדשות היום")
CHANNEL_HANDLE = os.getenv("YT_HANDLE", "hadashot_hayom")
HASHTAGS = os.getenv("YT_HASHTAGS", "#חדשות #חדשות_היום #ישראל")
BASE_TAGS = [t.strip() for t in os.getenv(
    "YT_BASE_TAGS", "חדשות,חדשות היום,מבזקי חדשות,עדכון יומי,סיכום חדשות,ישראל,חדשות ישראל,"
                    "מזרח התיכון,חדשות בעברית,טלגרם").split(",") if t.strip()]

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
THUMB_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"


def env(name):
    v = os.getenv(name)
    if not v:
        sys.exit(f"Missing env var: {name}")
    return v


def latest_day_dir():
    days = sorted(p for p in Path("daily").glob("*") if (p / "video.mp4").exists())
    if not days:
        sys.exit("No daily/*/video.mp4 found - run render.py first")
    return days[-1]


def access_token():
    r = requests.post(TOKEN_URL, data={
        "client_id": env("YT_CLIENT_ID"),
        "client_secret": env("YT_CLIENT_SECRET"),
        "refresh_token": env("YT_REFRESH_TOKEN"),
        "grant_type": "refresh_token",
    }, timeout=60)
    if r.status_code != 200:
        sys.exit(f"Token refresh failed ({r.status_code}): {r.text[:500]}\n"
                 "If it says invalid_grant, the refresh token expired - run yt_login.py again "
                 "and update the YT_REFRESH_TOKEN secret.")
    return r.json()["access_token"]


def clean(text, limit):
    text = (text or "").replace("<", "").replace(">", "").strip()
    return text[:limit]


def stamp(sec):
    sec = int(sec)
    return f"{sec // 3600}:{sec // 60 % 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def chapters(script, audio):
    """YouTube chapters from audio.json: first at 0:00, at least 3, each at least 10 seconds."""
    if not audio:
        return []
    parts = audio.get("parts", [])
    heads = {f"seg{i:02d}": s.get("headline", "") for i, s in enumerate(script.get("segments", []), 1)}
    rows = []
    for p in parts:
        pid = p.get("id", "")
        name = "פתיחה" if pid == "intro" else "סיום" if pid == "outro" else heads.get(pid, "")
        if name:
            rows.append((0 if pid == "intro" else p.get("start", 0), name))
    if len(rows) < 3 or rows[0][0] != 0:
        return []
    ends = [r[0] for r in rows[1:]] + [audio.get("total_duration", rows[-1][0] + 10)]
    if any(e - r[0] < 10 for r, e in zip(rows, ends)):
        rows = [r for r, e in zip(rows, ends) if e - r[0] >= 10 or r[0] == 0]
        if len(rows) < 3:
            return []
    return [f"{stamp(t)} {clean(n, 90)}" for t, n in rows]


def rich_title(script):
    segs = script.get("segments", [])
    head = clean(segs[0].get("headline", ""), 70) if segs else ""
    date = script.get("date", "")
    if not head:
        return clean(script.get("title", "סיכום חדשות"), 100)
    return clean(f"{head} | {CHANNEL_NAME} {date}".strip(), 100)


def rich_description(script, audio):
    desc = (script.get("description") or "").strip()
    first, _, rest = desc.partition("\n\n")
    blocks = [first]
    ch = chapters(script, audio)
    if ch:
        blocks.append("בפרק היום:\n" + "\n".join(ch))
    if rest.strip():
        blocks.append(rest.strip())
    blocks.append(f"🔔 מהדורה חדשה כל בוקר – הירשמו והפעילו את הפעמון:\n"
                  f"https://www.youtube.com/@{CHANNEL_HANDLE}?sub_confirmation=1")
    if HASHTAGS.strip():
        blocks.append(HASHTAGS.strip())
    return "\n\n".join(b for b in blocks if b)


def build_metadata(script, audio=None):
    tags, total = [], 0
    source = script.get("tags", [])
    if RICH:
        seen = set()
        source = [t for t in BASE_TAGS + list(source) if not (t in seen or seen.add(t))]
    for t in source:
        t = clean(t, 100)
        if not t:
            continue
        cost = len(t) + (2 if " " in t else 0) + 1
        if total + cost > 480:
            break
        tags.append(t)
        total += cost
    title = rich_title(script) if RICH else clean(script.get("title", "סיכום חדשות"), 100)
    description = rich_description(script, audio) if RICH else script.get("description", "")
    return {
        "snippet": {
            "title": title,
            "description": clean(description, 4900),
            "tags": tags,
            "categoryId": CATEGORY,
            "defaultLanguage": "he",
            "defaultAudioLanguage": "he",
        },
        "status": {
            "privacyStatus": PRIVACY,
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True,
        },
    }


def upload_video(token, video_path, metadata):
    size = video_path.stat().st_size
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Type": "video/mp4",
        "X-Upload-Content-Length": str(size),
    }
    r = requests.post(UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                      headers=headers, json=metadata, timeout=120)
    if r.status_code != 200 or "Location" not in r.headers:
        sys.exit(f"Could not start upload ({r.status_code}): {r.text[:800]}")
    session_url = r.headers["Location"]

    for attempt in range(4):
        try:
            with open(video_path, "rb") as f:
                r = requests.put(session_url, data=f, timeout=1800, headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "video/mp4",
                    "Content-Length": str(size),
                })
            if r.status_code in (200, 201):
                return r.json()
            print(f"  upload HTTP {r.status_code}: {r.text[:300]}")
            if r.status_code < 500:
                break
        except requests.RequestException as e:
            print(f"  upload error: {e}")
        wait = 30 * (attempt + 1)
        print(f"  retrying in {wait}s...")
        time.sleep(wait)
    sys.exit("Video upload failed")


def set_thumbnail(token, video_id, png_path):
    img = Image.open(png_path).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)          # YouTube limit is 2 MB
    r = requests.post(THUMB_URL, params={"videoId": video_id},
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"},
                      data=buf.getvalue(), timeout=120)
    if r.status_code == 200:
        print("Thumbnail set")
    else:
        print(f"[!] Thumbnail not set ({r.status_code}) - channel probably not phone-verified yet. "
              f"YouTube will pick a frame instead.\n    {r.text[:300]}")


def main():
    if not UPLOAD:
        print("YT_UPLOAD is off - skipping upload")
        return

    day_dir = latest_day_dir()
    script = json.loads((day_dir / "script.json").read_text(encoding="utf-8"))
    video = day_dir / "video.mp4"
    audio_file = day_dir / "audio.json"
    audio = json.loads(audio_file.read_text(encoding="utf-8")) if audio_file.exists() else None
    metadata = build_metadata(script, audio)

    token = access_token()
    print(f"Uploading {video} ({video.stat().st_size / 1e6:.0f} MB) as {PRIVACY}...")
    print(f"Title: {metadata['snippet']['title']}")
    if RICH:
        print(f"Tags: {', '.join(metadata['snippet']['tags'])}")
        print("Description:\n" + metadata["snippet"]["description"] + "\n")
    result = upload_video(token, video, metadata)
    video_id = result["id"]
    url = f"https://youtu.be/{video_id}"

    thumb = day_dir / "thumbnail.png"
    if SET_THUMBNAIL and thumb.exists():
        set_thumbnail(token, video_id, thumb)

    (day_dir / "upload.json").write_text(json.dumps(
        {"video_id": video_id, "url": url, "privacy": PRIVACY,
         "upload_status": result.get("status", {}).get("uploadStatus")},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nUploaded: {url}")


if __name__ == "__main__":
    main()
