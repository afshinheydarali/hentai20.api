import io
import os
import re
from typing import Optional
from urllib.parse import urlparse

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from app.resources.errors import CRASH
from app.routers.hentai20.hentai20 import build_chapter_zip, get_manga

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_USER_IDS = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()}
MAX_TELEGRAM_FILE_MB = int(os.getenv("MAX_TELEGRAM_FILE_MB", "45") or "45")
MAX_TELEGRAM_FILE_BYTES = MAX_TELEGRAM_FILE_MB * 1024 * 1024
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")

SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_./-]{0,180}$")
CHAPTER_RE = re.compile(r"([a-zA-Z0-9][a-zA-Z0-9_-]*-chapter-[a-zA-Z0-9._-]+)/?")
BLOCKED_TERMS = {
    "underage", "minor", "child", "children", "kid", "kids", "loli", "shota",
    "junior high", "middle school", "elementary", "schoolgirl", "schoolboy",
    "15 years old", "14 years old", "13 years old", "12 years old", "11 years old", "10 years old",
}


def allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user) and (not ALLOWED_USER_IDS or user.id in ALLOWED_USER_IDS)


def valid_slug(value: str) -> bool:
    return bool(value and SLUG_RE.fullmatch(value)) and ".." not in value


def extract_chapter_id(text: str) -> Optional[str]:
    text = (text or "").strip()
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme in {"http", "https"}:
        path = parsed.path.strip("/")
        path = path.removeprefix("hentai/read/").removeprefix("hentai/download/")
        match = CHAPTER_RE.search(path)
        if match:
            return match.group(1) + "/"
        if valid_slug(path):
            return path + "/"
    match = CHAPTER_RE.search(text)
    if match:
        return match.group(1) + "/"
    if valid_slug(text):
        return text if text.endswith("/") else text + "/"
    return None


def manga_slug_from_chapter_id(chapter_id: str) -> str:
    return re.sub(r"-chapter-[a-zA-Z0-9._-]+$", "", chapter_id.strip("/"))


def blocked_text(*values: str) -> bool:
    text = "\n".join(v or "" for v in values).lower()
    return any(term in text for term in BLOCKED_TERMS)


async def safe_to_package(chapter_id: str) -> tuple[bool, str]:
    slug = manga_slug_from_chapter_id(chapter_id)
    manga = await get_manga(slug)
    if manga == CRASH or type(manga) is int:
        return False, "Could not verify this chapter safely."
    info = manga.get("manga", {})
    title = info.get("title", slug)
    if blocked_text(title, info.get("description", ""), slug, chapter_id):
        return False, "Blocked: this title/description appears to involve minors or unsafe terms."
    return True, title


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await update.message.reply_text("Access denied.")
        return
    await update.message.reply_text(
        "Send a chapter id or URL and I will return a ZIP.\n\n"
        "Example: 69-university-chapter-35/\n"
        "Commands:\n/chapter 69-university-chapter-35/\n/manga 69-university"
    )


async def manga_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await update.message.reply_text("Access denied.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /manga manga-slug")
        return
    slug = context.args[0].strip().strip("/")
    if not valid_slug(slug):
        await update.message.reply_text("Invalid manga slug.")
        return
    data = await get_manga(slug)
    if data == CRASH or type(data) is int:
        await update.message.reply_text("Could not fetch manga details.")
        return
    info = data.get("manga", {})
    if blocked_text(info.get("title", ""), info.get("description", ""), slug):
        await update.message.reply_text("Blocked: this title/description appears to involve minors or unsafe terms.")
        return
    lines = [f"Title: {info.get('title', slug)}", "", "Chapters:"]
    for chapter in info.get("chapters", [])[:25]:
        lines.append(f"- {chapter.get('name')}: {chapter.get('chapter_id')}")
    await update.message.reply_text("\n".join(lines))


async def send_chapter(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: Optional[str] = None) -> None:
    if not allowed(update):
        await update.message.reply_text("Access denied.")
        return
    chapter_id = extract_chapter_id(raw_text or " ".join(context.args))
    if not chapter_id:
        await update.message.reply_text("Send a valid chapter id, for example: 69-university-chapter-35/")
        return
    ok, title = await safe_to_package(chapter_id)
    if not ok:
        await update.message.reply_text(title)
        return
    status = await update.message.reply_text(f"Building ZIP for {chapter_id} ...")
    result = await build_chapter_zip(chapter_id)
    if result == CRASH or type(result) is int:
        await status.edit_text("Could not build chapter ZIP.")
        return
    filename, archive_bytes = result
    if len(archive_bytes) > MAX_TELEGRAM_FILE_BYTES:
        if PUBLIC_BASE_URL:
            await status.edit_text(f"ZIP is too large for Telegram. Download:\n{PUBLIC_BASE_URL}/hentai/download/{chapter_id}")
        else:
            await status.edit_text(f"ZIP is too large: {len(archive_bytes) / 1024 / 1024:.1f} MB")
        return
    bio = io.BytesIO(archive_bytes)
    bio.name = filename
    await update.message.reply_document(document=bio, filename=filename, caption=f"{title}\n{chapter_id}")
    await status.delete()


async def chapter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_chapter(update, context)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        await send_chapter(update, context, raw_text=update.message.text)


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Please set BOT_TOKEN environment variable.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("manga", manga_cmd))
    app.add_handler(CommandHandler("chapter", chapter_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    print("Telegram bot started.", flush=True)
    app.run_polling()


if __name__ == "__main__":
    main()
