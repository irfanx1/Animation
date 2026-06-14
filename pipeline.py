"""
pipeline.py — MangaVoice Ultra v2.0 Core Engine
================================================
Professional full-frame video, NO blurred sides.
Every page fills the entire frame beautifully.

Pipeline:
  1. Download & extract (images / PDF / ZIP)
  2. Smart image prep — full-frame, professional crop
  3. Claude AI → per-panel narration JSON
  4. TTS (gTTS free / ElevenLabs premium)
  5. Cinematic animation (Ken Burns, speed lines, etc.)
  6. Color grading per style
  7. Subtitle burn-in (animated fade)
  8. FFmpeg final encode
"""

import os, io, json, time, asyncio, logging, textwrap, zipfile
import tempfile, hashlib, math, random, base64
from pathlib import Path
from typing import Optional

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

# ══════════════════════════════════════════════════════════════════════
# TTS
# ══════════════════════════════════════════════════════════════════════
ELEVENLABS_VOICES = {
    ("en","calm"):      "EXAVITQu4vr4xnSDxMaL",
    ("en","dramatic"):  "VR6AewLTigWG4xSOukaG",
    ("en","energetic"): "yoZ06aMxZJJ28mfd3POQ",
    ("hi","calm"):      "EXAVITQu4vr4xnSDxMaL",
    ("hi","dramatic"):  "VR6AewLTigWG4xSOukaG",
    ("hi","energetic"): "yoZ06aMxZJJ28mfd3POQ",
}
GTTS_LANG = {"en": "en", "hi": "hi"}

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
    with open(path, "wb") as f: f.write(r.content)

# ══════════════════════════════════════════════════════════════════════
# IMAGE PREP  — THE KEY: full-frame, no blurred sides
# ══════════════════════════════════════════════════════════════════════
def smart_fill_frame(img: Image.Image, target_w: int, target_h: int,
                     color_grade: str = "vivid") -> Image.Image:
    """
    Fill the entire target frame with the manga page — professionally.
    Strategy:
      1. If page aspect is close to target → smart-crop centre
      2. If page is taller (portrait manga) → crop to fill height, centre-crop width
      3. If page is wider → crop to fill width, centre-crop height
    Result: ZERO black bars, ZERO blur. Pure manga filling the frame.
    """
    img = img.convert("RGB")
    sw, sh = img.size

    # Scale so image covers the entire target (crop-to-fill)
    scale = max(target_w / sw, target_h / sh)
    new_w = int(sw * scale)
    new_h = int(sh * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)

    # Centre crop
    left = (new_w - target_w) // 2
    top  = (new_h - target_h) // 2
    img  = img.crop((left, top, left + target_w, top + target_h))

    # Apply color grade
    img = apply_color_grade(img, color_grade)
    return img


def apply_color_grade(img: Image.Image, grade: str) -> Image.Image:
    """Professional color grading per style."""
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
        img = ImageEnhance.Color(img).enhance(0.0)        # greyscale
        img = ImageEnhance.Contrast(img).enhance(1.6)
        img = ImageEnhance.Sharpness(img).enhance(2.0)

    return img


# ══════════════════════════════════════════════════════════════════════
# ANIMATION FRAMES
# ══════════════════════════════════════════════════════════════════════
def ease_in_out(t: float) -> float:
    """Smooth easing function."""
    return t * t * (3 - 2 * t)

