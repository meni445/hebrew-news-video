import json
import os
import sys
import time
from pathlib import Path

import requests

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
TARGET_MINUTES = float(os.getenv("TARGET_MINUTES", "5"))
MAX_STORIES = int(os.getenv("MAX_STORIES", "8"))
MAX_POST_CHARS = int(os.getenv("MAX_POST_CHARS", "700"))
WORDS_PER_MIN = 130  # approx. Hebrew speaking pace

PROMPT = """You are the editor of a daily Hebrew news video for YouTube.
Below are the last 24 hours of posts from several Hebrew Telegram news channels.
Many posts repeat the same story.

Tasks:
1. Group posts about the same event into one story. Ignore ads, promotions,
   channel self-promotion, greetings and posts with no news value.
2. Pick the __MAX_STORIES__ most important stories. Prefer stories reported by
   several channels or with high views. Order from most to least important.
3. Write a script for a presenter who reads it aloud, in natural spoken Hebrew.

Rules for the narration:
- Hebrew only. Neutral, factual tone. No opinions.
- A claim from a single unconfirmed source must be attributed
  (e.g. "לפי דיווחים", "על פי פרסומים ברשתות"). Never present rumors as fact.
- No graphic descriptions of violence or injuries. Do not name wounded or
  killed people unless the name was officially published.
- Spoken style: short sentences. No emojis, hashtags, links or bullet symbols.
  Avoid abbreviations except very common ones (צה"ל, ארה"ב).
- Total narration about __WORDS__ words (about __MINUTES__ minutes).
- The intro greets viewers and says this is the news summary for __DATE__.
- The outro is one or two short sentences asking to subscribe.

Return ONLY valid JSON, no markdown, in exactly this shape:
{
  "title": "Hebrew YouTube title, max 90 characters, includes the date __DATE__",
  "description": "Hebrew YouTube description: 2-3 sentence summary, then one short line per story",
  "tags": ["up to 12 Hebrew tags"],
  "intro": "spoken intro",
  "segments": [
    {
      "headline": "short Hebrew headline, up to 8 words",
      "narration": "spoken text for this story",
      "source_ids": ["channel/id", "..."]
    }
  ],
  "outro": "spoken outro"
}

POSTS:
__POSTS__
"""


def latest_day_dir():
    days = sorted(p for p in Path("daily").glob("*") if (p / "posts.json").exists())
    if not days:
        sys.exit("No daily/*/posts.json found - run collect.py first")
    return days[-1]


def build_posts_text(posts):
    lines, seen = [], set()
    for p in posts:
        text = (p.get("text") or "").strip()
        if not text:
            continue
        key = text[:120]
        if key in seen:
            continue
        seen.add(key)
        if len(text) > MAX_POST_CHARS:
            text = text[:MAX_POST_CHARS] + "..."
        meta = f'{p["channel"]}/{p["id"]} | {p["date"][11:16]} | views={p.get("views") or 0}'
        lines.append(f"[{meta}]\n{text}")
    return "\n\n".join(lines), len(lines)


def call_gemini(prompt, api_key):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4},
    }
    for attempt in range(5):
        r = requests.post(url, headers=headers, json=body, timeout=300)
        if r.status_code in (429, 500, 502, 503, 504):
            wait = 20 * (attempt + 1)
            print(f"Gemini HTTP {r.status_code}, retrying in {wait}s")
            time.sleep(wait)
            continue
        if r.status_code != 200:
            sys.exit(f"Gemini error {r.status_code}: {r.text[:1000]}")
        data = r.json()
        try:
            return "".join(part.get("text", "") for part in data["candidates"][0]["content"]["parts"])
        except (KeyError, IndexError):
            sys.exit(f"Unexpected Gemini response: {json.dumps(data, ensure_ascii=False)[:1000]}")
    sys.exit("Gemini failed after retries")


def parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    text = text[: text.rfind("}") + 1]
    return json.loads(text)


def main():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        sys.exit("Missing env var: GEMINI_API_KEY")

    day_dir = latest_day_dir()
    data = json.loads((day_dir / "posts.json").read_text(encoding="utf-8"))
    posts_text, n = build_posts_text(data["posts"])
    if n == 0:
        sys.exit("No text posts to summarize")

    y, m, d = day_dir.name.split("-")
    date_he = f"{d}.{m}.{y}"
    words = int(TARGET_MINUTES * WORDS_PER_MIN)

    prompt = (PROMPT.replace("__MAX_STORIES__", str(MAX_STORIES))
                    .replace("__WORDS__", str(words))
                    .replace("__MINUTES__", f"{TARGET_MINUTES:g}")
                    .replace("__DATE__", date_he)
                    .replace("__POSTS__", posts_text))

    print(f"Sending {n} unique posts to {MODEL}...")
    script = parse_json(call_gemini(prompt, api_key))

    if not script.get("segments"):
        sys.exit("Gemini returned no segments")

    channels = ", ".join(sorted(data.get("channels", {}).keys()))
    script["description"] = (
        script.get("description", "").strip()
        + f"\n\nמקורות: ערוצי טלגרם {channels}"
        + "\nהסרטון נוצר בסיוע בינה מלאכותית."
    )
    script["date"] = date_he

    (day_dir / "script.json").write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")

    parts = [script.get("intro", "")]
    for i, s in enumerate(script["segments"], 1):
        parts.append(f"--- {i}. {s.get('headline', '')} ---\n{s.get('narration', '')}")
    parts.append(script.get("outro", ""))
    full = "\n\n".join(p for p in parts if p)
    (day_dir / "script.txt").write_text(full, encoding="utf-8")

    total_words = len(full.split())
    print(f"Title: {script.get('title')}")
    print(f"Stories: {len(script['segments'])}, words: {total_words}, "
          f"~{total_words / WORDS_PER_MIN:.1f} min")
    print(f"Saved -> {day_dir / 'script.json'}")


if __name__ == "__main__":
    main()
