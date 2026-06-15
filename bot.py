"""
███╗   ███╗ █████╗ ███╗   ██╗ ██████╗  █████╗ ██╗   ██╗ ██████╗ ██╗ ██████╗███████╗
████╗ ████║██╔══██╗████╗  ██║██╔════╝ ██╔══██╗██║   ██║██╔═══██╗██║██╔════╝██╔════╝
██╔████╔██║███████║██╔██╗ ██║██║  ███╗███████║██║   ██║██║   ██║██║██║     █████╗
██║╚██╔╝██║██╔══██║██║╚██╗██║██║   ██║██╔══██║╚██╗ ██╔╝██║   ██║██║██║     ██╔══╝
██║ ╚═╝ ██║██║  ██║██║ ╚████║╚██████╔╝██║  ██║ ╚████╔╝ ╚██████╔╝██║╚██████╗███████╗
╚═╝     ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝ ╚═════╝ ╚═╝  ╚═╝  ╚═══╝   ╚═════╝ ╚═╝ ╚═════╝╚══════╝
                         B O T  v2.0  —  Ultra Edition
"""

import os, json, asyncio, logging, sqlite3, time, hashlib
from pathlib import Path
from datetime import datetime
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, InputMediaPhoto
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.constants import ParseMode, ChatAction

# ══════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════
BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
DB_FILE     = BASE_DIR / "mangavoice.db"

with open(CONFIG_FILE) as f:
    config = json.load(f)

BOT_TOKEN      = config["BOT_TOKEN"]
ANTHROPIC_KEY  = config.get("ANTHROPIC_API_KEY", "")
ELEVENLABS_KEY = config.get("ELEVENLABS_API_KEY", "")

