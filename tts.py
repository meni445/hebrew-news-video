import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import time
import wave
from pathlib import Path

import edge_tts
import requests

ENGINE = os.getenv("TTS_ENGINE", "edge")              # "edge" or "gemini" (gemini falls back to edge)

# Edge settings (also used as fallback)
VOICE = os.getenv("TTS_VOICE", "he-IL-AvriNeural")
RATE = os.getenv("TTS_RATE", "+0%")
PITCH = os.getenv("TTS_PITCH", "+0Hz")

# Gemini TTS settings
GEMINI_TTS_MODELS = [m.strip() for m in os.getenv("GEMINI_TTS_MODEL", "").split(",") if m.strip()]
GEMINI_TTS_VOICE = os.getenv("GEMINI_TTS_VOICE", "Charon")
GEMINI_TTS_STYLE = os.getenv(
    "GEMINI_TTS_STYLE",
    "Read the following Hebrew news text aloud in Hebrew, exactly as written, "
    "in a deep, warm, calm and authoritative male documentary narrator voice, at a steady pace")
GEMINI_TTS_GAP = float(os.getenv("GEMINI_TTS_GAP", "25"))      # seconds between requests
GEMINI_TTS_RETRIES = int(os.getenv("GEMINI_TTS_RETRIES", "4"))

PAUSE = float(os.getenv("TTS_PAUSE", "0.6"))
DEEP_EQ = os.getenv("TTS_DEEP_EQ", "0") == "1"
PRON_FILE = os.getenv("TTS_PRONUNCIATIONS", "pronunciations.txt")
NIQQUD = os.getenv("TTS_NIQQUD", "0") == "1"
NIQQUD_MODEL_URL = os.getenv(
    "NIQQUD_MODEL_URL",
    "https://huggingface.co/thewh1teagle/phonikud-onnx/resolve/main/phonikud-1.0.int8.onnx")
NIQQUD_MODEL_PATH = Path(os.getenv("NIQQUD_MODEL_PATH", "models/phonikud-1.0.int8.onnx"))
SR = 24000

# Noise trim per part: cuts noise/static before the first and after the last spoken
# sound, then fades in/out so parts join without clicks. Off by default.
CLEAN = os.getenv("TTS_CLEAN", "0") == "1"
CLEAN_KEEP_MS = int(os.getenv("TTS_CLEAN_KEEP_MS", "150"))    # kept around speech
CLEAN_FADE_MS = int(os.getenv("TTS_CLEAN_FADE_MS", "60"))
CLEAN_RANGE_DB = float(os.getenv("TTS_CLEAN_RANGE_DB", "30"))  # speech = within this of the loud level
CLEAN_MAX_ZCR = float(os.getenv("TTS_CLEAN_MAX_ZCR", "0.25"))  # static/hiss has a high zero-crossing rate

HEB_MONTHS = ["ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני",
              "יולי", "אוגוסט", "ספטמבר", "אוקטובר", "נובמבר", "דצמבר"]
DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")

EQ_FILTER = ("equalizer=f=110:t=q:w=1.0:g=3,"
             "equalizer=f=3500:t=q:w=1.0:g=1.5,"
             "acompressor=threshold=-20dB:ratio=3:attack=5:release=120,"
             "loudnorm=I=-14:TP=-1.5:LRA=9")

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
    pairs.sort(key=lambda x: len(x[0]), reverse=True)
    return pairs


PRONUNCIATIONS = load_pronunciations(PRON_FILE)

_nakdan = None


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
    out = []
    for s in re.split(r"(?<=[.!?])\s+", text):
        if not s.strip():
            continue
        try:
            out.append(clean_marks(nak.add_diacritics(s)))
        except Exception as e:
            print(f"  [!] niqqud failed for a sentence ({e}), using plain text")
            out.append(s)
    return " ".join(out)


def speakable(text):
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


# ---------------- Edge TTS ----------------

