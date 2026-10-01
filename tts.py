import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import edge_tts

VOICE = os.getenv("TTS_VOICE", "he-IL-AvriNeural")   # or he-IL-HilaNeural (female)
RATE = os.getenv("TTS_RATE", "+0%")                    # e.g. "+5%" to speak a bit faster
PAUSE = float(os.getenv("TTS_PAUSE", "0.6"))           # seconds of silence between parts
SR = 24000

HEB_MONTHS = ["ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני",
              "יולי", "אוגוסט", "ספטמבר", "אוקטובר", "נובמבר", "דצמבר"]
DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")


def speakable(text):
    """Make text easier for the voice: 01.10.2026 -> 1 באוקטובר 2026."""
    def repl(m):
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{d} ב{HEB_MONTHS[mo - 1]} {y}"
        return m.group(0)
    text = DATE_RE.sub(repl, text or "")
    return re.sub(r"\s+", " ", text).strip()


def run(cmd):
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        sys.exit(f"Command failed: {' '.join(cmd)}\n{r.stderr[-1500:]}")


def duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def latest_day_dir():
    days = sorted(p for p in Path("daily").glob("*") if (p / "script.json").exists())
    if not days:
        sys.exit("No daily/*/script.json found - run script.py first")
    return days[-1]


async def synth(text, mp3_path, srt_path):
    for attempt in range(4):
        try:
            comm = edge_tts.Communicate(text, VOICE, rate=RATE)
            sub = edge_tts.SubMaker()
            with open(mp3_path, "wb") as f:
                async for chunk in comm.stream():
                    if chunk["type"] == "audio":
                        f.write(chunk["data"])
                    elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                        sub.feed(chunk)
            if mp3_path.stat().st_size == 0:
                raise RuntimeError("empty audio")
            try:
                srt_path.write_text(sub.get_srt(), encoding="utf-8")
            except Exception as e:
                print(f"  [!] subtitles not saved: {e}")
            return
        except Exception as e:
            wait = 10 * (attempt + 1)
            print(f"  TTS error: {e} -> retry in {wait}s")
            await asyncio.sleep(wait)
    sys.exit(f"TTS failed for {mp3_path.name}")


async def main():
    day_dir = latest_day_dir()
    script = json.loads((day_dir / "script.json").read_text(encoding="utf-8"))

    parts = [{"id": "intro", "headline": "", "text": script.get("intro", "")}]
    for i, s in enumerate(script["segments"], 1):
        parts.append({"id": f"seg{i:02d}", "headline": s.get("headline", ""),
                      "text": s.get("narration", "")})
    parts.append({"id": "outro", "headline": "", "text": script.get("outro", "")})
    parts = [p for p in parts if p["text"].strip()]

    audio_dir = day_dir / "audio"
    audio_dir.mkdir(exist_ok=True)

    silence = audio_dir / "silence.wav"
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"anullsrc=r={SR}:cl=mono",
         "-t", str(PAUSE), str(silence)])

    print(f"Voice: {VOICE}, rate {RATE}")
    t = 0.0
    concat_lines = []
    for idx, p in enumerate(parts):
        text = speakable(p["text"])
        mp3 = audio_dir / f"{p['id']}.mp3"
        wav = audio_dir / f"{p['id']}.wav"
        srt = audio_dir / f"{p['id']}.srt"
        print(f"Speaking {p['id']} ({len(text.split())} words)...")
        await synth(text, mp3, srt)
        run(["ffmpeg", "-y", "-i", str(mp3), "-ar", str(SR), "-ac", "1", str(wav)])
        dur = duration(wav)

        p.update({"spoken_text": text, "audio": f"audio/{wav.name}",
                  "srt": f"audio/{srt.name}" if srt.exists() else None,
                  "start": round(t, 3), "duration": round(dur, 3)})
        concat_lines.append(f"file '{wav.name}'")
        t += dur
        if idx < len(parts) - 1:
            concat_lines.append(f"file '{silence.name}'")
            t += PAUSE

    list_file = audio_dir / "concat.txt"
    list_file.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")
    narration = audio_dir / "narration.mp3"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
         "-c:a", "libmp3lame", "-b:a", "128k", str(narration)])

    manifest = {"voice": VOICE, "rate": RATE, "pause": PAUSE,
                "total_duration": round(duration(narration), 3),
                "narration": "audio/narration.mp3", "parts": parts}
    (day_dir / "audio.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                        encoding="utf-8")

    print(f"\nParts: {len(parts)}, total {manifest['total_duration'] / 60:.1f} min")
    print(f"Saved -> {narration}")


if __name__ == "__main__":
    asyncio.run(main())
