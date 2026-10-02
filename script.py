import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests

MODELS = [m.strip() for m in os.getenv("GEMINI_MODEL", "gemini-3.8-flash").split(",") if m.strip()]
RETRIES_PER_MODEL = int(os.getenv("GEMINI_RETRIES", "3"))
RETRY_WAIT = int(os.getenv("GEMINI_WAIT", "20"))          # seconds, grows each retry
PROOFREAD = os.getenv("PROOFREAD", "0") == "1"             # off by default
SCRIPT_ATTEMPTS = int(os.getenv("SCRIPT_ATTEMPTS", "3"))   # re-ask if the JSON is broken
TARGET_MINUTES = float(os.getenv("TARGET_MINUTES", "5"))
MAX_STORIES = int(os.getenv("MAX_STORIES", "8"))
MAX_POST_CHARS = int(os.getenv("MAX_POST_CHARS", "700"))
WORDS_PER_MIN = 130  # approx. Hebrew speaking pace

HEB_DAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]  # Monday=0

# Arabic, Cyrillic, Hangul, Japanese, CJK - should never appear in the Hebrew script
FOREIGN = re.compile(
    r"[\u0600-\u06FF\u0750-\u077F\u0400-\u04FF\u1100-\u11FF"
    r"\u3040-\u30FF\u3130-\u318F\u4E00-\u9FFF\uAC00-\uD7AF]+"
)
# ASCII double quote between two Hebrew letters (e.g. צה"ל) - breaks JSON
HEB_QUOTE = re.compile(r'(?<=[\u05D0-\u05EA])"(?=[\u05D0-\u05EA])')
GERSHAYIM = "\u05F4"   # ״
GERESH = "\u05F3"      # ׳

PROMPT = """You are the editor of a daily Hebrew news video for YouTube.
Below are the last 24 hours of posts from several Hebrew Telegram news channels.
Many posts repeat the same story.

Tasks:
1. Group posts about the same event into one story. Ignore ads, promotions,
   channel self-promotion, greetings and posts with no news value.
2. Pick up to __MAX_STORIES__ of the most important stories. Prefer stories
   reported by several channels or with high views. Order from most to least important.
3. Write a script for a presenter who reads it aloud, in natural spoken Hebrew.

Rules for the stories:
- Each event appears in exactly ONE story. Never split one event (or its
  reactions and follow-ups) into two stories.
- Skip vague stories without concrete facts. Fewer, solid stories are better
  than filler.
- Headlines must be specific: name the country, city or body involved.

Rules for the narration:
- Hebrew only. Neutral, factual tone. No opinions.
- Write ONLY Hebrew letters, digits and basic punctuation. Never use characters
  from Arabic, Korean, Chinese, Russian or any other script.
- Hebrew acronyms MUST use the Hebrew gershayim character ״ (e.g. צה״ל, ארה״ב,
  נתב״ג), NEVER the ASCII double quote character. ASCII double quotes inside
  text break the JSON.
- Check spelling and grammar carefully.
- Say "לפי דיווח רשמי" only when an official body is named in the posts
  (e.g. דובר צה״ל, משרד החוץ, משטרת ישראל) and name that body.
  Otherwise attribute with "לפי דיווחים" or "על פי פרסומים ברשתות".
  Never present rumors as fact.
- No graphic descriptions of violence or injuries. Do not name wounded or
  killed people unless the name was officially published.
- Spoken style: short sentences. No emojis, hashtags, links or bullet symbols.
  Avoid abbreviations except very common ones (צה״ל, ארה״ב).
- Total narration (intro + all stories + outro) MUST be between __MIN_WORDS__
  and __WORDS__ words (about __MINUTES__ minutes). Give important stories more detail.
- The intro greets viewers and says this is the news summary for
  יום __WEEKDAY__, __DATE__. Use exactly this day and date.
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

PROOF_PROMPT = """You are a Hebrew copy editor. Below is a JSON news script that
will be read aloud by a Hebrew text-to-speech voice.

Fix ONLY:
- spelling and grammar mistakes and typos
- any characters that are not Hebrew (Arabic, Korean, Chinese, Cyrillic etc.):
  replace them with the correct Hebrew word that fits the sentence

Do NOT add, remove, merge or reorder stories. Do NOT change facts, names,
numbers or attributions. Keep exactly the same JSON keys and the same number
of segments. Hebrew acronyms must use the gershayim character ״ (e.g. צה״ל),
never the ASCII double quote. Return ONLY the corrected JSON, no markdown.

JSON:
__JSON__
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


def short_error(r):
    try:
        return r.json()["error"]["message"][:200]
    except Exception:
        return r.text[:200]


def call_gemini(prompt, api_key, models):
    """Returns (text, model) or (None, None) if every model failed."""
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4},
    }
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        print(f"Trying model: {model}")
        for attempt in range(RETRIES_PER_MODEL):
            try:
                r = requests.post(url, headers=headers, json=body, timeout=300)
            except requests.RequestException as e:
                print(f"  network error: {e}")
                time.sleep(15)
                continue
            if r.status_code == 200:
                data = r.json()
                try:
                    text = "".join(part.get("text", "") for part in data["candidates"][0]["content"]["parts"])
                    return text, model
                except (KeyError, IndexError):
                    print(f"  unexpected response: {json.dumps(data, ensure_ascii=False)[:300]}")
                    break
            if r.status_code in (429, 500, 502, 503, 504):
                if attempt == RETRIES_PER_MODEL - 1:
                    print(f"  HTTP {r.status_code}: {short_error(r)}")
                    break
                wait = min(RETRY_WAIT * (attempt + 1), 300)
                print(f"  HTTP {r.status_code}: {short_error(r)} -> retry in {wait}s")
                time.sleep(wait)
                continue
            print(f"  HTTP {r.status_code}: {short_error(r)} -> next model")
            break
        print(f"  giving up on {model}")
    return None, None


