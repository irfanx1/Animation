"""
MangaVoice Ultra v4.0 — bot.py
AI: Groq | TTS: gTTS/ElevenLabs | Video: FFmpeg
Features: Start image, 6 voices, blur opacity, zoom speed,
          parallax, action clips, advanced BG controls
"""

import json, asyncio, logging, sqlite3, requests as req_lib
from pathlib import Path

from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand)
from telegram.ext import (Application, CommandHandler, MessageHandler,
                           CallbackQueryHandler, ContextTypes, filters)
from telegram.constants import ParseMode, ChatAction

# ═══════════════════════════════════════════════════════════
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    level=logging.INFO)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════
BASE   = Path(__file__).parent
CONFIG = json.loads((BASE/"config.json").read_text())

BOT_TOKEN      = CONFIG["BOT_TOKEN"]
GROQ_KEY       = CONFIG.get("GROQ_API_KEY","")
EL_KEY         = CONFIG.get("ELEVENLABS_API_KEY","")
START_IMAGE_URL= CONFIG.get("START_IMAGE_URL","")

# ═══════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════
DB = BASE/"mangavoice.db"

def db_init():
    con = sqlite3.connect(DB)
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
            bg_blur          INTEGER DEFAULT 4,
            blur_radius      INTEGER DEFAULT 22,
            blur_brightness  REAL    DEFAULT 0.82,
            zoom_amount      REAL    DEFAULT 0.06,
            parallax         INTEGER DEFAULT 1,
            action_clips     INTEGER DEFAULT 1,
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
    # Migrate old DBs safely
    migrations = [
        "ALTER TABLE users ADD COLUMN bg_blur INTEGER DEFAULT 2",
        "ALTER TABLE users ADD COLUMN blur_radius INTEGER DEFAULT 22",
        "ALTER TABLE users ADD COLUMN blur_brightness REAL DEFAULT 0.82",
        "ALTER TABLE users ADD COLUMN zoom_amount REAL DEFAULT 0.06",
        "ALTER TABLE users ADD COLUMN parallax INTEGER DEFAULT 1",
        "ALTER TABLE users ADD COLUMN action_clips INTEGER DEFAULT 1",
    ]
    for m in migrations:
        try: con.execute(m); con.commit()
        except: pass
    con.close()

def db_get(cid:int):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM users WHERE chat_id=?",(cid,)).fetchone()
    con.close()
    return dict(row) if row else None

def db_ensure(cid:int, username="", name=""):
    if db_get(cid) is None:
        con = sqlite3.connect(DB)
        con.execute("INSERT OR IGNORE INTO users (chat_id,username,name) VALUES (?,?,?)",
                    (cid,username,name))
        con.commit(); con.close()
    return db_get(cid)

def db_set(cid:int, **kw):
    if not kw: return
    con = sqlite3.connect(DB)
    sets = ",".join(f"{k}=?" for k in kw)
    con.execute(f"UPDATE users SET {sets} WHERE chat_id=?", list(kw.values())+[cid])
    con.commit(); con.close()

def db_log(cid, pages, style, lang):
    con = sqlite3.connect(DB)
    con.execute("INSERT INTO history (chat_id,pages,style,lang) VALUES (?,?,?,?)",
                (cid,pages,style,lang))
    con.execute("UPDATE users SET videos=videos+1 WHERE chat_id=?",(cid,))
    con.commit(); con.close()

def db_stats(cid):
    con = sqlite3.connect(DB)
    row = con.execute("SELECT videos,joined FROM users WHERE chat_id=?",(cid,)).fetchone()
    total = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    con.close()
    return {"videos":row[0] if row else 0,
            "joined":(row[1] or "")[:10] if row else "N/A",
            "total_users":total}

