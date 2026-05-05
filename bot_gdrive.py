import io
import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from app.resources.errors import CRASH
from app.routers.hentai20.hentai20 import build_chapter_zip, get_filter_mangas, get_manga
from gdrive_uploader import upload_chapter_zip

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_USER_IDS = {int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()}
MAX_TELEGRAM_FILE_MB = int(os.getenv("MAX_TELEGRAM_FILE_MB", "45") or "45")
MAX_TELEGRAM_FILE_BYTES = MAX_TELEGRAM_FILE_MB * 1024 * 1024
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
DEFAULT_PAGE = int(os.getenv("DEFAULT_PAGE", "1") or "1")
DEFAULT_SEARCH_LIMIT = int(os.getenv("DEFAULT_SEARCH_LIMIT", "10") or "10")
MAX_CHAPTERS_PER_ALL = int(os.getenv("MAX_CHAPTERS_PER_ALL", "10") or "10")
CHAPTERS_PER_PAGE = int(os.getenv("CHAPTERS_PER_PAGE", "20") or "20")
BASE_URL = "https://hentai20.io"

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


def blocked_text(*values: str) -> bool:
    text = "\n".join(v or "" for v in values).lower()
    return any(term in text for term in BLOCKED_TERMS)


def extract_chapter_id(text: str) -> Optional[str]:
    text = (text or "").strip()
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme in {"http", "https"}:
        path = parsed.path.strip("/").removeprefix("hentai/read/").removeprefix("hentai/download/")
        m = CHAPTER_RE.search(path)
        if m:
            return m.group(1) + "/"
        if valid_slug(path):
            return path + "/"
    m = CHAPTER_RE.search(text)
    if m:
        return m.group(1) + "/"
    if valid_slug(text):
        return text if text.endswith("/") else text + "/"
    return None


def manga_slug_from_chapter_id(chapter_id: str) -> str:
    return re.sub(r"-chapter-[a-zA-Z0-9._-]+$", "", chapter_id.strip("/"))


def parse_int_arg(args: List[str], default: int = 1) -> int:
    for arg in args:
        if arg.isdigit():
            return max(1, int(arg))
    return default


def normalize_filter_args(args: List[str]) -> Dict[str, str]:
    params = {"page": str(parse_int_arg(args, DEFAULT_PAGE))}
    for arg in args:
        if "=" not in arg:
            continue
        key, value = arg.split("=", 1)
        key, value = key.strip().lower(), value.strip()
        if not value:
            continue
        if key == "genre":
            params["genre[]"] = value
        elif key == "type":
            params["type"] = value
        elif key in {"sort", "order"}:
            params["order"] = value
        elif key == "status":
            params["status"] = value
    return params


async def search_mangas(query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> List[Dict[str, Any]]:
    try:
        r = requests.get(f"{BASE_URL}/", params={"s": query}, headers={"User-Agent": "Mozilla/5.0", "Referer": BASE_URL + "/"}, timeout=(5, 20))
        r.raise_for_status()
    except Exception:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    results = []
    for item in soup.select(".listupd .bsx > a")[:limit]:
        image = item.select_one("img")
        latest = item.select_one(".epxs")
        score = item.select_one(".numscore")
        slug = (item.get("href") or "").rstrip("/").split("/")[-1]
        if slug:
            results.append({"title": item.get("title") or slug, "slug": slug, "image_url": image.get("src") if image else "", "latest_chapter": latest.get_text(strip=True) if latest else "", "score": score.get_text(strip=True) if score else ""})
    return results


async def get_safe_manga(slug: str) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not valid_slug(slug):
        return None, "Invalid manga slug."
    data = await get_manga(slug)
    if data == CRASH or type(data) is int:
        return None, "Could not fetch manga details."
    info = data.get("manga", {})
    if blocked_text(info.get("title", ""), info.get("description", ""), slug):
        return None, "Blocked: this title/description appears to involve minors or unsafe terms."
    return info, None


async def safe_to_package(chapter_id: str) -> tuple[bool, str]:
    slug = manga_slug_from_chapter_id(chapter_id)
    info, err = await get_safe_manga(slug)
    if err or not info:
        return False, err or "Could not verify this chapter safely."
    return True, info.get("title", slug)


def choose_destination_keyboard(chapter_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Send in Telegram", callback_data=f"sendtg:{chapter_id}")],
        [InlineKeyboardButton("Upload to Google Drive", callback_data=f"sendgd:{chapter_id}")],
    ])


