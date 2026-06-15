"""
pipeline.py — MangaVoice Ultra v2.1 (Stable FFmpeg Edition)
============================================================
Fixes "Response ended prematurely" by using direct FFmpeg
subprocess calls instead of MoviePy's fragile write_videofile.

Steps:
  1. Download files from Telegram
  2. Extract pages (images / PDF / ZIP)
  3. Claude AI → narration JSON per panel
  4. TTS → MP3 per panel
  5. Render each panel as image sequence → MP4 via FFmpeg
  6. Concatenate all panel clips → final video
  7. Return output path
"""

import os, io, json, time, asyncio, logging, textwrap
import zipfile, tempfile, base64, math, random, subprocess
from pathlib import Path

import requests
import numpy as np
from PIL import (
    Image, ImageFilter, ImageEnhance, ImageDraw,
    ImageFont, ImageOps, ImageChops
)

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════
QUALITY_DIMS = {
    "sd":  (854,  480),
    "hd":  (1280, 720),
    "4k":  (1920, 1080),
}
FPS = 24

ELEVENLABS_VOICES = {
    ("en","calm"):      "EXAVITQu4vr4xnSDxMaL",
    ("en","dramatic"):  "VR6AewLTigWG4xSOukaG",
    ("en","energetic"): "yoZ06aMxZJJ28mfd3POQ",
    ("hi","calm"):      "EXAVITQu4vr4xnSDxMaL",
    ("hi","dramatic"):  "VR6AewLTigWG4xSOukaG",
    ("hi","energetic"): "yoZ06aMxZJJ28mfd3POQ",
}
GTTS_LANG = {"en": "en", "hi": "hi"}

# ══════════════════════════════════════════════════════════════════════
# FFMPEG HELPERS
# ══════════════════════════════════════════════════════════════════════
def ffmpeg(*args, check=True) -> subprocess.CompletedProcess:
    """Run ffmpeg quietly. Raises on error."""
    cmd = ["ffmpeg", "-y"] + list(args)
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        err = result.stderr.decode(errors="replace")[-800:]
        raise RuntimeError(f"FFmpeg failed:\n{err}")
    return result