# ═══════════════════════════════════════════════════════════
# SETTINGS LABELS & CYCLES
# ═══════════════════════════════════════════════════════════
CYCLES = {
    "lang":        ["en","hi"],
    "voice":       ["calm","dramatic","energetic","narrator","deep","whisper"],
    "style":       ["cinematic","manga","noir","retro","anime","dramatic"],
    "color_grade": ["vivid","muted","warm","cold","manga_ink","golden","cinematic","bleach"],
    "speed":       ["slow","normal","fast"],
    "quality":     ["sd","hd","4k"],
    "bg_blur":     [0,1,2,3,4],
    "blur_radius": [8,14,20,26,32,38],
    "blur_brightness": [0.65,0.72,0.78,0.82,0.88,0.94],
    "zoom_amount": [0.0,0.03,0.06,0.09,0.12],
}
LABELS = {
    "lang":        {"en":"🇬🇧 English","hi":"🇮🇳 Hindi"},
    "voice":       {"calm":"🧘 Calm","dramatic":"🎭 Dramatic","energetic":"⚡ Energetic",
                    "narrator":"🎙️ Narrator","deep":"🔊 Deep","whisper":"🤫 Whisper"},
    "style":       {"cinematic":"🎬 Cinematic","manga":"💥 Manga","noir":"🖤 Noir",
                    "retro":"📺 Retro","anime":"✨ Anime","dramatic":"🔥 Dramatic"},
    "color_grade": {"vivid":"🌈 Vivid","muted":"🫧 Muted","warm":"🔥 Warm","cold":"❄️ Cold",
                    "manga_ink":"🖋️ Ink","golden":"✨ Golden","cinematic":"🎞️ Cinematic","bleach":"⬜ Bleach"},
    "quality":     {"sd":"📱 SD","hd":"🖥️ HD","4k":"💎 4K"},
    "speed":       {"slow":"🐢 Slow","normal":"🚶 Normal","fast":"🏃 Fast"},
    "bg_blur":     {0:"🖼️ Crop Fill",1:"🌫️ Blur BG",2:"🌫️✨ Blur+Zoom",
                    3:"📜 Scroll",4:"📜🌫️ Scroll+Blur"},
    "blur_radius": {8:"Blur: Light",14:"Blur: Soft",20:"Blur: Medium",
                    26:"Blur: Heavy",32:"Blur: Max",38:"Blur: Extreme"},
    "blur_brightness": {0.65:"BG Light: 65%",0.72:"BG Light: 72%",0.78:"BG Light: 78%",
                        0.82:"BG Light: 82%",0.88:"BG Light: 88%",0.94:"BG Light: 94%"},
    "zoom_amount": {0.0:"Zoom: OFF",0.03:"Zoom: Gentle",0.06:"Zoom: Normal",
                    0.09:"Zoom: Strong",0.12:"Zoom: Max"},
}

def lbl(key, val):
    return LABELS.get(key,{}).get(val, str(val))

def next_val(current, key):
    lst = CYCLES[key]
    try:    return lst[(lst.index(current)+1)%len(lst)]
    except: return lst[0]

# ═══════════════════════════════════════════════════════════
# SETTINGS TEXT
# ═══════════════════════════════════════════════════════════
def settings_text(s:dict) -> str:
    bg_val  = s.get("bg_blur",2)
    is_scroll = bg_val in (3,4)
    br      = s.get("blur_radius",22)
    bb      = s.get("blur_brightness",0.4)
    za      = s.get("zoom_amount",0.06)
    par     = s.get("parallax",1)
    ac      = s.get("action_clips",1)

    # Find closest label values
    br_lbl  = lbl("blur_radius", min(CYCLES["blur_radius"], key=lambda x:abs(x-br)))
    bb_lbl  = lbl("blur_brightness", min(CYCLES["blur_brightness"], key=lambda x:abs(x-bb)))
    za_lbl  = lbl("zoom_amount", min(CYCLES["zoom_amount"], key=lambda x:abs(x-za)))

    base = (
        "⚙️ *Settings — MangaVoice Ultra v4.0*\n\n"
        f"🌐 Language    : {lbl('lang',s.get('lang','en'))}\n"
        f"🎤 Voice       : {lbl('voice',s.get('voice','calm'))}\n"
        f"🎬 Style       : {lbl('style',s.get('style','cinematic'))}\n"
        f"🎨 Color Grade : {lbl('color_grade',s.get('color_grade','vivid'))}\n"
        f"⚡ Speed       : {lbl('speed',s.get('speed','normal'))}\n"
        f"📐 Quality     : {lbl('quality',s.get('quality','hd'))}\n"
        f"📝 Subtitles   : {'ON ✅' if s.get('subtitles',1) else 'OFF ❌'}\n"
        f"⚡ Action Clips: {'ON ✅' if ac else 'OFF ❌'}\n\n"
        f"━━━ 🌫️ BG Controls ━━━\n"
        f"🖼️ BG Mode     : {lbl('bg_blur',bg_val)}\n"
    )
    if bg_val in (1,2,4):
        base += (
            f"🌫️ {br_lbl}\n"
            f"🌑 {bb_lbl}\n"
            f"🔍 {za_lbl}\n"
            f"🎭 Parallax BG : {'ON ✅' if par else 'OFF ❌'}\n"
        )
    base += "\n_Tap buttons to change settings_"
    return base