async def edge_synth(text, mp3_path, srt_path):
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
            print(f"  Edge TTS error: {e} -> retry in {wait}s")
            await asyncio.sleep(wait)
    sys.exit(f"Edge TTS failed for {mp3_path.name}")


# ---------------- Gemini TTS ----------------

def short_error(r):
    try:
        return r.json()["error"]["message"][:200]
    except Exception:
        return r.text[:200]


def gemini_synth(text, wav_path, api_key):
    """Returns True on success. Writes a mono 16-bit WAV."""
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    body = {
        "contents": [{"parts": [{"text": f"{GEMINI_TTS_STYLE}:\n\n{text}" if GEMINI_TTS_STYLE.strip() else text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": GEMINI_TTS_VOICE}}},
        },
    }
    for model in GEMINI_TTS_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(GEMINI_TTS_RETRIES):
            try:
                r = requests.post(url, headers=headers, json=body, timeout=300)
            except requests.RequestException as e:
                print(f"  network error: {e}")
                time.sleep(20)
                continue
            if r.status_code == 200:
                try:
                    parts = r.json()["candidates"][0]["content"]["parts"]
                    inline = next(p["inlineData"] for p in parts if "inlineData" in p)
                except (KeyError, IndexError, StopIteration):
                    print(f"  {model}: no audio in response")
                    break
                pcm = base64.b64decode(inline["data"])
                if pcm[:4] == b"RIFF":                      # WAV container, not raw PCM
                    k = pcm.find(b"data")
                    if k != -1:
                        pcm = pcm[k + 8:]
                if len(pcm) % 2:
                    pcm = pcm[:-1]
                m = re.search(r"rate=(\d+)", inline.get("mimeType", ""))
                rate = int(m.group(1)) if m else 24000
                with wave.open(str(wav_path), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(rate)
                    w.writeframes(pcm)
                return True
            if r.status_code in (429, 500, 502, 503, 504):
                wait = 30 * (attempt + 1)
                print(f"  {model} HTTP {r.status_code}: {short_error(r)} -> retry in {wait}s")
                time.sleep(wait)
                continue
            print(f"  {model} HTTP {r.status_code}: {short_error(r)} -> next model")
            break
    return False


# ---------------- cleanup ----------------

def clean_edges(path):
    """Trim non-speech noise at both ends of a mono 16-bit WAV and fade the edges."""
    import numpy as np
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    flen = max(1, int(sr * 0.02))
    nf = len(data) // flen
    if nf < 10:
        return None
    x = data[:nf * flen].reshape(nf, flen)
    db = 20 * np.log10(np.sqrt((x ** 2).mean(axis=1)) / 32768 + 1e-9)
    zcr = (np.diff(np.signbit(x), axis=1) != 0).mean(axis=1)
    loud = np.percentile(db, 95)
    speech = np.where((db > loud - CLEAN_RANGE_DB) & (zcr < CLEAN_MAX_ZCR))[0]
    if len(speech) == 0:
        return None
    keep = CLEAN_KEEP_MS // 20
    s = max(0, int(speech[0]) - keep)
    e = min(nf, int(speech[-1]) + 1 + keep)
    end = len(data) if e == nf else e * flen
    out = data[s * flen:end].copy()
    fade = min(len(out) // 4, int(sr * CLEAN_FADE_MS / 1000))
    if fade > 0:
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        out[:fade] *= ramp
        out[-fade:] *= ramp[::-1]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.clip(out, -32768, 32767).astype(np.int16).tobytes())
    return s * flen / sr, (len(data) - end) / sr


# ---------------- main ----------------

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

    print(f"Pronunciation rules loaded: {len(PRONUNCIATIONS)}, niqqud {'on' if NIQQUD else 'off'}")
    for p in parts:
        p["spoken_text"] = speakable(p["text"])
    (audio_dir / "spoken.txt").write_text(
        "\n\n".join(f"--- {p['id']} ---\n{p['spoken_text']}" for p in parts), encoding="utf-8")

    sources = {}
    used = "edge"

    if ENGINE == "gemini":
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key or not GEMINI_TTS_MODELS:
            print("[!] Gemini TTS needs GEMINI_API_KEY and GEMINI_TTS_MODEL - using Edge")
        else:
            style_info = f"style '{GEMINI_TTS_STYLE}'" if GEMINI_TTS_STYLE.strip() else "no style instruction"
            print(f"Engine: Gemini TTS, voice {GEMINI_TTS_VOICE}, {style_info}, models {GEMINI_TTS_MODELS}")
            ok = True
            for i, p in enumerate(parts):
                if i > 0:
                    time.sleep(GEMINI_TTS_GAP)
                src = audio_dir / f"{p['id']}_gemini.wav"
                print(f"Speaking {p['id']} ({len(p['spoken_text'].split())} words)...")
                if not gemini_synth(p["spoken_text"], src, api_key):
                    ok = False
                    break
                sources[p["id"]] = src
            if ok:
                used = "gemini"
            else:
                print("[!] Gemini TTS failed - redoing the whole narration with Edge")
                sources = {}

    if used == "edge":
        print(f"Engine: Edge, voice {VOICE}, rate {RATE}, pitch {PITCH}")
        for p in parts:
            mp3 = audio_dir / f"{p['id']}.mp3"
            srt = audio_dir / f"{p['id']}.srt"
            print(f"Speaking {p['id']} ({len(p['spoken_text'].split())} words)...")
            await edge_synth(p["spoken_text"], mp3, srt)
            sources[p["id"]] = mp3

    silence = audio_dir / "silence.wav"
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"anullsrc=r={SR}:cl=mono",
         "-t", str(PAUSE), "-c:a", "pcm_s16le", str(silence)])
    print(f"Noise trim at part edges: {'on' if CLEAN else 'off'}")

    t = 0.0
    concat_lines = []
    for idx, p in enumerate(parts):
        wav = audio_dir / f"{p['id']}.wav"
        run(["ffmpeg", "-y", "-i", str(sources[p["id"]]), "-ar", str(SR), "-ac", "1",
             "-c:a", "pcm_s16le", str(wav)])
        if CLEAN:
            try:
                cut = clean_edges(wav)
                if cut:
                    print(f"  {p['id']}: trimmed {cut[0]:.2f}s at start, {cut[1]:.2f}s at end")
            except Exception as e:
                print(f"  [!] cleanup failed for {p['id']} ({e}), keeping it as is")
        dur = duration(wav)
        srt = audio_dir / f"{p['id']}.srt"
        p.update({"audio": f"audio/{wav.name}",
                  "srt": f"audio/{srt.name}" if (used == "edge" and srt.exists()) else None,
                  "start": round(t, 3), "duration": round(dur, 3)})
        concat_lines.append(f"file '{wav.name}'")
        t += dur
        if idx < len(parts) - 1:
            concat_lines.append(f"file '{silence.name}'")
            t += PAUSE

    list_file = audio_dir / "concat.txt"
    list_file.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")

    raw = audio_dir / "narration_raw.wav"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
         "-c:a", "pcm_s16le", str(raw)])

    narration = audio_dir / "narration.mp3"
    cmd = ["ffmpeg", "-y", "-i", str(raw)]
    if DEEP_EQ:
        cmd += ["-af", EQ_FILTER, "-ar", str(SR)]
    cmd += ["-c:a", "libmp3lame", "-b:a", "192k", str(narration)]
    run(cmd)

    manifest = {"engine": used,
                "voice": GEMINI_TTS_VOICE if used == "gemini" else VOICE,
                "deep_eq": DEEP_EQ, "niqqud": NIQQUD, "pause": PAUSE, "clean": CLEAN,
                "total_duration": round(duration(narration), 3),
                "narration": "audio/narration.mp3", "parts": parts}
    (day_dir / "audio.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                        encoding="utf-8")

    print(f"\nEngine used: {used}")
    print(f"Parts: {len(parts)}, total {manifest['total_duration'] / 60:.1f} min")
    print(f"Saved -> {narration}")


if __name__ == "__main__":
    asyncio.run(main())
