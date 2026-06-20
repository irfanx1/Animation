"""
MangaVoice Ultra v4.0 — pipeline.py
AI: Groq (free, vision, fast)
TTS: gTTS free / ElevenLabs premium  
Video: Direct FFmpeg — rock solid
Pages: Up to 35
NEW: blur opacity, fast zoom curve, action clips, parallax bg
"""

import io, json, math, asyncio, logging, textwrap
import zipfile, tempfile, base64, subprocess, shutil, random
from pathlib import Path

import requests
import numpy as np
from PIL import (Image, ImageFilter, ImageEnhance,
                 ImageDraw, ImageFont, ImageChops, ImageOps)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════
QUALITY_DIMS = {"sd":(854,480),"hd":(1280,720),"4k":(1920,1080)}
FPS          = 15        # Reduced from 24 — much faster render, still smooth
MAX_PAGES    = 35
GROQ_BATCH   = 4
GROQ_MODELS  = [
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
    "llama-3.2-11b-vision-preview",
]
GTTS_LANG    = {"en":"en","hi":"hi"}
EL_VOICES    = {
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

# ═══════════════════════════════════════════════
# FFMPEG
# ═══════════════════════════════════════════════
def run_ffmpeg(*args):
    cmd = ["ffmpeg","-y","-loglevel","error"]+[str(a) for a in args]
    r   = subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if r.returncode!=0:
        raise RuntimeError(f"FFmpeg:\n{r.stderr.decode(errors='replace')[-600:]}")

def get_dur(path)->float:
    r = subprocess.run(
        ["ffprobe","-v","error","-show_entries","format=duration",
         "-of","default=noprint_wrappers=1:nokey=1",str(path)],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:    return max(float(r.stdout.decode().strip()),1.5)
    except: return 4.0

def check_ffmpeg():
    if subprocess.run(["ffmpeg","-version"],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE).returncode!=0:
        raise RuntimeError("FFmpeg not found! sudo apt install ffmpeg")

# ═══════════════════════════════════════════════
# TTS
# ═══════════════════════════════════════════════
def do_gtts(text,lang,path):
    from gtts import gTTS
    gTTS(text=text,lang=lang,slow=False).save(path)

def do_elevenlabs(text,voice_id,api_key,path):
    r = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers={"xi-api-key":api_key,"Content-Type":"application/json"},
        json={"text":text,"model_id":"eleven_multilingual_v2",
              "voice_settings":{"stability":0.45,"similarity_boost":0.8}},timeout=30)
    r.raise_for_status()
    Path(path).write_bytes(r.content)

def make_silent_audio(path,dur=3.0):
    run_ffmpeg("-f","lavfi","-i","anullsrc=r=44100:cl=mono",
               "-t",str(dur),"-q:a","9","-acodec","libmp3lame",str(path))

# ═══════════════════════════════════════════════
# EASING FUNCTIONS  (key for feel of animation)
# ═══════════════════════════════════════════════
def ease_in_out(t):
    t=min(max(t,0),1)
    return t*t*(3-2*t)

def ease_out_expo(t):
    """Fast start, slow end — for zoom that starts quick."""
    t=min(max(t,0),1)
    return 1-(2**(-10*t)) if t>0 else 0

def ease_in_expo(t):
    """Slow start, fast end — for dramatic zoom-in."""
    t=min(max(t,0),1)
    return 2**(10*t-10) if t<1 else 1

def ease_out_bounce(t):
    """Bouncy zoom — good for manga impact."""
    t=min(max(t,0),1)
    n1,d1=7.5625,2.75
    if t<1/d1:   return n1*t*t
    elif t<2/d1: t-=1.5/d1; return n1*t*t+0.75
    elif t<2.5/d1: t-=2.25/d1; return n1*t*t+0.9375
    else:           t-=2.625/d1; return n1*t*t+0.984375

# ═══════════════════════════════════════════════
# COLOR GRADE
# ═══════════════════════════════════════════════
def apply_grade(img,grade):
    if grade=="vivid":
        img=ImageEnhance.Color(img).enhance(1.4)
        img=ImageEnhance.Contrast(img).enhance(1.2)
        img=ImageEnhance.Sharpness(img).enhance(1.3)
    elif grade=="muted":
        img=ImageEnhance.Color(img).enhance(0.55)
        img=ImageEnhance.Contrast(img).enhance(0.88)
    elif grade=="warm":
        r,g,b=img.split()
        img=Image.merge("RGB",(r.point(lambda x:min(255,x+28)),g,b.point(lambda x:max(0,x-18))))
        img=ImageEnhance.Contrast(img).enhance(1.12)
    elif grade=="cold":
        r,g,b=img.split()
        img=Image.merge("RGB",(r.point(lambda x:max(0,x-12)),g,b.point(lambda x:min(255,x+22))))
    elif grade=="manga_ink":
        img=ImageEnhance.Color(img).enhance(0.0)
        img=ImageEnhance.Contrast(img).enhance(1.8)
        img=ImageEnhance.Sharpness(img).enhance(2.5)
    elif grade=="golden":
        r,g,b=img.split()
        img=Image.merge("RGB",(r.point(lambda x:min(255,x+20)),g.point(lambda x:min(255,x+8)),b.point(lambda x:max(0,x-20))))
        img=ImageEnhance.Contrast(img).enhance(1.15)
    elif grade=="cinematic":
        # Teal & orange LUT simulation
        r,g,b=img.split()
        r=r.point(lambda x:min(255,int(x*1.1)+10))
        b=b.point(lambda x:min(255,int(x*0.85)+15))
        img=Image.merge("RGB",(r,g,b))
        img=ImageEnhance.Contrast(img).enhance(1.18)
        img=ImageEnhance.Color(img).enhance(0.9)
    elif grade=="bleach":
        img=ImageEnhance.Contrast(img).enhance(1.4)
        img=ImageEnhance.Color(img).enhance(0.3)
        img=ImageEnhance.Brightness(img).enhance(1.1)
    return img

# ═══════════════════════════════════════════════
# VIGNETTE
# ═══════════════════════════════════════════════
def add_vignette(img,strength=0.5):
    w,h=img.size
    mask=Image.new("L",(w,h),0)
    d=ImageDraw.Draw(mask)
    cx,cy=w//2,h//2
    for i in range(40,0,-1):
        a=int(255*(1-(i/40)**0.6)*strength)
        rx,ry=int(cx*i/40),int(cy*i/40)
        d.ellipse([cx-rx,cy-ry,cx+rx,cy+ry],fill=255-a)
    return Image.composite(img,Image.new("RGB",(w,h),(0,0,0)),mask)

# ═══════════════════════════════════════════════
# SPEED LINES (manga action effect)
# ═══════════════════════════════════════════════
def add_speed_lines(img,intensity=0.6,alpha=128):
    w,h=img.size
    overlay=Image.new("RGBA",(w,h),(0,0,0,0))
    draw=ImageDraw.Draw(overlay)
    cx,cy=w//2,h//2
    n_lines=int(40*intensity)
    for _ in range(n_lines):
        angle=random.uniform(0,2*math.pi)
        r_start=random.uniform(min(w,h)*0.05,min(w,h)*0.15)
        r_end=random.uniform(min(w,h)*0.6,min(w,h)*1.2)
        x1=cx+r_start*math.cos(angle)
        y1=cy+r_start*math.sin(angle)
        x2=cx+r_end*math.cos(angle+random.uniform(-0.08,0.08))
        y2=cy+r_end*math.sin(angle+random.uniform(-0.08,0.08))
        lw=random.randint(1,3)
        draw.line([(x1,y1),(x2,y2)],fill=(255,255,255,alpha),width=lw)
    base=img.convert("RGBA")
    return Image.alpha_composite(base,overlay).convert("RGB")

# ═══════════════════════════════════════════════
# FLASH FRAME (white flash for impact moments)
# ═══════════════════════════════════════════════
def flash_frame(img,intensity=0.5):
    white=Image.new("RGB",img.size,(255,255,255))
    return Image.blend(img,white,intensity)

# ═══════════════════════════════════════════════
# BLUR BG FRAME BUILDERS
# ═══════════════════════════════════════════════
def build_blur_bg(img,tw,th,blur_radius,brightness,progress=0.0,
                  zoom_in=True,zoom_amount=0.0,ease_fn=ease_in_out,
                  parallax=False):
    """
    Full-page manga in centre over blurred background.
    blur_radius  : 0–40  (user-controlled opacity/blur)
    brightness   : 0.2–0.8 (how dark the bg is)
    zoom_amount  : 0.0–0.12 (how much the page zooms, 0=static)
    parallax     : bg moves opposite to fg for depth effect
    """
    img=img.convert("RGB")
    sw,sh=img.size

    # ── Background ──────────────────────────────────────────
    bg=img.resize((tw,th),Image.BILINEAR)
    if blur_radius>0:
        bg=bg.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    bg=ImageEnhance.Brightness(bg).enhance(brightness)

    # Parallax: bg slowly pans opposite to fg
    if parallax and zoom_amount>0:
        p=ease_fn(progress)
        pan=int(tw*0.02*p)*(1 if zoom_in else -1)
        bg_arr=np.array(bg)
        if pan>0:
            bg_arr=np.roll(bg_arr,pan,axis=1)
            bg_arr[:,:pan]=bg_arr[:,pan:pan+1]
        elif pan<0:
            bg_arr=np.roll(bg_arr,pan,axis=1)
            bg_arr[:,pan:]=bg_arr[:,pan-1:pan]
        bg=Image.fromarray(bg_arr)

    # ── Foreground: fit full page ────────────────────────────
    pad=int(min(tw,th)*0.045)
    max_w=tw-pad*2
    max_h=th-pad*2
    base_scale=min(max_w/sw,max_h/sh)

    # Apply zoom with chosen easing
    p=ease_fn(progress)
    if zoom_amount>0:
        zoom=(1.0+zoom_amount*p) if zoom_in else (1.0+zoom_amount*(1-p))
    else:
        zoom=1.0
    fs=base_scale*zoom

    nw=int(sw*fs)
    nh=int(sh*fs)
    fg=img.resize((nw,nh),Image.BILINEAR)

    # Pan with zoom
    if zoom_amount>0:
        pan_x=int(tw*0.008*p)*(1 if zoom_in else -1)
        pan_y=int(th*0.004*p)
    else:
        pan_x=pan_y=0

    x=max(0,(tw-nw)//2+pan_x)
    y=max(0,(th-nh)//2+pan_y)
    # Clamp to frame
    x=min(x,tw-nw) if nw<=tw else 0
    y=min(y,th-nh) if nh<=th else 0

    result=bg.copy()
    result.paste(fg,(x,y))

    # Shadow border around page
    draw=ImageDraw.Draw(result)
    draw.rectangle([x-4,y-4,x+nw+4,y+nh+4],outline=(0,0,0),width=5)
    draw.rectangle([x-1,y-1,x+nw+1,y+nh+1],outline=(40,40,40),width=1)

    return result

def build_crop_fill(img,tw,th,progress=0.0,zoom_in=True,
                    zoom_amount=0.03,style="cinematic",ease_fn=ease_in_out):
    """Crop-to-fill with configurable zoom amount and easing."""
    img=img.convert("RGB")
    sw,sh=img.size
    # Scale to fill
    base_scale=max(tw/sw,th/sh)
    p=ease_fn(progress)

    if style=="manga":
        # Bounce zoom — fast punch at start
        p2=ease_out_bounce(min(progress*1.5,1.0))
        scale=base_scale*(1.0+zoom_amount*1.5*p2)
    elif style=="dramatic":
        p2=ease_in_expo(progress)
        scale=base_scale*((1.0+zoom_amount*p2) if zoom_in else (1.0+zoom_amount*(1-p2)))
    else:
        scale=base_scale*((1.0+zoom_amount*p) if zoom_in else (1.0+zoom_amount*(1-p)))

    nw=int(sw*scale)
    nh=int(sh*scale)
    img=img.resize((nw,nh),Image.LANCZOS)
    pan_x=int(nw*0.008*p)*(1 if zoom_in else -1)
    pan_y=int(nh*0.004*p)
    left=max(0,min((nw-tw)//2+pan_x,nw-tw))
    top =max(0,min((nh-th)//2+pan_y,nh-th))
    return img.crop((left,top,left+tw,top+th))

# ═══════════════════════════════════════════════
# STYLE FX
# ═══════════════════════════════════════════════
def apply_style_fx(img,style,progress,emotion="calm"):
    if style=="noir" or (style=="dramatic" and emotion in ("action","tragedy")):
        img=add_vignette(img,0.5)
    elif style=="retro":
        arr=np.array(img,dtype=np.int16)
        grain=np.random.randint(-12,12,arr.shape,dtype=np.int16)
        img=Image.fromarray(np.clip(arr+grain,0,255).astype(np.uint8))
        img=add_vignette(img,0.38)
    elif style=="anime":
        glow=img.filter(ImageFilter.GaussianBlur(radius=3))
        img=ImageChops.screen(img,ImageEnhance.Brightness(glow).enhance(0.28))
    elif style=="manga":
        img=img.filter(ImageFilter.SHARPEN)
    # Action flash at very start for intense scenes
    if emotion=="action" and progress<0.04 and style in ("manga","dramatic"):
        img=flash_frame(img,0.35*(1-progress/0.04))
    return img

# ═══════════════════════════════════════════════
# SUBTITLES  (clean Netflix-style bar)
# ═══════════════════════════════════════════════
_FONT={}
def load_font(size):
    if size in _FONT: return _FONT[size]
    for p in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Windows/Fonts/arialbd.ttf",
    ]:
        if Path(p).exists():
            try:
                f=ImageFont.truetype(p,size); _FONT[size]=f; return f
            except: pass
    f=ImageFont.load_default(); _FONT[size]=f; return f

def burn_subtitle(frame,text,alpha,style,tw):
    if not text.strip() or alpha<=0: return frame
    frame=frame.copy().convert("RGBA")
    ov=Image.new("RGBA",frame.size,(0,0,0,0))
    draw=ImageDraw.Draw(ov)
    fs=max(18,int(tw*0.027))
    fnt=load_font(fs)
    lines=textwrap.wrap(text,width=50)
    lh=int(fs*1.55)
    total_h=len(lines)*lh+20
    w,h=frame.size
    y0=h-total_h-int(h*0.035)
    # Full-width semi-transparent bar
    draw.rectangle([0,y0-14,w,y0+total_h+10],fill=(0,0,0,int(185*alpha)))
    # Accent line
    acc={"dramatic":(255,210,0),"noir":(180,180,180),"retro":(255,175,70),
         "anime":(100,200,255),"manga":(255,50,50)}.get(style,(255,255,255))
    draw.rectangle([0,y0-14,w,y0-11],fill=(*acc,int(220*alpha)))
    txt_a=int(255*alpha)
    col={"dramatic":(255,225,50,txt_a),"noir":(220,220,220,txt_a),
         "retro":(255,205,120,txt_a),"anime":(160,225,255,txt_a),
         "manga":(255,255,255,txt_a)}.get(style,(255,255,255,txt_a))
    for i,line in enumerate(lines):
        bbox=draw.textbbox((0,0),line,font=fnt)
        lw=bbox[2]-bbox[0]
        x=(w-lw)//2; y=y0+i*lh
        draw.text((x+1,y+1),line,font=fnt,fill=(0,0,0,int(txt_a*0.9)))
        draw.text((x,y),line,font=fnt,fill=col)
    return Image.alpha_composite(frame,ov).convert("RGB")

# ═══════════════════════════════════════════════
# ACTION CLIP GENERATOR
# ═══════════════════════════════════════════════
def generate_action_clip(img,tw,th,dur,tmp,idx,style,grade):
    """
    For high-action panels: generates a dynamic multi-shot clip.
    Zooms into key sub-regions of the panel, adds speed lines,
    flash frames — feels like real animation.
    """
    img=img.convert("RGB")
    w,h=img.size

    # Define 4 sub-region crops (simulate camera cuts)
    regions=[
        (0,0,w,h//2),          # top half
        (w//4,h//4,3*w//4,3*h//4), # centre
        (0,h//2,w,h),          # bottom half
        (w//3,0,w,h),          # right side
    ]
    random.shuffle(regions)
    regions=regions[:3]  # use 3 cuts

    n_frames=max(1,int(dur*FPS))
    cut_frames=n_frames//len(regions)
    fdir=tmp/f"act_{idx:04d}"
    fdir.mkdir(exist_ok=True)
    fi=0

    for r_idx,(rx0,ry0,rx1,ry1) in enumerate(regions):
        rw,rh=rx1-rx0,ry1-ry0
        crop=img.crop((rx0,ry0,rx1,ry1))
        crop=apply_grade(crop.convert("RGB"),grade)

        for ci in range(cut_frames):
            p=ci/max(cut_frames-1,1)
            # Zoom into the region
            scale=max(tw/rw,th/rh)*(1.0+0.08*ease_out_expo(p))
            nw2=int(rw*scale); nh2=int(rh*scale)
            frame=crop.resize((nw2,nh2),Image.LANCZOS)
            left=(nw2-tw)//2; top=(nh2-th)//2
            left=max(0,min(left,nw2-tw)); top=max(0,min(top,nh2-th))
            frame=frame.crop((left,top,left+tw,top+th))

            # Speed lines on action panels
            if style in ("manga","dramatic") and r_idx<2:
                s_alpha=int(180*(1-p)*0.7)
                if s_alpha>20:
                    frame=add_speed_lines(frame,0.5,s_alpha)

            # Flash at cut start
            if ci<3 and r_idx>0:
                frame=flash_frame(frame,0.4*(1-ci/3))

            frame=apply_style_fx(frame,style,p,"action")
            frame.save(str(fdir/f"f{fi:07d}.jpg"),"JPEG",quality=85,optimize=False)
            fi+=1

    # Fill remaining frames with last frame if needed
    while fi<n_frames:
        frame.save(str(fdir/f"f{fi:07d}.jpg"),"JPEG",quality=85,optimize=False)
        fi+=1

    return fdir

# ═══════════════════════════════════════════════
# PIPELINE
# ═══════════════════════════════════════════════
class MangaPipeline:
    def __init__(self,bot,chat_id,update_progress,settings,groq_key,elevenlabs_key):
        self.bot  = bot
        self.cid  = chat_id
        self.upd  = update_progress
        self.s    = settings
        self.gk   = groq_key
        self.ek   = elevenlabs_key
        self.tmp  = Path(tempfile.mkdtemp())
        self.tw,self.th = QUALITY_DIMS.get(settings.get("quality","hd"),(1280,720))

    # ── 1. Download ──────────────────────────────────────────
    async def _download(self,file_infos):
        await self.upd(5,"Downloading manga files…")
        out=[]
        for i,fi in enumerate(file_infos):
            tf=await self.bot.get_file(fi["file_id"])
            ext={"image":"jpg","pdf":"pdf","zip":"zip"}.get(fi["type"],"jpg")
            dst=self.tmp/f"raw_{i:03d}.{ext}"
            await tf.download_to_drive(str(dst))
            out.append((dst,fi["type"]))
        return out

    # ── 2. Extract ───────────────────────────────────────────
    async def _extract(self,raw):
        await self.upd(12,"Extracting manga pages…")
        imgs=[]
        for path,ftype in raw:
            if ftype=="image":   imgs.append(path)
            elif ftype=="pdf":   imgs.extend(self._from_pdf(path))
            elif ftype=="zip":   imgs.extend(self._from_zip(path))
        valid=[]
        for p in sorted(imgs):
            try:
                with Image.open(p) as t: t.verify()
                valid.append(p)
            except: pass
        return valid[:MAX_PAGES]

    def _from_pdf(self,pdf):
        try:
            import fitz
            doc=fitz.open(str(pdf))
            out=[]
            for i,page in enumerate(doc):
                pix=page.get_pixmap(matrix=fitz.Matrix(2,2))
                dst=self.tmp/f"pdf_{pdf.stem}_{i:04d}.jpg"
                pix.save(str(dst))
                out.append(dst)
            doc.close()
            return out
        except ImportError:
            raise ImportError("Install: pip install pymupdf")

    def _from_zip(self,zpath):
        exdir=self.tmp/f"zip_{zpath.stem}"
        exdir.mkdir(exist_ok=True)
        out=[]
        with zipfile.ZipFile(zpath) as zf:
            names=sorted([n for n in zf.namelist()
                if Path(n).suffix.lower() in (".jpg",".jpeg",".png",".webp")
                and "__MACOSX" not in n and not Path(n).name.startswith(".")])
            for n in names:
                dst=exdir/f"{len(out):04d}{Path(n).suffix}"
                dst.write_bytes(zf.read(n))
                out.append(dst)
        return out

    # ── 3. AI Script (Groq, batched) ─────────────────────────
    async def _ai_script(self,images):
        await self.upd(20,"AI reading your manga…")
        key=(self.gk or "").strip()
        if not key:
            raise ValueError("GROQ_API_KEY empty in config.json!\nGet free key: console.groq.com")
        if not key.startswith("gsk_"):
            raise ValueError(f"GROQ_API_KEY wrong format: {key[:14]}...\nMust start with gsk_")

        lang_p=("Respond ONLY in Hindi (Devanagari)." if self.s.get("lang")=="hi"
                else "Respond ONLY in English.")
        tone_p={
            "calm":     "Smooth, contemplative story-teller.",
            "dramatic": "Intense cinematic narrator — punchy, dramatic.",
            "energetic":"Excited anime dub — exclamations, fast-paced.",
            "narrator": "Deep documentary narrator voice.",
            "deep":     "Serious, authoritative, powerful voice.",
            "whisper":  "Tense, quiet, suspenseful whisper.",
        }.get(self.s.get("voice","calm"),"Smooth story-teller.")

        all_panels=[]
        batches=[images[i:i+GROQ_BATCH] for i in range(0,len(images),GROQ_BATCH)]

        for b_idx,batch in enumerate(batches):
            pct=20+int((b_idx/len(batches))*22)
            offset=b_idx*GROQ_BATCH
            await self.upd(pct,f"AI analysing pages {offset+1}–{offset+len(batch)}/{len(images)}…")

            parts=[]
            for p in batch:
                img=Image.open(p).convert("RGB")
                img.thumbnail((480,480),Image.LANCZOS)
                buf=io.BytesIO()
                img.save(buf,"JPEG",quality=48,optimize=True)
                b64=base64.b64encode(buf.getvalue()).decode()
                parts.append({"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{b64}"}})

            parts.append({"type":"text","text":(
                f"You are a professional manga narrator. {lang_p} {tone_p}\n"
                f"These are manga pages {offset+1} to {offset+len(batch)}.\n"
                "Return ONLY a JSON array — no markdown.\n"
                f"Exactly {len(batch)} objects:\n"
                '[{"panel":1,"narration":"2-3 vivid sentences","subtitle":"max 8 words",'
                '"emotion":"action|mystery|romance|comedy|tragedy|calm","is_action_scene":true}]\n'
                "Set is_action_scene:true for fights, explosions, intense moments."
            )})

            payload={"messages":[{"role":"user","content":parts}],"temperature":0.75,"max_tokens":1024}
            resp=None; last_err="unknown"

            for model in GROQ_MODELS:
                payload["model"]=model
                for attempt in range(3):
                    try:
                        r=await asyncio.get_event_loop().run_in_executor(None,lambda m=model:requests.post(
                            "https://api.groq.com/openai/v1/chat/completions",
                            json={**payload,"model":m},
                            headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"},
                            timeout=90))
                        if r.status_code==200:   resp=r; break
                        elif r.status_code==401: raise ValueError("Groq key rejected! console.groq.com")
                        elif r.status_code==429:
                            await self.upd(pct,"Rate limit — waiting 20s…")
                            await asyncio.sleep(20)
                        else: last_err=f"{r.status_code}:{r.text[:100]}"; break
                    except ValueError: raise
                    except Exception as e: last_err=str(e)[:80]; await asyncio.sleep(5)
                if resp: break

            batch_panels=[]
            if resp:
                try:
                    raw=resp.json()["choices"][0]["message"]["content"].strip()
                    raw=raw.lstrip("```json").lstrip("```").rstrip("```").strip()
                    parsed=json.loads(raw)
                    if isinstance(parsed,list): batch_panels=parsed
                except: pass

            while len(batch_panels)<len(batch):
                i=len(batch_panels)
                batch_panels.append({"panel":offset+i+1,
                    "narration":f"The story continues on page {offset+i+1} with building intensity.",
                    "subtitle":f"Page {offset+i+1}","emotion":"calm","is_action_scene":False})
            all_panels.extend(batch_panels[:len(batch)])
            if b_idx<len(batches)-1: await asyncio.sleep(2)
        return all_panels

    # ── 4. TTS ───────────────────────────────────────────────
    async def _tts(self,panels):
        await self.upd(44,"Generating voice narration…")
        loop=asyncio.get_event_loop()
        lang=self.s.get("lang","en")
        voice=self.s.get("voice","calm")
        paths=[]
        for i,panel in enumerate(panels):
            text=(panel.get("narration") or f"Page {i+1}.").strip()
            dst=self.tmp/f"audio_{i:04d}.mp3"
            try:
                if self.ek:
                    vid=EL_VOICES.get((lang,voice),"EXAVITQu4vr4xnSDxMaL")
                    await loop.run_in_executor(None,lambda t=text,v=vid,d=str(dst):do_elevenlabs(t,v,self.ek,d))
                else:
                    lc=GTTS_LANG.get(lang,"en")
                    await loop.run_in_executor(None,lambda t=text,l=lc,d=str(dst):do_gtts(t,l,d))
            except Exception as e:
                logger.warning(f"TTS panel {i}: {e}")
                try:
                    lc=GTTS_LANG.get(lang,"en")
                    await loop.run_in_executor(None,lambda t=text,l=lc,d=str(dst):do_gtts(t,l,d))
                except:
                    make_silent_audio(dst,3.0)
            paths.append(dst)
        return paths

    # ── 5. Render one panel ──────────────────────────────────
    def _render_panel(self,base,audio,panel,idx,zoom_in):
        s=self.s
        style=s.get("style","cinematic")
        show_sub=bool(s.get("subtitles",1))
        spd={"slow":1.6,"normal":1.0,"fast":0.65}.get(s.get("speed","normal"),1.0)
        quality=s.get("quality","hd")
        bitrate={"sd":"2000k","hd":"5000k","4k":"12000k"}.get(quality,"5000k")
        tw,th=self.tw,self.th

        dur=get_dur(audio)*spd
        dur=min(max(dur,2.5), 8.0)  # cap at 8s per panel
        subtitle=panel.get("subtitle","") if show_sub else ""
        emotion=panel.get("emotion","calm")
        is_action=panel.get("is_action_scene",False)
        n_frames=max(1,int(dur*FPS))

        # BG mode settings
        bg_val=s.get("bg_blur",0)
        blur_r=int(s.get("blur_radius",22))
        blur_b=float(s.get("blur_brightness",0.4))
        parallax=bool(s.get("parallax",1))
        zoom_amt=float(s.get("zoom_amount",0.06))

        # Easing function based on emotion/style
        if style=="manga" or emotion=="action":
            ease_fn=ease_out_expo
        elif style=="dramatic":
            ease_fn=ease_in_out
        elif emotion in ("romance","calm"):
            ease_fn=ease_in_out
        else:
            ease_fn=ease_out_expo

        fdir=self.tmp/f"f_{idx:04d}"
        fdir.mkdir(exist_ok=True)

        # ACTION CLIP: use multi-cut animation for intense scenes
        if is_action and s.get("action_clips",1) and style in ("manga","dramatic","cinematic"):
            act_dir=generate_action_clip(base,tw,th,dur,self.tmp,idx,style,
                                          s.get("color_grade","vivid"))
            # Build video from action frames
            silent=self.tmp/f"s_{idx:04d}.mp4"
            run_ffmpeg("-framerate",str(FPS),"-i",str(act_dir/f"f%07d.png"),
                       "-c:v","libx264","-preset","ultrafast",
                       "-b:v",bitrate,"-pix_fmt","yuv420p","-t",str(dur),str(silent))
            shutil.rmtree(str(act_dir),ignore_errors=True)
        else:
            # Standard render
            grade=s.get("color_grade","vivid")
            for fi in range(n_frames):
                progress=fi/max(n_frames-1,1)
                fade_in=min(fi/(FPS*0.45+1),1.0)
                fade_out=min((n_frames-fi)/(FPS*0.45+1),1.0)
                sub_alpha=min(fade_in,fade_out)

                if bg_val==0:
                    # Crop fill mode
                    frame=build_crop_fill(base,tw,th,progress,zoom_in,
                                          zoom_amt,style,ease_fn)
                    frame=apply_grade(frame,grade)
                    frame=apply_style_fx(frame,style,progress,emotion)
                elif bg_val==1:
                    # Blur bg, no zoom
                    frame=build_blur_bg(base,tw,th,blur_r,blur_b,
                                        progress,zoom_in,0.0,ease_fn,parallax)
                    frame=apply_style_fx(frame,style,progress,emotion)
                else:
                    # Blur bg + zoom
                    frame=build_blur_bg(base,tw,th,blur_r,blur_b,
                                        progress,zoom_in,zoom_amt,ease_fn,parallax)
                    frame=apply_style_fx(frame,style,progress,emotion)

                if subtitle and sub_alpha>0:
                    frame=burn_subtitle(frame,subtitle,sub_alpha,style,tw)
                frame.save(str(fdir/f"f{fi:07d}.jpg"),"JPEG",quality=85,optimize=False)

            silent=self.tmp/f"s_{idx:04d}.mp4"
            run_ffmpeg("-framerate",str(FPS),"-i",str(fdir/"f%07d.jpg"),
                       "-c:v","libx264","-preset","ultrafast",
                       "-b:v",bitrate,"-pix_fmt","yuv420p","-t",str(dur),str(silent))
            shutil.rmtree(str(fdir),ignore_errors=True)

        out=self.tmp/f"clip_{idx:04d}.mp4"
        run_ffmpeg("-i",str(silent),"-i",str(audio),
                   "-c:v","copy","-c:a","aac","-b:a","128k","-shortest",str(out))
        silent.unlink(missing_ok=True)
        return out

    # ── 6. Concat ────────────────────────────────────────────
    def _concat(self,clips):
        lst=self.tmp/"concat.txt"
        with open(lst,"w") as f:
            for c in clips: f.write(f"file '{c.resolve()}'\n")
        quality=self.s.get("quality","hd")
        bitrate={"sd":"2000k","hd":"5000k","4k":"12000k"}.get(quality,"5000k")
        out=self.tmp/"final.mp4"
        run_ffmpeg("-f","concat","-safe","0","-i",str(lst),
                   "-c:v","libx264","-preset","ultrafast",
                   "-b:v",bitrate,"-c:a","aac","-b:a","128k",
                   "-pix_fmt","yuv420p","-movflags","+faststart",str(out))
        return out

    # ── Entry ─────────────────────────────────────────────────
    async def run(self,file_infos):
        check_ffmpeg()
        raw=await self._download(file_infos)
        images=await self._extract(raw)
        if not images:
            raise ValueError("No valid images found! Send JPG/PNG/PDF/ZIP manga.")
        panels=await self._ai_script(images)
        n=min(len(images),len(panels))
        images=images[:n]; panels=panels[:n]
        audios=await self._tts(panels)

        await self.upd(66,f"Preparing {n} pages…")
        loop=asyncio.get_event_loop()
        bases=[]
        for p in images:
            img=await loop.run_in_executor(None,lambda _p=p:Image.open(_p).convert("RGB"))
            bases.append(img)

        clips=[]
        for i,(base,audio,panel) in enumerate(zip(bases,audios,panels)):
            pct=68+int((i/n)*24)
            label="⚡ Action clip" if panel.get("is_action_scene") else f"🎬 Page {i+1}/{n}"
            await self.upd(pct,f"Rendering {label}…")
            clip=await loop.run_in_executor(None,
                lambda b=base,a=audio,p=panel,idx=i:
                    self._render_panel(b,a,p,idx,zoom_in=(idx%2==0)))
            clips.append(clip)

        await self.upd(93,"Merging final video…")
        final=await loop.run_in_executor(None,lambda:self._concat(clips))
        await self.upd(100,"Done!")
        return str(final),n