# ═══════════════════════════════════════════════════════════
# KEYBOARDS
# ═══════════════════════════════════════════════════════════
def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Settings",       callback_data="show:settings"),
         InlineKeyboardButton("❓ Help",             callback_data="show:help")],
        [InlineKeyboardButton("📊 My Stats",        callback_data="show:stats"),
         InlineKeyboardButton("🔑 Test API Key",    callback_data="show:testkey")],
        [InlineKeyboardButton("🎨 Quick Style",     callback_data="show:quickstyle"),
         InlineKeyboardButton("📖 Formats",          callback_data="show:formats")],
    ])

def kb_settings(s:dict) -> InlineKeyboardMarkup:
    sub = "✅ Subtitles ON" if s.get("subtitles",1) else "❌ Subtitles OFF"
    ac  = "✅ Action Clips" if s.get("action_clips",1) else "❌ Action Clips"
    par = "✅ Parallax BG"  if s.get("parallax",1) else "❌ Parallax BG"
    bg_val = s.get("bg_blur",2)

    br_val = s.get("blur_radius",22)
    bb_val = s.get("blur_brightness",0.4)
    za_val = s.get("zoom_amount",0.06)
    br_lbl = lbl("blur_radius",   min(CYCLES["blur_radius"],   key=lambda x:abs(x-br_val)))
    bb_lbl = lbl("blur_brightness",min(CYCLES["blur_brightness"],key=lambda x:abs(x-bb_val)))
    za_lbl = lbl("zoom_amount",   min(CYCLES["zoom_amount"],   key=lambda x:abs(x-za_val)))

    rows = [
        [InlineKeyboardButton("━━ 🌐 LANGUAGE ━━",   callback_data="noop")],
        [InlineKeyboardButton(lbl("lang",s.get("lang","en")),        callback_data="cycle:lang"),
         InlineKeyboardButton("→ Change",                             callback_data="cycle:lang")],

        [InlineKeyboardButton("━━ 🎤 VOICE ━━",      callback_data="noop")],
        [InlineKeyboardButton(lbl("voice",s.get("voice","calm")),     callback_data="cycle:voice"),
         InlineKeyboardButton("→ Change",                             callback_data="cycle:voice")],

        [InlineKeyboardButton("━━ 🎬 STYLE ━━",      callback_data="noop")],
        [InlineKeyboardButton(lbl("style",s.get("style","cinematic")),callback_data="cycle:style"),
         InlineKeyboardButton("→ Change",                             callback_data="cycle:style")],

        [InlineKeyboardButton("━━ 🎨 COLOUR GRADE ━━", callback_data="noop")],
        [InlineKeyboardButton(lbl("color_grade",s.get("color_grade","vivid")),callback_data="cycle:color_grade"),
         InlineKeyboardButton("→ Change",                             callback_data="cycle:color_grade")],

        [InlineKeyboardButton("━━ ⚙️ VIDEO OPTIONS ━━", callback_data="noop")],
        [InlineKeyboardButton(lbl("speed",s.get("speed","normal")),  callback_data="cycle:speed"),
         InlineKeyboardButton(lbl("quality",s.get("quality","hd")),  callback_data="cycle:quality")],
        [InlineKeyboardButton(sub,  callback_data="toggle:subtitles"),
         InlineKeyboardButton(ac,   callback_data="toggle:action_clips")],

        [InlineKeyboardButton("━━ 🌫️ BACKGROUND CONTROLS ━━", callback_data="noop")],
        [InlineKeyboardButton(lbl("bg_blur",bg_val),              callback_data="cycle:bg_blur"),
         InlineKeyboardButton("→ Change Mode",                    callback_data="cycle:bg_blur")],
    ]

    if bg_val in (1,2,4):
        rows += [
            [InlineKeyboardButton(f"🌫️ {br_lbl}",  callback_data="cycle:blur_radius"),
             InlineKeyboardButton("→ Blur Amount",  callback_data="cycle:blur_radius")],
            [InlineKeyboardButton(f"🌑 {bb_lbl}",  callback_data="cycle:blur_brightness"),
             InlineKeyboardButton("→ BG Darkness",  callback_data="cycle:blur_brightness")],
            [InlineKeyboardButton(f"🔍 {za_lbl}",  callback_data="cycle:zoom_amount"),
             InlineKeyboardButton("→ Zoom Speed",   callback_data="cycle:zoom_amount")],
            [InlineKeyboardButton(par,              callback_data="toggle:parallax")],
        ]

    rows += [
        [InlineKeyboardButton("━━━━━━━━━━━━━━━━━━━━━", callback_data="noop")],
        [InlineKeyboardButton("✅ Save & Close",  callback_data="settings:close"),
         InlineKeyboardButton("🔄 Reset All",     callback_data="settings:reset")],
    ]
    return InlineKeyboardMarkup(rows)

