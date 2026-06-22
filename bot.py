"""
MangaVoice Ultra v5.0 - bot.py
Clean structure. All features. Zero stale references.
AI: Groq (free) | TTS: gTTS / ElevenLabs | Video: FFmpeg
"""

import json, asyncio, logging, sqlite3
import requests as req_lib
from pathlib import Path

from telegram import (Update, InlineKeyboardButton,
                      InlineKeyboardMarkup, BotCommand)
from telegram.ext import (Application, CommandHandler, MessageHandler,
                           CallbackQueryHandler, ContextTypes, filters)
from telegram.constants import ParseMode, ChatAction

# ─────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
BASE    = Path(__file__).parent
CFG     = json.loads((BASE / "config.json").read_text())

TOKEN        = CFG["BOT_TOKEN"]
GROQ_KEY     = CFG.get("GROQ_API_KEY", "")
EL_KEY       = CFG.get("ELEVENLABS_API_KEY", "")
START_IMG    = CFG.get("START_IMAGE_URL", "")

# ─────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────
DB_PATH = BASE / "mangavoice.db"

DEFAULT = {
    "lang":            "en",
    "voice":           "calm",
    "style":           "cinematic",
    "color_grade":     "vivid",
    "subtitles":       1,
    "speed":           "normal",
    "quality":         "hd",
    "blur_radius":     18,
    "blur_brightness": 0.82,
    "zoom_amount":     0.04,
}