def ffprobe_duration(path: str) -> float:
    """Get audio/video duration via ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        return float(result.stdout.decode().strip())
    except Exception:
        return 4.0

def check_ffmpeg():
    r = subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError(
            "FFmpeg not found!\n"
            "Install it:\n"
            "  Ubuntu/Debian: sudo apt install ffmpeg\n"
            "  macOS: brew install ffmpeg\n"
            "  Windows: https://ffmpeg.org/download.html"
        )

# ══════════════════════════════════════════════════════════════════════
# TTS
# ══════════════════════════════════════════════════════════════════════
def tts_gtts(text: str, lang: str, path: str):
    from gtts import gTTS
    gTTS(text=text, lang=lang, slow=False).save(path)

def tts_elevenlabs(text: str, voice_id: str, api_key: str, path: str):
    r = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers={"xi-api-key": api_key, "Content-Type": "application/json"},
        json={"text": text, "model_id": "eleven_multilingual_v2",
              "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}}
    )
    r.raise_for_status()
    with open(path, "wb") as f:
        f.write(r.content)

# ══════════════════════════════════════════════════════════════════════
# IMAGE PROCESSING
# ══════════════════════════════════════════════════════════════════════
def smart_fill_frame(img: Image.Image, tw: int, th: int,
                     color_grade: str = "vivid") -> Image.Image:
    """
    Fill entire frame — crop-to-fill, NO black bars, NO blur.
    Scale so image covers target, then centre-crop.
    """
    img  = img.convert("RGB")
    sw, sh = img.size
    scale  = max(tw / sw, th / sh)
    nw, nh = int(sw * scale), int(sh * scale)
    img    = img.resize((nw, nh), Image.LANCZOS)
    left   = (nw - tw) // 2
    top    = (nh - th) // 2
    img    = img.crop((left, top, left + tw, top + th))
    return apply_color_grade(img, color_grade)


def apply_color_grade(img: Image.Image, grade: str) -> Image.Image:
    if grade == "vivid":
        img = ImageEnhance.Color(img).enhance(1.35)
        img = ImageEnhance.Contrast(img).enhance(1.15)
        img = ImageEnhance.Sharpness(img).enhance(1.2)
    elif grade == "muted":
        img = ImageEnhance.Color(img).enhance(0.6)
        img = ImageEnhance.Contrast(img).enhance(0.9)
    elif grade == "warm":
        r, g, b = img.split()
        r = r.point(lambda x: min(255, x + 25))
        b = b.point(lambda x: max(0,   x - 15))
        img = Image.merge("RGB", (r, g, b))
        img = ImageEnhance.Contrast(img).enhance(1.1)
    elif grade == "cold":
        r, g, b = img.split()
        b = b.point(lambda x: min(255, x + 20))
        r = r.point(lambda x: max(0,   x - 10))
        img = Image.merge("RGB", (r, g, b))
    elif grade == "manga_ink":
        img = ImageEnhance.Color(img).enhance(0.0)
        img = ImageEnhance.Contrast(img).enhance(1.6)
        img = ImageEnhance.Sharpness(img).enhance(2.0)
    return img


def ease_in_out(t: float) -> float:
    return t * t * (3 - 2 * t)


def render_frame(base: Image.Image, progress: float,
                 style: str, zoom_in: bool,
                 tw: int, th: int,
                 subtitle: str, sub_alpha: float) -> Image.Image:
    """Render a single animation frame."""
    p  = ease_in_out(min(max(progress, 0), 1))
    img = base.copy()
    w, h = img.size

    # Ken Burns parameters per style
    if style == "cinematic":
        scale = (1.0 + 0.08 * p) if zoom_in else (1.08 - 0.08 * p)
        px = int(w * 0.02 * p) * (1 if zoom_in else -1)
        py = int(h * 0.015 * p)
    elif style == "manga":
        burst = min(p * 2, 1.0)
        scale = 1.0 + 0.12 * burst
        px = py = 0
    elif style == "noir":
        scale = 1.0 + 0.06 * p
        px = int(w * 0.03 * p)
        py = 0
    elif style == "retro":
        scale = 1.0 + 0.05 * p
        px = int(w * 0.01 * math.sin(p * math.pi))
        py = 0
    elif style == "anime":
        scale = 1.0 + 0.07 * p
        px = py = 0
    else:
        scale = 1.0 + 0.07 * p
        px = py = 0

    # Scale
    nw, nh = int(w * scale), int(h * scale)
    img = img.resize((nw, nh), Image.LANCZOS)
    cx = max(0, min((nw - w) // 2 + px, nw - w))
    cy = max(0, min((nh - h) // 2 + py, nh - h))
    img = img.crop((cx, cy, cx + w, cy + h))

    # Style FX
    if style == "noir":
        img = _vignette(img, 0.55)
    elif style == "retro":
        arr   = np.array(img, dtype=np.int16)
        grain = np.random.randint(-12, 12, arr.shape, dtype=np.int16)
        arr   = np.clip(arr + grain, 0, 255).astype(np.uint8)
        img   = Image.fromarray(arr)
        img   = _vignette(img, 0.4)
    elif style == "anime":
        glow  = img.filter(ImageFilter.GaussianBlur(radius=3))
        img   = ImageChops.screen(img, ImageEnhance.Brightness(glow).enhance(0.4))
    elif style == "manga":
        img   = img.filter(ImageFilter.SHARPEN)

    # Subtitles
    if subtitle and sub_alpha > 0:
        img = burn_subtitle(img, subtitle, sub_alpha, style, tw)

    return img


def _vignette(img: Image.Image, strength: float = 0.5) -> Image.Image:
    w, h   = img.size
    mask   = Image.new("L", (w, h), 0)
    draw   = ImageDraw.Draw(mask)
    cx, cy = w // 2, h // 2
    for i in range(40, 0, -1):
        alpha = int(255 * (1 - (i / 40) ** 0.6) * strength)
        rx = int(cx * i / 40)
        ry = int(cy * i / 40)
        draw.ellipse([cx-rx, cy-ry, cx+rx, cy+ry], fill=255 - alpha)
    black = Image.new("RGB", (w, h), (0, 0, 0))
    return Image.composite(img, black, mask)


# ══════════════════════════════════════════════════════════════════════
# SUBTITLE RENDERER
# ══════════════════════════════════════════════════════════════════════
FONT_CACHE = {}

def _load_font(size: int):
    if size in FONT_CACHE:
        return FONT_CACHE[size]
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Windows/Fonts/arialbd.ttf",
    ]:
        if Path(path).exists():
            try:
                fnt = ImageFont.truetype(path, size)
                FONT_CACHE[size] = fnt
                return fnt
            except Exception:
                pass
    fnt = ImageFont.load_default()
    FONT_CACHE[size] = fnt
    return fnt


def burn_subtitle(frame: Image.Image, text: str,
                  alpha: float, style: str, tw: int) -> Image.Image:
    if not text.strip() or alpha <= 0:
        return frame
    frame   = frame.copy().convert("RGBA")
    overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    draw    = ImageDraw.Draw(overlay)
    fsize   = max(20, int(tw * 0.030))
    fnt     = _load_font(fsize)
    lines   = textwrap.wrap(text, width=46)
    line_h  = int(fsize * 1.45)
    total_h = len(lines) * line_h + 22
    w, h    = frame.size
    y0      = h - total_h - 38
    bg_a    = int(200 * alpha)
    draw.rounded_rectangle([16, y0-10, w-16, y0+total_h+6], radius=14, fill=(0,0,0,bg_a))
    txt_a = int(255 * alpha)
    color = {
        "dramatic": (255, 220, 50,  txt_a),
        "noir":     (200, 200, 200, txt_a),
        "retro":    (255, 200, 120, txt_a),
        "anime":    (150, 220, 255, txt_a),
    }.get(style, (255, 255, 255, txt_a))
    for i, line in enumerate(lines):
        bbox = draw.textbbox((0,0), line, font=fnt)
        lw = bbox[2] - bbox[0]
        x  = (w - lw) // 2
        y  = y0 + i * line_h
        draw.text((x+2, y+2), line, font=fnt, fill=(0,0,0,int(txt_a*0.8)))
        draw.text((x,   y),   line, font=fnt, fill=color)
    return Image.alpha_composite(frame, overlay).convert("RGB")


# ══════════════════════════════════════════════════════════════════════
# CORE PIPELINE
# ══════════════════════════════════════════════════════════════════════
class MangaPipeline:

    def __init__(self, bot, chat_id, update_progress, settings,
                 anthropic_key, elevenlabs_key):
        self.bot  = bot
        self.cid  = chat_id
        self.upd  = update_progress
        self.s    = settings
        self.ak   = anthropic_key
        self.ek   = elevenlabs_key
        self.tmp  = Path(tempfile.mkdtemp())
        self.tw, self.th = QUALITY_DIMS.get(settings.get("quality", "hd"), (1280, 720))

    # ── Step 1: Download ──────────────────────────────────────────────
    async def _download(self, file_infos: list) -> list:
        await self.upd(5, "Downloading files from Telegram…")
        paths = []
        for i, fi in enumerate(file_infos):
            tf  = await self.bot.get_file(fi["file_id"])
            ext = {"image": "jpg", "pdf": "pdf", "zip": "zip"}.get(fi["type"], "bin")
            dst = self.tmp / f"raw_{i:03d}.{ext}"
            await tf.download_to_drive(str(dst))
            paths.append((dst, fi["type"]))
        return paths

    # ── Step 2: Extract pages ─────────────────────────────────────────
    async def _extract(self, raw: list) -> list[Path]:
        await self.upd(15, "Extracting manga pages…")
        imgs = []
        for path, ftype in raw:
            if ftype == "image":
                imgs.append(path)
            elif ftype == "pdf":
                imgs.extend(self._pdf_pages(path))
            elif ftype == "zip":
                imgs.extend(self._zip_pages(path))
        return sorted(imgs)[:20]

    def _pdf_pages(self, pdf: Path) -> list[Path]:
        try:
            import fitz
            doc = fitz.open(str(pdf))
            out = []
            for i, page in enumerate(doc):
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                dst = self.tmp / f"pdf_{pdf.stem}_{i:03d}.jpg"
                pix.save(str(dst))
                out.append(dst)
            doc.close()
            return out
        except ImportError:
            raise ImportError("pymupdf — run: pip install pymupdf")

    def _zip_pages(self, zpath: Path) -> list[Path]:
        out   = []
        exdir = self.tmp / f"zip_{zpath.stem}"
        exdir.mkdir(exist_ok=True)
        with zipfile.ZipFile(zpath) as zf:
            names = sorted([
                n for n in zf.namelist()
                if Path(n).suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
                and "__MACOSX" not in n
            ])
            for n in names:
                dst = exdir / Path(n).name
                dst.write_bytes(zf.read(n))
                out.append(dst)
        return out

    # ── Step 3: AI Script (Google Gemini — FREE, no credit card) ───────
    async def _ai_script(self, images: list[Path]) -> list[dict]:
        await self.upd(30, "Gemini AI is reading your manga…")

        lang_prompt = (
            "Respond ONLY in Hindi (Devanagari script)."
            if self.s.get("lang") == "hi"
            else "Respond ONLY in English."
        )
        voice_tone = {
            "calm":      "smooth, contemplative story-teller",
            "dramatic":  "intense cinematic narrator, short punchy sentences",
            "energetic": "excited anime dub energy with exclamations",
        }.get(self.s.get("voice", "calm"), "smooth story-teller")

        prompt = (
            f"You are a professional manga narrator. {lang_prompt} "
            f"Use a {voice_tone} tone. "
            "Analyse these manga page images carefully. "
            "Return ONLY a valid JSON array, no markdown, no extra text. "
            "One object per image in this exact format: "
            '[{"panel":1,"narration":"2-3 vivid spoken sentences describing the action and mood","subtitle":"max 10 words","emotion":"action"}]'
        )

        key = (self.ak or "").strip()
        if not key:
            raise ValueError(
                "GEMINI_API_KEY is empty in config.json!\n"
                "Get a FREE key at: aistudio.google.com\n"
                "No credit card needed!"
            )

        # Build multimodal parts: images + text prompt
        parts = []
        for p in images[:12]:
            with open(p, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            mt = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
            parts.append({"inline_data": {"mime_type": mt, "data": b64}})
        parts.append({"text": prompt})

        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "temperature": 0.7,
                "maxOutputTokens": 2048,
            }
        }

        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={key}"

        loop = asyncio.get_event_loop()
        RETRYABLE = {502, 503, 529}

        resp = None
        for attempt in range(1, 5):
            resp = await loop.run_in_executor(None, lambda: requests.post(
                url, json=payload,
                headers={"Content-Type": "application/json"},
                timeout=120
            ))
            if resp.status_code not in RETRYABLE:
                break
            wait = attempt * 8
            await self.upd(30, f"Gemini busy — retrying in {wait}s… ({attempt}/4)")
            await asyncio.sleep(wait)

        if resp.status_code == 400:
            raise ValueError(f"Gemini bad request: {resp.text[:300]}")
        elif resp.status_code == 401 or resp.status_code == 403:
            raise ValueError(
                "Gemini API key rejected!\n"
                "Get a free key at aistudio.google.com\n"
                "Paste it in config.json as GEMINI_API_KEY"
            )
        elif resp.status_code == 429:
            raise ValueError("Gemini rate limit hit. Wait 1 minute and try again.")
        elif resp.status_code not in (200,):
            raise ValueError(f"Gemini API error {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        try:
            raw = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError) as e:
            raise ValueError(f"Unexpected Gemini response format: {str(data)[:300]}")

        raw = raw.lstrip("```json").lstrip("```").rstrip("```").strip()
        return json.loads(raw)


    # ── Step 4: TTS ───────────────────────────────────────────────────
    async def _tts(self, panels: list[dict]) -> list[Path]:
        await self.upd(50, "Generating AI voice narration…")
        loop  = asyncio.get_event_loop()
        paths = []
        lang  = self.s.get("lang", "en")
        voice = self.s.get("voice", "calm")

        for i, panel in enumerate(panels):
            text = panel.get("narration", "")
            dst  = self.tmp / f"audio_{i:03d}.mp3"
            if self.ek:
                vid = ELEVENLABS_VOICES.get((lang, voice), "EXAVITQu4vr4xnSDxMaL")
                await loop.run_in_executor(None, lambda: tts_elevenlabs(text, vid, self.ek, str(dst)))
            else:
                lc = GTTS_LANG.get(lang, "en")
                await loop.run_in_executor(None, lambda t=text, l=lc, d=str(dst): tts_gtts(t, l, d))
            paths.append(dst)
        return paths

    # ── Step 5: Render panel → MP4 via FFmpeg ─────────────────────────
    def _render_panel_video(
        self,
        base: Image.Image,
        audio_path: Path,
        panel: dict,
        panel_idx: int,
        zoom_in: bool,
    ) -> Path:
        """
        Render one panel as a proper MP4 clip using FFmpeg directly.
        Saves PNG frames → FFmpeg encodes them with audio.
        """
        style     = self.s.get("style", "cinematic")
        show_subs = bool(self.s.get("subtitles", 1))
        speed_mul = {"slow": 1.5, "normal": 1.0, "fast": 0.65}.get(self.s.get("speed", "normal"), 1.0)
        quality   = self.s.get("quality", "hd")
        bitrate   = {"sd": "2000k", "hd": "5000k", "4k": "12000k"}.get(quality, "5000k")
        tw, th    = self.tw, self.th

        # Get audio duration
        dur = ffprobe_duration(str(audio_path)) * speed_mul
        dur = max(dur, 2.5)

        subtitle  = panel.get("subtitle", "") if show_subs else ""
        n_frames  = max(1, int(dur * FPS))

        # Render frames to PNG files
        frames_dir = self.tmp / f"frames_{panel_idx:03d}"
        frames_dir.mkdir(exist_ok=True)

        for fi in range(n_frames):
            progress  = fi / max(n_frames - 1, 1)
            fade_in   = min(fi / max(FPS * 0.4, 1), 1.0)
            fade_out  = min((n_frames - fi) / max(FPS * 0.4, 1), 1.0)
            sub_alpha = min(fade_in, fade_out)

            frame = render_frame(base, progress, style, zoom_in, tw, th, subtitle, sub_alpha)
            frame.save(str(frames_dir / f"f{fi:06d}.png"), "PNG")

        # Build silent video from frames
        silent_out = self.tmp / f"panel_{panel_idx:03d}_silent.mp4"
        ffmpeg(
            "-framerate", str(FPS),
            "-i", str(frames_dir / "f%06d.png"),
            "-c:v", "libx264",
            "-preset", "fast",
            "-b:v", bitrate,
            "-pix_fmt", "yuv420p",
            "-t", str(dur),
            str(silent_out)
        )

        # Merge with audio
        final_out = self.tmp / f"panel_{panel_idx:03d}.mp4"
        ffmpeg(
            "-i", str(silent_out),
            "-i", str(audio_path),
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "128k",
            "-shortest",
            str(final_out)
        )

        # Clean up frames to save disk space
        import shutil
        shutil.rmtree(str(frames_dir), ignore_errors=True)
        silent_out.unlink(missing_ok=True)

        return final_out

    # ── Step 6: Concatenate all clips → final video ───────────────────
    def _concat_clips(self, clip_paths: list[Path]) -> Path:
        """Use FFmpeg concat demuxer — most stable method."""
        concat_list = self.tmp / "concat.txt"
        with open(concat_list, "w") as f:
            for p in clip_paths:
                f.write(f"file '{p.resolve()}'\n")

        out = self.tmp / "mangavoice_final.mp4"
        quality = self.s.get("quality", "hd")
        bitrate = {"sd": "2000k", "hd": "5000k", "4k": "12000k"}.get(quality, "5000k")

        ffmpeg(
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_list),
            "-c:v", "libx264",
            "-preset", "fast",
            "-b:v", bitrate,
            "-c:a", "aac",
            "-b:a", "128k",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(out)
        )
        return out

    # ── Entry Point ───────────────────────────────────────────────────
    async def run(self, file_infos: list) -> tuple[str, int]:
        check_ffmpeg()

        raw_files = await self._download(file_infos)
        images    = await self._extract(raw_files)

        if not images:
            raise ValueError("No valid images found in the uploaded files.")

        panels = await self._ai_script(images)

        n      = min(len(images), len(panels))
        images = images[:n]
        panels = panels[:n]

        audios = await self._tts(panels)

        await self.upd(65, "Colour grading manga pages…")
        loop      = asyncio.get_event_loop()
        grade     = self.s.get("color_grade", "vivid")
        base_imgs = []
        for p in images:
            img = await loop.run_in_executor(
                None,
                lambda _p=p: smart_fill_frame(Image.open(_p).convert("RGB"), self.tw, self.th, grade)
            )
            base_imgs.append(img)

        await self.upd(70, "Rendering animated panels…")
        clip_paths = []
        for i, (base, audio, panel) in enumerate(zip(base_imgs, audios, panels)):
            pct   = 70 + int((i / n) * 20)
            label = f"Rendering panel {i+1}/{n}…"
            await self.upd(pct, label)

            clip = await loop.run_in_executor(
                None,
                lambda b=base, a=audio, p=panel, idx=i: self._render_panel_video(
                    b, a, p, idx, zoom_in=(idx % 2 == 0)
                )
            )
            clip_paths.append(clip)

        await self.upd(92, "Merging clips into final video…")
        final = await loop.run_in_executor(None, lambda: self._concat_clips(clip_paths))

        await self.upd(100, "Done! Uploading video…")
        return str(final), n