# ══════════════════════════════════════════════════════════════════════
# DATABASE  (SQLite — persistent across restarts)
# ══════════════════════════════════════════════════════════════════════
def init_db():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id     INTEGER PRIMARY KEY,
            username    TEXT,
            first_name  TEXT,
            lang        TEXT    DEFAULT 'en',
            voice       TEXT    DEFAULT 'calm',
            style       TEXT    DEFAULT 'cinematic',
            subtitles   INTEGER DEFAULT 1,
            speed       TEXT    DEFAULT 'normal',
            quality     TEXT    DEFAULT 'hd',
            color_grade TEXT    DEFAULT 'vivid',
            bg_blur     INTEGER DEFAULT 0,
            joined_at   TEXT    DEFAULT (datetime('now')),
            video_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     INTEGER,
            pages       INTEGER,
            style       TEXT,
            lang        TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        );
    """)
    con.commit()
    con.close()

def db_get(chat_id: int) -> dict:
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    con.close()
    if row:
        return dict(row)
    return None

def db_upsert(chat_id: int, username: str, first_name: str, **kwargs):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT chat_id FROM users WHERE chat_id=?", (chat_id,))
    exists = cur.fetchone()
    if exists:
        if kwargs:
            sets = ", ".join(f"{k}=?" for k in kwargs)
            cur.execute(f"UPDATE users SET {sets} WHERE chat_id=?",
                        list(kwargs.values()) + [chat_id])
    else:
        cur.execute("""
            INSERT INTO users (chat_id, username, first_name)
            VALUES (?,?,?)
        """, (chat_id, username, first_name))
        if kwargs:
            sets = ", ".join(f"{k}=?" for k in kwargs)
            cur.execute(f"UPDATE users SET {sets} WHERE chat_id=?",
                        list(kwargs.values()) + [chat_id])
    con.commit()
    con.close()

def db_log_video(chat_id: int, pages: int, style: str, lang: str):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("INSERT INTO history (chat_id,pages,style,lang) VALUES (?,?,?,?)",
                (chat_id, pages, style, lang))
    cur.execute("UPDATE users SET video_count=video_count+1 WHERE chat_id=?", (chat_id,))
    con.commit()
    con.close()

def db_stats(chat_id: int) -> dict:
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT video_count, joined_at FROM users WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM users")
    total = cur.fetchone()[0]
    con.close()
    if row:
        return {"video_count": row[0], "joined_at": row[1], "total_users": total}
    return {"video_count": 0, "joined_at": "N/A", "total_users": total}

def get_settings(chat_id: int, username="", first_name="") -> dict:
    s = db_get(chat_id)
    if s is None:
        db_upsert(chat_id, username, first_name)
        s = db_get(chat_id)
    return s

def update_setting(chat_id: int, key: str, value):
    db_upsert(chat_id, "", "", **{key: value})

# ══════════════════════════════════════════════════════════════════════
# CYCLE MAPS
# ══════════════════════════════════════════════════════════════════════
CYCLES = {
    "lang":        ["en", "hi"],
    "voice":       ["calm", "dramatic", "energetic"],
    "style":       ["cinematic", "manga", "noir", "retro", "anime"],
    "speed":       ["slow", "normal", "fast"],
    "quality":     ["sd", "hd", "4k"],
    "color_grade": ["vivid", "muted", "warm", "cold", "manga_ink"],
    "bg_blur":     [0, 1],
}

LANG_FLAGS  = {"en": "🇬🇧 English", "hi": "🇮🇳 Hindi"}
VOICE_ICONS = {"calm": "🧘 Calm", "dramatic": "🎭 Dramatic", "energetic": "⚡ Energetic"}
STYLE_ICONS = {
    "cinematic": "🎬 Cinematic",
    "manga":     "💥 Manga",
    "noir":      "🖤 Noir",
    "retro":     "📺 Retro",
    "anime":     "✨ Anime",
}
GRADE_ICONS = {
    "vivid":     "🌈 Vivid",
    "muted":     "🫧 Muted",
    "warm":      "🔥 Warm",
    "cold":      "❄️ Cold",
    "manga_ink": "🖋️ Ink",
}
QUALITY_ICONS = {"sd": "📱 SD", "hd": "🖥️ HD", "4k": "💎 4K"}

def cycle_val(current, key):
    lst = CYCLES[key]
    idx = lst.index(current) if current in lst else 0
    return lst[(idx + 1) % len(lst)]

# ══════════════════════════════════════════════════════════════════════
# KEYBOARDS
# ══════════════════════════════════════════════════════════════════════
def settings_keyboard(s: dict) -> InlineKeyboardMarkup:
    sub_label = "✅ Subtitles" if s["subtitles"] else "❌ Subtitles"
    bg_label  = "🌫️ BG Blur ON" if s["bg_blur"] else "🖼️ BG Blur OFF"
    kb = [
        [InlineKeyboardButton("━━━ 🌐 LANGUAGE ━━━",     callback_data="noop")],
        [InlineKeyboardButton(LANG_FLAGS[s["lang"]],       callback_data="cycle:lang"),
         InlineKeyboardButton("Change →",                  callback_data="cycle:lang")],

        [InlineKeyboardButton("━━━ 🎤 VOICE ━━━",         callback_data="noop")],
        [InlineKeyboardButton(VOICE_ICONS[s["voice"]],     callback_data="cycle:voice"),
         InlineKeyboardButton("Change →",                  callback_data="cycle:voice")],

        [InlineKeyboardButton("━━━ 🎬 STYLE ━━━",         callback_data="noop")],
        [InlineKeyboardButton(STYLE_ICONS[s["style"]],     callback_data="cycle:style"),
         InlineKeyboardButton("Change →",                  callback_data="cycle:style")],

        [InlineKeyboardButton("━━━ 🎨 COLOR GRADE ━━━",   callback_data="noop")],
        [InlineKeyboardButton(GRADE_ICONS[s["color_grade"]], callback_data="cycle:color_grade"),
         InlineKeyboardButton("Change →",                  callback_data="cycle:color_grade")],

        [InlineKeyboardButton("━━━ ⚙️ OPTIONS ━━━",       callback_data="noop")],
        [InlineKeyboardButton(sub_label,                   callback_data="toggle:subtitles"),
         InlineKeyboardButton(bg_label,                    callback_data="toggle:bg_blur")],

        [InlineKeyboardButton("⚡ " + s["speed"].upper(), callback_data="cycle:speed"),
         InlineKeyboardButton(QUALITY_ICONS[s["quality"]], callback_data="cycle:quality")],

        [InlineKeyboardButton("━━━━━━━━━━━━━━━━━━━━━",    callback_data="noop")],
        [InlineKeyboardButton("✅ Save & Close",            callback_data="settings:close"),
         InlineKeyboardButton("🔄 Reset Defaults",          callback_data="settings:reset")],
    ]
    return InlineKeyboardMarkup(kb)

def main_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("⚙️  Settings",      callback_data="show:settings"),
         InlineKeyboardButton("❓  Help",            callback_data="show:help")],
        [InlineKeyboardButton("📊  My Stats",       callback_data="show:stats"),
         InlineKeyboardButton("🎬  Demo Mode",       callback_data="show:demo")],
        [InlineKeyboardButton("📖  Supported Formats", callback_data="show:formats")],
    ]
    return InlineKeyboardMarkup(kb)

def confirm_keyboard(count: int) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(f"🚀  Generate Video ({count} page{'s' if count>1 else ''})",
                              callback_data="confirm_process:go")],
        [InlineKeyboardButton("➕  Add More Pages",  callback_data="add_more"),
         InlineKeyboardButton("❌  Cancel",           callback_data="cancel_process")],
        [InlineKeyboardButton("⚙️  Change Settings", callback_data="show:settings_inline")],
    ]
    return InlineKeyboardMarkup(kb)

# ══════════════════════════════════════════════════════════════════════
# START MESSAGE  (ultra cinematic)
# ══════════════════════════════════════════════════════════════════════
START_ART = """
╔══════════════════════════════════════╗
║   🎌  M A N G A V O I C E  B O T   ║
║          ✦  U L T R A  v2.0  ✦      ║
╚══════════════════════════════════════╝
"""

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    cid  = update.effective_chat.id
    name = user.first_name or "Manga Fan"
    s    = get_settings(cid, user.username or "", name)

    await ctx.bot.send_chat_action(cid, ChatAction.TYPING)

    text = (
        f"```\n{START_ART}```\n"
        f"👋 *Konnichiwa, {name}\\-san\\!*\n\n"
        "Welcome to the most *advanced manga narrator bot* on Telegram\\.\n\n"
        "🔥 *What I can do:*\n"
        "┣ 📸 Animate manga pages with *cinematic effects*\n"
        "┣ 🎤 Generate *AI voice narration* per panel\n"
        "┣ 🌐 Narrate in *English or Hindi*\n"
        "┣ 📝 Burn *animated subtitles* on video\n"
        "┣ 🎨 Apply *5 animation styles* \\+ *5 color grades*\n"
        "┣ 📄 Supports *JPG, PNG, PDF, ZIP* manga files\n"
        "┗ 💎 Export in *SD / HD / 4K* quality\n\n"
        "📤 *Just send your manga files and tap Generate\\!*"
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=main_keyboard()
    )

# ══════════════════════════════════════════════════════════════════════
# HELP
# ══════════════════════════════════════════════════════════════════════
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎌 *MangaVoice Ultra — Complete Guide*\n\n"
        "━━━━ 📤 *HOW TO USE* ━━━━\n"
        "1\\. Send your manga as:\n"
        "   • 📸 Individual images \\(JPG/PNG/WEBP\\)\n"
        "   • 📄 Manga PDF \\(full chapter\\)\n"
        "   • 📦 ZIP file \\(folder of pages\\)\n"
        "2\\. Tap *Generate Video* when ready\n"
        "3\\. Wait 1\\-3 mins → receive cinematic video\\!\n\n"
        "━━━━ 🎬 *ANIMATION STYLES* ━━━━\n"
        "🎬 *Cinematic* — Hollywood slow zoom \\+ pan\n"
        "💥 *Manga* — Speed lines \\+ impact zooms\n"
        "🖤 *Noir* — Dramatic shadows \\+ contrast\n"
        "📺 *Retro* — Film grain \\+ vignette\n"
        "✨ *Anime* — Glowing edges \\+ shimmer\n\n"
        "━━━━ 🎨 *COLOR GRADES* ━━━━\n"
        "🌈 Vivid · 🫧 Muted · 🔥 Warm · ❄️ Cold · 🖋️ Ink\n\n"
        "━━━━ 🎤 *VOICES* ━━━━\n"
        "🧘 *Calm* — Smooth story\\-teller\n"
        "🎭 *Dramatic* — Intense action narrator\n"
        "⚡ *Energetic* — Anime dub energy\n\n"
        "━━━━ ⌨️ *COMMANDS* ━━━━\n"
        "/start — Main menu\n"
        "/settings — Settings panel\n"
        "/help — This guide\n"
        "/stats — Your usage stats\n"
        "/cancel — Cancel processing\n"
    )
    msg = update.message or update.callback_query.message
    await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)

# ══════════════════════════════════════════════════════════════════════
# SETTINGS COMMAND
# ══════════════════════════════════════════════════════════════════════
async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    cid  = update.effective_chat.id
    s    = get_settings(cid, user.username or "", user.first_name or "")
    text = _settings_text(s)
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=settings_keyboard(s)
    )

def _settings_text(s: dict) -> str:
    return (
        "⚙️ *Your Settings*\n\n"
        f"🌐 Language   : `{LANG_FLAGS[s['lang']]}`\n"
        f"🎤 Voice      : `{VOICE_ICONS[s['voice']]}`\n"
        f"🎬 Style      : `{STYLE_ICONS[s['style']]}`\n"
        f"🎨 Color Grade: `{GRADE_ICONS[s['color_grade']]}`\n"
        f"📝 Subtitles  : `{'ON ✅' if s['subtitles'] else 'OFF ❌'}`\n"
        f"🌫️ BG Blur    : `{'ON ✅' if s['bg_blur'] else 'OFF ❌'}`\n"
        f"⚡ Speed      : `{s['speed'].upper()}`\n"
        f"📐 Quality    : `{QUALITY_ICONS[s['quality']]}`\n"
    )

# ══════════════════════════════════════════════════════════════════════
# STATS
# ══════════════════════════════════════════════════════════════════════
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid  = update.effective_chat.id
    data = db_stats(cid)
    text = (
        "📊 *Your MangaVoice Stats*\n\n"
        f"🎬 Videos Generated : `{data['video_count']}`\n"
        f"📅 Member Since     : `{data['joined_at'][:10]}`\n"
        f"👥 Total Bot Users  : `{data['total_users']}`\n"
    )
    msg = update.message or update.callback_query.message
    await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)

# ══════════════════════════════════════════════════════════════════════
# TEST KEY
# ══════════════════════════════════════════════════════════════════════
async def cmd_testkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔑 Testing your Anthropic API key…")
    key = ANTHROPIC_KEY.strip()

    if not key:
        await update.message.reply_text(
            "❌ *ANTHROPIC\\_API\\_KEY is empty in config\\.json\\!*\n\n"
            "Add your key and restart the bot\\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return
    if not key.startswith("sk-ant-"):
        await update.message.reply_text(
            f"❌ *Key looks wrong:* `{key[:15]}…`\n\n"
            "It must start with `sk\\-ant\\-`\n"
            "Copy it fresh from console\\.anthropic\\.com",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    import requests as req
    try:
        r = req.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "Say OK"}],
            },
            timeout=15,
        )
        if r.status_code == 200:
            await update.message.reply_text(
                "✅ *API key is working perfectly\\!*\n\nYou can now send manga pages\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
        elif r.status_code == 401:
            await update.message.reply_text(
                "❌ *401 Unauthorized*\n\n"
                "Your key is being rejected\\. Most common causes:\n\n"
                "1\\. *No billing added* → go to console\\.anthropic\\.com/billing\n"
                "2\\. *Wrong key* → create a fresh one at console\\.anthropic\\.com/api\\-keys\n"
                "3\\. *Extra spaces* in config\\.json around the key\n\n"
                "After fixing, restart the bot and run /testkey again\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
        elif r.status_code == 403:
            await update.message.reply_text(
                "❌ *403 Forbidden* — Add billing at console\\.anthropic\\.com/billing",
                parse_mode=ParseMode.MARKDOWN_V2
            )
        else:
            await update.message.reply_text(
                f"⚠️ *API Error {r.status_code}:*\n`{r.text[:300]}`",
                parse_mode=ParseMode.MARKDOWN_V2
            )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Connection error: `{str(e)[:200]}`",
            parse_mode=ParseMode.MARKDOWN_V2
        )

# ══════════════════════════════════════════════════════════════════════
# CANCEL
# ══════════════════════════════════════════════════════════════════════
async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("pending_files", None)
    ctx.user_data.pop("status_msg_id", None)
    ctx.user_data["processing"] = False
    await update.message.reply_text(
        "❌ *Cancelled\\.*\n\nSend new manga pages whenever you're ready\\!",
        parse_mode=ParseMode.MARKDOWN_V2
    )

# ══════════════════════════════════════════════════════════════════════
# FILE HANDLER  (images, PDF, ZIP)
# ══════════════════════════════════════════════════════════════════════
ALLOWED_MIME = {
    "image/jpeg", "image/png", "image/webp",
    "application/pdf",
    "application/zip", "application/x-zip-compressed",
    "application/octet-stream",
}

async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid  = update.effective_chat.id
    msg  = update.message
    user = update.effective_user

    if ctx.user_data.get("processing"):
        await msg.reply_text("⏳ Still processing your previous manga. Please wait or /cancel")
        return

    s     = get_settings(cid, user.username or "", user.first_name or "")
    files = ctx.user_data.setdefault("pending_files", [])

    if msg.photo:
        photo = max(msg.photo, key=lambda p: p.file_size)
        files.append({"type": "image", "file_id": photo.file_id, "name": f"page_{len(files):03d}.jpg"})

    elif msg.document:
        doc  = msg.document
        mime = doc.mime_type or ""
        name = doc.file_name or "file"
        ext  = Path(name).suffix.lower()

        if mime in ("image/jpeg","image/png","image/webp") or ext in (".jpg",".jpeg",".png",".webp"):
            files.append({"type": "image", "file_id": doc.file_id, "name": name})
        elif mime == "application/pdf" or ext == ".pdf":
            files.append({"type": "pdf",   "file_id": doc.file_id, "name": name})
        elif "zip" in mime or ext == ".zip":
            files.append({"type": "zip",   "file_id": doc.file_id, "name": name})
        else:
            await msg.reply_text(
                "⚠️ Unsupported file\\. Send: *JPG/PNG/WEBP, PDF, or ZIP*",
                parse_mode=ParseMode.MARKDOWN_V2
            )
            return
    else:
        return

    count = len(files)
    # Build a nice queue preview
    type_counts = {}
    for f in files:
        type_counts[f["type"]] = type_counts.get(f["type"], 0) + 1

    type_str = " · ".join(
        f"{'📸' if t=='image' else '📄' if t=='pdf' else '📦'} {n} {t}"
        for t, n in type_counts.items()
    )

    preview = (
        f"📚 *Queue: {count} file\\(s\\)*\n"
        f"┗ {type_str}\n\n"
        f"🎬 `{STYLE_ICONS[s['style']]}` · "
        f"🎤 `{VOICE_ICONS[s['voice']]}` · "
        f"🌐 `{LANG_FLAGS[s['lang']]}`\n\n"
        "_Keep sending pages or tap Generate when ready_"
    )

    kb = confirm_keyboard(count)
    status_id = ctx.user_data.get("status_msg_id")
    try:
        if status_id:
            await ctx.bot.edit_message_text(
                preview, chat_id=cid, message_id=status_id,
                parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb
            )
        else:
            sent = await msg.reply_text(preview, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)
            ctx.user_data["status_msg_id"] = sent.message_id
    except Exception:
        sent = await msg.reply_text(preview, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)
        ctx.user_data["status_msg_id"] = sent.message_id

# ══════════════════════════════════════════════════════════════════════
# CALLBACK HANDLER
# ══════════════════════════════════════════════════════════════════════
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    cid  = q.message.chat_id
    user = update.effective_user
    await q.answer()
    data = q.data

    if data == "noop":
        return

    s = get_settings(cid, user.username or "", user.first_name or "")

    # ─ Cycle a setting ──────────────────────────────────────────
    if data.startswith("cycle:"):
        key  = data.split(":")[1]
        newv = cycle_val(s[key], key)
        update_setting(cid, key, newv)
        s    = get_settings(cid)
        await q.edit_message_text(
            _settings_text(s),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=settings_keyboard(s)
        )

    # ─ Toggle boolean ───────────────────────────────────────────
    elif data.startswith("toggle:"):
        key  = data.split(":")[1]
        newv = 0 if s[key] else 1
        update_setting(cid, key, newv)
        s    = get_settings(cid)
        await q.edit_message_text(
            _settings_text(s),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=settings_keyboard(s)
        )

    # ─ Settings panel ───────────────────────────────────────────
    elif data in ("show:settings", "show:settings_inline"):
        await q.edit_message_text(
            _settings_text(s),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=settings_keyboard(s)
        )

    elif data == "settings:close":
        await q.edit_message_text(
            "✅ *Settings saved\\!*\n\nSend your manga pages now\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=main_keyboard()
        )

    elif data == "settings:reset":
        for k, v in [("lang","en"),("voice","calm"),("style","cinematic"),
                     ("subtitles",1),("speed","normal"),("quality","hd"),
                     ("color_grade","vivid"),("bg_blur",0)]:
            update_setting(cid, k, v)
        s = get_settings(cid)
        await q.edit_message_text(
            "🔄 *Settings reset to defaults\\!*\n\n" + _settings_text(s),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=settings_keyboard(s)
        )

    # ─ Help ─────────────────────────────────────────────────────
    elif data == "show:help":
        await q.message.reply_text(
            "Use /help for the full guide\\.", parse_mode=ParseMode.MARKDOWN_V2
        )

    # ─ Stats ────────────────────────────────────────────────────
    elif data == "show:stats":
        d = db_stats(cid)
        await q.answer(
            f"🎬 Videos: {d['video_count']} | 👥 Users: {d['total_users']}",
            show_alert=True
        )

    # ─ Formats info ─────────────────────────────────────────────
    elif data == "show:formats":
        await q.answer(
            "Supported: JPG · PNG · WEBP · PDF · ZIP\nMax 20 pages per video",
            show_alert=True
        )

    # ─ Demo mode ────────────────────────────────────────────────
    elif data == "show:demo":
        await q.answer(
            "🎬 Demo mode coming soon!\nSend your own manga for now.",
            show_alert=True
        )

    # ─ Confirm processing ────────────────────────────────────────
    elif data == "confirm_process:go":
        await q.edit_message_text("⏳ *Starting video generation…*", parse_mode=ParseMode.MARKDOWN_V2)
        ctx.user_data["status_msg_id"] = None
        asyncio.create_task(run_pipeline(ctx.bot, cid, ctx, q.message.message_id))

    # ─ Cancel ───────────────────────────────────────────────────
    elif data == "cancel_process":
        ctx.user_data.pop("pending_files", None)
        ctx.user_data.pop("status_msg_id", None)
        await q.edit_message_text("❌ *Cancelled\\.*", parse_mode=ParseMode.MARKDOWN_V2)

    elif data == "add_more":
        await q.answer("📸 Send more pages! Tap Generate when done.", show_alert=False)

# ══════════════════════════════════════════════════════════════════════
# PIPELINE RUNNER
# ══════════════════════════════════════════════════════════════════════
async def run_pipeline(bot, cid: int, ctx: ContextTypes.DEFAULT_TYPE, msg_id: int):
    if ctx.user_data.get("processing"):
        return
    ctx.user_data["processing"] = True

    files = ctx.user_data.pop("pending_files", [])
    if not files:
        await bot.send_message(cid, "⚠️ No files queued. Send some manga pages first!")
        ctx.user_data["processing"] = False
        return

    s = db_get(cid) or {}

    # Animated progress message
    progress = await bot.send_message(
        cid,
        "```\n🎬 MANGAVOICE ULTRA — Processing\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "[ ░░░░░░░░░░░░░░░░░░░░ ]  0%\n"
        "Initialising pipeline…\n```",
        parse_mode=ParseMode.MARKDOWN_V2
    )

    async def update_progress(pct: int, label: str):
        filled = int(pct / 5)
        bar    = "█" * filled + "░" * (20 - filled)
        try:
            await bot.edit_message_text(
                f"```\n🎬 MANGAVOICE ULTRA — Processing\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"[ {bar} ]  {pct}%\n"
                f"{label}\n```",
                chat_id=cid,
                message_id=progress.message_id,
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception:
            pass

    try:
        from pipeline import MangaPipeline
        pipeline = MangaPipeline(
            bot=bot,
            chat_id=cid,
            update_progress=update_progress,
            settings=s,
            anthropic_key=ANTHROPIC_KEY,
            elevenlabs_key=ELEVENLABS_KEY,
        )
        output_video, page_count = await pipeline.run(files)

        db_log_video(cid, page_count, s.get("style","cinematic"), s.get("lang","en"))

        style_label = STYLE_ICONS.get(s.get("style","cinematic"), "🎬 Cinematic")
        caption = (
            "🎌 *Your Manga Video is Ready\\!*\n\n"
            f"📄 Pages     : `{page_count}`\n"
            f"🎬 Style     : `{style_label}`\n"
            f"🎤 Voice     : `{VOICE_ICONS.get(s.get('voice','calm'), '🧘 Calm')}`\n"
            f"🌐 Language  : `{LANG_FLAGS.get(s.get('lang','en'), '🇬🇧 English')}`\n"
            f"📝 Subtitles : `{'ON ✅' if s.get('subtitles') else 'OFF ❌'}`\n\n"
            "_Enjoy your cinematic manga experience\\!_ 🍿"
        )

        await bot.send_video(
            chat_id=cid,
            video=open(output_video, "rb"),
            caption=caption,
            parse_mode=ParseMode.MARKDOWN_V2,
            supports_streaming=True,
            width=1920 if s.get("quality") == "4k" else 1280,
            height=1080 if s.get("quality") == "4k" else 720,
        )

        try:
            await bot.delete_message(cid, progress.message_id)
        except Exception:
            pass

        Path(output_video).unlink(missing_ok=True)

    except ImportError as e:
        await bot.edit_message_text(
            f"⚠️ *Missing dependency:* `{e}`\n\nInstall: `pip install {str(e).split()[-1]}`",
            chat_id=cid, message_id=progress.message_id,
            parse_mode=ParseMode.MARKDOWN_V2
        )
    except Exception as e:
        logger.exception("Pipeline failed")
        await bot.edit_message_text(
            f"❌ *Processing failed:*\n`{str(e)[:300]}`",
            chat_id=cid, message_id=progress.message_id,
            parse_mode=ParseMode.MARKDOWN_V2
        )
    finally:
        ctx.user_data["processing"] = False

# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start",    "Main menu"),
        BotCommand("settings", "Settings panel"),
        BotCommand("help",     "How to use"),
        BotCommand("stats",    "Your stats"),
        BotCommand("cancel",   "Cancel processing"),
        BotCommand("testkey",  "Test your Anthropic API key"),
    ])

def main():
    init_db()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("stats",    cmd_stats))
    app.add_handler(CommandHandler("cancel",   cmd_cancel))
    app.add_handler(CommandHandler("testkey",  cmd_testkey))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.Document.ALL,
        handle_file
    ))
    print("🎌 MangaVoice Ultra Bot v2.0 — Running!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
