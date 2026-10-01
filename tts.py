import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import edge_tts
import requests

VOICE = os.getenv("TTS_VOICE", "he-IL-AvriNeural")   # or he-IL-HilaNeural (female)
RATE = os.getenv("TTS_RATE", "+0%")                    # e.g. "+5%" faster, "-5%" slower
PITCH = os.getenv("TTS_PITCH", "+0Hz")                 # e.g. "-8Hz" for a deeper voice
PAUSE = float(os.getenv("TTS_PAUSE", "0.6"))           # seconds of silence between parts
DEEP_EQ = os.getenv("TTS_DEEP_EQ", "0") == "1"         # bass warmth + compression + loudness (off by default)
PRON_FILE = os.getenv("TTS_PRONUNCIATIONS", "pronunciations.txt")
NIQQUD = os.getenv("TTS_NIQQUD", "0") == "1"           # automatic niqqud before speaking (off by default)
NIQQUD_MODEL_URL = os.getenv(
    "NIQQUD_MODEL_URL",
    "https://huggingface.co/thewh1teagle/phonikud-onnx/resolve/main/phonikud-1.0.int8.onnx")
NIQQUD_MODEL_PATH = Path(os.getenv("NIQQUD_MODEL_PATH", "models/phonikud-1.0.int8.onnx"))
SR = 24000

HEB_MONTHS = ["ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני",
              "יולי", "אוגוסט", "ספטמבר", "אוקטובר", "נובמבר", "דצמבר"]
DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")

# warm low end, gentle compression, YouTube loudness (-14 LUFS)
EQ_FILTER = ("equalizer=f=110:t=q:w=1.0:g=3,"
             "equalizer=f=3500:t=q:w=1.0:g=1.5,"
             "acompressor=threshold=-20dB:ratio=3:attack=5:release=120,"
             "loudnorm=I=-14:TP=-1.5:LRA=9")

# standard niqqud + dagesh + shin/sin dots + qamats qatan + maqaf + sof pasuq
KEEP_MARKS = set(range(0x05B0, 0x05BD)) | {0x05BE, 0x05C1, 0x05C2, 0x05C3, 0x05C7}


def load_pronunciations(path):
    p = Path(path)
    if not p.exists():
        return []
    pairs = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        src, dst = line.split("=", 1)
        src, dst = src.strip(), dst.strip()
        if src and dst:
            pairs.append((src, dst))
    pairs.sort(key=lambda x: len(x[0]), reverse=True)   # longest first
    return pairs


PRONUNCIATIONS = load_pronunciations(PRON_FILE)

_nakdan = None  # None = not loaded yet, False = unavailable


def get_nakdan():
    global _nakdan
    if _nakdan is None:
        try:
            from phonikud_onnx import Phonikud
            if not NIQQUD_MODEL_PATH.exists():
                NIQQUD_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
                print(f"Downloading niqqud model from {NIQQUD_MODEL_URL} ...")
                tmp = NIQQUD_MODEL_PATH.with_suffix(".part")
                with requests.get(NIQQUD_MODEL_URL, stream=True, timeout=900) as r:
                    r.raise_for_status()
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(1 << 20):
                            f.write(chunk)
                tmp.rename(NIQQUD_MODEL_PATH)
            _nakdan = Phonikud(str(NIQQUD_MODEL_PATH))
            print("Niqqud model loaded")
        except Exception as e:
            print(f"[!] niqqud disabled: {e}")
            _nakdan = False
    return _nakdan or None


def clean_marks(text):
    """Keep standard niqqud only; drop Phonikud's extra stress/shva/prefix markers."""
    out = []
    for ch in text:
        cp = ord(ch)
        if ch == "|":
            continue
        if 0x0591 <= cp <= 0x05C7 and cp not in KEEP_MARKS:
            continue
        out.append(ch)
    return "".join(out)


def diacritize(text):
    nak = get_nakdan()
    if not nak:
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out = []
    for s in sentences:
        if not s.strip():
            continue
        try:
            out.append(clean_marks(nak.add_diacritics(s)))
        except Exception as e:
            print(f"  [!] niqqud failed for a sentence ({e}), using plain text")
            out.append(s)
    return " ".join(out)


def speakable(text):
    """Dates as words -> protect dictionary words -> niqqud -> restore dictionary words."""
    def repl(m):
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{d} ב{HEB_MONTHS[mo - 1]} {y}"
        return m.group(0)
    text = DATE_RE.sub(repl, text or "")

    tokens = {}
    for i, (src, dst) in enumerate(PRONUNCIATIONS):
        if src in text:
            token = f"PRN{i:03d}X"
            tokens[token] = dst
            text = text.replace(src, token)

    if NIQQUD:
        text = diacritize(text)

    for token, dst in tokens.items():
        text = text.replace(token, dst)
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
            comm = edge_tts.Communicate(text, VOICE, rate=RATE, pitch=PITCH)
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

    print(f"Voice: {VOICE}, rate {RATE}, pitch {PITCH}, deep EQ {'on' if DEEP_EQ else 'off'}, "
          f"niqqud {'on' if NIQQUD else 'off'}")
    print(f"Pronunciation rules loaded: {len(PRONUNCIATIONS)}")
    t = 0.0
    concat_lines = []
    spoken_dump = []
    for idx, p in enumerate(parts):
        text = speakable(p["text"])
        spoken_dump.append(f"--- {p['id']} ---\n{text}")
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

    (audio_dir / "spoken.txt").write_text("\n\n".join(spoken_dump), encoding="utf-8")

    list_file = audio_dir / "concat.txt"
    list_file.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")

    raw = audio_dir / "narration_raw.wav"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
         "-c:a", "pcm_s16le", str(raw)])

    narration = audio_dir / "narration.mp3"
    cmd = ["ffmpeg", "-y", "-i", str(raw)]
    if DEEP_EQ:
        cmd += ["-af", EQ_FILTER, "-ar", str(SR)]
    cmd += ["-c:a", "libmp3lame", "-b:a", "160k", str(narration)]
    run(cmd)

    manifest = {"voice": VOICE, "rate": RATE, "pitch": PITCH, "deep_eq": DEEP_EQ,
                "niqqud": NIQQUD and bool(_nakdan), "pause": PAUSE,
                "total_duration": round(duration(narration), 3),
                "narration": "audio/narration.mp3", "parts": parts}
    (day_dir / "audio.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                        encoding="utf-8")

    print(f"\nParts: {len(parts)}, total {manifest['total_duration'] / 60:.1f} min")
    print(f"Saved -> {narration}")


if __name__ == "__main__":
    asyncio.run(main())
