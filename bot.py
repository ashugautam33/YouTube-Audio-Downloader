
import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import yt_dlp
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Conservative limits for a small free hosting server.
MAX_FILE_SIZE = 40 * 1024 * 1024
DOWNLOAD_TIMEOUT = 180

SUPPORTED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger("youtube_audio_bot")


# ============================================================
# URL VALIDATION
# ============================================================

def normalize_youtube_url(text: str) -> str | None:
    """Validate supported YouTube URL formats."""
    text = text.strip()

    if not text or len(text) > 2048:
        return None

    if not re.match(r"^https?://", text, re.IGNORECASE):
        text = "https://" + text

    try:
        parsed = urlparse(text)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower()

    if host not in SUPPORTED_HOSTS:
        return None

    if parsed.scheme not in ("http", "https"):
        return None

    video_id = ""

    if host.endswith("youtu.be"):
        video_id = parsed.path.strip("/").split("/")[0]
    elif parsed.path == "/watch":
        video_id = parse_qs(parsed.query).get("v", [""])[0]
    else:
        match = re.match(
            r"^/(?:shorts|embed|live|v)/([A-Za-z0-9_-]+)",
            parsed.path,
        )
        if match:
            video_id = match.group(1)

    if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id or ""):
        return None

    return text


# ============================================================
# DOWNLOAD FUNCTION
# ============================================================

def download_audio(url: str, folder: str) -> dict:
    """
    Download audio using yt-dlp and convert to MP3 using FFmpeg.

    This function runs in a worker thread, not the Telegram event loop.
    """
    if not shutil.which("ffmpeg"):
        raise RuntimeError(
            "FFmpeg is not installed or is not available in PATH."
        )

    output_template = str(
        Path(folder) / "%(title).70s-%(id)s.%(ext)s"
    )

    options = {
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
        "continuedl": True,
        "overwrites": True,
        "restrictfilenames": True,
        "max_filesize": MAX_FILE_SIZE,
        "cachedir": False,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }
        ],
    }

    logger.info("Starting audio download.")

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)

    if not info:
        raise RuntimeError("yt-dlp returned no video information.")

    files = list(Path(folder).glob("*.mp3"))

    if not files:
        raise RuntimeError(
            "No MP3 file was produced. Check the FFmpeg installation "
            "and the yt-dlp logs."
        )

    audio_path = max(files, key=lambda item: item.stat().st_mtime)

    if not audio_path.exists() or audio_path.stat().st_size == 0:
        raise RuntimeError("The resulting audio file is empty.")

    if audio_path.stat().st_size > MAX_FILE_SIZE:
        raise RuntimeError(
            "The converted audio exceeds the configured size limit."
        )

    return {
        "path": str(audio_path),
        "title": info.get("title") or audio_path.stem,
        "duration": info.get("duration"),
    }


# ============================================================
# FORMATTING AND ERROR HANDLING
# ============================================================

def format_duration(seconds) -> str:
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return "Unknown"

    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"

    return f"{minutes}:{seconds:02d}"


async def safe_edit(message, text: str) -> None:
    try:
        await message.edit_text(text)
    except Exception:
        logger.debug("Could not edit status message.", exc_info=True)


def classify_error(error: Exception) -> str:
    """Return a user-friendly message without hiding the server logs."""
    message = str(error).lower()

    if (
        "sign in to confirm" in message
        or "not a bot" in message
        or "confirm you're not a bot" in message
    ):
        return (
            "YouTube is requiring authentication for this video. "
            "Try another publicly accessible video or an authorized "
            "audio source. This server cannot guarantee access to "
            "restricted videos."
        )

    if "private video" in message or "video is private" in message:
        return "This video is private and cannot be accessed by the bot."

    if "members-only" in message or "join this channel" in message:
        return "This video has a membership restriction."

    if "ffmpeg" in message or "ffprobe" in message:
        return (
            "Audio conversion failed. FFmpeg may be missing or "
            "unavailable on the hosting server."
        )

    if "max-filesize" in message or "too large" in message:
        return "This audio is larger than the bot's supported size limit."

    if "timed out" in message or "timeout" in message:
        return "The download timed out. Please try again later."

    if "403" in message or "forbidden" in message:
        return (
            "The source refused the download request. "
            "Try again later or use an authorized source."
        )

    if "video unavailable" in message or "unavailable" in message:
        return "This video may be unavailable or restricted."

    return (
        "Unable to retrieve this audio. The video may be unavailable, "
        "restricted, or not downloadable. Check the server logs."
    )