def ken_burns(base: Image.Image, progress: float,
              style: str, zoom_in: bool,
              target_w: int, target_h: int) -> np.ndarray:
    """
    Ken Burns on a pre-cropped full-frame image.
    Zoom range: 100%→108% (subtle, professional)
    """
    p  = ease_in_out(progress)
    tw, th = base.size

    if style == "cinematic":
        scale = (1.0 + 0.08 * p) if zoom_in else (1.08 - 0.08 * p)
        pan_x = int(tw * 0.02 * p) * (1 if zoom_in else -1)
        pan_y = int(th * 0.015 * p)

    elif style == "manga":
        # Impact zoom — fast burst at start
        burst  = min(p * 2, 1.0)
        scale  = 1.0 + 0.12 * burst
        pan_x  = pan_y = 0

    elif style == "noir":
        scale  = 1.0 + 0.06 * p
        pan_x  = int(tw * 0.03 * p)
        pan_y  = 0

    elif style == "retro":
        scale  = 1.0 + 0.05 * p
        pan_x  = int(tw * 0.01 * math.sin(p * math.pi))
        pan_y  = 0

    elif style == "anime":
        # Slight bounce zoom
        bounce = 1.0 + 0.1 * abs(math.sin(p * math.pi))
        scale  = 1.0 + 0.07 * p
        pan_x  = pan_y = 0

    else:
        scale = 1.0 + 0.07 * p
        pan_x = pan_y = 0

    nw = int(tw * scale)
    nh = int(th * scale)
    big = base.resize((nw, nh), Image.LANCZOS)

    cx = max(0, (nw - tw) // 2 + pan_x)
    cy = max(0, (nh - th) // 2 + pan_y)
    cx = min(cx, nw - tw)
    cy = min(cy, nh - th)
    frame = big.crop((cx, cy, cx + tw, cy + th))

    # Style post-processing
    frame = _style_fx(frame, style, progress)
    return np.array(frame)


def _style_fx(img: Image.Image, style: str, progress: float) -> Image.Image:
    """Per-style overlay effects."""

    if style == "noir":
        # Vignette
        img = _vignette(img, strength=0.55)

    elif style == "retro":
        # Film grain
        arr   = np.array(img, dtype=np.int16)
        grain = np.random.randint(-12, 12, arr.shape, dtype=np.int16)
        arr   = np.clip(arr + grain, 0, 255).astype(np.uint8)
        img   = Image.fromarray(arr)
        img   = _vignette(img, strength=0.4)

    elif style == "anime":
        # Soft glow
        glow  = img.filter(ImageFilter.GaussianBlur(radius=3))
        img   = ImageChops.screen(img, ImageEnhance.Brightness(glow).enhance(0.4))

    elif style == "manga":
        # Sharpen for ink feel
        img   = img.filter(ImageFilter.SHARPEN)

    return img


def _vignette(img: Image.Image, strength: float = 0.5) -> Image.Image:
    w, h    = img.size
    mask    = Image.new("L", (w, h), 0)
    draw    = ImageDraw.Draw(mask)
    cx, cy  = w // 2, h // 2
    steps   = 40
    for i in range(steps, 0, -1):
        alpha = int(255 * (1 - (i / steps) ** 0.6) * strength)
        rx    = int(cx * i / steps)
        ry    = int(cy * i / steps)
        draw.ellipse(
            [cx - rx, cy - ry, cx + rx, cy + ry],
            fill=255 - alpha
        )
    black = Image.new("RGB", (w, h), (0, 0, 0))
    img   = Image.composite(img, black, mask)
    return img


def transition_frame(
    a: Image.Image, b: Image.Image,
    t: float, style: str
) -> np.ndarray:
    """Cinematic transition between panels."""
    w, h = a.size

    if style == "manga":
        # Hard horizontal wipe
        split = int(w * ease_in_out(t))
        out   = a.copy()
        if split > 0:
            out.paste(b.crop((0, 0, split, h)), (0, 0))
        return np.array(out)

    elif style == "noir":
        # Radial iris wipe
        out  = a.copy()
        mask = Image.new("L", (w, h), 0)
        cx, cy = w // 2, h // 2
        r    = int(math.sqrt(cx**2 + cy**2) * ease_in_out(t))
        ImageDraw.Draw(mask).ellipse([cx-r, cy-r, cx+r, cy+r], fill=255)
        out.paste(b, mask=mask)
        return np.array(out)

    elif style == "retro":
        # Diagonal wipe
        arr_a = np.array(a, dtype=np.float32)
        arr_b = np.array(b, dtype=np.float32)
        yy, xx = np.mgrid[0:h, 0:w]
        diag  = (xx / w + yy / h) / 2
        alpha = np.clip((diag - (1 - ease_in_out(t))) / 0.3, 0, 1)
        out   = (arr_a * (1 - alpha[:,:,None]) + arr_b * alpha[:,:,None]).astype(np.uint8)
        return out

    else:
        # Smooth cross-dissolve (default)
        arr_a = np.array(a, dtype=np.float32)
        arr_b = np.array(b, dtype=np.float32)
        t2    = ease_in_out(t)
        return (arr_a * (1 - t2) + arr_b * t2).astype(np.uint8)


# ══════════════════════════════════════════════════════════════════════
# SUBTITLE RENDERER
# ══════════════════════════════════════════════════════════════════════
FONT_CACHE = {}

def _load_font(size: int):
    if size in FONT_CACHE:
        return FONT_CACHE[size]
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Windows/Fonts/arialbd.ttf",
    ]
    fnt = None
    for c in candidates:
        if Path(c).exists():
            try:
                fnt = ImageFont.truetype(c, size)
                break
            except Exception:
                pass
    if fnt is None:
        fnt = ImageFont.load_default()
    FONT_CACHE[size] = fnt
    return fnt


def burn_subtitle(frame: Image.Image, text: str,
                  alpha: float, style: str, target_w: int) -> Image.Image:
    if not text.strip() or alpha <= 0:
        return frame

    frame = frame.copy().convert("RGBA")
    overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    draw  = ImageDraw.Draw(overlay)

    font_size = max(20, int(target_w * 0.030))
    fnt       = _load_font(font_size)
    lines     = textwrap.wrap(text, width=46)
    line_h    = int(font_size * 1.45)
    total_h   = len(lines) * line_h + 22
    w, h      = frame.size
    y0        = h - total_h - 38

    # Pill background
    bg_a = int(200 * alpha)
    draw.rounded_rectangle(
        [16, y0 - 10, w - 16, y0 + total_h + 6],
        radius=14, fill=(0, 0, 0, bg_a)
    )

    # Text
    txt_a = int(255 * alpha)
    colors = {
        "dramatic": (255, 220, 50, txt_a),
        "noir":     (200, 200, 200, txt_a),
        "retro":    (255, 200, 120, txt_a),
        "anime":    (150, 220, 255, txt_a),
    }
    txt_color = colors.get(style, (255, 255, 255, txt_a))

    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=fnt)
        lw   = bbox[2] - bbox[0]
        x    = (w - lw) // 2
        y    = y0 + i * line_h
        # Shadow
        draw.text((x + 2, y + 2), line, font=fnt, fill=(0, 0, 0, int(txt_a * 0.8)))
        draw.text((x, y), line, font=fnt, fill=txt_color)

    merged = Image.alpha_composite(frame, overlay)
    return merged.convert("RGB")


