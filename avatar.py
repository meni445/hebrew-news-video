import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

import requests
from PIL import Image

ENABLED = os.getenv("AVATAR_ENABLED", "0") == "1"                 # off by default
IMAGE = Path(os.getenv("AVATAR_IMAGE", "assets/presenter.jpg"))
WIDTH = int(os.getenv("AVATAR_WIDTH", "540"))
CHIN_PAD = float(os.getenv("AVATAR_CHIN_PAD", "0.15"))            # extend face box down to include the chin
W2L_DIR = Path(os.getenv("WAV2LIP_DIR", "models/Wav2Lip")).resolve()
W2L_REPO = os.getenv("WAV2LIP_REPO", "https://github.com/Rudrabha/Wav2Lip.git")
MODEL = Path(os.getenv("WAV2LIP_MODEL", "models/wav2lip_gan.pth")).resolve()
MODEL_URLS = [u.strip() for u in os.getenv(
    "WAV2LIP_MODEL_URLS",
    "https://huggingface.co/camenduru/Wav2Lip/resolve/main/checkpoints/wav2lip_gan.pth,"
    "https://huggingface.co/numz/wav2lip_studio/resolve/main/Wav2lip/wav2lip_gan.pth"
).split(",") if u.strip()]
MIN_MODEL_BYTES = 300_000_000


class AvatarError(Exception):
    pass


def run(cmd, cwd=None, timeout=3600):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0, (r.stdout + r.stderr)[-2500:]


def latest_day_dir():
    days = sorted(p for p in Path("daily").glob("*") if (p / "audio.json").exists())
    if not days:
        sys.exit("No daily/*/audio.json found - run tts.py first")
    return days[-1]


def ensure_repo():
    if not (W2L_DIR / "inference.py").exists():
        print("Cloning Wav2Lip...")
        ok, log = run(["git", "clone", "--depth", "1", W2L_REPO, str(W2L_DIR)])
        if not ok:
            raise AvatarError(f"clone failed:\n{log}")
    # patch 1: newer librosa needs keyword arguments
    audio_py = W2L_DIR / "audio.py"
    t = audio_py.read_text(encoding="utf-8")
    t2 = t.replace("librosa.filters.mel(hp.sample_rate, hp.n_fft,",
                   "librosa.filters.mel(sr=hp.sample_rate, n_fft=hp.n_fft,")
    if t2 != t:
        audio_py.write_text(t2, encoding="utf-8")
    # patch 2: newer torch defaults to weights_only=True
    inf_py = W2L_DIR / "inference.py"
    t = inf_py.read_text(encoding="utf-8")
    if "weights_only" not in t:
        inf_py.write_text(t.replace("torch.load(checkpoint_path",
                                    "torch.load(checkpoint_path, weights_only=False"), encoding="utf-8")
    (W2L_DIR / "temp").mkdir(exist_ok=True)


def ensure_model():
    if MODEL.exists() and MODEL.stat().st_size > MIN_MODEL_BYTES:
        return
    MODEL.parent.mkdir(parents=True, exist_ok=True)
    for url in MODEL_URLS:
        print(f"Downloading Wav2Lip model from {url} ...")
        tmp = MODEL.with_suffix(".part")
        try:
            with requests.get(url, stream=True, timeout=900) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            if tmp.stat().st_size > MIN_MODEL_BYTES:
                tmp.rename(MODEL)
                return
            print(f"  file too small ({tmp.stat().st_size} bytes), trying next")
        except Exception as e:
            print(f"  failed: {e}")
        tmp.unlink(missing_ok=True)
    raise AvatarError("model download failed from all URLs")


def face_box(img_path):
    import cv2
    img = cv2.imread(str(img_path))
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(80, 80))
    if len(faces) == 0:
        raise AvatarError("no face found in the image")
    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
    ih, iw = gray.shape
    y1 = max(0, int(y))
    y2 = min(ih, int(y + h + h * CHIN_PAD))
    x1 = max(0, int(x))
    x2 = min(iw, int(x + w))
    return y1, y2, x1, x2


def still_video(src, out, seconds):
    ok, log = run(["ffmpeg", "-y", "-loop", "1", "-i", str(src), "-t", f"{seconds:.2f}", "-r", "25",
                   "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(out)])
    if not ok:
        print(f"[!] still video failed too:\n{log}")


def lip_sync(day_dir, src, narration, out):
    box = face_box(src)
    print(f"Face box (top, bottom, left, right): {box}")

    wav = day_dir / "avatar_audio.wav"
    ok, log = run(["ffmpeg", "-y", "-i", str(narration), "-ar", "16000", "-ac", "1", str(wav)])
    if not ok:
        raise AvatarError(f"audio conversion failed:\n{log}")

    ensure_repo()
    ensure_model()

    raw = day_dir / "avatar_raw.mp4"
    print("Running Wav2Lip (CPU)...")
    cmd = [sys.executable, "inference.py",
           "--checkpoint_path", str(MODEL),
           "--face", str(src.resolve()),
           "--audio", str(wav.resolve()),
           "--outfile", str(raw.resolve()),
           "--box", *[str(v) for v in box]]
    ok, log = run(cmd, cwd=W2L_DIR, timeout=5400)
    if not ok or not raw.exists():
        raise AvatarError(f"Wav2Lip error:\n{log}")

    ok, log = run(["ffmpeg", "-y", "-i", str(raw), "-an", "-c:v", "libx264", "-preset", "veryfast",
                   "-crf", "20", "-pix_fmt", "yuv420p", str(out)])
    if not ok:
        raise AvatarError(f"final encode failed:\n{log}")


def main():
    if not ENABLED:
        print("AVATAR_ENABLED is off - skipping")
        return
    if not IMAGE.exists():
        print(f"No presenter image at {IMAGE} - skipping")
        return

    day_dir = latest_day_dir()
    audio = json.loads((day_dir / "audio.json").read_text(encoding="utf-8"))
    narration = day_dir / audio["narration"]
    total = audio["total_duration"]
    out = day_dir / "avatar.mp4"

    # smaller copy of the image with even dimensions
    src = day_dir / "avatar_src.jpg"
    img = Image.open(IMAGE).convert("RGB")
    if img.width > WIDTH:
        img = img.resize((WIDTH, int(img.height * WIDTH / img.width)), Image.LANCZOS)
    img = img.crop((0, 0, img.width - img.width % 2, img.height - img.height % 2))
    img.save(src, quality=95)

    try:
        lip_sync(day_dir, src, narration, out)
        print(f"Saved -> {out}")
    except Exception as e:
        if not isinstance(e, AvatarError):
            traceback.print_exc()
        print(f"[!] lip-sync failed ({str(e)[:2000]}) - using a still image of the presenter")
        still_video(src, out, total)


if __name__ == "__main__":
    main()
