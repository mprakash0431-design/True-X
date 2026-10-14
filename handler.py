
import asyncio
import ipaddress
import logging
import os
import shutil
import socket
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, unquote

import aiohttp
import py7zr
import rarfile
from pyrogram import filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config

LOGGER = logging.getLogger(__name__)

ACTIVE_TASKS = {}
TASK_STATUS = {}
USER_SETTINGS = {}

TIMEOUT = aiohttp.ClientTimeout(
    total=None, connect=30, sock_read=120
)


# ---------------- HELPERS ----------------

def format_size(size):
    size = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.2f} {unit}"
        size /= 1024


def safe_name(name):
    name = os.path.basename(unquote(name)).strip()
    return name or "download.bin"


async def delete_message(message):
    try:
        await message.delete()
    except Exception:
        pass


def get_settings(user_id):
    if user_id not in USER_SETTINGS:
        USER_SETTINGS[user_id] = {
            "upload_type": "Document",
            "thumbnail": "",
            "split_size": 0,
            "equal_splits": False,
            "media_group": False,
            "caption": "",
            "prefix": "",
            "suffix": "",
            "rename": "",
            "metadata": False,
            "audio_track": "",
            "subtitle_track": "",
        }
    return USER_SETTINGS[user_id]


# ---------------- SETTINGS MENUS ----------------

def home_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "Universal Settings", callback_data="settings:universal"
        )],
        [InlineKeyboardButton(
            "Leech Settings", callback_data="settings:leech"
        )],
        [InlineKeyboardButton(
            "Reset Setting", callback_data="settings:reset"
        )],
        [InlineKeyboardButton(
            "Close", callback_data="settings:close"
        )],
    ])


def universal_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "Language: English", callback_data="settings:language"
        )],
        [
            InlineKeyboardButton("Back", callback_data="settings:home"),
            InlineKeyboardButton("Close", callback_data="settings:close"),
        ],
    ])


def leech_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "Send as Media / Document", callback_data="settings:upload"
        )],
        [InlineKeyboardButton(
            "Thumbnail", callback_data="settings:thumbnail"
        )],
        [
            InlineKeyboardButton("Leech Splits", callback_data="settings:splits"),
            InlineKeyboardButton("Leech Caption", callback_data="settings:caption"),
        ],
        [
            InlineKeyboardButton("Leech Prefix", callback_data="settings:prefix"),
            InlineKeyboardButton("Leech Suffix", callback_data="settings:suffix"),
        ],
        [
            InlineKeyboardButton("Leech Rename", callback_data="settings:rename"),
            InlineKeyboardButton("Leech Metadata", callback_data="settings:metadata"),
        ],
        [
            InlineKeyboardButton("Audio Track", callback_data="settings:audio"),
            InlineKeyboardButton("Subtitle Track", callback_data="settings:subtitle"),
        ],
        [
            InlineKeyboardButton("Back", callback_data="settings:home"),
            InlineKeyboardButton("Close", callback_data="settings:close"),
        ],
    ])


def leech_settings_text(user_id):
    s = get_settings(user_id)
    return (
        "⚙️ **Leech Settings**\n\n"
        f"Leech Type: `{s['upload_type']}`\n"
        f"Custom Thumbnail: `{'Set' if s['thumbnail'] else 'Not Set'}`\n"
        f"Leech Split Size: `{s['split_size'] or 'Default'}`\n"
        f"Equal Splits: `{'Enabled' if s['equal_splits'] else 'Disabled'}`\n"
        f"Media Group: `{'Enabled' if s['media_group'] else 'Disabled'}`\n"
        f"Leech Caption: `{s['caption'] or 'Not Set'}`\n"
        f"Leech Prefix: `{s['prefix'] or 'Not Set'}`\n"
        f"Leech Suffix: `{s['suffix'] or 'Not Set'}`\n"
        f"Leech Rename: `{s['rename'] or 'Not Set'}`\n"
        f"Leech Metadata: `{'Enabled' if s['metadata'] else 'Disabled'}`\n"
        f"Audio Track: `{s['audio_track'] or 'Default'}`\n"
        f"Subtitle Track: `{s['subtitle_track'] or 'Default'}`"
    )


