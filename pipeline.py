"""
MangaVoice Ultra v3.0 — pipeline.py
AI: Groq (free, vision, fast)
TTS: gTTS (free) / ElevenLabs (premium)
Video: Direct FFmpeg — stable, no MoviePy crashes
Pages: Up to 35 per video
Frame: crop-to-fill, ZERO black bars, ZERO blur
"""

import io, json, math, asyncio, logging, textwrap
import zipfile, tempfile, base64, subprocess, shutil
from pathlib import Path

import requests
import numpy as np
from PIL import (Image, ImageFilter, ImageEnhance,
                 ImageDraw, ImageFont, ImageChops)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
QUALITY_DIMS = {"sd":(854,480), "hd":(1280,720), "4k":(1920,1080)}
FPS          = 24
MAX_PAGES    = 35
GROQ_BATCH   = 4        # images per Groq API call (max 5, use 4 safely)
GROQ_MODELS  = [        # fallback chain — tries each if previous fails
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
    "llama-3.2-11b-vision-preview",
]
GTTS_LANG    = {"en":"en","hi":"hi"}
EL_VOICES    = {
    ("en","calm"):      "EXAVITQu4vr4xnSDxMaL",
    ("en","dramatic"):  "VR6AewLTigWG4xSOukaG",
    ("en","energetic"): "yoZ06aMxZJJ28mfd3POQ",
    ("hi","calm"):      "EXAVITQu4vr4xnSDxMaL",
    ("hi","dramatic"):  "VR6AewLTigWG4xSOukaG",
    ("hi","energetic"): "yoZ06aMxZJJ28mfd3POQ",
}

# ═══════════════════════════════════════════════════════════
# FFMPEG UTILS
# ═══════════════════════════════════════════════════════════
def run_ffmpeg(*args):
    cmd = ["ffmpeg","-y","-loglevel","error"] + [str(a) for a in args]
    r   = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError(f"FFmpeg error:\n{r.stderr.decode(errors='replace')[-800:]}")