def kb_confirm(count:int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🚀 Generate Video — {count} page{'s' if count>1 else ''}",
                              callback_data="do:process")],
        [InlineKeyboardButton("➕ Add More Pages", callback_data="noop"),
         InlineKeyboardButton("❌ Cancel",          callback_data="do:cancel")],
        [InlineKeyboardButton("⚙️ Settings",        callback_data="show:settings"),
         InlineKeyboardButton("🎨 Quick Style",     callback_data="show:quickstyle")],
    ])

def kb_quickstyle() -> InlineKeyboardMarkup:
    """One-tap style presets."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Cinematic",   callback_data="preset:cinematic"),
         InlineKeyboardButton("💥 Action",       callback_data="preset:action")],
        [InlineKeyboardButton("🖤 Dark Noir",    callback_data="preset:noir"),
         InlineKeyboardButton("✨ Anime Glow",   callback_data="preset:anime")],
        [InlineKeyboardButton("🖋️ Manga B&W",   callback_data="preset:manga_bw"),
         InlineKeyboardButton("🔥 Epic Drama",   callback_data="preset:epic")],
        [InlineKeyboardButton("❄️ Cold Thriller",callback_data="preset:cold"),
         InlineKeyboardButton("✨ Golden Hour",   callback_data="preset:golden")],
        [InlineKeyboardButton("← Back",          callback_data="show:settings")],
    ])

# Style presets: (style, color_grade, bg_blur, zoom_amount, blur_radius, blur_brightness)
PRESETS = {
    "cinematic": ("cinematic","cinematic",4,0.05,18,0.82),
    "action":    ("dramatic","vivid",4,0.07,18,0.82),
    "noir":      ("noir","muted",4,0.0,18,0.75),
    "anime":     ("anime","vivid",4,0.05,18,0.82),
    "manga_bw":  ("manga","manga_ink",0,0.04,18,0.82),
    "epic":      ("dramatic","golden",4,0.06,18,0.82),
    "cold":      ("cinematic","cold",4,0.05,18,0.82),
    "golden":    ("cinematic","golden",4,0.05,18,0.85),
}

# ═══════════════════════════════════════════════════════════
# INFO MESSAGES
# ═══════════════════════════════════════════════════════════
INFO_MSGS = {
    "lang":  "🌐 Language: English or Hindi narration + subtitles",
    "voice": ("🎤 Voices:\n"
              "🧘 Calm — smooth story-teller\n"
              "🎭 Dramatic — intense cinematic\n"
              "⚡ Energetic — anime dub\n"
              "🎙️ Narrator — deep documentary\n"
              "🔊 Deep — powerful & authoritative\n"
              "🤫 Whisper — tense & suspenseful\n"
              "(Deep/Whisper/Narrator need ElevenLabs key)"),
    "style": ("🎬 Styles: how each page animates\n"
              "Cinematic/Manga/Noir/Retro/Anime/Dramatic"),
    "grade": "🎨 Colour grade changes the look & mood of colours",
    "speed": "⚡ Speed: how long each page is shown (Slow=more time)",
    "quality":"📐 Quality: SD=faster upload, HD=good, 4K=best",
    "bg":    ("🌫️ BG & Scroll Modes:\n"
              "🖼️ Crop Fill — fills screen, zooms in\n"
              "🌫️ Blur BG — full page + blurred bg\n"
              "🌫️✨ Blur+Zoom — full page + blur + zoom\n"
              "📜 Scroll — full width page, scrolls top→bottom\n"
              "📜🌫️ Scroll+Blur — centred page, blurred sides, scrolls\n"
              "RECOMMENDED: Scroll+Blur (mode 4)"),
}

# ═══════════════════════════════════════════════════════════
# HELP TEXT
# ═══════════════════════════════════════════════════════════
HELP_TEXT = """🎌 *MangaVoice Ultra v4.1 — Help*

━━ 📤 HOW TO USE ━━
1. Send manga (JPG/PNG, PDF, or ZIP)
2. Tap *Generate Video*
3. Wait 2–5 min → get your cinematic video!