# ---------------- DOWNLOAD AND EXTRACT ----------------

async def validate_public_host(hostname, port):
    try:
        results = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise ValueError("Could not resolve download hostname.") from exc

    if not results:
        raise ValueError("Hostname has no valid address.")

    for result in results:
        address = ipaddress.ip_address(result[4][0])
        if not address.is_global:
            raise ValueError("Private/local network URLs are not allowed.")


async def download_file(url, destination, status):
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Send a valid HTTP/HTTPS URL.")

    await validate_public_host(
        parsed.hostname,
        parsed.port or (443 if parsed.scheme == "https" else 80),
    )

    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.get(
            url,
            allow_redirects=True,
            headers={"User-Agent": "MiniLeechBot/1.0"},
        ) as response:
            response.raise_for_status()

            final_url = urlparse(str(response.url))
            if final_url.scheme not in ("http", "https"):
                raise ValueError("Unsupported redirect URL.")

            await validate_public_host(
                final_url.hostname,
                final_url.port or (
                    443 if final_url.scheme == "https" else 80
                ),
            )

            total = int(response.headers.get("Content-Length", 0))
            limit_mb = int(getattr(config, "DIRECT_LIMIT", 0) or 0)
            limit_bytes = limit_mb * 1024 * 1024

            if limit_bytes and total > limit_bytes:
                raise ValueError(
                    f"File exceeds configured limit: {limit_mb} MB."
                )

            downloaded = 0

            with open(destination, "wb") as output:
                async for chunk in response.content.iter_chunked(262144):
                    downloaded += len(chunk)

                    if limit_bytes and downloaded > limit_bytes:
                        raise ValueError("Download limit exceeded.")

                    output.write(chunk)
                    status["downloaded"] = downloaded
                    status["total"] = total

    if downloaded == 0:
        raise ValueError("Downloaded file is empty.")

    return downloaded


def extract_archive(archive, output_dir):
    archive = Path(archive)
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)

    def check_path(name):
        target = (root / name).resolve()
        if target != root and root not in target.parents:
            raise ValueError("Unsafe archive path detected.")

    suffix = archive.suffix.lower()

    if suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            for item in zf.infolist():
                check_path(item.filename)
            zf.extractall(root)

    elif suffix == ".rar":
        with rarfile.RarFile(archive) as rf:
            for item in rf.infolist():
                check_path(item.filename)
            rf.extractall(path=root)

    elif suffix == ".7z":
        with py7zr.SevenZipFile(archive, mode="r") as zf:
            for name in zf.getnames():
                check_path(name)
            zf.extractall(path=root)

    else:
        raise ValueError("Supported archives: ZIP, RAR and 7Z.")


async def upload_file(client, user_id, path, caption=""):
    path = Path(path)
    if not path.is_file():
        return

    await client.send_document(
        chat_id=user_id,
        document=str(path),
        caption=caption[:1024],
    )