async def ask_destination(update: Update, chapter_id: str) -> None:
    ok, title = await safe_to_package(chapter_id)
    if not ok:
        await update.effective_message.reply_text(title)
        return
    await update.effective_message.reply_text(
        f"Chapter selected:\n{title}\n{chapter_id}\n\nChoose output:",
        reply_markup=choose_destination_keyboard(chapter_id),
    )


async def build_zip_for_chapter(chapter_id: str) -> tuple[Optional[str], Optional[bytes], Optional[str]]:
    ok, title = await safe_to_package(chapter_id)
    if not ok:
        return None, None, title
    result = await build_chapter_zip(chapter_id)
    if result == CRASH or type(result) is int:
        return None, None, "Could not build chapter ZIP."
    filename, archive_bytes = result
    return filename, archive_bytes, title


async def send_zip_telegram(update: Update, chapter_id: str) -> None:
    msg = update.effective_message
    status = await msg.reply_text(f"Building ZIP for {chapter_id} ...")
    filename, archive_bytes, title = await build_zip_for_chapter(chapter_id)
    if not filename or not archive_bytes:
        await status.edit_text(title or "Could not build ZIP.")
        return
    if len(archive_bytes) > MAX_TELEGRAM_FILE_BYTES:
        if PUBLIC_BASE_URL:
            await status.edit_text(f"ZIP is too large for Telegram.\nDownload:\n{PUBLIC_BASE_URL}/hentai/download/{chapter_id}")
        else:
            await status.edit_text(f"ZIP is too large: {len(archive_bytes) / 1024 / 1024:.1f} MB")
        return
    bio = io.BytesIO(archive_bytes)
    bio.name = filename
    await msg.reply_document(document=bio, filename=filename, caption=f"{title}\n{chapter_id}")
    await status.delete()


async def send_zip_drive(update: Update, chapter_id: str) -> None:
    msg = update.effective_message
    status = await msg.reply_text(f"Building ZIP and uploading to Google Drive for {chapter_id} ...")
    filename, archive_bytes, title = await build_zip_for_chapter(chapter_id)
    if not filename or not archive_bytes:
        await status.edit_text(title or "Could not build ZIP.")
        return
    try:
        link = upload_chapter_zip(archive_bytes, filename, title or manga_slug_from_chapter_id(chapter_id))
    except Exception as exc:
        await status.edit_text(f"Google Drive upload failed: {type(exc).__name__}: {exc}")
        return
    await status.edit_text(f"Uploaded to Google Drive:\n{link}")


