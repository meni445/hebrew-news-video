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
PROOFREAD = int(os.getenv("PROOFREAD", "0") or 0)          # 0 off, 1 spelling pass, 2 spelling + editor pass
SCRIPT_ATTEMPTS = int(os.getenv("SCRIPT_ATTEMPTS", "3"))   # re-ask if the JSON is broken
TARGET_MINUTES = float(os.getenv("TARGET_MINUTES", "5"))
MAX_STORIES = int(os.getenv("MAX_STORIES", "8"))
MAX_POST_CHARS = int(os.getenv("MAX_POST_CHARS", "700"))
WORDS_PER_MIN = 130  # approx. Hebrew speaking pace
# Name the Telegram channels in the YouTube description (1) or write a generic source line (0)
DESC_SOURCES = os.getenv("DESC_SOURCES", "1") == "1"
# Ask Gemini for a short, punchy thumbnail text (used by render.py with THUMB_V2=1). Off by default.
THUMB_TEXT = os.getenv("THUMB_TEXT", "0") == "1"

THUMB_RULES = """
Also add the key "thumbnail_text": 2 to 4 Hebrew words (max 22 characters) for the
video thumbnail, about the FIRST (most important) story. Big, concrete and factual,
like a newspaper front-page headline. No question marks, no emojis, no exaggeration
or claims that are not in the posts, no graphic words.
"""

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

EDIT_RULES = """Rules for every change:
- Do NOT add, remove, merge or reorder stories.
- Do NOT change facts, names, numbers, dates or attributions.
- Keep exactly the same JSON keys, the same number of segments and the same number of tags.
- Hebrew acronyms must use the gershayim character ״ (e.g. צה״ל), never the ASCII double quote.

Return ONLY JSON, no markdown, in this shape:
{"script": <the full corrected JSON, same structure as below>,
 "changes": [{"from": "original words", "to": "corrected words", "reason": "short reason"}]}
If nothing needs fixing, return the JSON unchanged with an empty "changes" list.

JSON:
__JSON__
"""

SPELLING_PROMPT = """You are a Hebrew copy editor. Below is the content of a Hebrew YouTube news
video: its title, description, tags, and a script that a Hebrew text-to-speech voice reads aloud.

Fix ONLY:
- spelling and grammar mistakes and typos (in every field, including the title, description,
  tags and headlines)
- any characters that are not Hebrew (Arabic, Korean, Chinese, Cyrillic etc.):
  replace them with the correct Hebrew word that fits the sentence

""" + EDIT_RULES

EDITOR_PROMPT = """You are a senior Hebrew news editor doing the final check before publishing.
Below is the content of a Hebrew YouTube news video: its title, description, tags, and a
script that a Hebrew text-to-speech voice reads aloud.

Read every field carefully, word by word, and fix:
- real Hebrew words that are WRONG IN CONTEXT - a spell checker misses these
  (for example "לח ולעולם" instead of "בארץ ובעולם", "בבצורת החקירות" instead of "בלשכת החקירות")
- wrong gender or number agreement, wrong prepositions, missing or extra words
- broken, cut-off or unclear sentences
- phrases that sound unnatural when read aloud in a news broadcast
- the title and headlines must be correct, natural Hebrew with no typos at all

""" + EDIT_RULES


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


# ---------------- proofreading ----------------

def editable(script):
    return {
        "title": script.get("title", ""),
        "description": script.get("description", ""),
        "tags": script.get("tags", []),
        "intro": script.get("intro", ""),
        "segments": [{"headline": s.get("headline", ""), "narration": s.get("narration", "")}
                     for s in script["segments"]],
        "outro": script.get("outro", ""),
        **({"thumbnail_text": script["thumbnail_text"]} if script.get("thumbnail_text") else {}),
    }


