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
_video_env = os.getenv("AVATAR_VIDEO", "").strip()
VIDEO = Path(_video_env) if _video_env else None                   # optional idle clip of the presenter
WIDTH = int(os.getenv("AVATAR_WIDTH", "540"))
DETECTOR = os.getenv("AVATAR_DETECTOR", "haar")                    # haar | s3fd (Wav2Lip's own detector)
MOTION = os.getenv("AVATAR_MOTION", "0") == "1"                    # gentle head drift for still images
CHIN_PAD = float(os.getenv("AVATAR_CHIN_PAD", "0.15"))            # haar only: extend box down to the chin
PADS = os.getenv("WAV2LIP_PADS", "0 10 0 0").split()               # top bottom left right
MAX_IDLE_SEC = float(os.getenv("AVATAR_IDLE_MAX_SEC", "20"))
W2L_DIR = Path(os.getenv("WAV2LIP_DIR", "models/Wav2Lip")).resolve()
W2L_REPO = os.getenv("WAV2LIP_REPO", "https://github.com/Rudrabha/Wav2Lip.git")
MODEL = Path(os.getenv("WAV2LIP_MODEL", "models/wav2lip_gan.pth")).resolve()
MODEL_URLS = [u.strip() for u in os.getenv(
    "WAV2LIP_MODEL_URLS",
    "https://github.com/justinjohn0306/Wav2Lip/releases/download/models/wav2lip_gan.pth"
).split(",") if u.strip()]
S3FD_URL = os.getenv("S3FD_URL", "https://www.adrianbulat.com/downloads/python-fan/s3fd-619a316812.pth")
MIN_MODEL_BYTES = 300_000_000
MIN_S3FD_BYTES = 50_000_000


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


def download(url, dest, min_bytes):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    try:
        with requests.get(url, stream=True, timeout=900) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        if tmp.stat().st_size > min_bytes:
            tmp.rename(dest)
            return True
        print(f"  file too small ({tmp.stat().st_size} bytes)")
    except Exception as e:
        print(f"  download failed: {e}")
    tmp.unlink(missing_ok=True)
    return False


def ensure_repo():
    if not (W2L_DIR / "inference.py").exists():
        print("Cloning Wav2Lip...")
        ok, log = run(["git", "clone", "--depth", "1", W2L_REPO, str(W2L_DIR)])
        if not ok:
            raise AvatarError(f"clone failed:\n{log}")
    patches = [
        (W2L_DIR / "audio.py", "librosa.filters.mel(hp.sample_rate, hp.n_fft,",
         "librosa.filters.mel(sr=hp.sample_rate, n_fft=hp.n_fft,"),
        (W2L_DIR / "face_detection" / "utils.py", "dtype=np.int)", "dtype=int)"),
    ]
    for path, old, new in patches:
        t = path.read_text(encoding="utf-8")
        if old in t:
            path.write_text(t.replace(old, new), encoding="utf-8")
    inf_py = W2L_DIR / "inference.py"
    t = inf_py.read_text(encoding="utf-8")
    if "weights_only" not in t:
        inf_py.write_text(t.replace("torch.load(checkpoint_path",
                                    "torch.load(checkpoint_path, weights_only=False"), encoding="utf-8")
    (W2L_DIR / "temp").mkdir(exist_ok=True)


def ensure_model():
    if MODEL.exists() and MODEL.stat().st_size > MIN_MODEL_BYTES:
        return
    for url in MODEL_URLS:
        print(f"Downloading Wav2Lip model from {url} ...")
        if download(url, MODEL, MIN_MODEL_BYTES):
            return
    raise AvatarError("model download failed from all URLs")


def ensure_s3fd():
    dest = W2L_DIR / "face_detection" / "detection" / "sfd" / "s3fd.pth"
    if dest.exists() and dest.stat().st_size > MIN_S3FD_BYTES:
        return
    print(f"Downloading face detector from {S3FD_URL} ...")
    if not download(S3FD_URL, dest, MIN_S3FD_BYTES):
        raise AvatarError("face detector download failed")


def haar_box(img_path):
    import cv2
    img = cv2.imread(str(img_path))
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(80, 80))
    if len(faces) == 0:
        raise AvatarError("no face found in the image")
    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
    ih, iw = gray.shape
    return (max(0, int(y)), min(ih, int(y + h + h * CHIN_PAD)), max(0, int(x)), min(iw, int(x + w)))


def prepare_image(src_img, dest):
    img = Image.open(src_img).convert("RGB")
    if img.width > WIDTH:
        img = img.resize((WIDTH, int(img.height * WIDTH / img.width)), Image.LANCZOS)
    img = img.crop((0, 0, img.width - img.width % 2, img.height - img.height % 2))
    img.save(dest, quality=95)