# ══════════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════
class MangaPipeline:

    def __init__(self, bot, chat_id, update_progress, settings,
                 anthropic_key, elevenlabs_key):
        self.bot      = bot
        self.cid      = chat_id
        self.upd      = update_progress
        self.s        = settings
        self.ak       = anthropic_key
        self.ek       = elevenlabs_key
        self.tmp      = Path(tempfile.mkdtemp())
        self.tw, self.th = QUALITY_DIMS.get(settings.get("quality","hd"), (1280,720))

    # ─── Step 1: Download ───────────────────────────────────────────
    async def _download(self, file_infos: list) -> list[Path]:
        await self.upd(5, "Downloading files from Telegram…")
        paths = []
        for i, fi in enumerate(file_infos):
            tf  = await self.bot.get_file(fi["file_id"])
            ext = {"image":"jpg","pdf":"pdf","zip":"zip"}.get(fi["type"],"bin")
            dst = self.tmp / f"raw_{i:03d}.{ext}"
            await tf.download_to_drive(str(dst))
            paths.append((dst, fi["type"]))
        return paths

    # ─── Step 2: Extract to images ─────────────────────────────────
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
        return sorted(imgs)[:20]      # cap at 20 pages

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
        out     = []
        exdir   = self.tmp / f"zip_{zpath.stem}"
        exdir.mkdir(exist_ok=True)
        with zipfile.ZipFile(zpath) as zf:
            names = sorted([
                n for n in zf.namelist()
                if Path(n).suffix.lower() in (".jpg",".jpeg",".png",".webp")
                and not n.startswith("__MACOSX")
            ])
            for n in names:
                dst = exdir / Path(n).name
                data = zf.read(n)
                dst.write_bytes(data)
                out.append(dst)
        return out

    # ─── Step 3: AI Script ─────────────────────────────────────────
    async def _ai_script(self, images: list[Path]) -> list[dict]:
        await self.upd(30, "Claude AI is reading your manga…")

        lang_prompt = (
            "Respond ONLY in Hindi (Devanagari script)."
            if self.s.get("lang") == "hi"
            else "Respond ONLY in English."
        )
        voice_tone = {
            "calm":      "smooth, contemplative story-teller",
            "dramatic":  "intense cinematic narrator, short punchy sentences, dramatic pauses",
            "energetic": "excited anime dub energy, exclamations, fast-paced",
        }.get(self.s.get("voice","calm"), "smooth story-teller")

        system = (
            f"You are a professional manga narrator. {lang_prompt} "
            f"Use a {voice_tone} tone. "
            "Analyse the manga page images. "
            "Return ONLY a valid JSON array — NO markdown, NO preamble. "
            "One object per image with keys:\n"
            '  "panel": <1-based int>\n'
            '  "narration": <2-3 vivid spoken sentences>\n'
            '  "subtitle": <max 10 words for on-screen text>\n'
            '  "emotion": <action|mystery|romance|comedy|tragedy|calm>\n'
        )

        content = []
        for p in images[:12]:
            with open(p,"rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            mt  = "image/png" if p.suffix.lower()==".png" else "image/jpeg"
            content.append({"type":"image","source":{"type":"base64","media_type":mt,"data":b64}})
        content.append({"type":"text","text":"Narrate each panel."})

        headers = {
            "x-api-key": self.ak,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 2048,
            "system": system,
            "messages": [{"role":"user","content":content}],
        }

        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(None, lambda: requests.post(
            "https://api.anthropic.com/v1/messages",
            json=payload, headers=headers, timeout=90
        ))
        resp.raise_for_status()
        raw = resp.json()["content"][0]["text"].strip()
        raw = raw.lstrip("```json").lstrip("```").rstrip("```").strip()
        return json.loads(raw)

    # ─── Step 4: TTS ───────────────────────────────────────────────
    async def _tts(self, panels: list[dict]) -> list[Path]:
        await self.upd(50, "Generating AI voice narration…")
        loop   = asyncio.get_event_loop()
        paths  = []
        lang   = self.s.get("lang","en")
        voice  = self.s.get("voice","calm")

        for i, p in enumerate(panels):
            text = p.get("narration","")
            dst  = self.tmp / f"audio_{i:03d}.mp3"

            if self.ek:
                vid = ELEVENLABS_VOICES.get((lang, voice), "EXAVITQu4vr4xnSDxMaL")
                await loop.run_in_executor(None, lambda: tts_elevenlabs(text, vid, self.ek, str(dst)))
            else:
                lc  = GTTS_LANG.get(lang, "en")
                await loop.run_in_executor(None, lambda t=text, l=lc, d=str(dst): tts_gtts(t,l,d))
            paths.append(dst)
        return paths

    # ─── Step 5+6: Build video ─────────────────────────────────────
    async def _build_video(
        self,
        images: list[Path],
        audios: list[Path],
        panels: list[dict],
    ) -> Path:
        await self.upd(65, "Animating panels — cinematic mode…")

        from moviepy import ImageClip, AudioFileClip, concatenate_videoclips

        style      = self.s.get("style","cinematic")
        grade      = self.s.get("color_grade","vivid")
        show_subs  = bool(self.s.get("subtitles",1))
        speed_mul  = {"slow":1.5,"normal":1.0,"fast":0.65}.get(self.s.get("speed","normal"),1.0)
        tw, th     = self.tw, self.th

        # Pre-process all images to full-frame
        await self.upd(68, "Colour grading manga pages…")
        loop       = asyncio.get_event_loop()
        base_imgs  = []
        for p in images:
            img = await loop.run_in_executor(
                None,
                lambda _p=p: smart_fill_frame(Image.open(_p).convert("RGB"), tw, th, grade)
            )
            base_imgs.append(img)

        clips = []
        trans_dur = 0.45

        await self.upd(72, "Building animated clips…")

        for i, (base, audio_path, panel) in enumerate(
            zip(base_imgs, audios, panels)
        ):
            # Audio duration
            try:
                audio = AudioFileClip(str(audio_path))
                dur   = max(audio.duration * speed_mul, 2.0)
            except Exception:
                audio = None
                dur   = 4.0

            subtitle = panel.get("subtitle","") if show_subs else ""
            zoom_in  = (i % 2 == 0)

            def make_frame(t, _b=base.copy(), _d=dur, _zi=zoom_in, _sub=subtitle):
                prog    = min(t / max(_d - 0.01, 0.01), 1.0)
                frame   = Image.fromarray(ken_burns(_b, prog, style, _zi, tw, th))
                if _sub:
                    fade    = min(t * 2.5, 1.0, (_d - t) * 2.5)
                    frame   = burn_subtitle(frame, _sub, max(0, fade), style, tw)
                return np.array(frame)

            clip = ImageClip(make_frame, duration=dur, ismask=False).with_fps(FPS)
            if audio:
                clip = clip.with_audio(audio)
            clips.append(clip)

            # Transition to next panel
            if i < len(base_imgs) - 1:
                next_b   = base_imgs[i + 1]
                _cur     = base.copy()
                _nxt     = next_b.copy()
                def make_trans(t, _a=_cur, _b=_nxt, _td=trans_dur):
                    frame = Image.fromarray(transition_frame(_a, _b, t / _td, style))
                    return np.array(frame)
                trans_clip = ImageClip(make_trans, duration=trans_dur).with_fps(FPS)
                clips.append(trans_clip)

        await self.upd(85, "Encoding final video with FFmpeg…")

        quality   = self.s.get("quality","hd")
        bitrate   = {"sd":"2000k","hd":"5000k","4k":"12000k"}.get(quality,"5000k")
        final     = concatenate_videoclips(clips, method="compose")
        out_path  = self.tmp / "mangavoice_output.mp4"

        await loop.run_in_executor(None, lambda: final.write_videofile(
            str(out_path),
            fps=FPS,
            codec="libx264",
            audio_codec="aac",
            bitrate=bitrate,
            preset="fast",
            threads=4,
            logger=None,
        ))
        return out_path

    # ─── Entry Point ───────────────────────────────────────────────
    async def run(self, file_infos: list) -> tuple[str, int]:
        raw_files = await self._download(file_infos)
        images    = await self._extract(raw_files)

        if not images:
            raise ValueError("No valid images found in the provided files.")

        panels = await self._ai_script(images)

        n       = min(len(images), len(panels))
        images  = images[:n]
        panels  = panels[:n]

        audios  = await self._tts(panels)
        out     = await self._build_video(images, audios, panels)

        await self.upd(100, "Done! Uploading video…")
        return str(out), n