def get_duration(path) -> float:
    r = subprocess.run(
        ["ffprobe","-v","error","-show_entries","format=duration",
         "-of","default=noprint_wrappers=1:nokey=1",str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:    return max(float(r.stdout.decode().strip()), 1.5)
    except: return 4.0

def check_ffmpeg():
    if subprocess.run(["ffmpeg","-version"],
                      stdout=subprocess.PIPE,stderr=subprocess.PIPE).returncode != 0:
        raise RuntimeError(
            "FFmpeg not installed!\n"
            "Ubuntu: sudo apt install ffmpeg\n"
            "Mac:    brew install ffmpeg\n"
            "Win:    https://ffmpeg.org/download.html")

# ═══════════════════════════════════════════════════════════
# TTS
# ═══════════════════════════════════════════════════════════
def do_gtts(text:str, lang:str, path:str):
    from gtts import gTTS
    gTTS(text=text, lang=lang, slow=False).save(path)

def do_elevenlabs(text:str, voice_id:str, api_key:str, path:str):
    r = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers={"xi-api-key":api_key,"Content-Type":"application/json"},
        json={"text":text,"model_id":"eleven_multilingual_v2",
              "voice_settings":{"stability":0.5,"similarity_boost":0.75}},
        timeout=30)
    r.raise_for_status()
    Path(path).write_bytes(r.content)

# ═══════════════════════════════════════════════════════════
# IMAGE PROCESSING
# ═══════════════════════════════════════════════════════════
def make_blur_bg_frame(img:Image.Image, tw:int, th:int) -> Image.Image:
    """
    BLUR-BG MODE: Shows FULL manga page in centre.
    Background = same image heavily blurred & stretched to fill frame.
    Like YouTube Shorts / Instagram Reels style.
    """
    img = img.convert("RGB")
    sw, sh = img.size

    # --- Background: stretch + heavy blur ---
    bg = img.resize((tw, th), Image.LANCZOS)
    bg = bg.filter(ImageFilter.GaussianBlur(radius=28))
    # Darken bg so manga stands out
    bg = ImageEnhance.Brightness(bg).enhance(0.45)

    # --- Foreground: fit full page with small padding ---
    pad   = int(min(tw, th) * 0.04)   # 4% padding on each side
    max_w = tw - pad * 2
    max_h = th - pad * 2
    scale = min(max_w / sw, max_h / sh)
    nw    = int(sw * scale)
    nh    = int(sh * scale)
    fg    = img.resize((nw, nh), Image.LANCZOS)

    # Centre paste
    x = (tw - nw) // 2
    y = (th - nh) // 2
    bg.paste(fg, (x, y))

    # Subtle shadow border around manga page
    draw = ImageDraw.Draw(bg)
    shadow_rect = [x-3, y-3, x+nw+3, y+nh+3]
    draw.rectangle(shadow_rect, outline=(0,0,0), width=4)
    draw.rectangle([x-1, y-1, x+nw+1, y+nh+1], outline=(255,255,255,80), width=1)

    return bg


def make_blur_bg_frame_zoomed(img:Image.Image, tw:int, th:int,
                               progress:float, zoom_in:bool) -> Image.Image:
    """
    BLUR-BG + ZOOM MODE: Full page in centre with gentle Ken Burns zoom.
    Background stays blurred. Manga page slowly zooms in/out.
    """
    img = img.convert("RGB")
    sw, sh = img.size

    # Background
    bg = img.resize((tw, th), Image.LANCZOS)
    bg = bg.filter(ImageFilter.GaussianBlur(radius=30))
    bg = ImageEnhance.Brightness(bg).enhance(0.40)

    p     = ease_in_out(progress)
    pad   = int(min(tw, th) * 0.04)
    max_w = tw - pad * 2
    max_h = th - pad * 2
    base_scale = min(max_w / sw, max_h / sh)

    # Gentle zoom: 0% to 6% (very subtle)
    zoom = (1.0 + 0.06 * p) if zoom_in else (1.06 - 0.06 * p)
    final_scale = base_scale * zoom
    nw = int(sw * final_scale)
    nh = int(sh * final_scale)

    fg = img.resize((nw, nh), Image.LANCZOS)

    # Centre with slight pan
    pan_x = int(tw * 0.012 * p) * (1 if zoom_in else -1)
    pan_y = int(th * 0.006 * p)
    x = (tw - nw) // 2 + pan_x
    y = (th - nh) // 2 + pan_y

    # Clamp so manga never goes off-screen
    x = max(pad//2 - nw + tw//2, min(x, tw - nw - pad//2 + nw - tw//2 + tw//2))
    y = max(pad//2, min(y, th - nh - pad//2))
    x = max(-(nw - tw)//2 - 20, min(x, (nw - tw)//2 + 20))
    y = max(-(nh - th)//2 - 20, min(y, (nh - th)//2 + 20))
    x = (tw - nw) // 2 + pan_x
    y = (th - nh) // 2 + pan_y
    x = max(0 - nw + 10, min(x, tw - 10))
    y = max(0 - nh + 10, min(y, th - 10))

    bg.paste(fg, (x, y))

    # Border
    draw = ImageDraw.Draw(bg)
    draw.rectangle([x-3, y-3, x+nw+3, y+nh+3], outline=(0,0,0), width=4)

    return bg


def crop_to_fill(img:Image.Image, tw:int, th:int) -> Image.Image:
    """CROP MODE: Scale image so it covers target, then centre-crop."""
    img   = img.convert("RGB")
    sw,sh = img.size
    scale = max(tw/sw, th/sh)
    nw,nh = int(sw*scale), int(sh*scale)
    img   = img.resize((nw,nh), Image.LANCZOS)
    left  = (nw-tw)//2
    top   = (nh-th)//2
    return img.crop((left,top,left+tw,top+th))

def apply_grade(img:Image.Image, grade:str) -> Image.Image:
    if grade=="vivid":
        img = ImageEnhance.Color(img).enhance(1.4)
        img = ImageEnhance.Contrast(img).enhance(1.2)
        img = ImageEnhance.Sharpness(img).enhance(1.3)
    elif grade=="muted":
        img = ImageEnhance.Color(img).enhance(0.55)
        img = ImageEnhance.Contrast(img).enhance(0.88)
    elif grade=="warm":
        r,g,b = img.split()
        r = r.point(lambda x:min(255,x+28))
        b = b.point(lambda x:max(0,x-18))
        img = Image.merge("RGB",(r,g,b))
        img = ImageEnhance.Contrast(img).enhance(1.12)
    elif grade=="cold":
        r,g,b = img.split()
        b = b.point(lambda x:min(255,x+22))
        r = r.point(lambda x:max(0,x-12))
        img = Image.merge("RGB",(r,g,b))
    elif grade=="manga_ink":
        img = ImageEnhance.Color(img).enhance(0.0)
        img = ImageEnhance.Contrast(img).enhance(1.8)
        img = ImageEnhance.Sharpness(img).enhance(2.5)
    elif grade=="golden":
        r,g,b = img.split()
        r = r.point(lambda x:min(255,x+20))
        g = g.point(lambda x:min(255,x+8))
        b = b.point(lambda x:max(0,x-20))
        img = Image.merge("RGB",(r,g,b))
        img = ImageEnhance.Contrast(img).enhance(1.15)
    return img

def ease_in_out(t:float) -> float:
    t = min(max(t,0),1)
    return t*t*(3-2*t)

def add_vignette(img:Image.Image, strength:float=0.5) -> Image.Image:
    w,h   = img.size
    mask  = Image.new("L",(w,h),0)
    d     = ImageDraw.Draw(mask)
    cx,cy = w//2,h//2
    for i in range(40,0,-1):
        a     = int(255*(1-(i/40)**0.6)*strength)
        rx,ry = int(cx*i/40),int(cy*i/40)
        d.ellipse([cx-rx,cy-ry,cx+rx,cy+ry], fill=255-a)
    black = Image.new("RGB",(w,h),(0,0,0))
    return Image.composite(img, black, mask)

def render_frame(base:Image.Image, progress:float, style:str,
                 zoom_in:bool, tw:int, th:int,
                 subtitle:str, sub_alpha:float,
                 bg_mode:str="crop") -> Image.Image:
    """
    bg_mode:
      "crop"     — crop-to-fill, no bars (old default)
      "blur"     — full page shown, blurred bg, NO zoom
      "blur_zoom"— full page shown, blurred bg, gentle Ken Burns zoom
    """
    p = ease_in_out(progress)

    # ── BG BLUR MODES ─────────────────────────────────────────
    if bg_mode == "blur":
        img = make_blur_bg_frame(base, tw, th)
        img = apply_style_fx(img, style, p, tw, th)
        if subtitle and sub_alpha > 0:
            img = burn_subtitle(img, subtitle, sub_alpha, style, tw)
        return img

    if bg_mode == "blur_zoom":
        img = make_blur_bg_frame_zoomed(base, tw, th, progress, zoom_in)
        img = apply_style_fx(img, style, p, tw, th)
        if subtitle and sub_alpha > 0:
            img = burn_subtitle(img, subtitle, sub_alpha, style, tw)
        return img

    # ── CROP MODE (classic) ───────────────────────────────────
    img = base.copy()
    w,h = img.size

    if style=="cinematic":
        scale = (1.0+0.03*p) if zoom_in else (1.03-0.03*p)
        px    = int(w*0.008*p)*(1 if zoom_in else -1)
        py    = int(h*0.005*p)
    elif style=="manga":
        burst = min(p*3, 1.0)
        scale = 1.0+0.04*burst
        px=py=0
    elif style=="noir":
        scale = 1.0+0.025*p
        px    = int(w*0.008*p)
        py    = 0
    elif style=="retro":
        scale = 1.0+0.02*p
        px    = int(w*0.004*math.sin(p*math.pi))
        py    = 0
    elif style=="anime":
        scale = 1.0+0.03*p
        px=py=0
    elif style=="dramatic":
        scale = (1.0+0.04*p) if zoom_in else (1.04-0.04*p)
        px    = int(w*0.012*p)*(1 if zoom_in else -1)
        py    = int(h*0.006*p)
    else:
        scale=1.0+0.025*p; px=py=0

    nw,nh = int(w*scale),int(h*scale)
    img   = img.resize((nw,nh), Image.LANCZOS)
    cx    = max(0,min((nw-w)//2+px, nw-w))
    cy    = max(0,min((nh-h)//2+py, nh-h))
    img   = img.crop((cx,cy,cx+w,cy+h))
    img   = apply_style_fx(img, style, p, tw, th)

    if subtitle and sub_alpha>0:
        img = burn_subtitle(img, subtitle, sub_alpha, style, tw)
    return img


def apply_style_fx(img:Image.Image, style:str, p:float,
                   tw:int, th:int) -> Image.Image:
    """Apply per-style visual effects (vignette, grain, glow, etc.)."""
    if style in ("noir","dramatic"):
        img = add_vignette(img, 0.45 if style=="noir" else 0.28)
    elif style=="retro":
        arr   = np.array(img,dtype=np.int16)
        grain = np.random.randint(-10,10,arr.shape,dtype=np.int16)
        img   = Image.fromarray(np.clip(arr+grain,0,255).astype(np.uint8))
        img   = add_vignette(img, 0.35)
    elif style=="anime":
        glow  = img.filter(ImageFilter.GaussianBlur(radius=3))
        img   = ImageChops.screen(img, ImageEnhance.Brightness(glow).enhance(0.25))
    elif style=="manga":
        img   = img.filter(ImageFilter.SHARPEN)
    return img

# ═══════════════════════════════════════════════════════════
# SUBTITLES
# ═══════════════════════════════════════════════════════════
_FONT_CACHE = {}
def load_font(size:int):
    if size in _FONT_CACHE: return _FONT_CACHE[size]
    for p in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Windows/Fonts/arialbd.ttf",
    ]:
        if Path(p).exists():
            try:
                f = ImageFont.truetype(p, size)
                _FONT_CACHE[size] = f
                return f
            except: pass
    f = ImageFont.load_default()
    _FONT_CACHE[size] = f
    return f

def burn_subtitle(frame:Image.Image, text:str, alpha:float,
                  style:str, tw:int) -> Image.Image:
    if not text.strip() or alpha<=0: return frame
    frame   = frame.copy().convert("RGBA")
    overlay = Image.new("RGBA", frame.size, (0,0,0,0))
    draw    = ImageDraw.Draw(overlay)
    # Clean modern font size — not too big, not too small
    fs      = max(18, int(tw*0.026))
    fnt     = load_font(fs)
    lines   = textwrap.wrap(text, width=52)
    lh      = int(fs*1.55)
    total_h = len(lines)*lh+20
    w,h     = frame.size
    # Position: bottom 8% of frame
    y0      = h - total_h - int(h*0.04)
    # Clean semi-transparent bar — full width, thin
    bar_h   = total_h + 28
    bg_a    = int(175*alpha)
    draw.rectangle([0, y0-14, w, y0+bar_h], fill=(0,0,0,bg_a))
    # Thin accent line on top of subtitle bar
    accent_colors = {
        "dramatic": (255,210,0),
        "noir":     (180,180,180),
        "retro":    (255,180,80),
        "anime":    (100,200,255),
        "manga":    (255,60,60),
    }
    ac = accent_colors.get(style,(255,255,255))
    draw.rectangle([0, y0-14, w, y0-11], fill=(*ac, int(200*alpha)))
    txt_a = int(255*alpha)
    # Clean white text for all styles (most professional look)
    txt_colors = {
        "dramatic": (255,220,50,txt_a),
        "noir":     (220,220,220,txt_a),
        "retro":    (255,205,120,txt_a),
        "anime":    (160,225,255,txt_a),
        "manga":    (255,255,255,txt_a),
    }
    color = txt_colors.get(style,(255,255,255,txt_a))
    for i,line in enumerate(lines):
        bbox = draw.textbbox((0,0), line, font=fnt)
        lw   = bbox[2]-bbox[0]
        x    = (w-lw)//2
        y    = y0+i*lh
        # Tight shadow only (no double-border bloat)
        draw.text((x+1,y+1), line, font=fnt, fill=(0,0,0,int(txt_a*0.9)))
        draw.text((x,y),     line, font=fnt, fill=color)
    return Image.alpha_composite(frame, overlay).convert("RGB")

# ═══════════════════════════════════════════════════════════
# PIPELINE CLASS
# ═══════════════════════════════════════════════════════════
class MangaPipeline:
    def __init__(self, bot, chat_id, update_progress,
                 settings, groq_key, elevenlabs_key):
        self.bot  = bot
        self.cid  = chat_id
        self.upd  = update_progress
        self.s    = settings
        self.gk   = groq_key
        self.ek   = elevenlabs_key
        self.tmp  = Path(tempfile.mkdtemp())
        self.tw, self.th = QUALITY_DIMS.get(settings.get("quality","hd"),(1280,720))

    # ── 1. Download ────────────────────────────────────────────────
    async def _download(self, file_infos:list) -> list:
        await self.upd(5, "Downloading your manga files…")
        out = []
        for i,fi in enumerate(file_infos):
            tf  = await self.bot.get_file(fi["file_id"])
            ext = {"image":"jpg","pdf":"pdf","zip":"zip"}.get(fi["type"],"jpg")
            dst = self.tmp/f"raw_{i:03d}.{ext}"
            await tf.download_to_drive(str(dst))
            out.append((dst, fi["type"]))
        return out

    # ── 2. Extract all pages ───────────────────────────────────────
    async def _extract(self, raw:list) -> list[Path]:
        await self.upd(12, "Extracting manga pages…")
        imgs = []
        for path,ftype in raw:
            if ftype=="image":   imgs.append(path)
            elif ftype=="pdf":   imgs.extend(self._from_pdf(path))
            elif ftype=="zip":   imgs.extend(self._from_zip(path))

        valid = []
        for p in sorted(imgs):
            try:
                with Image.open(p) as test:
                    test.verify()
                valid.append(p)
            except: pass

        return valid[:MAX_PAGES]

    def _from_pdf(self, pdf:Path) -> list[Path]:
        try:
            import fitz
            doc = fitz.open(str(pdf))
            out = []
            for i,page in enumerate(doc):
                pix = page.get_pixmap(matrix=fitz.Matrix(2,2))
                dst = self.tmp/f"pdf_{pdf.stem}_{i:04d}.jpg"
                pix.save(str(dst))
                out.append(dst)
            doc.close()
            return out
        except ImportError:
            raise ImportError("Install: pip install pymupdf")

    def _from_zip(self, zpath:Path) -> list[Path]:
        exdir = self.tmp/f"zip_{zpath.stem}"
        exdir.mkdir(exist_ok=True)
        out   = []
        with zipfile.ZipFile(zpath) as zf:
            names = sorted([
                n for n in zf.namelist()
                if Path(n).suffix.lower() in (".jpg",".jpeg",".png",".webp")
                and "__MACOSX" not in n
                and not Path(n).name.startswith(".")
            ])
            for n in names:
                dst = exdir/f"{len(out):04d}{Path(n).suffix}"
                dst.write_bytes(zf.read(n))
                out.append(dst)
        return out

    # ── 3. AI narration via Groq (batched, with fallback) ──────────
    async def _ai_script(self, images:list[Path]) -> list[dict]:
        await self.upd(20, "AI reading your manga story…")

        key = (self.gk or "").strip()
        if not key:
            raise ValueError(
                "GROQ_API_KEY is empty in config.json!\n"
                "Get FREE key at: console.groq.com (no credit card)")
        if not key.startswith("gsk_"):
            raise ValueError(
                f"GROQ_API_KEY wrong (got: {key[:14]}...)\n"
                "Must start with gsk_ — get fresh key at console.groq.com")

        lang_p = ("Respond ONLY in Hindi (Devanagari script)."
                  if self.s.get("lang")=="hi" else "Respond ONLY in English.")
        tone_p = {
            "calm":      "Use a smooth, contemplative story-teller voice.",
            "dramatic":  "Use an intense cinematic narrator tone — short punchy sentences, dramatic.",
            "energetic": "Use excited anime dub energy — exclamations, fast-paced.",
        }.get(self.s.get("voice","calm"), "Use a smooth story-teller voice.")

        all_panels = []
        batches    = [images[i:i+GROQ_BATCH] for i in range(0,len(images),GROQ_BATCH)]

        for b_idx,batch in enumerate(batches):
            pct    = 20+int((b_idx/len(batches))*22)
            offset = b_idx*GROQ_BATCH
            await self.upd(pct,
                f"AI analysing pages {offset+1}–{offset+len(batch)} of {len(images)}…")

            # Compress images small for API
            parts = []
            for p in batch:
                img = Image.open(p).convert("RGB")
                img.thumbnail((480,480), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf,"JPEG",quality=50,optimize=True)
                b64 = base64.b64encode(buf.getvalue()).decode()
                parts.append({"type":"image_url",
                               "image_url":{"url":f"data:image/jpeg;base64,{b64}"}})

            parts.append({"type":"text","text":(
                f"You are a professional manga narrator. {lang_p} {tone_p}\n"
                f"These are manga pages {offset+1} to {offset+len(batch)}.\n"
                "Return ONLY a JSON array — no markdown, no explanation.\n"
                f"Exactly {len(batch)} objects:\n"
                '[{"panel":1,"narration":"2-3 vivid cinematic sentences","subtitle":"max 8 words","emotion":"action"}]'
            )})

            payload = {
                "messages":[{"role":"user","content":parts}],
                "temperature":0.75,
                "max_tokens":1024,
            }

            # Try each model in fallback chain
            resp     = None
            last_err = "unknown error"
            for model in GROQ_MODELS:
                payload["model"] = model
                for attempt in range(3):
                    try:
                        r = await asyncio.get_event_loop().run_in_executor(
                            None, lambda m=model: requests.post(
                                "https://api.groq.com/openai/v1/chat/completions",
                                json={**payload,"model":m},
                                headers={"Authorization":f"Bearer {key}",
                                         "Content-Type":"application/json"},
                                timeout=90))
                        if r.status_code==200:
                            resp=r; break
                        elif r.status_code==401:
                            raise ValueError("Groq key rejected! Get fresh key at console.groq.com")
                        elif r.status_code==429:
                            await self.upd(pct, f"Rate limit — waiting 20s… (model: {model})")
                            await asyncio.sleep(20)
                            continue
                        elif r.status_code==413:
                            last_err = "413 request too large"; break
                        else:
                            last_err = f"{r.status_code}: {r.text[:120]}"
                            break
                    except ValueError: raise
                    except Exception as e:
                        last_err = str(e)[:120]
                        await asyncio.sleep(5)
                if resp: break

            # Parse response or use fallback narration
            batch_panels = []
            if resp:
                try:
                    raw = resp.json()["choices"][0]["message"]["content"].strip()
                    raw = raw.lstrip("```json").lstrip("```").rstrip("```").strip()
                    parsed = json.loads(raw)
                    if isinstance(parsed,list):
                        batch_panels = parsed
                except Exception:
                    pass

            # Pad with generic narration if needed
            while len(batch_panels) < len(batch):
                i = len(batch_panels)
                batch_panels.append({
                    "panel":  offset+i+1,
                    "narration": (
                        f"The story unfolds dramatically on page {offset+i+1}. "
                        "Tension rises as the scene intensifies with every panel."
                    ),
                    "subtitle": f"Page {offset+i+1}",
                    "emotion": "action"
                })

            all_panels.extend(batch_panels[:len(batch)])

            # Polite delay between batches
            if b_idx < len(batches)-1:
                await asyncio.sleep(4)

        return all_panels

    # ── 4. TTS for all panels ──────────────────────────────────────
    async def _tts(self, panels:list[dict]) -> list[Path]:
        await self.upd(44, "Generating voice narration for all pages…")
        loop  = asyncio.get_event_loop()
        lang  = self.s.get("lang","en")
        voice = self.s.get("voice","calm")
        paths = []

        for i,panel in enumerate(panels):
            text = (panel.get("narration") or f"Page {i+1}.").strip()
            dst  = self.tmp/f"audio_{i:04d}.mp3"

            try:
                if self.ek:
                    vid = EL_VOICES.get((lang,voice),"EXAVITQu4vr4xnSDxMaL")
                    await loop.run_in_executor(
                        None, lambda t=text,v=vid,d=str(dst): do_elevenlabs(t,v,self.ek,d))
                else:
                    lc = GTTS_LANG.get(lang,"en")
                    await loop.run_in_executor(
                        None, lambda t=text,l=lc,d=str(dst): do_gtts(t,l,d))
            except Exception as e:
                logger.warning(f"TTS failed panel {i}: {e} — using gTTS fallback")
                try:
                    lc = GTTS_LANG.get(lang,"en")
                    await loop.run_in_executor(
                        None, lambda t=text,l=lc,d=str(dst): do_gtts(t,l,d))
                except Exception:
                    # Create 3s silent audio as last resort
                    run_ffmpeg("-f","lavfi","-i","anullsrc=r=44100:cl=mono",
                               "-t","3","-q:a","9","-acodec","libmp3lame",str(dst))
            paths.append(dst)
        return paths

    # ── 5. Render one panel to MP4 ────────────────────────────────
    def _render_panel(self, base:Image.Image, audio:Path,
                      panel:dict, idx:int, zoom_in:bool) -> Path:
        style    = self.s.get("style","cinematic")
        show_sub = bool(self.s.get("subtitles",1))
        spd_map  = {"slow":1.6,"normal":1.0,"fast":0.65}
        spd      = spd_map.get(self.s.get("speed","normal"),1.0)
        quality  = self.s.get("quality","hd")
        bitrate  = {"sd":"2000k","hd":"5000k","4k":"12000k"}.get(quality,"5000k")
        tw,th    = self.tw,self.th

        dur      = get_duration(audio)*spd
        dur      = max(dur, 2.5)
        subtitle = panel.get("subtitle","") if show_sub else ""
        n_frames = max(1,int(dur*FPS))

        # Determine background mode from settings
        bg_blur_val = self.s.get("bg_blur", 0)
        if bg_blur_val == 0:
            bg_mode = "crop"           # classic crop-to-fill
        elif bg_blur_val == 1:
            bg_mode = "blur"           # full page + blur bg, no zoom
        else:
            bg_mode = "blur_zoom"      # full page + blur bg + gentle zoom

        fdir = self.tmp/f"f_{idx:04d}"
        fdir.mkdir(exist_ok=True)

        for fi in range(n_frames):
            prog     = fi/max(n_frames-1,1)
            fade_in  = min(fi/(FPS*0.5+1), 1.0)
            fade_out = min((n_frames-fi)/(FPS*0.5+1), 1.0)
            alpha    = min(fade_in, fade_out)
            frame    = render_frame(base,prog,style,zoom_in,tw,th,subtitle,alpha,bg_mode)
            frame.save(str(fdir/f"f{fi:07d}.png"), "PNG")

        silent = self.tmp/f"s_{idx:04d}.mp4"
        run_ffmpeg("-framerate",str(FPS),
                   "-i",str(fdir/"f%07d.png"),
                   "-c:v","libx264","-preset","fast",
                   "-b:v",bitrate,"-pix_fmt","yuv420p",
                   "-t",str(dur), str(silent))

        out = self.tmp/f"clip_{idx:04d}.mp4"
        run_ffmpeg("-i",str(silent),"-i",str(audio),
                   "-c:v","copy","-c:a","aac","-b:a","128k",
                   "-shortest", str(out))

        shutil.rmtree(str(fdir), ignore_errors=True)
        silent.unlink(missing_ok=True)
        return out

    # ── 6. Concatenate all clips ───────────────────────────────────
    def _concat_clips(self, clips:list[Path]) -> Path:
        lst = self.tmp/"concat.txt"
        with open(lst,"w") as f:
            for c in clips:
                f.write(f"file '{c.resolve()}'\n")
        quality = self.s.get("quality","hd")
        bitrate = {"sd":"2000k","hd":"5000k","4k":"12000k"}.get(quality,"5000k")
        out     = self.tmp/"final.mp4"
        run_ffmpeg("-f","concat","-safe","0","-i",str(lst),
                   "-c:v","libx264","-preset","fast",
                   "-b:v",bitrate,"-c:a","aac","-b:a","128k",
                   "-pix_fmt","yuv420p","-movflags","+faststart",
                   str(out))
        return out

    # ── Main entry ─────────────────────────────────────────────────
    async def run(self, file_infos:list) -> tuple[str,int]:
        check_ffmpeg()

        raw    = await self._download(file_infos)
        images = await self._extract(raw)
        if not images:
            raise ValueError(
                "No valid images found!\n"
                "Please send JPG/PNG/WEBP images, a manga PDF, or a ZIP of manga pages.")

        panels = await self._ai_script(images)
        n      = min(len(images), len(panels))
        images = images[:n]
        panels = panels[:n]
        audios = await self._tts(panels)

        # Grade all images first
        await self.upd(66, f"Colour grading {n} manga pages…")
        loop     = asyncio.get_event_loop()
        grade    = self.s.get("color_grade","vivid")
        bg_mode  = {0:"crop",1:"blur",2:"blur_zoom"}.get(self.s.get("bg_blur",0),"crop")
        bases    = []
        for p in images:
            if bg_mode == "crop":
                # Crop mode: pre-crop image to frame size
                img = await loop.run_in_executor(
                    None,
                    lambda _p=p: crop_to_fill(
                        apply_grade(Image.open(_p).convert("RGB"), grade),
                        self.tw, self.th))
            else:
                # Blur modes: keep original aspect ratio image, graded only
                img = await loop.run_in_executor(
                    None,
                    lambda _p=p: apply_grade(Image.open(_p).convert("RGB"), grade))
            bases.append(img)

        # Render each panel
        clips = []
        for i,(base,audio,panel) in enumerate(zip(bases,audios,panels)):
            pct = 68+int((i/n)*24)
            await self.upd(pct, f"Animating page {i+1} of {n}…")
            clip = await loop.run_in_executor(
                None,
                lambda b=base,a=audio,p=panel,idx=i:
                    self._render_panel(b,a,p,idx,zoom_in=(idx%2==0)))
            clips.append(clip)

        await self.upd(93, "Merging all clips into final video…")
        final = await loop.run_in_executor(None, lambda: self._concat_clips(clips))
        await self.upd(100, "Done! Sending your video…")
        return str(final), n