━━ 🎬 ANIMATION STYLES ━━
🎬 *Cinematic* — Slow subtle zoom + pan. Movie-like.
💥 *Manga* — Bounce-zoom punch. Fast-action feel.
🖤 *Noir* — Vignette shadows. Dark & moody.
📺 *Retro* — Film grain + drift. Old-school.
✨ *Anime* — Glow bloom. Anime screenshot feel.
🔥 *Dramatic* — Strong zoom + dark edges.

━━ 🎨 COLOUR GRADES ━━
🌈 Vivid · 🫧 Muted · 🔥 Warm · ❄️ Cold
🖋️ Manga Ink (B&W) · ✨ Golden · 🎞️ Cinematic · ⬜ Bleach

━━ 🎤 VOICES ━━
🧘 Calm · 🎭 Dramatic · ⚡ Energetic
🎙️ Narrator · 🔊 Deep · 🤫 Whisper
_(Deep/Whisper/Narrator = ElevenLabs premium)_

━━ 🌫️ BG MODE ━━
🖼️ *Crop Fill* — zooms in, fills screen fully
🌫️ *Blur BG* — full manga page + blurred background
🌫️✨ *Blur+Zoom* — full page + blur + gentle zoom
📜 *Scroll* — full width, camera scrolls top to bottom
📜🌫️ *Scroll+Blur* — centred page, blurred sides, scrolls *(BEST)*

━━ 🌫️ BG CONTROLS (when Blur mode active) ━━
*Blur Amount* — how blurry the background is
*BG Darkness* — how dark the blurred background is
*Zoom Speed* — how much the page zooms (0=none to Max)
*Parallax* — background slowly drifts opposite to page

━━ ⚡ ACTION CLIPS ━━
When ON — action/fight panels get dynamic multi-cut
animation: zooms into sub-regions, speed lines, flash!

━━ 🎨 QUICK STYLE PRESETS ━━
One-tap combinations: Cinematic, Action, Noir, 
Anime, Manga B&W, Epic Drama, Cold Thriller, Golden