def parse_json(text):
    """Parse model output; repair ASCII quotes inside Hebrew acronyms if needed."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    text = text[: text.rfind("}") + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError as first_error:
        repaired = HEB_QUOTE.sub(GERSHAYIM, text)
        try:
            data = json.loads(repaired)
            print("  [i] repaired ASCII quotes inside Hebrew acronyms")
            return data
        except json.JSONDecodeError:
            raise first_error


def generate_script(prompt, api_key):
    """Call the model chain once, then re-ask the working model if JSON is broken."""
    raw, used_model = call_gemini(prompt, api_key, MODELS)
    if raw is None:
        sys.exit("All Gemini models failed")
    for attempt in range(1, SCRIPT_ATTEMPTS + 1):
        try:
            script = parse_json(raw)
            if script.get("segments"):
                return script, used_model
            print(f"  [!] attempt {attempt}: no segments in response")
        except json.JSONDecodeError as e:
            print(f"  [!] attempt {attempt}: invalid JSON ({e})")
        if attempt == SCRIPT_ATTEMPTS:
            break
        print(f"  re-asking {used_model}...")
        raw, _ = call_gemini(prompt, api_key, [used_model])
        if raw is None:
            break
    sys.exit("Could not get a valid script from Gemini")


def proofread(script, api_key, model):
    subset = {
        "intro": script.get("intro", ""),
        "segments": [{"headline": s.get("headline", ""), "narration": s.get("narration", "")}
                     for s in script["segments"]],
        "outro": script.get("outro", ""),
    }
    prompt = PROOF_PROMPT.replace("__JSON__", json.dumps(subset, ensure_ascii=False, indent=2))
    print("Proofreading...")
    raw, _ = call_gemini(prompt, api_key, [model])
    if raw is None:
        print("  proofread failed, keeping original")
        return script
    try:
        fixed = parse_json(raw)
    except Exception as e:
        print(f"  proofread returned invalid JSON ({e}), keeping original")
        return script
    if len(fixed.get("segments", [])) != len(script["segments"]):
        print("  proofread changed the number of segments, keeping original")
        return script
    script["intro"] = fixed.get("intro") or script.get("intro", "")
    script["outro"] = fixed.get("outro") or script.get("outro", "")
    for orig, new in zip(script["segments"], fixed["segments"]):
        orig["headline"] = new.get("headline") or orig.get("headline", "")
        orig["narration"] = new.get("narration") or orig.get("narration", "")
    print("  proofread applied")
    return script


def clean_text(text, where, warnings):
    text = (text or "").replace(GERSHAYIM, '"').replace(GERESH, "'")
    found = FOREIGN.findall(text)
    if found:
        warnings.append(f"{where}: removed {found}")
        text = FOREIGN.sub("", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
    return text


def clean_script(script):
    warnings = []
    for key in ("title", "description", "intro", "outro"):
        script[key] = clean_text(script.get(key, ""), key, warnings)
    for i, s in enumerate(script["segments"], 1):
        s["headline"] = clean_text(s.get("headline", ""), f"segment {i} headline", warnings)
        s["narration"] = clean_text(s.get("narration", ""), f"segment {i} narration", warnings)
    script["tags"] = [clean_text(t, "tag", warnings) for t in script.get("tags", [])]
    for w in warnings:
        print(f"[!] foreign characters {w}")
    return script


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
    weekday_he = HEB_DAYS[date(int(y), int(m), int(d)).weekday()]
    words = int(TARGET_MINUTES * WORDS_PER_MIN)
    min_words = int(words * 0.85)

    prompt = (PROMPT.replace("__MAX_STORIES__", str(MAX_STORIES))
                    .replace("__MIN_WORDS__", str(min_words))
                    .replace("__WORDS__", str(words))
                    .replace("__MINUTES__", f"{TARGET_MINUTES:g}")
                    .replace("__WEEKDAY__", weekday_he)
                    .replace("__DATE__", date_he)
                    .replace("__POSTS__", posts_text))

    print(f"Sending {n} unique posts...")
    script, used_model = generate_script(prompt, api_key)

    if PROOFREAD:
        script = proofread(script, api_key, used_model)

    script = clean_script(script)

    channels = ", ".join(sorted(data.get("channels", {}).keys()))
    script["description"] = (
        script.get("description", "").strip()
        + f"\n\nמקורות: ערוצי טלגרם {channels}"
        + "\nהסרטון נוצר בסיוע בינה מלאכותית."
    )
    script["date"] = date_he
    script["weekday"] = weekday_he
    script["model"] = used_model

    (day_dir / "script.json").write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")

    parts = [script.get("intro", "")]
    for i, s in enumerate(script["segments"], 1):
        parts.append(f"--- {i}. {s.get('headline', '')} ---\n{s.get('narration', '')}")
    parts.append(script.get("outro", ""))
    full = "\n\n".join(p for p in parts if p)
    (day_dir / "script.txt").write_text(full, encoding="utf-8")

    total_words = len(full.split())
    print(f"Model used: {used_model}")
    print(f"Title: {script.get('title')}")
    print(f"Stories: {len(script['segments'])}, words: {total_words}, "
          f"~{total_words / WORDS_PER_MIN:.1f} min")
    print(f"Saved -> {day_dir / 'script.json'}")


if __name__ == "__main__":
    main()
