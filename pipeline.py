"""
MangaVoice Ultra v5.0 - pipeline.py
Clean rewrite. Stable. Fast. Professional.
"""

import io, json, math, asyncio, logging, textwrap
import zipfile, tempfile, base64, subprocess, shutil, random
from pathlib import Path

import requests
import numpy as np
from PIL import (Image, ImageFilter, ImageEnhance,
                 ImageDraw, ImageFont, ImageChops)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────
QUALITY_DIMS = {"sd": (720, 1280), "hd": (1080, 1920), "4k": (1440, 2560)}
FPS          = 15
MAX_PAGES    = 50

GROQ_MODELS = [
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
    "llama-3.2-11b-vision-preview",
]
GTTS_LANG   = {"en": "en", "hi": "hi"}
EL_VOICES   = {
    ("en","calm"):      "EXAVITQu4vr4xnSDxMaL",
    ("en","dramatic"):  "VR6AewLTigWG4xSOukaG",
    ("en","energetic"): "yoZ06aMxZJJ28mfd3POQ",
    ("en","narrator"):  "onwK4e9ZLuTAKqWW03F9",
    ("en","deep"):      "TxGEqnHWrfWFTfGW9XjX",
    ("en","whisper"):   "XB0fDUnXU5powFXDhCwa",
    ("hi","calm"):      "EXAVITQu4vr4xnSDxMaL",
    ("hi","dramatic"):  "VR6AewLTigWG4xSOukaG",
    ("hi","energetic"): "yoZ06aMxZJJ28mfd3POQ",
    ("hi","narrator"):  "onwK4e9ZLuTAKqWW03F9",
    ("hi","deep"):      "TxGEqnHWrfWFTfGW9XjX",
    ("hi","whisper"):   "XB0fDUnXU5powFXDhCwa",
}

# ─────────────────────────────────────────────
#  FFMPEG HELPERS  ← ALL FIXES ARE HERE
# ─────────────────────────────────────────────