def search_keyboard(results: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = []
    for item in results:
        title = item.get("title") or item.get("slug")
        latest = item.get("latest_chapter", "")
        text = f"{title[:42]} - {latest}" if latest else title[:60]
        rows.append([InlineKeyboardButton(text, callback_data=f"manga:{item.get('slug')}")])
    return InlineKeyboardMarkup(rows)


def manga_keyboard(slug: str, chapters: List[Dict[str, str]], page: int = 0) -> InlineKeyboardMarkup:
    page = max(0, page)
    total = len(chapters)
    per_page = max(1, CHAPTERS_PER_PAGE)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages - 1)
    start = page * per_page
    rows = []
    for chapter in chapters[start:start + per_page]:
        cid = chapter.get("chapter_id", "")
        name = chapter.get("name", cid)
        rows.append([InlineKeyboardButton(name, callback_data=f"choose:{cid}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("Previous", callback_data=f"mp:{slug}:{page-1}"))
    nav.append(InlineKeyboardButton(f"Page {page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next", callback_data=f"mp:{slug}:{page+1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("Download first chapters", callback_data=f"all:{slug}")])
    return InlineKeyboardMarkup(rows)


async def show_manga(update: Update, slug: str, page: int = 0, edit: bool = False) -> None:
    info, err = await get_safe_manga(slug)
    target = update.callback_query.message if update.callback_query else update.effective_message
    if err or not info:
        await target.reply_text(err or "Could not fetch manga details.")
        return
    chapters = info.get("chapters", [])
    per_page = max(1, CHAPTERS_PER_PAGE)
    total_pages = max(1, (len(chapters) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    text = "\n".join([f"Title: {info.get('title', slug)}", f"Score: {info.get('score', '-')}", f"Chapters: {len(chapters)}", f"Page: {page + 1}/{total_pages}", "", "Select a chapter:"])
    markup = manga_keyboard(slug, chapters, page)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await target.reply_text(text, reply_markup=markup)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await update.message.reply_text("Access denied.")
        return
    await update.message.reply_text("Commands:\n/search name\n/filter page=1 sort=latest type=manhwa status=completed genre=...\n/manga manga-slug\n/chapter chapter-id\n/all manga-slug")


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await update.message.reply_text("Access denied.")
        return
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Usage: /search manga name")
        return
    results = await search_mangas(query)
    if not results:
        await update.message.reply_text("No results found.")
        return
    lines = ["Search results:"]
    for i, item in enumerate(results, 1):
        lines.append(f"{i}. {item.get('title')} | {item.get('latest_chapter')} | slug: {item.get('slug')}")
    await update.message.reply_text("\n".join(lines), reply_markup=search_keyboard(results))


async def filter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await update.message.reply_text("Access denied.")
        return
    params = normalize_filter_args(context.args)
    data = await get_filter_mangas(endpoint="/manga/", params=params)
    if data == CRASH or type(data) is int:
        await update.message.reply_text("Could not fetch filter results.")
        return
    results = data.get("mangas", [])[:DEFAULT_SEARCH_LIMIT]
    if not results:
        await update.message.reply_text("No results found.")
        return
    lines = [f"Filter results - page {params.get('page', '1')}:"]
    for i, item in enumerate(results, 1):
        lines.append(f"{i}. {item.get('title')} | {item.get('latest_chapter')} | slug: {item.get('slug')}")
    await update.message.reply_text("\n".join(lines), reply_markup=search_keyboard(results))


async def manga_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, slug: Optional[str] = None) -> None:
    if not allowed(update):
        await update.effective_message.reply_text("Access denied.")
        return
    slug = slug or (" ".join(context.args).strip().strip("/") if context.args else "")
    if not slug:
        await update.effective_message.reply_text("Usage: /manga manga-slug")
        return
    await show_manga(update, slug, page=0)


async def chapter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await update.message.reply_text("Access denied.")
        return
    chapter_id = extract_chapter_id(" ".join(context.args))
    if not chapter_id:
        await update.message.reply_text("Usage: /chapter 69-university-chapter-35/")
        return
    await ask_destination(update, chapter_id)


async def all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, slug: Optional[str] = None) -> None:
    if not allowed(update):
        await update.effective_message.reply_text("Access denied.")
        return
    slug = slug or (" ".join(context.args).strip().strip("/") if context.args else "")
    if not slug:
        await update.effective_message.reply_text("Usage: /all manga-slug")
        return
    info, err = await get_safe_manga(slug)
    if err or not info:
        await update.effective_message.reply_text(err or "Could not fetch manga details.")
        return
    chapters = info.get("chapters", [])[:MAX_CHAPTERS_PER_ALL]
    if not chapters:
        await update.effective_message.reply_text("No chapters found.")
        return
    await update.effective_message.reply_text(f"Downloading {len(chapters)} chapters to Telegram. Limit: {MAX_CHAPTERS_PER_ALL}")
    for chapter in chapters:
        cid = chapter.get("chapter_id")
        if cid:
            await send_zip_telegram(update, cid)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not allowed(update):
        await q.message.reply_text("Access denied.")
        return
    data = q.data or ""
    if data == "noop":
        return
    if data.startswith("manga:"):
        await show_manga(update, data.removeprefix("manga:"), page=0)
    elif data.startswith("mp:"):
        _, slug, page = data.split(":", 2)
        await show_manga(update, slug, page=int(page), edit=True)
    elif data.startswith("choose:"):
        await ask_destination(update, data.removeprefix("choose:"))
    elif data.startswith("sendtg:"):
        await send_zip_telegram(update, data.removeprefix("sendtg:"))
    elif data.startswith("sendgd:"):
        await send_zip_drive(update, data.removeprefix("sendgd:"))
    elif data.startswith("all:"):
        await all_cmd(update, context, slug=data.removeprefix("all:"))


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text if update.message else ""
    cid = extract_chapter_id(text)
    if cid:
        await ask_destination(update, cid)
    else:
        context.args = text.split()
        await search_cmd(update, context)


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Please set BOT_TOKEN in .env or environment variables.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("filter", filter_cmd))
    app.add_handler(CommandHandler("manga", manga_cmd))
    app.add_handler(CommandHandler("chapter", chapter_cmd))
    app.add_handler(CommandHandler("all", all_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    print("Telegram bot with Google Drive support started.", flush=True)
    app.run_polling()


if __name__ == "__main__":
    main()