def db_init():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id          INTEGER PRIMARY KEY,
            username         TEXT    DEFAULT '',
            name             TEXT    DEFAULT '',
            lang             TEXT    DEFAULT 'en',
            voice            TEXT    DEFAULT 'calm',
            style            TEXT    DEFAULT 'cinematic',
            color_grade      TEXT    DEFAULT 'vivid',
            subtitles        INTEGER DEFAULT 1,
            speed            TEXT    DEFAULT 'normal',
            quality          TEXT    DEFAULT 'hd',
            blur_radius      INTEGER DEFAULT 18,
            blur_brightness  REAL    DEFAULT 0.82,
            zoom_amount      REAL    DEFAULT 0.04,
            joined           TEXT    DEFAULT (datetime('now')),
            videos           INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS history (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            pages   INTEGER,
            style   TEXT,
            lang    TEXT,
            ts      TEXT DEFAULT (datetime('now'))
        );
    """)
    # Safe migrations for existing DBs
    for col, defval in [
        ("blur_radius",     "INTEGER DEFAULT 18"),
        ("blur_brightness", "REAL    DEFAULT 0.82"),
        ("zoom_amount",     "REAL    DEFAULT 0.04"),
    ]:
        try:
            con.execute(f"ALTER TABLE users ADD COLUMN {col} {defval}")
            con.commit()
        except Exception:
            pass
    con.close()

def db_get(cid: int) -> dict | None:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM users WHERE chat_id=?", (cid,)).fetchone()
    con.close()
    return dict(row) if row else None

def db_ensure(cid: int, username: str = "", name: str = "") -> dict:
    s = db_get(cid)
    if s is None:
        con = sqlite3.connect(DB_PATH)
        con.execute(
            "INSERT OR IGNORE INTO users (chat_id, username, name) VALUES (?,?,?)",
            (cid, username, name))
        con.commit()
        con.close()
        s = db_get(cid)
    return s

def db_set(cid: int, **kw):
    if not kw: return
    con  = sqlite3.connect(DB_PATH)
    sets = ", ".join(f"{k}=?" for k in kw)
    con.execute(f"UPDATE users SET {sets} WHERE chat_id=?",
                list(kw.values()) + [cid])
    con.commit()
    con.close()

def db_log(cid: int, pages: int, style: str, lang: str):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO history (chat_id, pages, style, lang) VALUES (?,?,?,?)",
        (cid, pages, style, lang))
    con.execute("UPDATE users SET videos=videos+1 WHERE chat_id=?", (cid,))
    con.commit()
    con.close()

def db_stats(cid: int) -> dict:
    con   = sqlite3.connect(DB_PATH)
    row   = con.execute(
        "SELECT videos, joined FROM users WHERE chat_id=?", (cid,)).fetchone()
    total = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    con.close()
    return {
        "videos":      row[0] if row else 0,
        "joined":      (row[1] or "")[:10] if row else "N/A",
        "total_users": total,
    }

# ─────────────────────────────────────────────
#  SETTINGS — CYCLES & LABELS
# ─────────────────────────────────────────────
CYCLES = {
    "lang":            ["en", "hi"],
    "voice":           ["calm", "dramatic", "energetic", "narrator", "deep", "whisper"],
    "style":           ["cinematic", "manga", "noir", "retro", "anime", "dramatic"],
    "color_grade":     ["vivid", "muted", "warm", "cold", "manga_ink",
                        "golden", "cinematic", "bleach"],
    "speed":           ["slow", "normal", "fast"],
    "quality":         ["sd", "hd", "4k"],
    "blur_radius":     [8, 12, 18, 24, 30, 36],
    "blur_brightness": [0.65, 0.72, 0.78, 0.82, 0.88, 0.94],
    "zoom_amount":     [0.0, 0.02, 0.04, 0.06, 0.08, 0.11],
}

LABELS = {
    "lang": {
        "en": "🇬🇧 English",
        "hi": "🇮🇳 Hindi",
    },
    "voice": {
        "calm":     "🧘 Calm",
        "dramatic": "🎭 Dramatic",
        "energetic":"⚡ Energetic",
        "narrator": "🎙️ Narrator",
        "deep":     "🔊 Deep",
        "whisper":  "🤫 Whisper",
    },
    "style": {
        "cinematic":"🎬 Cinematic",
        "manga":    "💥 Manga",
        "noir":     "🖤 Noir",
        "retro":    "📺 Retro",
        "anime":    "✨ Anime",
        "dramatic": "🔥 Dramatic",
    },
    "color_grade": {
        "vivid":    "🌈 Vivid",
        "muted":    "🫧 Muted",
        "warm":     "🔥 Warm",
        "cold":     "❄️ Cold",
        "manga_ink":"🖋️ Ink B&W",
        "golden":   "✨ Golden",
        "cinematic":"🎞️ Cinematic",
        "bleach":   "⬜ Bleach",
    },
    "speed": {
        "slow":   "🐢 Slow",
        "normal": "🚶 Normal",
        "fast":   "🏃 Fast",
    },
    "quality": {
        "sd":  "📱 SD",
        "hd":  "🖥️ HD",
        "4k":  "💎 4K",
    },
    "blur_radius": {
        8:  "🌫️ Blur: Light",
        12: "🌫️ Blur: Soft",
        18: "🌫️ Blur: Medium",
        24: "🌫️ Blur: Heavy",
        30: "🌫️ Blur: Max",
        36: "🌫️ Blur: Extreme",
    },
    "blur_brightness": {
        0.65: "🌑 BG: 65%",
        0.72: "🌒 BG: 72%",
        0.78: "🌓 BG: 78%",
        0.82: "🌔 BG: 82%",
        0.88: "🌕 BG: 88%",
        0.94: "☀️ BG: 94%",
    },
    "zoom_amount": {
        0.0:  "🔍 Zoom: OFF",
        0.02: "🔍 Zoom: Subtle",
        0.04: "🔍 Zoom: Gentle",
        0.06: "🔍 Zoom: Normal",
        0.08: "🔍 Zoom: Strong",
        0.11: "🔍 Zoom: Max",
    },
}

PRESETS = {
    # name: (style, color_grade, blur_radius, blur_brightness, zoom_amount)
    "cinematic": ("cinematic", "cinematic",  18, 0.82, 0.04),
    "action":    ("dramatic",  "vivid",      18, 0.82, 0.06),
    "noir":      ("noir",      "muted",      18, 0.75, 0.02),
    "anime":     ("anime",     "vivid",      18, 0.82, 0.04),
    "manga_bw":  ("manga",     "manga_ink",  18, 0.88, 0.04),
    "epic":      ("dramatic",  "golden",     18, 0.82, 0.06),
    "cold":      ("cinematic", "cold",       18, 0.82, 0.04),
    "golden":    ("cinematic", "golden",     18, 0.85, 0.04),
}

def lbl(key: str, val) -> str:
    return LABELS.get(key, {}).get(val, str(val))

def next_val(key: str, current):
    lst = CYCLES[key]
    try:    return lst[(lst.index(current) + 1) % len(lst)]
    except: return lst[0]

# ─────────────────────────────────────────────
#  SETTINGS TEXT
# ─────────────────────────────────────────────
def settings_text(s: dict) -> str:
    br = s.get("blur_radius", 18)
    bb = s.get("blur_brightness", 0.82)
    za = s.get("zoom_amount", 0.04)

    br_lbl = lbl("blur_radius",     min(CYCLES["blur_radius"],     key=lambda x: abs(x-br)))
    bb_lbl = lbl("blur_brightness", min(CYCLES["blur_brightness"], key=lambda x: abs(x-bb)))
    za_lbl = lbl("zoom_amount",     min(CYCLES["zoom_amount"],     key=lambda x: abs(x-za)))

    return (
        "⚙️ *Settings — MangaVoice Ultra v5.0*\n\n"
        f"🌐 Language    : {lbl('lang',        s.get('lang',        'en'))}\n"
        f"🎤 Voice       : {lbl('voice',       s.get('voice',       'calm'))}\n"
        f"🎬 Style       : {lbl('style',       s.get('style',       'cinematic'))}\n"
        f"🎨 Colour Grade: {lbl('color_grade', s.get('color_grade', 'vivid'))}\n"
        f"⚡ Speed       : {lbl('speed',       s.get('speed',       'normal'))}\n"
        f"📐 Quality     : {lbl('quality',     s.get('quality',     'hd'))}\n"
        f"📝 Subtitles   : {'ON ✅' if s.get('subtitles', 1) else 'OFF ❌'}\n\n"
        f"━━━ 🌫️ Background ━━━\n"
        f"{br_lbl}\n"
        f"{bb_lbl}\n"
        f"{za_lbl}\n\n"
        "_Tap a button to change it_"
    )

# ─────────────────────────────────────────────
#  KEYBOARDS
# ─────────────────────────────────────────────
def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Settings",      callback_data="show:settings"),
         InlineKeyboardButton("🎨 Presets",        callback_data="show:presets")],
        [InlineKeyboardButton("❓ Help",            callback_data="show:help"),
         InlineKeyboardButton("📊 Stats",           callback_data="show:stats")],
        [InlineKeyboardButton("🔑 Test API Key",   callback_data="do:testkey")],
    ])

def kb_settings(s: dict) -> InlineKeyboardMarkup:
    sub = "✅ Subtitles" if s.get("subtitles", 1) else "❌ Subtitles"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Language",       callback_data="noop"),
         InlineKeyboardButton(lbl("lang",  s.get("lang","en")),   callback_data="cycle:lang")],
        [InlineKeyboardButton("🎤 Voice",          callback_data="noop"),
         InlineKeyboardButton(lbl("voice", s.get("voice","calm")),callback_data="cycle:voice")],
        [InlineKeyboardButton("🎬 Anim Style",     callback_data="noop"),
         InlineKeyboardButton(lbl("style", s.get("style","cinematic")),callback_data="cycle:style")],
        [InlineKeyboardButton("🎨 Colour",         callback_data="noop"),
         InlineKeyboardButton(lbl("color_grade",s.get("color_grade","vivid")),callback_data="cycle:color_grade")],
        [InlineKeyboardButton("⚡ Speed",          callback_data="noop"),
         InlineKeyboardButton(lbl("speed",   s.get("speed","normal")),  callback_data="cycle:speed")],
        [InlineKeyboardButton("📐 Quality",        callback_data="noop"),
         InlineKeyboardButton(lbl("quality", s.get("quality","hd")),    callback_data="cycle:quality")],
        [InlineKeyboardButton(sub,                 callback_data="toggle:subtitles")],
        [InlineKeyboardButton("━━ 🌫️ Background ━━",callback_data="noop")],
        [InlineKeyboardButton("Blur Amount",       callback_data="noop"),
         InlineKeyboardButton(lbl("blur_radius",
             min(CYCLES["blur_radius"], key=lambda x: abs(x-s.get("blur_radius",18)))),
             callback_data="cycle:blur_radius")],
        [InlineKeyboardButton("BG Brightness",     callback_data="noop"),
         InlineKeyboardButton(lbl("blur_brightness",
             min(CYCLES["blur_brightness"], key=lambda x: abs(x-s.get("blur_brightness",0.82)))),
             callback_data="cycle:blur_brightness")],
        [InlineKeyboardButton("Page Zoom",         callback_data="noop"),
         InlineKeyboardButton(lbl("zoom_amount",
             min(CYCLES["zoom_amount"], key=lambda x: abs(x-s.get("zoom_amount",0.04)))),
             callback_data="cycle:zoom_amount")],
        [InlineKeyboardButton("✅ Done",            callback_data="settings:close"),
         InlineKeyboardButton("🔄 Reset",           callback_data="settings:reset")],
    ])

def kb_presets() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Cinematic",    callback_data="preset:cinematic"),
         InlineKeyboardButton("🔥 Action",        callback_data="preset:action")],
        [InlineKeyboardButton("🖤 Noir",          callback_data="preset:noir"),
         InlineKeyboardButton("✨ Anime",          callback_data="preset:anime")],
        [InlineKeyboardButton("🖋️ Manga B&W",    callback_data="preset:manga_bw"),
         InlineKeyboardButton("🌟 Epic",           callback_data="preset:epic")],
        [InlineKeyboardButton("❄️ Cold",          callback_data="preset:cold"),
         InlineKeyboardButton("✨ Golden",          callback_data="preset:golden")],
        [InlineKeyboardButton("← Back",           callback_data="show:settings")],
    ])

def kb_confirm(count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🚀 Generate Video  ({count} page{'s' if count > 1 else ''})",
            callback_data="do:process")],
        [InlineKeyboardButton("⚙️ Settings",   callback_data="show:settings"),
         InlineKeyboardButton("🎨 Presets",     callback_data="show:presets")],
        [InlineKeyboardButton("❌ Cancel",      callback_data="do:cancel")],
    ])

# ─────────────────────────────────────────────
#  HELP TEXT
# ─────────────────────────────────────────────
HELP_TEXT = """🎌 *MangaVoice Ultra v5.0*