def prepare_video(src_video, dest):
    """Scale, 25 fps, and play forward + backward so the loop has no jump."""
    graph = (f"[0:v]scale={WIDTH}:-2,fps=25,setsar=1,split[a][b];"
             f"[b]reverse[r];[a][r]concat=n=2:v=1:a=0,format=yuv420p[v]")
    ok, log = run(["ffmpeg", "-y", "-t", str(MAX_IDLE_SEC), "-i", str(src_video), "-an",
                   "-filter_complex", graph, "-map", "[v]",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", str(dest)])
    if not ok:
        raise AvatarError(f"idle video preparation failed:\n{log}")


def motion_vf():
    """Slow, subtle drift so a still portrait does not look frozen."""
    return ("scale=trunc(iw*1.08/2)*2:trunc(ih*1.08/2)*2,"
            "crop=trunc(iw/1.08/2)*2:trunc(ih/1.08/2)*2:"
            "x='(iw-ow)/2*(1+0.6*sin(t*0.45))':y='(ih-oh)/2*(1+0.5*sin(t*0.31+1))',"
            "format=yuv420p")


def still_video(src, out, seconds):
    vf = motion_vf() if MOTION else "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p"
    ok, log = run(["ffmpeg", "-y", "-loop", "1", "-i", str(src), "-t", f"{seconds:.2f}", "-r", "25",
                   "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(out)])
    if not ok:
        print(f"[!] still video failed too:\n{log}")


def lip_sync(day_dir, face_src, is_video, narration, out):
    wav = day_dir / "avatar_audio.wav"
    ok, log = run(["ffmpeg", "-y", "-i", str(narration), "-ar", "16000", "-ac", "1", str(wav)])
    if not ok:
        raise AvatarError(f"audio conversion failed:\n{log}")

    ensure_repo()
    ensure_model()

    raw = day_dir / "avatar_raw.mp4"
    cmd = [sys.executable, "inference.py",
           "--checkpoint_path", str(MODEL),
           "--face", str(face_src.resolve()),
           "--audio", str(wav.resolve()),
           "--outfile", str(raw.resolve()),
           "--pads", *PADS]
    if is_video or DETECTOR == "s3fd":
        ensure_s3fd()
        print("Face detection: Wav2Lip s3fd")
    else:
        box = haar_box(face_src)
        print(f"Face detection: haar box (top, bottom, left, right) {box}")
        cmd += ["--box", *[str(v) for v in box]]

    print(f"Running Wav2Lip (CPU) with {MODEL.name}...")
    ok, log = run(cmd, cwd=W2L_DIR, timeout=5400)
    if not ok or not raw.exists():
        raise AvatarError(f"Wav2Lip error:\n{log}")

    vf = motion_vf() if (MOTION and not is_video) else "format=yuv420p"
    ok, log = run(["ffmpeg", "-y", "-i", str(raw), "-an", "-vf", vf, "-c:v", "libx264",
                   "-preset", "veryfast", "-crf", "20", str(out)])
    if not ok:
        raise AvatarError(f"final encode failed:\n{log}")


def main():
    if not ENABLED:
        print("AVATAR_ENABLED is off - skipping")
        return
    has_video = VIDEO is not None and VIDEO.is_file()
    if not has_video and not IMAGE.is_file():
        print(f"No presenter image ({IMAGE}) or idle video - skipping")
        return

    day_dir = latest_day_dir()
    audio = json.loads((day_dir / "audio.json").read_text(encoding="utf-8"))
    narration = day_dir / audio["narration"]
    total = audio["total_duration"]
    out = day_dir / "avatar.mp4"
    still_src = day_dir / "avatar_src.jpg"

    try:
        if has_video:
            face_src = day_dir / "avatar_src.mp4"
            prepare_video(VIDEO, face_src)
            run(["ffmpeg", "-y", "-i", str(face_src), "-frames:v", "1", str(still_src)])
        else:
            prepare_image(IMAGE, still_src)
            face_src = still_src
        print(f"Avatar source: {'idle video ' + str(VIDEO) if has_video else 'image ' + str(IMAGE)}")
        lip_sync(day_dir, face_src, has_video, narration, out)
        print(f"Saved -> {out}")
    except Exception as e:
        if not isinstance(e, AvatarError):
            traceback.print_exc()
        print(f"[!] lip-sync failed ({str(e)[:2000]}) - using a still image of the presenter")
        if not still_src.exists() and IMAGE.is_file():
            prepare_image(IMAGE, still_src)
        if still_src.exists():
            still_video(still_src, out, total)


if __name__ == "__main__":
    main()
