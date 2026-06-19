"""
MangaVoice Ultra v3.0 — bot.py
AI: Groq (free) | TTS: gTTS / ElevenLabs | Video: FFmpeg
"""

import json, asyncio, logging, sqlite3
from pathlib import Path
from datetime import datetime

from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand)
from telegram.ext import (Application, CommandHandler, MessageHandler,
                           CallbackQueryHandler, ContextTypes, filters)
from telegram.constants import ParseMode, ChatAction

# ═══════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════
BASE    = Path(__file__).parent
CONFIG  = json.loads((BASE/"config.json").read_text())

BOT_TOKEN  = CONFIG["BOT_TOKEN"]
GROQ_KEY   = CONFIG.get("GROQ_API_KEY","")
EL_KEY     = CONFIG.get("ELEVENLABS_API_KEY","")

# ═══════════════════════════════════════════════════════════
# DATABASE  (SQLite, persistent)
# ═══════════════════════════════════════════════════════════
DB = BASE/"mangavoice.db"

def db_init():
    con = sqlite3.connect(DB)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id     INTEGER PRIMARY KEY,
            username    TEXT    DEFAULT '',
            name        TEXT    DEFAULT '',
            lang        TEXT    DEFAULT 'en',
            voice       TEXT    DEFAULT 'calm',
            style       TEXT    DEFAULT 'cinematic',
            color_grade TEXT    DEFAULT 'vivid',
            subtitles   INTEGER DEFAULT 1,
            speed       TEXT    DEFAULT 'normal',
            quality     TEXT    DEFAULT 'hd',
            joined      TEXT    DEFAULT (datetime('now')),
            videos      INTEGER DEFAULT 0
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
    con.commit(); con.close()

def db_get(cid:int) -> dict|None:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM users WHERE chat_id=?",(cid,)).fetchone()
    con.close()
    return dict(row) if row else None

def db_ensure(cid:int, username:str="", name:str="") -> dict:
    s = db_get(cid)
    if s is None:
        con = sqlite3.connect(DB)
        con.execute("INSERT OR IGNORE INTO users (chat_id,username,name) VALUES (?,?,?)",
                    (cid,username,name))
        con.commit(); con.close()
        s = db_get(cid)
    return s

def db_set(cid:int, **kwargs):
    if not kwargs: return
    con = sqlite3.connect(DB)
    sets = ",".join(f"{k}=?" for k in kwargs)
    con.execute(f"UPDATE users SET {sets} WHERE chat_id=?",
                list(kwargs.values())+[cid])
    con.commit(); con.close()

def db_log(cid:int, pages:int, style:str, lang:str):
    con = sqlite3.connect(DB)
    con.execute("INSERT INTO history (chat_id,pages,style,lang) VALUES (?,?,?,?)",
                (cid,pages,style,lang))
    con.execute("UPDATE users SET videos=videos+1 WHERE chat_id=?",(cid,))
    con.commit(); con.close()

def db_stats(cid:int) -> dict:
    con = sqlite3.connect(DB)
    row = con.execute("SELECT videos,joined FROM users WHERE chat_id=?",(cid,)).fetchone()
    total = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    con.close()
    return {"videos":row[0] if row else 0,
            "joined":(row[1] or "")[:10] if row else "N/A",
            "total_users":total}

# ═══════════════════════════════════════════════════════════
# SETTINGS LABELS
# ═══════════════════════════════════════════════════════════
CYCLES = {
    "lang":        ["en","hi"],
    "voice":       ["calm","dramatic","energetic"],
    "style":       ["cinematic","manga","noir","retro","anime","dramatic"],
    "color_grade": ["vivid","muted","warm","cold","manga_ink","golden"],
    "speed":       ["slow","normal","fast"],
    "quality":     ["sd","hd","4k"],
}
LABELS = {
    "lang":        {"en":"🇬🇧 English","hi":"🇮🇳 Hindi"},
    "voice":       {"calm":"🧘 Calm","dramatic":"🎭 Dramatic","energetic":"⚡ Energetic"},
    "style":       {"cinematic":"🎬 Cinematic","manga":"💥 Manga","noir":"🖤 Noir",
                    "retro":"📺 Retro","anime":"✨ Anime","dramatic":"🔥 Dramatic"},
    "color_grade": {"vivid":"🌈 Vivid","muted":"🫧 Muted","warm":"🔥 Warm",
                    "cold":"❄️ Cold","manga_ink":"🖋️ Ink","golden":"✨ Golden"},
    "quality":     {"sd":"📱 SD","hd":"🖥️ HD","4k":"💎 4K"},
    "speed":       {"slow":"🐢 Slow","normal":"🚶 Normal","fast":"🏃 Fast"},
}

