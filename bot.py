
import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
MAX_FILE_SIZE = 48 * 1024 * 1024
MAX_DURATION = 60 * 60
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(2)


def is_youtube_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
        allowed = {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "music.youtube.com",
            "youtu.be",
            "www.youtu.be",
        }
        return (
            parsed.scheme in ("http", "https")
            and host in allowed
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


def clean_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)
    return name[:100].strip(" .") or "audio"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 YouTube Audio Bot\n\n"
        "Send a YouTube video link to get its audio as MP3.\n\n"
        "Use /help for instructions."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "How to use:\n"
        "1. Copy a YouTube video URL.\n"
        "2. Send it here.\n"
        "3. Wait for the audio file.\n\n"
        "Only download content you own or are authorized "
        "to download. Maximum duration: 60 minutes."
    )


def download_audio(url: str, folder: str) -> tuple[str, str, int]:
    output_template = str(Path(folder) / "audio.%(ext)s")

    options = {
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 25,
        "retries": 2,
        "extractor_retries": 2,
        "max_filesize": MAX_FILE_SIZE,
        "match_filter": yt_dlp.utils.match_filter_func(
            "duration > 3600"
        ),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)

    title = clean_filename(info.get("title", "YouTube Audio"))
    duration = int(info.get("duration") or 0)

    if duration > MAX_DURATION:
        raise ValueError("This video is longer than 60 minutes.")

    files = list(Path(folder).glob("audio.*"))
    files = [p for p in files if p.suffix.lower() == ".mp3"]

    if not files:
        raise RuntimeError("The MP3 file was not created.")

    audio_path = str(files[0])

    if os.path.getsize(audio_path) > MAX_FILE_SIZE:
        raise ValueError(
            "Audio exceeds the bot's upload size limit."
        )

    return audio_path, title, duration


async def handle_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    if not message or not message.text:
        return

    urls = re.findall(r"https?://\S+", message.text)
    url = next(
        (u.rstrip(".,!?)>]") for u in urls if is_youtube_url(
            u.rstrip(".,!?)>]")
        )),
        None,
    )

    if not url:
        await message.reply_text(
            "Please send a valid YouTube video link."
        )
        return

    status = await message.reply_text(
        "⏳ Processing your link..."
    )

    async with DOWNLOAD_SEMAPHORE:
        try:
            with tempfile.TemporaryDirectory(
                prefix="youtube_audio_"
            ) as folder:
                await status.edit_text(
                    "🎧 Downloading and converting audio..."
                )

                audio_path, title, duration = await asyncio.to_thread(
                    download_audio, url, folder
                )

                await message.reply_chat_action(
                    ChatAction.UPLOAD_DOCUMENT
                )

                with open(audio_path, "rb") as audio:
                    await message.reply_audio(
                        audio=audio,
                        title=title[:64],
                        caption=f"🎵 {title}",
                        filename=f"{title}.mp3",
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=30,
                    )

                await status.delete()

        except yt_dlp.utils.DownloadError:
            logger.exception("YouTube download failed")
            await status.edit_text(
                "❌ Unable to retrieve this audio. "
                "The video may be unavailable, restricted, "
                "or not downloadable."
            )
        except Exception:
            logger.exception("Audio processing failed")
            await status.edit_text(
                "❌ Audio processing failed. Please try "
                "another permitted video or try again later."
            )


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "Set the BOT_TOKEN environment variable."
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link)
    )

    logger.info("YouTube Audio Bot is starting")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