async def run_leech(client, message, url, extract=False):
    user = message.from_user
    if not user:
        return

    user_id = user.id
    workdir = tempfile.mkdtemp(prefix="mini_leech_")
    status = {
        "state": "Downloading",
        "downloaded": 0,
        "total": 0,
        "started_at": datetime.now(timezone.utc),
    }
    TASK_STATUS[user_id] = status

    try:
        filename = safe_name(urlparse(url).path.split("/")[-1])
        file_path = os.path.join(workdir, filename)

        await message.reply_text("⬇️ Download started...")
        size = await download_file(url, file_path, status)

        if extract:
            status["state"] = "Extracting"
            extract_dir = os.path.join(workdir, "extracted")
            await asyncio.to_thread(
                extract_archive, file_path, extract_dir
            )

            files = [
                item for item in Path(extract_dir).rglob("*")
                if item.is_file()
            ]

            if not files:
                raise ValueError("No files found in archive.")

            status["state"] = "Uploading"

            for index, item in enumerate(files, start=1):
                status["state"] = f"Uploading {index}/{len(files)}"
                await upload_file(
                    client, user_id, item,
                    f"{item.name}\nExtracted by Mini Leech Bot",
                )
        else:
            status["state"] = "Uploading"
            await upload_file(
                client, user_id, file_path,
                f"{filename}\nSize: {format_size(size)}",
            )

        status["state"] = "Completed"
        status["finished_at"] = datetime.now(timezone.utc)
        await message.reply_text("✅ Completed! Check your bot DM.")

    except asyncio.CancelledError:
        status["state"] = "Cancelled"
        status["finished_at"] = datetime.now(timezone.utc)
        raise

    except Exception as exc:
        status["state"] = "Failed"
        status["error"] = str(exc)[:300]
        status["finished_at"] = datetime.now(timezone.utc)
        LOGGER.exception("Leech failed for user %s", user_id)

        try:
            await message.reply_text(f"❌ Error: {str(exc)[:700]}")
        except Exception:
            pass

    finally:
        ACTIVE_TASKS.pop(user_id, None)
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------- HANDLER REGISTRATION ----------------

