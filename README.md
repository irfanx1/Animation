# 🎌 MangaVoice Ultra Bot v2.0

## ✨ What's New in v2.0

- **Zero black bars / zero blur** — manga fills the FULL frame professionally
- **5 Animation Styles**: Cinematic · Manga · Noir · Retro · Anime
- **5 Color Grades**: Vivid · Muted · Warm · Cold · Manga Ink
- **ZIP file support** — forward a zip of manga pages
- **PDF support** — full chapter PDFs extracted automatically
- **SQLite database** — settings & stats persist across bot restarts
- **Animated progress bar** in chat during processing
- **4K export** option
- **Iris wipe & diagonal transitions** per style
- **Vignette & film grain** effects for Noir/Retro styles
- **Anime glow effect** for Anime style

---

## 🛠 Setup

### 1. Get API Keys

**Telegram Bot Token**
→ Talk to @BotFather → /newbot → copy token

**Anthropic API Key** (for AI narration)
→ https://console.anthropic.com → API keys

**ElevenLabs API Key** (optional — premium voices)
→ https://elevenlabs.io (free tier: ~10k chars/month)

### 2. Edit config.json

```json
{
  "BOT_TOKEN":          "YOUR_TELEGRAM_BOT_TOKEN",
  "ANTHROPIC_API_KEY":  "sk-ant-YOUR_KEY",
  "ELEVENLABS_API_KEY": ""
}
```

### 3. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt

# FFmpeg (required for video encoding)
# Ubuntu/Debian:
sudo apt install ffmpeg

# macOS:
brew install ffmpeg

# Windows:
# Download from https://ffmpeg.org and add to PATH
```

### 4. Run

```bash
python bot.py
```

---

## 📤 How to Use

1. Open bot → `/start`
2. Configure settings via the inline buttons
3. Send manga files:
   - 📸 Photos (JPG/PNG/WEBP) — one by one
   - 📄 PDF — full manga chapter
   - 📦 ZIP — folder of manga pages
4. Tap **Generate Video**
5. Wait 1-3 minutes → receive your cinematic video!

---

## 🎬 Video Quality

| Setting | Resolution | Bitrate |
|---------|-----------|---------|
| SD      | 854×480   | 2 Mbps  |
| HD      | 1280×720  | 5 Mbps  |
| 4K      | 1920×1080 | 12 Mbps |

---

## 🎨 Animation Styles

| Style     | Effect                                    |
|-----------|-------------------------------------------|
| Cinematic | Ken Burns zoom + pan, cross-dissolve      |
| Manga     | Impact zoom + horizontal wipe transition  |
| Noir      | Vignette + radial iris wipe               |
| Retro     | Film grain + diagonal wipe + sepia        |
| Anime     | Glow overlay + soft shimmer               |

---

## 📁 File Architecture

```
manga_bot/
├── bot.py           Main bot — handlers, UI, settings
├── pipeline.py      Core engine — AI, TTS, animation, video
├── config.json      API keys (edit this)
├── requirements.txt Python dependencies
├── mangavoice.db    SQLite DB (auto-created on first run)
└── README.md        This file
```

---

## 🚀 Deploy on VPS (24/7)

```bash
# Using screen
screen -S mangabot
source venv/bin/activate
python bot.py
# Ctrl+A, D to detach

# OR using systemd
sudo nano /etc/systemd/system/mangabot.service
# [Unit]
# Description=MangaVoice Bot
# [Service]
# WorkingDirectory=/path/to/manga_bot
# ExecStart=/path/venv/bin/python bot.py
# Restart=always
# [Install]
# WantedBy=multi-user.target

sudo systemctl enable --now mangabot
```

---

## ⚠️ Troubleshooting

| Error | Fix |
|-------|-----|
| `No module named fitz` | `pip install pymupdf` |
| `No module named moviepy` | `pip install moviepy` |
| `ffmpeg not found` | `sudo apt install ffmpeg` |
| No audio in video | Install ffmpeg |
| Hindi text garbled | Install Noto fonts: `sudo apt install fonts-noto` |
| Telegram file too large | Use SD quality or fewer pages |