def lbl(key:str, val:str) -> str:
    return LABELS.get(key,{}).get(val, val.title())

def next_val(current:str, key:str) -> str:
    lst = CYCLES[key]
    return lst[(lst.index(current)+1)%len(lst)] if current in lst else lst[0]

# ═══════════════════════════════════════════════════════════
# KEYBOARDS
# ═══════════════════════════════════════════════════════════
def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Settings",      callback_data="show:settings"),
         InlineKeyboardButton("❓ Help",            callback_data="show:help")],
        [InlineKeyboardButton("📊 My Stats",       callback_data="show:stats"),
         InlineKeyboardButton("🔑 Test API Key",   callback_data="show:testkey")],
        [InlineKeyboardButton("📖 Formats Info",   callback_data="show:formats")],
    ])

def kb_settings(s:dict) -> InlineKeyboardMarkup:
    sub  = "✅ Subtitles ON"  if s["subtitles"] else "❌ Subtitles OFF"
    rows = [
        [InlineKeyboardButton("🌐 Language",       callback_data="info:lang"),
         InlineKeyboardButton(lbl("lang",s["lang"]),     callback_data="cycle:lang")],
        [InlineKeyboardButton("🎤 Voice",          callback_data="info:voice"),
         InlineKeyboardButton(lbl("voice",s["voice"]),   callback_data="cycle:voice")],
        [InlineKeyboardButton("🎬 Style",          callback_data="info:style"),
         InlineKeyboardButton(lbl("style",s["style"]),   callback_data="cycle:style")],
        [InlineKeyboardButton("🎨 Color Grade",    callback_data="info:grade"),
         InlineKeyboardButton(lbl("color_grade",s["color_grade"]), callback_data="cycle:color_grade")],
        [InlineKeyboardButton("⚡ Speed",          callback_data="info:speed"),
         InlineKeyboardButton(lbl("speed",s["speed"]),   callback_data="cycle:speed")],
        [InlineKeyboardButton("📐 Quality",        callback_data="info:quality"),
         InlineKeyboardButton(lbl("quality",s["quality"]),callback_data="cycle:quality")],
        [InlineKeyboardButton(sub,                 callback_data="toggle:subtitles")],
        [InlineKeyboardButton("✅ Done",            callback_data="settings:close"),
         InlineKeyboardButton("🔄 Reset",           callback_data="settings:reset")],
    ]
    return InlineKeyboardMarkup(rows)

def kb_confirm(count:int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🚀 Generate Video — {count} page{'s' if count>1 else ''}",
                              callback_data="do:process")],
        [InlineKeyboardButton("➕ Add More Pages",  callback_data="noop"),
         InlineKeyboardButton("❌ Cancel",           callback_data="do:cancel")],
        [InlineKeyboardButton("⚙️ Change Settings", callback_data="show:settings")],
    ])

# ═══════════════════════════════════════════════════════════
# SETTINGS TEXT
# ═══════════════════════════════════════════════════════════
def settings_text(s:dict) -> str:
    return (
        "⚙️ *Current Settings*\n\n"
        f"🌐 Language   : {lbl('lang',s['lang'])}\n"
        f"🎤 Voice      : {lbl('voice',s['voice'])}\n"
        f"🎬 Style      : {lbl('style',s['style'])}\n"
        f"🎨 Color Grade: {lbl('color_grade',s['color_grade'])}\n"
        f"⚡ Speed      : {lbl('speed',s['speed'])}\n"
        f"📐 Quality    : {lbl('quality',s['quality'])}\n"
        f"📝 Subtitles  : {'ON ✅' if s['subtitles'] else 'OFF ❌'}\n\n"
        "_Tap any right button to cycle through options_"
    )

# ═══════════════════════════════════════════════════════════
# /start
# ═══════════════════════════════════════════════════════════
BANNER = (
    "```\n"
    "╔══════════════════════════════════════╗\n"
    "║   🎌  M A N G A V O I C E  B O T   ║\n"
    "║         ✦  U L T R A  v 3 . 0  ✦   ║\n"
    "╚══════════════════════════════════════╝\n"
    "```"
)