━━ 📤 HOW TO USE ━━
1. Send manga — photos, PDF, or ZIP
2. Tap *Generate Video*
3. Receive your cinematic video in 2–5 min!

━━ 🎬 ANIMATION STYLES ━━
🎬 Cinematic — Slow, subtle scroll + gentle zoom
💥 Manga — Impact punch, fast feel
🖤 Noir — Dark vignette, moody
📺 Retro — Film grain + drift
✨ Anime — Soft glow bloom
🔥 Dramatic — Strong zoom + dark edges

━━ 🎨 COLOUR GRADES ━━
Vivid · Muted · Warm · Cold
Ink B&W · Golden · Cinematic · Bleach

━━ 🎤 6 VOICES ━━
Calm · Dramatic · Energetic
Narrator · Deep · Whisper

━━ 🌫️ BACKGROUND ━━
Manga page is centred on screen.
Left & right sides show blurred version of same page.
Controls:
• *Blur Amount* — how blurry the sides are
• *BG Brightness* — how light/dark the blur is
• *Page Zoom* — how much the page slowly zooms

━━ 🎨 PRESETS ━━
One-tap style combos: tap 🎨 Presets

━━ ⌨️ COMMANDS ━━
/start · /settings · /help · /stats · /testkey · /cancel
"""

# ─────────────────────────────────────────────
#  COMMANDS
# ─────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u    = update.effective_user
    cid  = update.effective_chat.id
    name = u.first_name or "Manga Fan"
    db_ensure(cid, u.username or "", name)
    await ctx.bot.send_chat_action(cid, ChatAction.TYPING)

    txt = (
        f"🎌 *MangaVoice Ultra v5.0*\n\n"
        f"Hey *{name}!*\n\n"
        "Send me your manga and I'll create a *cinematic animated video* "
        "with AI narration and voice-over!\n\n"
        "📤 *Supported formats:*\n"
        "• 📸 Photos / JPG / PNG\n"
        "• 📄 Manga PDF\n"
        "• 📦 ZIP of manga pages\n"
        "• Up to *50 pages* per video\n\n"
        "🎨 Use *Presets* for quick one-tap styles\n"
        "⚙️ Use *Settings* to customise everything\n\n"
        "_Just send your manga pages to get started!_"
    )

    if START_IMG:
        try:
            await ctx.bot.send_photo(
                chat_id=cid, photo=START_IMG,
                caption=txt, parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_main())
            return
        except Exception as e:
            logger.warning(f"Start image failed: {e}")

    await update.message.reply_text(
        txt, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    s = db_ensure(update.effective_chat.id, u.username or "", u.first_name or "")
    await update.message.reply_text(
        settings_text(s), parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_settings(s))


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.callback_query.message
    await msg.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    d   = db_stats(cid)
    await update.message.reply_text(
        "📊 *Your Stats*\n\n"
        f"🎬 Videos: `{d['videos']}`\n"
        f"📅 Since:  `{d['joined']}`\n"
        f"👥 Users:  `{d['total_users']}`",
        parse_mode=ParseMode.MARKDOWN)


async def cmd_testkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg and update.callback_query:
        msg = update.callback_query.message
    if not msg:
        return
    await msg.reply_text("🔑 Testing Groq API key…")
    key = GROQ_KEY.strip()
    if not key:
        await msg.reply_text(
            "❌ GROQ_API_KEY is empty!\n"
            "Get a free key at: console.groq.com")
        return
    if not key.startswith("gsk_"):
        await msg.reply_text(
            f"❌ Key format wrong: `{key[:14]}…`\n"
            "Must start with `gsk_`")
        return
    try:
        r = req_lib.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={"model": "llama-3.2-11b-vision-preview",
                  "messages": [{"role": "user", "content": "Say OK"}],
                  "max_tokens": 5},
            timeout=15)
        if r.status_code == 200:
            await msg.reply_text(
                "✅ *Groq API key is working!*\n\nSend your manga now!",
                parse_mode=ParseMode.MARKDOWN)
        elif r.status_code == 401:
            await msg.reply_text(
                "❌ Key rejected (401)\n"
                "Create a fresh key at console.groq.com → API Keys")
        else:
            await msg.reply_text(f"⚠️ Error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        await msg.reply_text(f"❌ Connection error: {str(e)[:200]}")


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("pending_files", None)
    ctx.user_data.pop("status_msg_id", None)
    ctx.user_data["processing"] = False
    await update.message.reply_text(
        "❌ Cancelled. Send new manga whenever you're ready!")

# ─────────────────────────────────────────────
#  FILE HANDLER
# ─────────────────────────────────────────────
async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    msg = update.message
    u   = update.effective_user

    if ctx.user_data.get("processing"):
        await msg.reply_text("⏳ Still processing. Use /cancel to stop.")
        return

    s     = db_ensure(cid, u.username or "", u.first_name or "")
    files = ctx.user_data.setdefault("pending_files", [])

    if msg.photo:
        photo = max(msg.photo, key=lambda p: p.file_size)
        files.append({"type": "image", "file_id": photo.file_id,
                      "name": f"img_{len(files):03d}.jpg"})
    elif msg.document:
        doc  = msg.document
        mime = doc.mime_type or ""
        name = doc.file_name or "file"
        ext  = Path(name).suffix.lower()
        if mime.startswith("image/") or ext in (".jpg", ".jpeg", ".png", ".webp"):
            files.append({"type": "image", "file_id": doc.file_id, "name": name})
        elif mime == "application/pdf" or ext == ".pdf":
            files.append({"type": "pdf", "file_id": doc.file_id, "name": name})
        elif "zip" in mime or ext == ".zip":
            files.append({"type": "zip", "file_id": doc.file_id, "name": name})
        else:
            await msg.reply_text("⚠️ Send JPG/PNG/PDF/ZIP manga files.")
            return
    else:
        return

    count    = len(files)
    tc: dict = {}
    for f in files:
        tc[f["type"]] = tc.get(f["type"], 0) + 1
    type_str = " · ".join(
        f"{'📸' if t=='image' else '📄' if t=='pdf' else '📦'} {n} {t}"
        for t, n in tc.items())

    preview = (
        f"📚 *Queue: {count} file{'s' if count>1 else ''}*\n"
        f"┗ {type_str}\n\n"
        f"🎬 {lbl('style',s.get('style','cinematic'))}  "
        f"🎤 {lbl('voice',s.get('voice','calm'))}  "
        f"🌐 {lbl('lang',s.get('lang','en'))}\n"
        f"🎨 {lbl('color_grade',s.get('color_grade','vivid'))}  "
        f"📐 {lbl('quality',s.get('quality','hd'))}\n\n"
        "_Send more pages or tap Generate_"
    )

    kb  = kb_confirm(count)
    sid = ctx.user_data.get("status_msg_id")
    try:
        if sid:
            await ctx.bot.edit_message_text(
                preview, chat_id=cid, message_id=sid,
                parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        else:
            sent = await msg.reply_text(
                preview, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
            ctx.user_data["status_msg_id"] = sent.message_id
    except Exception:
        sent = await msg.reply_text(
            preview, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        ctx.user_data["status_msg_id"] = sent.message_id

# ─────────────────────────────────────────────
#  CALLBACK HANDLER
# ─────────────────────────────────────────────
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    cid  = q.message.chat_id
    u    = update.effective_user
    data = q.data
    await q.answer()

    if data == "noop":
        return

    s = db_ensure(cid, u.username or "", u.first_name or "")

    # ── Cycle a setting ──────────────────────────────────
    if data.startswith("cycle:"):
        key = data.split(":", 1)[1]
        cur = s.get(key, CYCLES[key][0])
        db_set(cid, **{key: next_val(key, cur)})
        s = db_get(cid)
        try:
            await q.edit_message_text(
                settings_text(s), parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_settings(s))
        except Exception:
            pass

    # ── Toggle boolean ───────────────────────────────────
    elif data.startswith("toggle:"):
        key = data.split(":", 1)[1]
        db_set(cid, **{key: 0 if s.get(key, 1) else 1})
        s = db_get(cid)
        try:
            await q.edit_message_text(
                settings_text(s), parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_settings(s))
        except Exception:
            pass

    # ── Presets ──────────────────────────────────────────
    elif data.startswith("preset:"):
        name = data.split(":", 1)[1]
        if name in PRESETS:
            st, gr, br, bb, za = PRESETS[name]
            db_set(cid, style=st, color_grade=gr,
                   blur_radius=br, blur_brightness=bb, zoom_amount=za)
            s = db_get(cid)
            await q.answer(f"✅ {name.replace('_',' ').title()} applied!")
            try:
                await q.edit_message_text(
                    settings_text(s), parse_mode=ParseMode.MARKDOWN,
                    reply_markup=kb_settings(s))
            except Exception:
                pass

    # ── Show panels ──────────────────────────────────────
    elif data == "show:settings":
        try:
            await q.edit_message_text(
                settings_text(s), parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_settings(s))
        except Exception:
            await q.message.reply_text(
                settings_text(s), parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_settings(s))

    elif data == "show:presets":
        try:
            await q.edit_message_text(
                "🎨 *Quick Style Presets*\n\n"
                "One tap applies a full style combination.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_presets())
        except Exception:
            await q.message.reply_text(
                "🎨 *Quick Style Presets*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_presets())

    elif data == "show:help":
        await q.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

    elif data == "show:stats":
        d = db_stats(cid)
        await q.answer(
            f"🎬 Videos: {d['videos']} | 👥 Total users: {d['total_users']}",
            show_alert=True)

    # ── Settings controls ────────────────────────────────
    elif data == "settings:close":
        try:
            await q.edit_message_text(
                "✅ *Settings saved!*\n\nSend your manga pages now.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())
        except Exception:
            pass

    elif data == "settings:reset":
        db_set(cid, **DEFAULT)
        s = db_get(cid)
        await q.edit_message_text(
            "🔄 *Settings reset to defaults.*\n\n" + settings_text(s),
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_settings(s))

    # ── Actions ──────────────────────────────────────────
    elif data == "do:process":
        ctx.user_data["status_msg_id"] = None
        try:
            await q.edit_message_text("⏳ Starting…")
        except Exception:
            pass
        asyncio.create_task(_run_pipeline(ctx.bot, cid, ctx))

    elif data == "do:cancel":
        ctx.user_data.pop("pending_files", None)
        ctx.user_data.pop("status_msg_id", None)
        try:
            await q.edit_message_text("❌ Cancelled.")
        except Exception:
            pass

    elif data == "do:testkey":
        await cmd_testkey(update, ctx)

# ─────────────────────────────────────────────
#  PIPELINE RUNNER
# ─────────────────────────────────────────────
async def _run_pipeline(bot, cid: int, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.get("processing"):
        return
    ctx.user_data["processing"] = True

    files = ctx.user_data.pop("pending_files", [])
    if not files:
        await bot.send_message(cid, "⚠️ No files queued. Send manga pages first!")
        ctx.user_data["processing"] = False
        return

    s = db_get(cid) or {}

    prog = await bot.send_message(
        cid,
        "```\n"
        "🎬 MANGAVOICE ULTRA v5.0\n"
        "────────────────────────\n"
        "[ ░░░░░░░░░░░░░░░░░░░░ ]  0%\n"
        "Starting…\n"
        "```",
        parse_mode=ParseMode.MARKDOWN)

    async def upd(pct: int, label: str):
        filled = int(pct / 5)
        bar    = "█" * filled + "░" * (20 - filled)
        try:
            await bot.edit_message_text(
                f"```\n"
                f"🎬 MANGAVOICE ULTRA v5.0\n"
                f"────────────────────────\n"
                f"[ {bar} ]  {pct}%\n"
                f"{label}\n"
                f"```",
                chat_id=cid,
                message_id=prog.message_id,
                parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass

    try:
        from pipeline import MangaPipeline
        pipe = MangaPipeline(
            bot=bot, chat_id=cid, update_progress=upd,
            settings=s, groq_key=GROQ_KEY, elevenlabs_key=EL_KEY)
        out_path, page_count = await pipe.run(files)

        db_log(cid, page_count, s.get("style", "cinematic"), s.get("lang", "en"))

        quality = s.get("quality", "hd")
        dims    = {"sd": (720,1280), "hd": (1080,1920), "4k": (1440,2560)}.get(quality,(1080,1920))

        caption = (
            "🎌 *Your Manga Video is Ready!*\n\n"
            f"📄 Pages   : `{page_count}`\n"
            f"🎬 Style   : `{lbl('style',       s.get('style',       'cinematic'))}`\n"
            f"🎨 Grade   : `{lbl('color_grade',  s.get('color_grade', 'vivid'))}`\n"
            f"🎤 Voice   : `{lbl('voice',        s.get('voice',       'calm'))}`\n"
            f"🌐 Lang    : `{lbl('lang',         s.get('lang',        'en'))}`\n"
            f"📐 Quality : `{lbl('quality',      quality)}`\n\n"
            "_Enjoy! 🍿_"
        )

        await bot.send_video(
            chat_id=cid,
            video=open(out_path, "rb"),
            caption=caption,
            parse_mode=ParseMode.MARKDOWN,
            supports_streaming=True,
            width=dims[0], height=dims[1],
            write_timeout=300, read_timeout=300)

        try:
            await bot.delete_message(cid, prog.message_id)
        except Exception:
            pass
        Path(out_path).unlink(missing_ok=True)

    except ImportError as e:
        pkg = str(e).split("'")[-2] if "'" in str(e) else str(e)
        await bot.edit_message_text(
            f"⚠️ *Missing package:* `{pkg}`\nRun: `pip install {pkg}`",
            chat_id=cid, message_id=prog.message_id,
            parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.exception("Pipeline error")
        await bot.edit_message_text(
            f"❌ *Error:*\n`{str(e)[:400]}`\n\n_Use /cancel then try again_",
            chat_id=cid, message_id=prog.message_id,
            parse_mode=ParseMode.MARKDOWN)
    finally:
        ctx.user_data["processing"] = False

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start",    "Main menu"),
        BotCommand("settings", "Settings panel"),
        BotCommand("help",     "How to use"),
        BotCommand("stats",    "Your stats"),
        BotCommand("testkey",  "Test Groq API key"),
        BotCommand("cancel",   "Cancel processing"),
    ])

def main():
    db_init()
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .read_timeout(600)
        .write_timeout(600)
        .connect_timeout(60)
        .pool_timeout(600)
        .build()
    )
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("stats",    cmd_stats))
    app.add_handler(CommandHandler("testkey",  cmd_testkey))
    app.add_handler(CommandHandler("cancel",   cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.Document.ALL, handle_file))
    print("🎌 MangaVoice Ultra v5.0 — Running!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