━━ ⌨️ COMMANDS ━━
/start · /settings · /help · /stats · /testkey · /cancel
"""

# ═══════════════════════════════════════════════════════════
# /start  (with optional image)
# ═══════════════════════════════════════════════════════════
async def cmd_start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    u   = update.effective_user
    cid = update.effective_chat.id
    db_ensure(cid, u.username or "", u.first_name or "")
    await ctx.bot.send_chat_action(cid, ChatAction.TYPING)
    name = u.first_name or "Manga Fan"

    txt = (
        "🎌 *MangaVoice Ultra v4.0*\n\n"
        f"Hey *{name}!* Send me your manga and I'll turn it into a "
        "*cinematic animated video* with AI narration, voice-over & subtitles!\n\n"
        "📤 *Send manga as:*\n"
        "• 📸 Photos / JPG / PNG images\n"
        "• 📄 Manga PDF (full chapter)\n"
        "• 📦 ZIP file of manga pages\n"
        "• Up to *35 pages* per video\n\n"
        "🔥 *New in v4.0:*\n"
        "• 🌫️✨ Blur BG + Zoom (full page shown!)\n"
        "• 🎛️ Blur opacity & zoom speed controls\n"
        "• ⚡ Action clips (fight scenes get animated!)\n"
        "• 🎙️ 6 voice styles\n"
        "• 🎨 8 colour grades + Quick Presets\n"
        "• 🎭 Parallax BG effect\n\n"
        "_Use ⚙️ Settings or 🎨 Quick Style to customise_"
    )

    if START_IMAGE_URL:
        try:
            await ctx.bot.send_photo(
                chat_id=cid,
                photo=START_IMAGE_URL,
                caption=txt,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_main())
            return
        except Exception as e:
            logger.warning(f"Could not send start image: {e}")

    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=kb_main())

# ═══════════════════════════════════════════════════════════
# /settings
# ═══════════════════════════════════════════════════════════
async def cmd_settings(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    s = db_ensure(update.effective_chat.id, u.username or "", u.first_name or "")
    await update.message.reply_text(settings_text(s), parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=kb_settings(s))

# ═══════════════════════════════════════════════════════════
# /help
# ═══════════════════════════════════════════════════════════
async def cmd_help(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.callback_query.message
    await msg.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

# ═══════════════════════════════════════════════════════════
# /stats
# ═══════════════════════════════════════════════════════════
async def cmd_stats(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    d   = db_stats(cid)
    await update.message.reply_text(
        "📊 *Your Stats*\n\n"
        f"🎬 Videos Generated : `{d['videos']}`\n"
        f"📅 Member Since     : `{d['joined']}`\n"
        f"👥 Total Users      : `{d['total_users']}`\n",
        parse_mode=ParseMode.MARKDOWN)

# ═══════════════════════════════════════════════════════════
# /testkey
# ═══════════════════════════════════════════════════════════
async def cmd_testkey(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if not msg: return
    await msg.reply_text("🔑 Testing Groq API key…")
    key = GROQ_KEY.strip()
    if not key:
        await msg.reply_text("❌ GROQ_API_KEY empty in config.json!\nGet free key: console.groq.com")
        return
    if not key.startswith("gsk_"):
        await msg.reply_text(f"❌ Key wrong format: {key[:14]}…\nMust start with gsk_")
        return
    try:
        r = req_lib.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"},
            json={"model":"llama-3.2-11b-vision-preview",
                  "messages":[{"role":"user","content":"Say OK"}],"max_tokens":5},
            timeout=15)
        if r.status_code==200:
            await msg.reply_text("✅ *Groq API key working!*\n\nSend your manga now!",
                                 parse_mode=ParseMode.MARKDOWN)
        elif r.status_code==401:
            await msg.reply_text("❌ Key rejected (401). Get fresh key at console.groq.com")
        else:
            await msg.reply_text(f"⚠️ Error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        await msg.reply_text(f"❌ Connection error: {str(e)[:200]}")

# ═══════════════════════════════════════════════════════════
# /cancel
# ═══════════════════════════════════════════════════════════
async def cmd_cancel(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("pending_files",None)
    ctx.user_data.pop("status_msg_id",None)
    ctx.user_data["processing"] = False
    await update.message.reply_text("❌ Cancelled. Send new manga whenever you're ready!")

# ═══════════════════════════════════════════════════════════
# FILE HANDLER
# ═══════════════════════════════════════════════════════════
async def handle_file(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    msg = update.message
    u   = update.effective_user

    if ctx.user_data.get("processing"):
        await msg.reply_text("⏳ Still processing. Use /cancel to stop.")
        return

    s     = db_ensure(cid, u.username or "", u.first_name or "")
    files = ctx.user_data.setdefault("pending_files",[])

    if msg.photo:
        photo = max(msg.photo, key=lambda p:p.file_size)
        files.append({"type":"image","file_id":photo.file_id,"name":f"img_{len(files):03d}.jpg"})
    elif msg.document:
        doc  = msg.document
        mime = doc.mime_type or ""
        name = doc.file_name or "file"
        ext  = Path(name).suffix.lower()
        if mime.startswith("image/") or ext in (".jpg",".jpeg",".png",".webp"):
            files.append({"type":"image","file_id":doc.file_id,"name":name})
        elif mime=="application/pdf" or ext==".pdf":
            files.append({"type":"pdf","file_id":doc.file_id,"name":name})
        elif "zip" in mime or ext==".zip":
            files.append({"type":"zip","file_id":doc.file_id,"name":name})
        else:
            await msg.reply_text("⚠️ Send JPG/PNG/PDF/ZIP manga files.")
            return
    else:
        return

    count = len(files)
    tc    = {}
    for f in files: tc[f["type"]] = tc.get(f["type"],0)+1
    type_str = " · ".join(
        f"{'📸' if t=='image' else '📄' if t=='pdf' else '📦'} {n} {t}"
        for t,n in tc.items())

    bg_mode = lbl("bg_blur", s.get("bg_blur",2))
    preview = (
        f"📚 *Queue: {count} file(s)*\n"
        f"┗ {type_str}\n\n"
        f"🎬 {lbl('style',s.get('style','cinematic'))} · "
        f"🎤 {lbl('voice',s.get('voice','calm'))} · "
        f"🌐 {lbl('lang',s.get('lang','en'))}\n"
        f"🎨 {lbl('color_grade',s.get('color_grade','vivid'))} · "
        f"🌫️ {bg_mode}\n\n"
        "_Send more pages or tap Generate_"
    )

    kb = kb_confirm(count)
    sid = ctx.user_data.get("status_msg_id")
    try:
        if sid:
            await ctx.bot.edit_message_text(preview, chat_id=cid, message_id=sid,
                                            parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        else:
            sent = await msg.reply_text(preview, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
            ctx.user_data["status_msg_id"] = sent.message_id
    except Exception:
        sent = await msg.reply_text(preview, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        ctx.user_data["status_msg_id"] = sent.message_id

# ═══════════════════════════════════════════════════════════
# CALLBACK HANDLER
# ═══════════════════════════════════════════════════════════
async def on_callback(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    cid  = q.message.chat_id
    u    = update.effective_user
    data = q.data
    await q.answer()

    if data == "noop": return

    s = db_ensure(cid, u.username or "", u.first_name or "")

    # ── Cycle setting ─────────────────────────────────────
    if data.startswith("cycle:"):
        key = data.split(":")[1]
        cur = s.get(key, CYCLES[key][0])
        nv  = next_val(cur, key)
        db_set(cid, **{key: nv})
        s = db_get(cid)
        try:
            await q.edit_message_text(settings_text(s), parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=kb_settings(s))
        except Exception: pass

    # ── Toggle boolean ─────────────────────────────────────
    elif data.startswith("toggle:"):
        key = data.split(":")[1]
        db_set(cid, **{key: 0 if s.get(key,1) else 1})
        s = db_get(cid)
        try:
            await q.edit_message_text(settings_text(s), parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=kb_settings(s))
        except Exception: pass

    # ── Info popups ────────────────────────────────────────
    elif data.startswith("info:"):
        key = data.split(":")[1]
        await q.answer(INFO_MSGS.get(key,""), show_alert=True)

    # ── Quick style presets ────────────────────────────────
    elif data.startswith("preset:"):
        pname = data.split(":")[1]
        if pname in PRESETS:
            st,gr,bg,za,br,bb = PRESETS[pname]
            db_set(cid, style=st, color_grade=gr, bg_blur=bg,
                   zoom_amount=za, blur_radius=br, blur_brightness=bb)
            s = db_get(cid)
            await q.answer(f"✅ Preset applied: {pname.replace('_',' ').title()}!", show_alert=False)
            try:
                await q.edit_message_text(settings_text(s), parse_mode=ParseMode.MARKDOWN,
                                          reply_markup=kb_settings(s))
            except Exception: pass

    # ── Show panels ───────────────────────────────────────
    elif data == "show:settings":
        s = db_get(cid)
        try:
            await q.edit_message_text(settings_text(s), parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=kb_settings(s))
        except Exception:
            await q.message.reply_text(settings_text(s), parse_mode=ParseMode.MARKDOWN,
                                       reply_markup=kb_settings(s))

    elif data == "show:quickstyle":
        try:
            await q.edit_message_text(
                "🎨 *Quick Style Presets*\n\n"
                "One tap applies a full style combination:\n"
                "_Style + Color Grade + BG Mode + Zoom_",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_quickstyle())
        except Exception:
            await q.message.reply_text(
                "🎨 *Quick Style Presets*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_quickstyle())

    elif data == "show:help":
        await q.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

    elif data == "show:stats":
        d = db_stats(cid)
        await q.answer(f"🎬 Videos: {d['videos']} | 👥 Users: {d['total_users']}",
                       show_alert=True)

    elif data == "show:testkey":
        await cmd_testkey(update, ctx)

    elif data == "show:formats":
        await q.answer(
            "Supported:\n📸 JPG/PNG/WEBP\n📄 PDF\n📦 ZIP\nMax 35 pages",
            show_alert=True)

    elif data == "settings:close":
        try:
            await q.edit_message_text("✅ Settings saved!\n\nSend your manga now.",
                                      reply_markup=kb_main())
        except Exception: pass

    elif data == "settings:reset":
        db_set(cid, lang="en", voice="calm", style="cinematic",
               color_grade="vivid", subtitles=1, speed="normal", quality="hd",
               bg_blur=4, blur_radius=18, blur_brightness=0.82,
               zoom_amount=0.05, parallax=0, action_clips=1)
        s = db_get(cid)
        await q.edit_message_text("🔄 *Settings reset!*\n\n" + settings_text(s),
                                  parse_mode=ParseMode.MARKDOWN, reply_markup=kb_settings(s))

    # ── Process / Cancel ──────────────────────────────────
    elif data == "do:process":
        ctx.user_data["status_msg_id"] = None
        try:  await q.edit_message_text("⏳ Starting video generation…")
        except: pass
        asyncio.create_task(_run_pipeline(ctx.bot, cid, ctx))

    elif data == "do:cancel":
        ctx.user_data.pop("pending_files",None)
        ctx.user_data.pop("status_msg_id",None)
        try:  await q.edit_message_text("❌ Cancelled.")
        except: pass

# ═══════════════════════════════════════════════════════════
# PIPELINE RUNNER
# ═══════════════════════════════════════════════════════════
async def _run_pipeline(bot, cid:int, ctx:ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.get("processing"): return
    ctx.user_data["processing"] = True

    files = ctx.user_data.pop("pending_files",[])
    if not files:
        await bot.send_message(cid,"⚠️ No files queued! Send manga pages first.")
        ctx.user_data["processing"] = False
        return

    s = db_get(cid) or {}

    prog = await bot.send_message(
        cid,
        "```\n🎬 MANGAVOICE ULTRA v4.1\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "[ ░░░░░░░░░░░░░░░░░░░░ ]  0%\n"
        "Initialising…\n```",
        parse_mode=ParseMode.MARKDOWN)

    async def upd(pct:int, label:str):
        filled = int(pct/5)
        bar    = "█"*filled+"░"*(20-filled)
        try:
            await bot.edit_message_text(
                f"```\n🎬 MANGAVOICE ULTRA v4.1\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"[ {bar} ]  {pct}%\n"
                f"{label}\n```",
                chat_id=cid, message_id=prog.message_id,
                parse_mode=ParseMode.MARKDOWN)
        except: pass

    try:
        from pipeline import MangaPipeline
        pipe = MangaPipeline(bot=bot, chat_id=cid, update_progress=upd,
                             settings=s, groq_key=GROQ_KEY, elevenlabs_key=EL_KEY)
        out_path, page_count = await pipe.run(files)
        db_log(cid, page_count, s.get("style","cinematic"), s.get("lang","en"))

        quality = s.get("quality","hd")
        dims    = {"sd":(854,480),"hd":(1280,720),"4k":(1920,1080)}.get(quality,(1280,720))
        bg_mode = lbl("bg_blur", s.get("bg_blur",2))
        caption = (
            "🎌 *Your Manga Video is Ready!*\n\n"
            f"📄 Pages      : `{page_count}`\n"
            f"🎬 Style      : `{lbl('style',s.get('style','cinematic'))}`\n"
            f"🎨 Grade      : `{lbl('color_grade',s.get('color_grade','vivid'))}`\n"
            f"🎤 Voice      : `{lbl('voice',s.get('voice','calm'))}`\n"
            f"🌐 Language   : `{lbl('lang',s.get('lang','en'))}`\n"
            f"🌫️ BG Mode    : `{bg_mode}`\n"
            f"📐 Quality    : `{lbl('quality',quality)}`\n"
            f"📝 Subtitles  : `{'ON ✅' if s.get('subtitles',1) else 'OFF ❌'}`\n"
            f"⚡ Action Clips: `{'ON ✅' if s.get('action_clips',1) else 'OFF ❌'}`\n\n"
            "_Enjoy your cinematic manga! 🍿_"
        )
        await bot.send_video(chat_id=cid, video=open(out_path,"rb"),
                             caption=caption, parse_mode=ParseMode.MARKDOWN,
                             supports_streaming=True, width=dims[0], height=dims[1],
                             write_timeout=300, read_timeout=300)
        try: await bot.delete_message(cid, prog.message_id)
        except: pass
        Path(out_path).unlink(missing_ok=True)

    except ImportError as e:
        pkg = str(e).split("'")[-2] if "'" in str(e) else str(e)
        await bot.edit_message_text(
            f"⚠️ *Missing:* `{pkg}`\nRun: `pip install {pkg}`",
            chat_id=cid, message_id=prog.message_id, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.exception("Pipeline error")
        await bot.edit_message_text(
            f"❌ *Error:*\n`{str(e)[:400]}`\n\n_Use /cancel then try again_",
            chat_id=cid, message_id=prog.message_id, parse_mode=ParseMode.MARKDOWN)
    finally:
        ctx.user_data["processing"] = False

# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════
async def post_init(app:Application):
    await app.bot.set_my_commands([
        BotCommand("start",   "Main menu"),
        BotCommand("settings","Settings panel"),
        BotCommand("help",    "How to use"),
        BotCommand("stats",   "Your usage stats"),
        BotCommand("testkey", "Test Groq API key"),
        BotCommand("cancel",  "Cancel processing"),
    ])

def main():
    db_init()
    app = (Application.builder()
           .token(BOT_TOKEN)
           .post_init(post_init)
           .read_timeout(600)
           .write_timeout(600)
           .connect_timeout(60)
           .pool_timeout(600)
           .build())
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("settings",cmd_settings))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("testkey", cmd_testkey))
    app.add_handler(CommandHandler("cancel",  cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.PHOTO|filters.Document.ALL, handle_file))
    print("🎌 MangaVoice Ultra v4.1 — Running!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
