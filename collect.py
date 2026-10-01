import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from telethon import TelegramClient
from telethon.sessions import StringSession

TZ = ZoneInfo("Asia/Jerusalem")
HOURS = float(os.getenv("HOURS", "24"))
DOWNLOAD_MEDIA = os.getenv("DOWNLOAD_MEDIA", "0") == "1"   # off by default
MAX_PER_CHANNEL = int(os.getenv("MAX_PER_CHANNEL", "500"))


def env(name):
    v = os.getenv(name)
    if not v:
        sys.exit(f"Missing env var: {name}")
    return v


def load_channels(path="channels.txt"):
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
            if line.startswith(prefix):
                line = line[len(prefix):]
        out.append(line.lstrip("@").strip("/"))
    return out


async def main():
    api_id = int(env("TG_API_ID"))
    api_hash = env("TG_API_HASH")
    session = env("TG_SESSION")
    channels = load_channels()

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=HOURS)
    day = now.astimezone(TZ).strftime("%Y-%m-%d")
    outdir = Path("daily") / day
    media_dir = outdir / "media"
    outdir.mkdir(parents=True, exist_ok=True)

    posts, stats = [], {}

    async with TelegramClient(StringSession(session), api_id, api_hash) as client:
        for ch in channels:
            count = 0
            try:
                entity = await client.get_entity(ch)
                title = getattr(entity, "title", ch)
                async for m in client.iter_messages(entity, limit=MAX_PER_CHANNEL):
                    if m.date < since:
                        break
                    text = (m.message or "").strip()
                    if not text and not m.media:
                        continue
                    item = {
                        "channel": ch,
                        "channel_title": title,
                        "id": m.id,
                        "date": m.date.astimezone(TZ).isoformat(timespec="seconds"),
                        "text": text,
                        "views": m.views,
                        "forwards": m.forwards,
                        "grouped_id": m.grouped_id,
                        "has_photo": bool(m.photo),
                        "has_video": bool(m.video),
                        "link": f"https://t.me/{ch}/{m.id}",
                        "media_file": None,
                    }
                    if DOWNLOAD_MEDIA and m.photo:
                        media_dir.mkdir(exist_ok=True)
                        p = await m.download_media(file=str(media_dir / f"{ch}_{m.id}"))
                        if p:
                            item["media_file"] = str(Path(p).relative_to(outdir))
                    posts.append(item)
                    count += 1
            except Exception as e:
                print(f"[!] {ch}: {e}")
            stats[ch] = count
            print(f"{ch}: {count} posts")

    posts.sort(key=lambda p: p["date"])
    data = {
        "generated_at": now.astimezone(TZ).isoformat(timespec="seconds"),
        "hours": HOURS,
        "channels": stats,
        "count": len(posts),
        "posts": posts,
    }
    out_file = outdir / "posts.json"
    out_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSaved {len(posts)} posts -> {out_file}")


if __name__ == "__main__":
    asyncio.run(main())