def apply_edit(script, fixed):
    if not isinstance(fixed, dict):
        return False
    segs = fixed.get("segments")
    if not isinstance(segs, list) or len(segs) != len(script["segments"]):
        return False
    for key in ("title", "description", "intro", "outro", "thumbnail_text"):
        val = fixed.get(key)
        if isinstance(val, str) and val.strip():
            script[key] = val
    tags = fixed.get("tags")
    if isinstance(tags, list) and tags and all(isinstance(t, str) for t in tags):
        script["tags"] = tags
    for orig, new in zip(script["segments"], segs):
        if not isinstance(new, dict):
            continue
        for key in ("headline", "narration"):
            val = new.get(key)
            if isinstance(val, str) and val.strip():
                orig[key] = val
    return True


def edit_pass(script, api_key, model, template, label):
    prompt = template.replace("__JSON__", json.dumps(editable(script), ensure_ascii=False, indent=2))
    print(f"{label}...")
    raw, _ = call_gemini(prompt, api_key, [model])
    if raw is None:
        print(f"  {label} failed, keeping text as is")
        return []
    try:
        data = parse_json(raw)
    except Exception as e:
        print(f"  {label} returned invalid JSON ({e}), keeping text as is")
        return []
    fixed = data.get("script", data) if isinstance(data, dict) else None
    changes = data.get("changes", []) if isinstance(data, dict) else []
    if not isinstance(changes, list):
        changes = []
    if not apply_edit(script, fixed):
        print(f"  {label} changed the structure, keeping text as is")
        return []
    print(f"  {label}: {len(changes)} change(s)")
    for c in changes[:40]:
        if isinstance(c, dict):
            print(f"    {c.get('from', '')}  ->  {c.get('to', '')}   ({c.get('reason', '')})")
    return [dict(c, layer=label) for c in changes if isinstance(c, dict)]


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
    if script.get("thumbnail_text"):
        script["thumbnail_text"] = clean_text(script["thumbnail_text"], "thumbnail_text", warnings).strip()
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

    template = PROMPT.replace("POSTS:\n__POSTS__", THUMB_RULES + "\nPOSTS:\n__POSTS__") if THUMB_TEXT else PROMPT
    prompt = (template.replace("__MAX_STORIES__", str(MAX_STORIES))
                    .replace("__MIN_WORDS__", str(min_words))
                    .replace("__WORDS__", str(words))
                    .replace("__MINUTES__", f"{TARGET_MINUTES:g}")
                    .replace("__WEEKDAY__", weekday_he)
                    .replace("__DATE__", date_he)
                    .replace("__POSTS__", posts_text))

    print(f"Sending {n} unique posts...")
    script, used_model = generate_script(prompt, api_key)

    all_changes = []
    if PROOFREAD >= 1:
        all_changes += edit_pass(script, api_key, used_model, SPELLING_PROMPT, "Proofreading (spelling)")
    if PROOFREAD >= 2:
        all_changes += edit_pass(script, api_key, used_model, EDITOR_PROMPT, "Proofreading (editor)")

    script = clean_script(script)

    if DESC_SOURCES:
        channels = ", ".join(sorted(data.get("channels", {}).keys()))
        source_line = f"מקורות: ערוצי טלגרם {channels}"
    else:
        source_line = "מבוסס על דיווחים שפורסמו בערוצי חדשות בטלגרם."
    script["description"] = (
        script.get("description", "").strip()
        + f"\n\n{source_line}"
        + "\nהסרטון נוצר בסיוע בינה מלאכותית."
    )
    script["date"] = date_he
    script["weekday"] = weekday_he
    script["model"] = used_model
    script["proofread_changes"] = all_changes

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
    if THUMB_TEXT:
        print(f"Thumbnail text: {script.get('thumbnail_text') or '(none - render.py will use the first headline)'}")
    print(f"Stories: {len(script['segments'])}, words: {total_words}, "
          f"~{total_words / WORDS_PER_MIN:.1f} min, proofread changes: {len(all_changes)}")
    print(f"Saved -> {day_dir / 'script.json'}")


if __name__ == "__main__":
    main()