def register_handlers(app):

    @app.on_message(filters.command("start"))
    async def start_command(client, message):
        await message.reply_text(
            "👋 **Mini Advanced Leech Bot**\n\n"
            "/start - Welcome\n"
            "/leech - Download and upload a direct link\n"
            "/usetting - Open settings\n"
            "/status - Task status\n"
            "/cancel - Cancel your task\n"
            "/cancelall - Cancel all tasks (owner only)\n"
            "/restart - Restart bot (owner only)\n"
            "/help - Show commands\n"
            "/ping - Check response time",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "⚙️ User Settings", callback_data="settings:home"
                )]
            ]),
        )

    @app.on_message(filters.command("help"))
    async def help_command(client, message):
        await message.reply_text(
            "**Commands**\n\n"
            "/start - Welcome\n"
            "/leech - Direct link leech\n"
            "/usetting - User settings\n"
            "/status - Active task status\n"
            "/cancel - Cancel task\n"
            "/cancelall - Cancel all tasks (owner only)\n"
            "/restart - Restart bot (owner only)\n"
            "/help - Show commands\n"
            "/ping - Check response time"
        )

    @app.on_message(filters.command("usetting"))
    async def settings_command(client, message):
        await delete_message(message)
        await client.send_message(
            message.chat.id,
            "⚙️ **User Settings**\n\nChoose a settings menu:",
            reply_markup=home_keyboard(),
        )

    @app.on_callback_query(filters.regex(r"^settings:"))
    async def settings_callback(client, query):
        if not query.message:
            await query.answer()
            return

        action = query.data.split(":", 1)[1]
        user_id = query.from_user.id

        if action == "close":
            await query.message.delete()
            await query.answer("Settings closed.")
            return

        if action == "home":
            await query.message.edit_text(
                "⚙️ **User Settings**\n\nChoose a settings menu:",
                reply_markup=home_keyboard(),
            )

        elif action == "universal":
            await query.message.edit_text(
                "🌐 **Universal Settings**\n\nLanguage: English",
                reply_markup=universal_keyboard(),
            )

        elif action == "language":
            await query.answer(
                "English is the only configured language.",
                show_alert=True,
            )
            return

        elif action == "leech":
            await query.message.edit_text(
                leech_settings_text(user_id),
                reply_markup=leech_keyboard(),
            )

        elif action == "reset":
            await query.message.edit_text(
                "Do you want to reset your settings?",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "Yes", callback_data="settings:reset_yes"
                        ),
                        InlineKeyboardButton(
                            "No", callback_data="settings:home"
                        ),
                    ],
                    [InlineKeyboardButton(
                        "Close", callback_data="settings:close"
                    )],
                ]),
            )

        elif action == "reset_yes":
            USER_SETTINGS.pop(user_id, None)
            await query.message.edit_text(
                "✅ Settings reset.",
                reply_markup=home_keyboard(),
            )

        elif action == "upload":
            settings = get_settings(user_id)
            settings["upload_type"] = (
                "Media" if settings["upload_type"] == "Document"
                else "Document"
            )
            await query.message.edit_text(
                leech_settings_text(user_id),
                reply_markup=leech_keyboard(),
            )

        elif action == "metadata":
            settings = get_settings(user_id)
            settings["metadata"] = not settings["metadata"]
            await query.message.edit_text(
                leech_settings_text(user_id),
                reply_markup=leech_keyboard(),
            )

        elif action in (
            "thumbnail", "splits", "caption", "prefix",
            "suffix", "rename", "audio", "subtitle"
        ):
            await query.answer(
                "This setting needs an input handler.",
                show_alert=True,
            )
            return

        await query.answer()

    @app.on_message(filters.command("leech"))
    async def leech_command(client, message):
        user = message.from_user
        if not user:
            return

        parts = (message.text or "").split()

        if len(parts) < 2:
            await message.reply_text(
                "Usage:\n"
                "/leech https://example.com/file.zip\n"
                "/leech https://example.com/file.zip -e"
            )
            return

        if user.id in ACTIVE_TASKS:
            await message.reply_text("⚠️ You already have an active task.")
            return

        task = asyncio.create_task(
            run_leech(
                client,
                message,
                parts[1],
                extract="-e" in parts[2:],
            )
        )
        ACTIVE_TASKS[user.id] = task

    @app.on_message(filters.command("status"))
    async def status_command(client, message):
        user = message.from_user
        if not user:
            return

        status = TASK_STATUS.get(user.id)
        if not status:
            await message.reply_text("No task status available.")
            return

        total = status.get("total", 0)
        await message.reply_text(
            f"📊 **Task Status**\n\n"
            f"State: `{status['state']}`\n"
            f"Downloaded: `{format_size(status.get('downloaded', 0))}`\n"
            f"Total: `{format_size(total) if total else 'Unknown'}`"
        )

    @app.on_message(filters.command("cancel"))
    async def cancel_command(client, message):
        user = message.from_user
        if not user:
            return

        task = ACTIVE_TASKS.get(user.id)
        if task and not task.done():
            task.cancel()
            await message.reply_text("🛑 Cancellation requested.")
        else:
            await message.reply_text("No active task.")

    @app.on_message(filters.command("cancelall"))
    async def cancelall_command(client, message):
        user = message.from_user
        if not user or user.id != config.OWNER_ID:
            await message.reply_text("⛔ Owner only.")
            return

        tasks = [
            task for task in ACTIVE_TASKS.values()
            if not task.done()
        ]

        for task in tasks:
            task.cancel()

        await message.reply_text(
            f"🛑 Cancellation requested for {len(tasks)} task(s)."
        )

    @app.on_message(filters.command("restart"))
    async def restart_command(client, message):
        user = message.from_user
        if not user or user.id != config.OWNER_ID:
            await message.reply_text("⛔ Owner only.")
            return

        await message.reply_text(
            "Restart must be handled by your deployment supervisor."
        )

    @app.on_message(filters.command("ping"))
    async def ping_command(client, message):
        started = time.monotonic()
        reply = await message.reply_text("🏓 Pong!")
        elapsed = (time.monotonic() - started) * 1000
        await reply.edit_text(f"🏓 **Pong!** `{elapsed:.0f} ms`")

    LOGGER.info("Mini Leech Bot handlers registered.")