# ============================================================
# COMMAND HANDLERS
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message

    if not message:
        return

    await message.reply_text(
        "🎵 YouTube Audio Downloader\n\n"
        "Send a public YouTube video link to request an MP3.\n\n"
        "/start — Start the bot\n"
        "/help — Usage instructions\n\n"
        "Download only content you own or are authorized to use."
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message

    if not message:
        return

    await message.reply_text(
        "How to use this bot:\n\n"
        "1. Copy a YouTube video URL.\n"
        "2. Send the URL to this bot.\n"
        "3. Wait for processing to finish.\n"
        "4. Receive the MP3 if the source allows access.\n\n"
        "Private or authentication-protected videos may fail."
    )


# ============================================================
# LINK HANDLER
# ============================================================

async def handle_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message

    if not message or not message.text:
        return

    url = normalize_youtube_url(message.text)

    if not url:
        await message.reply_text(
            "Please send a valid YouTube video link.\n\n"
            "Example: https://www.youtube.com/watch?v=VIDEO_ID"
        )
        return

    status = await message.reply_text("⏳ Checking the video...")

    temp_dir = tempfile.mkdtemp(prefix="yt_audio_")
    started = time.monotonic()

    try:
        await context.bot.send_chat_action(
            chat_id=message.chat_id,
            action=ChatAction.TYPING,
        )

        await safe_edit(status, "⏳ Downloading and converting audio...")

        result = await asyncio.wait_for(
            asyncio.to_thread(download_audio, url, temp_dir),
            timeout=DOWNLOAD_TIMEOUT,
        )

        audio_path = Path(result["path"])

        if not audio_path.is_file():
            raise RuntimeError("The audio file could not be found.")

        if audio_path.stat().st_size > MAX_FILE_SIZE:
            raise RuntimeError("The audio file exceeds the size limit.")

        elapsed = int(time.monotonic() - started)
        title = str(result["title"])[:64]
        duration = format_duration(result.get("duration"))

        await safe_edit(status, "📤 Uploading MP3 to Telegram...")

        caption = (
            f"🎵 {title}\n"
            f"⏱ Duration: {duration}\n"
            f"⚡ Processed in {elapsed} seconds"
        )

        with audio_path.open("rb") as audio_file:
            await message.reply_audio(
                audio=audio_file,
                title=title,
                filename="audio.mp3",
                caption=caption[:1000],
                read_timeout=60,
                write_timeout=60,
                connect_timeout=30,
                pool_timeout=30,
            )

        await safe_edit(status, "✅ Audio sent successfully!")

    except asyncio.TimeoutError:
        logger.error("Download exceeded %s seconds.", DOWNLOAD_TIMEOUT)
        await safe_edit(
            status,
            "⌛ Processing took too long. Try a shorter video.",
        )

    except yt_dlp.utils.DownloadError:
        # Keep the complete technical traceback in server logs.
        logger.exception("yt-dlp download failed")
        await safe_edit(
            status,
            "❌ " + classify_error(
                RuntimeError("yt-dlp download failed")
            ),
        )

    except Exception as error:
        logger.exception("Audio processing failed")
        await safe_edit(status, "❌ " + classify_error(error))

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ============================================================
# GLOBAL ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    logger.error(
        "Unhandled Telegram bot exception",
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing. Create a private .env file beside "
            "bot.py containing BOT_TOKEN=your_new_token."
        )

    if not shutil.which("ffmpeg"):
        logger.warning(
            "FFmpeg was not found. MP3 conversion will not work until "
            "FFmpeg is installed on the server."
        )

    logger.info("Starting YouTube Audio Downloader.")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_link,
        )
    )
    application.add_error_handler(error_handler)

    application.run_polling(
        drop_pending_updates=False,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