def ffmpeg(*args):
    """
    FIX 1: Log the full command + stderr so errors are never silently blank.
    Previously stderr could be empty (e.g. missing input file, 0-byte audio)
    making the error message show only "FFmpeg failed:" with nothing after it.
    """
    cmd = ["ffmpeg", "-y", "-loglevel", "error"] + [str(a) for a in args]
    logger.debug(f"FFmpeg cmd: {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        # FIX 2: FileNotFoundError means ffmpeg binary is missing entirely.
        # subprocess.run raises this before even running — original code didn't catch it.
        raise RuntimeError(
            "FFmpeg binary not found on this system!\n"
            "Ubuntu/Debian: sudo apt install ffmpeg\n"
            "Mac:           brew install ffmpeg\n"
            "Windows:       https://ffmpeg.org/download.html"
        )
    if r.returncode != 0:
        err = r.stderr.decode(errors="replace").strip()
        logger.error(f"FFmpeg FAILED\nCmd: {' '.join(cmd)}\nStderr: {err}")
        # FIX 3: If stderr is empty, explain WHY it might be blank (input missing, etc.)
        if not err:
            err = (
                "FFmpeg exited with an error but produced no stderr output.\n"
                "Likely causes:\n"
                "  • Input file is missing or 0 bytes\n"
                "  • Frames directory is empty (no images rendered)\n"
                "  • Audio file failed to generate\n"
                f"Command was: {' '.join(cmd)}"
            )
        raise RuntimeError(f"FFmpeg failed:\n{err[-800:]}")


def probe_duration(path: str) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return max(float(r.stdout.decode().strip()), 1.5)
    except Exception:
        return 4.0


def check_ffmpeg():
    """
    FIX 4: Wrap in try/except FileNotFoundError so a missing ffmpeg binary
    gives a clear install message instead of an unhandled exception crash.
    Previously only checked returncode but never caught FileNotFoundError.
    """
    try:
        r = subprocess.run(["ffmpeg", "-version"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode != 0:
            raise RuntimeError(
                "FFmpeg is installed but returned an error on -version check.\n"
                "Try reinstalling: sudo apt install --reinstall ffmpeg"
            )
    except FileNotFoundError:
        raise RuntimeError(
            "FFmpeg not found! Install it first:\n"
            "Ubuntu/Debian: sudo apt install ffmpeg\n"
            "Mac:           brew install ffmpeg\n"
            "Windows:       https://ffmpeg.org/download.html"
        )


def make_silent(path: str, dur: float = 3.0):
    """
    FIX 5: Validate output path before calling ffmpeg.
    If the parent directory doesn't exist, ffmpeg fails silently.
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg("-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
           "-t", str(dur), "-q:a", "9", "-acodec", "libmp3lame", str(out))

# ─────────────────────────────────────────────
#  TTS
# ─────────────────────────────────────────────
def gtts(text: str, lang: str, path: str):
    from gtts import gTTS
    gTTS(text=text, lang=lang, slow=False).save(path)

def elevenlabs(text: str, voice_id: str, key: str, path: str):
    r = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers={"xi-api-key": key, "Content-Type": "application/json"},
        json={"text": text, "model_id": "eleven_multilingual_v2",
              "voice_settings": {"stability": 0.45, "similarity_boost": 0.8}},
        timeout=30)
    r.raise_for_status()
    Path(path).write_bytes(r.content)

# ─────────────────────────────────────────────
#  COLOUR GRADES
# ─────────────────────────────────────────────
def grade(img: Image.Image, style: str) -> Image.Image:
    g = style
    if g == "vivid":
        img = ImageEnhance.Color(img).enhance(1.35)
        img = ImageEnhance.Contrast(img).enhance(1.18)
        img = ImageEnhance.Sharpness(img).enhance(1.2)
    elif g == "muted":
        img = ImageEnhance.Color(img).enhance(0.55)
        img = ImageEnhance.Contrast(img).enhance(0.9)
    elif g == "warm":
        r2, g2, b2 = img.split()
        img = Image.merge("RGB", (r2.point(lambda x: min(255, x+25)),
                                   g2,
                                   b2.point(lambda x: max(0, x-15))))
        img = ImageEnhance.Contrast(img).enhance(1.1)
    elif g == "cold":
        r2, g2, b2 = img.split()
        img = Image.merge("RGB", (r2.point(lambda x: max(0, x-10)),
                                   g2,
                                   b2.point(lambda x: min(255, x+20))))
    elif g == "manga_ink":
        img = ImageEnhance.Color(img).enhance(0.0)
        img = ImageEnhance.Contrast(img).enhance(1.7)
        img = ImageEnhance.Sharpness(img).enhance(2.0)
    elif g == "golden":
        r2, g2, b2 = img.split()
        img = Image.merge("RGB", (r2.point(lambda x: min(255, x+18)),
                                   g2.point(lambda x: min(255, x+6)),
                                   b2.point(lambda x: max(0, x-18))))
        img = ImageEnhance.Contrast(img).enhance(1.12)
    elif g == "cinematic":
        r2, g2, b2 = img.split()
        img = Image.merge("RGB", (r2.point(lambda x: min(255, int(x*1.08)+8)),
                                   g2,
                                   b2.point(lambda x: min(255, int(x*0.88)+12))))
        img = ImageEnhance.Contrast(img).enhance(1.15)
    elif g == "bleach":
        img = ImageEnhance.Contrast(img).enhance(1.35)
        img = ImageEnhance.Color(img).enhance(0.3)
        img = ImageEnhance.Brightness(img).enhance(1.08)
    return img

# ─────────────────────────────────────────────
#  EASING
# ─────────────────────────────────────────────
def ease_smooth(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return t * t * (3 - 2 * t)

def ease_out(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return 1 - (1 - t) ** 2

# ─────────────────────────────────────────────
#  CORE FRAME BUILDER
# ─────────────────────────────────────────────
def build_frame(img: Image.Image,
                frame_w: int, frame_h: int,
                progress: float,
                zoom_amount: float = 0.04,
                zoom_in: bool = True,
                blur_radius: int = 18,
                bg_brightness: float = 0.82) -> Image.Image:
    img = img.convert("RGB")
    src_w, src_h = img.size
    p = ease_smooth(progress)

    bg = img.resize((frame_w, frame_h), Image.BILINEAR)
    bg = bg.filter(ImageFilter.GaussianBlur(radius=max(blur_radius, 6)))
    bg = ImageEnhance.Brightness(bg).enhance(bg_brightness)

    base_scale = frame_h / src_h
    zoom       = 1.0 + zoom_amount * (p if zoom_in else (1 - p))
    scale      = base_scale * zoom

    nw = int(src_w * scale)
    nh = int(src_h * scale)
    fg = img.resize((nw, nh), Image.BILINEAR)

    if nh > frame_h:
        max_scroll = nh - frame_h
        scroll_y   = int(max_scroll * p)
        crop_x     = max(0, (nw - frame_w) // 2)
        crop_w     = min(nw, frame_w)
        fg = fg.crop((crop_x, scroll_y, crop_x + crop_w, scroll_y + frame_h))
        fg_w, fg_h = fg.size
    else:
        fg_w, fg_h = nw, nh

    x = (frame_w - fg_w) // 2
    y = (frame_h - fg_h) // 2
    canvas = bg.copy()
    canvas.paste(fg, (max(0, x), max(0, y)))
    return canvas

# ─────────────────────────────────────────────
#  SUBTITLE RENDERER
# ─────────────────────────────────────────────
_FONT_CACHE: dict = {}

def load_font(size: int) -> ImageFont.FreeTypeFont:
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Windows/Fonts/arialbd.ttf",
    ]
    for p in candidates:
        if Path(p).exists():
            try:
                f = ImageFont.truetype(p, size)
                _FONT_CACHE[size] = f
                return f
            except Exception:
                pass
    f = ImageFont.load_default()
    _FONT_CACHE[size] = f
    return f

def draw_subtitle(frame: Image.Image, text: str,
                  alpha: float, frame_w: int) -> Image.Image:
    if not text.strip() or alpha <= 0:
        return frame
    frame   = frame.copy().convert("RGBA")
    overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    draw    = ImageDraw.Draw(overlay)

    fs      = max(16, int(frame_w * 0.022))
    fnt     = load_font(fs)
    lines   = textwrap.wrap(text.strip(), width=48)
    lh      = int(fs * 1.45)
    total_h = len(lines) * lh
    fw, fh  = frame.size
    y0      = fh - total_h - int(fh * 0.04)

    bar_a = int(160 * alpha)
    draw.rectangle([0, y0 - 8, fw, y0 + total_h + 8],
                   fill=(0, 0, 0, bar_a))

    txt_a = int(255 * alpha)
    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=fnt)
        lw   = bbox[2] - bbox[0]
        x    = (fw - lw) // 2
        y    = y0 + i * lh
        draw.text((x + 1, y + 1), line, font=fnt, fill=(0, 0, 0, int(txt_a * 0.8)))
        draw.text((x, y),         line, font=fnt, fill=(255, 255, 255, txt_a))

    return Image.alpha_composite(frame, overlay).convert("RGB")

# ─────────────────────────────────────────────
#  STYLE FX
# ─────────────────────────────────────────────
def add_vignette(img: Image.Image, strength: float = 0.4) -> Image.Image:
    w, h   = img.size
    mask   = Image.new("L", (w, h), 0)
    d      = ImageDraw.Draw(mask)
    cx, cy = w // 2, h // 2
    for i in range(40, 0, -1):
        a      = int(255 * (1 - (i / 40) ** 0.65) * strength)
        rx, ry = int(cx * i / 40), int(cy * i / 40)
        d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=255 - a)
    return Image.composite(img, Image.new("RGB", (w, h), (0, 0, 0)), mask)

def apply_style(img: Image.Image, anim_style: str,
                progress: float, emotion: str) -> Image.Image:
    if anim_style == "noir":
        img = add_vignette(img, 0.5)
    elif anim_style == "dramatic":
        img = add_vignette(img, 0.32)
    elif anim_style == "retro":
        arr   = np.array(img, dtype=np.int16)
        noise = np.random.randint(-10, 10, arr.shape, dtype=np.int16)
        img   = Image.fromarray(np.clip(arr + noise, 0, 255).astype(np.uint8))
        img   = add_vignette(img, 0.35)
    elif anim_style == "anime":
        glow  = img.filter(ImageFilter.GaussianBlur(radius=3))
        img   = ImageChops.screen(img, ImageEnhance.Brightness(glow).enhance(0.22))
    elif anim_style == "manga":
        img   = img.filter(ImageFilter.SHARPEN)
    if emotion == "action" and progress < 0.04 and anim_style in ("manga", "dramatic"):
        intensity = 0.30 * (1 - progress / 0.04)
        white     = Image.new("RGB", img.size, (255, 255, 255))
        img       = Image.blend(img, white, intensity)
    return img

# ─────────────────────────────────────────────
#  PIPELINE CLASS
# ─────────────────────────────────────────────
class MangaPipeline:

    def __init__(self, bot, chat_id, update_progress,
                 settings: dict, groq_key: str, elevenlabs_key: str):
        self.bot  = bot
        self.cid  = chat_id
        self.upd  = update_progress
        self.s    = settings
        self.gk   = groq_key
        self.ek   = elevenlabs_key
        self.tmp  = Path(tempfile.mkdtemp())

        q          = settings.get("quality", "hd")
        dims       = QUALITY_DIMS.get(q, (1080, 1920))
        self.fw    = dims[0]
        self.fh    = dims[1]

    # ──────────────────────────────────────────
    #  1. DOWNLOAD
    # ──────────────────────────────────────────
    async def _download(self, file_infos: list) -> list:
        await self.upd(5, "Downloading files…")
        out = []
        for i, fi in enumerate(file_infos):
            tf  = await self.bot.get_file(fi["file_id"])
            ext = {"image": "jpg", "pdf": "pdf", "zip": "zip"}.get(fi["type"], "jpg")
            dst = self.tmp / f"raw_{i:03d}.{ext}"
            await tf.download_to_drive(str(dst))
            out.append((dst, fi["type"]))
        return out

    # ──────────────────────────────────────────
    #  2. EXTRACT PAGES
    # ──────────────────────────────────────────
    async def _extract(self, raw: list) -> list[Path]:
        await self.upd(12, "Extracting pages…")
        imgs = []
        for path, ftype in raw:
            if ftype == "image":
                imgs.append(path)
            elif ftype == "pdf":
                imgs.extend(self._pdf(path))
            elif ftype == "zip":
                imgs.extend(self._zip(path))

        valid = []
        for p in sorted(imgs):
            try:
                with Image.open(p) as t:
                    t.verify()
                valid.append(p)
            except Exception:
                pass
        return valid[:MAX_PAGES]

    def _pdf(self, path: Path) -> list[Path]:
        try:
            import fitz
        except ImportError:
            raise ImportError("Install: pip install pymupdf")
        doc = fitz.open(str(path))
        out = []
        for i, pg in enumerate(doc):
            pix = pg.get_pixmap(matrix=fitz.Matrix(2, 2))
            dst = self.tmp / f"pdf_{path.stem}_{i:04d}.jpg"
            pix.save(str(dst))
            out.append(dst)
        doc.close()
        return out

    def _zip(self, path: Path) -> list[Path]:
        ext_dir = self.tmp / f"zip_{path.stem}"
        ext_dir.mkdir(exist_ok=True)
        out = []
        with zipfile.ZipFile(path) as zf:
            names = sorted([
                n for n in zf.namelist()
                if Path(n).suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
                and "__MACOSX" not in n
                and not Path(n).name.startswith(".")
            ])
            for n in names:
                dst = ext_dir / f"{len(out):04d}{Path(n).suffix}"
                dst.write_bytes(zf.read(n))
                out.append(dst)
        return out

    # ──────────────────────────────────────────
    #  3. AI NARRATION
    # ──────────────────────────────────────────
    async def _narrate(self, images: list[Path]) -> list[dict]:
        await self.upd(20, "AI reading your manga…")

        key = (self.gk or "").strip()
        if not key:
            raise ValueError("GROQ_API_KEY empty in config.json! Get free key: console.groq.com")
        if not key.startswith("gsk_"):
            raise ValueError(f"GROQ_API_KEY wrong format ({key[:12]}…). Must start with gsk_")

        lang_inst = ("Respond ONLY in Hindi (Devanagari script)."
                     if self.s.get("lang") == "hi"
                     else "Respond ONLY in English.")
        tone_inst = {
            "calm":     "Use a smooth, contemplative story-teller tone.",
            "dramatic": "Use an intense cinematic narrator tone — short, punchy, dramatic.",
            "energetic":"Use excited anime-dub energy — exclamations, fast-paced.",
            "narrator": "Use a deep documentary narrator tone.",
            "deep":     "Use a serious, authoritative, powerful tone.",
            "whisper":  "Use a tense, quiet, suspenseful whisper tone.",
        }.get(self.s.get("voice", "calm"), "Use a smooth story-teller tone.")

        BATCH = 4
        batches   = [images[i:i+BATCH] for i in range(0, len(images), BATCH)]
        all_panels: list[dict] = []

        for b_idx, batch in enumerate(batches):
            offset = b_idx * BATCH
            pct    = 20 + int((b_idx / len(batches)) * 22)
            await self.upd(pct, f"Analysing pages {offset+1}–{offset+len(batch)}/{len(images)}…")

            parts = []
            for p in batch:
                img = Image.open(p).convert("RGB")
                img.thumbnail((448, 448), Image.BILINEAR)
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=45, optimize=True)
                b64 = base64.b64encode(buf.getvalue()).decode()
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
                })

            parts.append({"type": "text", "text": (
                f"You are a professional manga narrator. {lang_inst} {tone_inst}\n"
                f"These are manga pages {offset+1} to {offset+len(batch)}.\n"
                "Return ONLY a JSON array — no markdown, no explanation.\n"
                f"Exactly {len(batch)} objects, one per image:\n"
                '[{"panel":1,"narration":"2-3 vivid spoken sentences",'
                '"subtitle":"max 7 words","emotion":"action|mystery|romance|comedy|tragedy|calm",'
                '"is_action":false}]\n'
                "Set is_action:true for fights, explosions, intense moments."
            )})

            payload = {
                "messages": [{"role": "user", "content": parts}],
                "temperature": 0.7,
                "max_tokens": 900,
            }

            resp      = None
            last_err  = "unknown"

            for model in GROQ_MODELS:
                for attempt in range(3):
                    try:
                        r = await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda m=model: requests.post(
                                "https://api.groq.com/openai/v1/chat/completions",
                                json={**payload, "model": m},
                                headers={
                                    "Authorization": f"Bearer {key}",
                                    "Content-Type": "application/json"
                                },
                                timeout=90
                            )
                        )
                        if r.status_code == 200:
                            resp = r
                            break
                        elif r.status_code == 401:
                            raise ValueError("Groq key rejected! Get fresh key at console.groq.com")
                        elif r.status_code == 429:
                            await self.upd(pct, "Rate limit — waiting 20s…")
                            await asyncio.sleep(20)
                        else:
                            last_err = f"{r.status_code}: {r.text[:100]}"
                            break
                    except ValueError:
                        raise
                    except Exception as e:
                        last_err = str(e)[:80]
                        await asyncio.sleep(4)
                if resp:
                    break

            batch_panels: list[dict] = []
            if resp:
                try:
                    raw = resp.json()["choices"][0]["message"]["content"].strip()
                    raw = raw.lstrip("```json").lstrip("```").rstrip("```").strip()
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        batch_panels = parsed
                except Exception:
                    pass

            while len(batch_panels) < len(batch):
                i = len(batch_panels)
                batch_panels.append({
                    "panel":     offset + i + 1,
                    "narration": f"The story continues on page {offset + i + 1}.",
                    "subtitle":  f"Page {offset + i + 1}",
                    "emotion":   "calm",
                    "is_action": False,
                })

            all_panels.extend(batch_panels[:len(batch)])

            if b_idx < len(batches) - 1:
                await asyncio.sleep(2)

        return all_panels

    # ──────────────────────────────────────────
    #  4. TTS
    # ──────────────────────────────────────────
    async def _tts(self, panels: list[dict]) -> list[Path]:
        await self.upd(44, "Generating voice narration…")
        loop  = asyncio.get_event_loop()
        lang  = self.s.get("lang", "en")
        voice = self.s.get("voice", "calm")
        paths = []

        for i, panel in enumerate(panels):
            text = (panel.get("narration") or f"Page {i+1}.").strip()
            dst  = self.tmp / f"audio_{i:04d}.mp3"
            try:
                if self.ek:
                    vid = EL_VOICES.get((lang, voice), "EXAVITQu4vr4xnSDxMaL")
                    await loop.run_in_executor(
                        None, lambda t=text, v=vid, d=str(dst): elevenlabs(t, v, self.ek, d))
                else:
                    lc = GTTS_LANG.get(lang, "en")
                    await loop.run_in_executor(
                        None, lambda t=text, l=lc, d=str(dst): gtts(t, l, d))
            except Exception as e:
                logger.warning(f"TTS panel {i} failed: {e}. Trying gTTS fallback.")
                try:
                    lc = GTTS_LANG.get(lang, "en")
                    await loop.run_in_executor(
                        None, lambda t=text, l=lc, d=str(dst): gtts(t, l, d))
                except Exception as e2:
                    logger.warning(f"gTTS fallback also failed: {e2}. Using silent audio.")
                    make_silent(str(dst), 3.0)

            # FIX 6: Validate audio file exists and is non-empty after TTS.
            # A 0-byte audio file causes FFmpeg to fail silently during merge.
            if not dst.exists() or dst.stat().st_size == 0:
                logger.warning(f"Audio file missing/empty for panel {i}, generating silence.")
                make_silent(str(dst), 3.0)

            paths.append(dst)

        return paths

    # ──────────────────────────────────────────
    #  5. RENDER ONE PANEL → MP4 clip
    # ──────────────────────────────────────────
    def _render_clip(self, src_img: Image.Image, audio: Path,
                     panel: dict, idx: int) -> Path:
        s          = self.s
        anim_style = s.get("style", "cinematic")
        clr_grade  = s.get("color_grade", "vivid")
        show_sub   = bool(s.get("subtitles", 1))
        spd_map    = {"slow": 1.5, "normal": 1.0, "fast": 0.60}
        spd        = spd_map.get(s.get("speed", "normal"), 1.0)
        quality    = s.get("quality", "hd")
        bitrate    = {"sd": "2000k", "hd": "4000k", "4k": "10000k"}.get(quality, "4000k")
        blur_r     = int(s.get("blur_radius", 18))
        bg_bright  = float(s.get("blur_brightness", 0.82))
        zoom_amt   = float(s.get("zoom_amount", 0.04))
        emotion    = panel.get("emotion", "calm")
        subtitle   = panel.get("subtitle", "") if show_sub else ""
        zoom_in    = (idx % 2 == 0)

        dur      = min(probe_duration(str(audio)) * spd, 9.0)
        dur      = max(dur, 2.5)
        n_frames = max(1, int(dur * FPS))

        src = grade(src_img.convert("RGB"), clr_grade)

        fdir = self.tmp / f"frames_{idx:04d}"
        fdir.mkdir(parents=True, exist_ok=True)

        for fi in range(n_frames):
            progress  = fi / max(n_frames - 1, 1)
            fade_in   = min(fi / (FPS * 0.4 + 1), 1.0)
            fade_out  = min((n_frames - fi) / (FPS * 0.4 + 1), 1.0)
            sub_alpha = min(fade_in, fade_out)

            frame = build_frame(
                src, self.fw, self.fh, progress,
                zoom_amount=zoom_amt,
                zoom_in=zoom_in,
                blur_radius=blur_r,
                bg_brightness=bg_bright
            )
            frame = apply_style(frame, anim_style, progress, emotion)

            if subtitle and sub_alpha > 0:
                frame = draw_subtitle(frame, subtitle, sub_alpha, self.fw)

            frame.save(str(fdir / f"f{fi:07d}.jpg"), "JPEG", quality=88)

        # FIX 7: Verify frames were actually written before calling FFmpeg.
        # If 0 frames exist, FFmpeg fails with empty stderr.
        written = list(fdir.glob("f*.jpg"))
        if not written:
            raise RuntimeError(
                f"No frames were rendered for panel {idx}. "
                "Image may be corrupt or frame dimensions are invalid."
            )

        silent = self.tmp / f"silent_{idx:04d}.mp4"
        ffmpeg(
            "-framerate", str(FPS),
            "-i", str(fdir / "f%07d.jpg"),
            "-c:v", "libx264", "-preset", "ultrafast",
            "-b:v", bitrate, "-pix_fmt", "yuv420p",
            "-t", str(dur),
            str(silent)
        )
        shutil.rmtree(str(fdir), ignore_errors=True)

        # FIX 8: Verify silent video was created before merging with audio.
        if not silent.exists() or silent.stat().st_size == 0:
            raise RuntimeError(
                f"Silent video for panel {idx} was not created by FFmpeg. "
                "Check FFmpeg installation and codec support (libx264)."
            )

        clip = self.tmp / f"clip_{idx:04d}.mp4"
        ffmpeg(
            "-i", str(silent), "-i", str(audio),
            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
            "-shortest", str(clip)
        )
        silent.unlink(missing_ok=True)
        return clip

    # ──────────────────────────────────────────
    #  6. CONCATENATE ALL CLIPS
    # ──────────────────────────────────────────
    def _concat(self, clips: list[Path]) -> Path:
        # FIX 9: Filter out any clips that failed / are missing before concat.
        # A missing clip in concat.txt causes FFmpeg to fail with empty stderr.
        valid_clips = [c for c in clips if c.exists() and c.stat().st_size > 0]
        if not valid_clips:
            raise RuntimeError("No valid video clips to concatenate. All panels failed to render.")
        if len(valid_clips) < len(clips):
            logger.warning(f"{len(clips) - len(valid_clips)} clip(s) missing — skipping them.")

        concat_file = self.tmp / "concat.txt"
        with open(concat_file, "w") as f:
            for c in valid_clips:
                f.write(f"file '{c.resolve()}'\n")

        quality = self.s.get("quality", "hd")
        bitrate = {"sd": "2000k", "hd": "4000k", "4k": "10000k"}.get(quality, "4000k")
        output  = self.tmp / "final.mp4"

        ffmpeg(
            "-f", "concat", "-safe", "0",
            "-i", str(concat_file),
            "-c:v", "libx264", "-preset", "ultrafast",
            "-b:v", bitrate, "-c:a", "aac", "-b:a", "128k",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(output)
        )
        return output

    # ──────────────────────────────────────────
    #  ENTRY POINT
    # ──────────────────────────────────────────
    async def run(self, file_infos: list) -> tuple[str, int]:
        check_ffmpeg()

        raw    = await self._download(file_infos)
        images = await self._extract(raw)

        if not images:
            raise ValueError(
                "No valid images found!\n"
                "Send JPG/PNG/WEBP images, a PDF, or a ZIP of manga pages.")

        panels = await self._narrate(images)
        n      = min(len(images), len(panels))
        images = images[:n]
        panels = panels[:n]
        audios = await self._tts(panels)

        await self.upd(65, f"Preparing {n} pages…")
        loop   = asyncio.get_event_loop()
        loaded = []
        for p in images:
            img = await loop.run_in_executor(
                None, lambda _p=p: Image.open(_p).convert("RGB"))
            loaded.append(img)

        clips = []
        for i, (img, audio, panel) in enumerate(zip(loaded, audios, panels)):
            pct   = 67 + int((i / n) * 25)
            label = f"Rendering page {i+1}/{n}…"
            await self.upd(pct, label)
            clip = await loop.run_in_executor(
                None,
                lambda b=img, a=audio, p=panel, idx=i: self._render_clip(b, a, p, idx)
            )
            clips.append(clip)

        await self.upd(93, "Merging video…")
        final = await loop.run_in_executor(None, lambda: self._concat(clips))
        await self.upd(100, "Done!")
        return str(final), n