async def cmd_start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    u   = update.effective_user
    cid = update.effective_chat.id
    s   = db_ensure(cid, u.username or "", u.first_name or "")
    await ctx.bot.send_chat_action(cid, ChatAction.TYPING)
    txt = (
        f"{BANNER}\n"
        f"👋 *Welcome, {u.first_name}\\!*\n\n"
        "Transform your manga into *cinematic animated videos* "
        "with AI narration, voice\\-over \\& subtitles\\!\n\n"
        "🔥 *Features:*\n"
        "┣ 📸 Anime **JPG/PNG**, 📄 **PDF**, 📦 **ZIP** manga\n"
        "┣ 🎬 6 animation styles \\(Cinematic, Manga, Noir…\\)\n"
        "┣ 🎨 6 colour grades \\(Vivid, Ink, Golden…\\)\n"
        "┣ 🎤 3 voice styles × 2 languages\n"
        "┣ 📝 Animated subtitles on every frame\n"
        "┣ 💎 SD / HD / 4K export quality\n"
        "┗ 📚 Up to *35 pages* per video\\!\n\n"
        "📤 *Send your manga pages now\\!*"
    )
    await update.message.reply_text(
        txt, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_main())

# ═══════════════════════════════════════════════════════════
# /settings
# ═══════════════════════════════════════════════════════════
async def cmd_settings(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    u   = update.effective_user
    cid = update.effective_chat.id
    s   = db_ensure(cid, u.username or "", u.first_name or "")
    await update.message.reply_text(
        settings_text(s), parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_settings(s))

# ═══════════════════════════════════════════════════════════
# /help
# ═══════════════════════════════════════════════════════════
HELP_TEXT = """🎌 *MangaVoice Ultra v3.0 — Help*

━━━━ 📤 *HOW TO USE* ━━━━
1. Send manga as:
   • 📸 Photos or image files (JPG/PNG/WEBP)
   • 📄 PDF manga chapter
   • 📦 ZIP of manga pages
2. Tap *Generate Video* when ready
3. Wait 2–5 mins → receive your video!

━━━━ 🎬 *ANIMATION STYLES* ━━━━
🎬 *Cinematic* — Slow zoom + pan, cross-dissolve
💥 *Manga* — Speed-zoom + sharp impact look
🖤 *Noir* — Deep vignette + dramatic shadows
📺 *Retro* — Film grain + diagonal wipe
✨ *Anime* — Glow overlay + screen blend
🔥 *Dramatic* — Fast zoom + heavy vignette

━━━━ 🎨 *COLOUR GRADES* ━━━━
🌈 Vivid · 🫧 Muted · 🔥 Warm
❄️ Cold · 🖋️ Manga Ink · ✨ Golden

━━━━ 🎤 *VOICES* ━━━━
🧘 Calm — Smooth story-teller
🎭 Dramatic — Intense cinematic narrator
⚡ Energetic — Anime dub energy

━━━━ ⌨️ *COMMANDS* ━━━━
/start — Main menu
/settings — Settings panel
/help — This guide
/stats — Your usage stats
/testkey — Test Groq API key
/cancel — Cancel processing
"""

async def cmd_help(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    await (update.message or update.callback_query.message).reply_text(
        HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

# ═══════════════════════════════════════════════════════════
# /stats
# ═══════════════════════════════════════════════════════════
async def cmd_stats(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    cid  = update.effective_chat.id
    data = db_stats(cid)
    txt  = (
        "📊 *Your Stats*\n\n"
        f"🎬 Videos Generated : `{data['videos']}`\n"
        f"📅 Member Since     : `{data['joined']}`\n"
        f"👥 Total Users      : `{data['total_users']}`\n"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)

# ═══════════════════════════════════════════════════════════
# /testkey
# ═══════════════════════════════════════════════════════════
async def cmd_testkey(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.callback_query.message
    await msg.reply_text("🔑 Testing your Groq API key…")
    key = GROQ_KEY.strip()
    if not key:
        await msg.reply_text(
            "❌ *GROQ\\_API\\_KEY is empty in config\\.json\\!*\n\n"
            "Get FREE key at: console\\.groq\\.com\nNo credit card needed\\!",
            parse_mode=ParseMode.MARKDOWN_V2); return
    if not key.startswith("gsk_"):
        await msg.reply_text(
            f"❌ Key format wrong: `{key[:14]}…`\n\nMust start with `gsk_`\n"
            "Get fresh key at console\\.groq\\.com",
            parse_mode=ParseMode.MARKDOWN_V2); return
    import requests as req
    try:
        r = req.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"},
            json={"model":"llama-3.2-11b-vision-preview",
                  "messages":[{"role":"user","content":"Say OK"}],
                  "max_tokens":5},
            timeout=15)
        if r.status_code==200:
            await msg.reply_text("✅ *Groq API key is working perfectly\\!*\n\nSend your manga now\\!",
                                 parse_mode=ParseMode.MARKDOWN_V2)
        elif r.status_code==401:
            await msg.reply_text("❌ *Key rejected \\(401\\)*\n\nCreate fresh key at console\\.groq\\.com → API Keys",
                                 parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await msg.reply_text(f"⚠️ Error `{r.status_code}`: {r.text[:200]}")
    except Exception as e:
        await msg.reply_text(f"❌ Connection error: `{str(e)[:200]}`")

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
    cid  = update.effective_chat.id
    msg  = update.message
    u    = update.effective_user

    if ctx.user_data.get("processing"):
        await msg.reply_text("⏳ Still processing previous manga. Use /cancel to stop.")
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
            await msg.reply_text("⚠️ Unsupported format. Send JPG/PNG/PDF/ZIP manga files.")
            return
    else:
        return

    count = len(files)
    tc    = {}
    for f in files:
        tc[f["type"]] = tc.get(f["type"],0)+1
    type_str = " · ".join(
        f"{'📸' if t=='image' else '📄' if t=='pdf' else '📦'} {n} {t}"
        for t,n in tc.items())

    preview = (
        f"📚 *Queue: {count} file(s)*\n"
        f"┗ {type_str}\n\n"
        f"🎬 {lbl('style',s['style'])} · "
        f"🎤 {lbl('voice',s['voice'])} · "
        f"🌐 {lbl('lang',s['lang'])}\n"
        f"🎨 {lbl('color_grade',s['color_grade'])} · "
        f"📐 {lbl('quality',s['quality'])}\n\n"
        f"_Send more pages or tap Generate_"
    )

    kb        = kb_confirm(count)
    status_id = ctx.user_data.get("status_msg_id")
    try:
        if status_id:
            await ctx.bot.edit_message_text(
                preview, chat_id=cid, message_id=status_id,
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
INFO_MSGS = {
    "lang":    "🌐 Language: narration & subtitles language",
    "voice":   "🎤 Voice: Calm=story-teller · Dramatic=cinematic · Energetic=anime",
    "style":   "🎬 Style: animation look — Cinematic/Manga/Noir/Retro/Anime/Dramatic",
    "grade":   "🎨 Color Grade: Vivid/Muted/Warm/Cold/Ink/Golden",
    "speed":   "⚡ Speed: how long each page shows (Slow=more time, Fast=quicker)",
    "quality": "📐 Quality: SD=fast upload · HD=good quality · 4K=best quality",
}

async def on_callback(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    cid  = q.message.chat_id
    u    = update.effective_user
    data = q.data
    await q.answer()

    if data=="noop": return

    s = db_ensure(cid, u.username or "", u.first_name or "")

    if data.startswith("cycle:"):
        key = data.split(":")[1]
        db_set(cid, **{key: next_val(s[key], key)})
        s   = db_get(cid)
        try:
            await q.edit_message_text(
                settings_text(s), parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_settings(s))
        except Exception: pass

    elif data.startswith("toggle:"):
        key = data.split(":")[1]
        db_set(cid, **{key: 0 if s[key] else 1})
        s   = db_get(cid)
        try:
            await q.edit_message_text(
                settings_text(s), parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_settings(s))
        except Exception: pass

    elif data.startswith("info:"):
        key = data.split(":")[1]
        await q.answer(INFO_MSGS.get(key,""), show_alert=True)

    elif data=="show:settings":
        try:
            await q.edit_message_text(
                settings_text(s), parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_settings(s))
        except Exception:
            await q.message.reply_text(
                settings_text(s), parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_settings(s))

    elif data=="settings:close":
        try:
            await q.edit_message_text(
                "✅ Settings saved!\n\nSend your manga pages now.",
                reply_markup=kb_main())
        except Exception: pass

    elif data=="settings:reset":
        db_set(cid, lang="en", voice="calm", style="cinematic",
               color_grade="vivid", subtitles=1, speed="normal", quality="hd")
        s = db_get(cid)
        await q.edit_message_text(
            "🔄 Settings reset to defaults!\n\n" + settings_text(s),
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_settings(s))

    elif data=="show:help":
        await q.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

    elif data=="show:stats":
        d = db_stats(cid)
        await q.answer(
            f"🎬 Your videos: {d['videos']} | 👥 Total users: {d['total_users']}",
            show_alert=True)

    elif data=="show:testkey":
        await cmd_testkey(update, ctx)

    elif data=="show:formats":
        await q.answer(
            "Supported formats:\n"
            "📸 JPG / PNG / WEBP images\n"
            "📄 PDF manga chapter\n"
            "📦 ZIP of manga pages\n"
            "Max 35 pages per video",
            show_alert=True)

    elif data=="do:process":
        ctx.user_data["status_msg_id"] = None
        try:
            await q.edit_message_text("⏳ Starting video generation…")
        except Exception: pass
        asyncio.create_task(_run_pipeline(ctx.bot, cid, ctx))

    elif data=="do:cancel":
        ctx.user_data.pop("pending_files",None)
        ctx.user_data.pop("status_msg_id",None)
        try:
            await q.edit_message_text("❌ Cancelled. Send new manga whenever you're ready!")
        except Exception: pass

# ═══════════════════════════════════════════════════════════
# PIPELINE RUNNER
# ═══════════════════════════════════════════════════════════
async def _run_pipeline(bot, cid:int, ctx:ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.get("processing"): return
    ctx.user_data["processing"] = True

    files = ctx.user_data.pop("pending_files",[])
    if not files:
        await bot.send_message(cid,"⚠️ No files queued. Send manga pages first!")
        ctx.user_data["processing"] = False
        return

    s = db_get(cid) or {}

    progress = await bot.send_message(
        cid,
        "```\n"
        "🎬 MANGAVOICE ULTRA v3.0\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "[ ░░░░░░░░░░░░░░░░░░░░ ]  0%\n"
        "Starting pipeline…\n"
        "```",
        parse_mode=ParseMode.MARKDOWN)

    async def upd(pct:int, label:str):
        filled = int(pct/5)
        bar    = "█"*filled + "░"*(20-filled)
        try:
            await bot.edit_message_text(
                f"```\n"
                f"🎬 MANGAVOICE ULTRA v3.0\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"[ {bar} ]  {pct}%\n"
                f"{label}\n"
                f"```",
                chat_id=cid, message_id=progress.message_id,
                parse_mode=ParseMode.MARKDOWN)
        except Exception: pass

    try:
        from pipeline import MangaPipeline
        pipe = MangaPipeline(
            bot=bot, chat_id=cid, update_progress=upd,
            settings=s, groq_key=GROQ_KEY, elevenlabs_key=EL_KEY)
        out_path, page_count = await pipe.run(files)

        db_log(cid, page_count, s.get("style","cinematic"), s.get("lang","en"))

        quality = s.get("quality","hd")
        dims    = {"sd":(854,480),"hd":(1280,720),"4k":(1920,1080)}.get(quality,(1280,720))

        caption = (
            "🎌 *Your Manga Video is Ready!*\n\n"
            f"📄 Pages      : `{page_count}`\n"
            f"🎬 Style      : `{lbl('style',s.get('style','cinematic'))}`\n"
            f"🎨 Grade      : `{lbl('color_grade',s.get('color_grade','vivid'))}`\n"
            f"🎤 Voice      : `{lbl('voice',s.get('voice','calm'))}`\n"
            f"🌐 Language   : `{lbl('lang',s.get('lang','en'))}`\n"
            f"📐 Quality    : `{lbl('quality',quality)}`\n"
            f"📝 Subtitles  : `{'ON ✅' if s.get('subtitles') else 'OFF ❌'}`\n\n"
            "_Enjoy your cinematic manga experience! 🍿_"
        )

        await bot.send_video(
            chat_id=cid,
            video=open(out_path,"rb"),
            caption=caption,
            parse_mode=ParseMode.MARKDOWN,
            supports_streaming=True,
            width=dims[0], height=dims[1])

        try: await bot.delete_message(cid, progress.message_id)
        except Exception: pass

        from pathlib import Path
        Path(out_path).unlink(missing_ok=True)

    except ImportError as e:
        pkg = str(e).split("'")[-2] if "'" in str(e) else str(e)
        await bot.edit_message_text(
            f"⚠️ *Missing package:* `{pkg}`\n\nRun: `pip install {pkg}`",
            chat_id=cid, message_id=progress.message_id,
            parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.exception("Pipeline error")
        await bot.edit_message_text(
            f"❌ *Error:*\n`{str(e)[:400]}`\n\n"
            "_Try /cancel and send the manga again_",
            chat_id=cid, message_id=progress.message_id,
            parse_mode=ParseMode.MARKDOWN)
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
        BotCommand("stats",   "Your stats"),
        BotCommand("testkey", "Test Groq API key"),
        BotCommand("cancel",  "Cancel processing"),
    ])

def main():
    db_init()
    app = (Application.builder()
           .token(BOT_TOKEN)
           .post_init(post_init)
           .build())
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("settings",cmd_settings))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("testkey", cmd_testkey))
    app.add_handler(CommandHandler("cancel",  cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.PHOTO|filters.Document.ALL, handle_file))
    print("🎌 MangaVoice Ultra v3.0 — Running!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
