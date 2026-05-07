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
from ehentai_client import (
    build_gallery_zip,
    extract_gallery_url,
    gallery_parts_from_url,
    gallery_url_from_parts,
    get_gallery_title,
    search_galleries,
)
from gdrive_uploader import upload_chapter_zip

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_USER_IDS = {int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()}
MAX_TELEGRAM_FILE_MB = int(os.getenv("MAX_TELEGRAM_FILE_MB", "45") or "45")
MAX_TELEGRAM_FILE_BYTES = MAX_TELEGRAM_FILE_MB * 1024 * 1024
DEFAULT_SEARCH_LIMIT = int(os.getenv("DEFAULT_SEARCH_LIMIT", "10") or "10")
MAX_CHAPTERS_PER_ALL = int(os.getenv("MAX_CHAPTERS_PER_ALL", "10") or "10")
CHAPTERS_PER_PAGE = int(os.getenv("CHAPTERS_PER_PAGE", "20") or "20")
H20_BASE_URL = os.getenv("H20_BASE_URL", "https://hentai20.io").rstrip("/")

CHAPTER_RE = re.compile(r"([a-zA-Z0-9][a-zA-Z0-9_-]*-chapter-[a-zA-Z0-9._-]+)/?")
SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_./-]{0,180}$")
SOURCE_ALIASES = {"hentai20": "hentai20", "h20": "hentai20", "ehentai": "ehentai", "eh": "ehentai", "both": "both", "all": "both"}
BLOCKED_TERMS = {
    "underage", "minor", "child", "children", "kid", "kids", "loli", "shota",
    "junior high", "middle school", "elementary", "schoolgirl", "schoolboy",
    "15 years old", "14 years old", "13 years old", "12 years old", "11 years old", "10 years old",
}


def allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user) and (not ALLOWED_USER_IDS or user.id in ALLOWED_USER_IDS)


def blocked_text(*values: str) -> bool:
    text = "\n".join(v or "" for v in values).lower()
    return any(term in text for term in BLOCKED_TERMS)


def valid_slug(value: str) -> bool:
    return bool(value and SLUG_RE.fullmatch(value)) and ".." not in value


def parse_search_args(args: List[str]) -> tuple[str, str]:
    args = [a.strip() for a in args if a.strip()]
    if not args:
        return "", "hentai20"
    maybe_source = args[-1].lower()
    if maybe_source in SOURCE_ALIASES:
        return " ".join(args[:-1]).strip(), SOURCE_ALIASES[maybe_source]
    return " ".join(args).strip(), "hentai20"


def extract_h20_target(text: str) -> tuple[Optional[str], Optional[str]]:
    text = (text or "").strip()
    if not text:
        return None, None
    parsed = urlparse(text)
    if parsed.scheme in {"http", "https"}:
        host = (parsed.hostname or "").lower()
        if "hentai20" not in host:
            return None, None
        path = parsed.path.strip("/")
        path = path.removeprefix("hentai/read/").removeprefix("hentai/download/").removeprefix("hentai/").removeprefix("manga/")
        match = CHAPTER_RE.search(path)
        if match:
            return "chapter", match.group(1) + "/"
        slug = path.split("/", 1)[0].strip("/")
        if valid_slug(slug):
            return "manga", slug
        return None, None
    match = CHAPTER_RE.search(text)
    if match:
        return "chapter", match.group(1) + "/"
    if valid_slug(text):
        return "manga", text.strip("/")
    return None, None


def manga_slug_from_chapter_id(chapter_id: str) -> str:
    return re.sub(r"-chapter-[a-zA-Z0-9._-]+$", "", chapter_id.strip("/"))


async def search_h20(query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> List[Dict[str, Any]]:
    try:
        response = requests.get(
            f"{H20_BASE_URL}/",
            params={"s": query},
            headers={"User-Agent": "Mozilla/5.0", "Referer": H20_BASE_URL + "/"},
            timeout=(5, 20),
        )
        response.raise_for_status()
    except Exception:
        return []
    soup = BeautifulSoup(response.text, "html.parser")
    results: List[Dict[str, Any]] = []
    for item in soup.select(".listupd .bsx > a")[:limit]:
        latest = item.select_one(".epxs")
        score = item.select_one(".numscore")
        slug = (item.get("href") or "").rstrip("/").split("/")[-1]
        title = item.get("title") or slug
        if slug and not blocked_text(title, slug):
            results.append({
                "title": title,
                "slug": slug,
                "latest_chapter": latest.get_text(strip=True) if latest else "",
                "score": score.get_text(strip=True) if score else "",
            })
    return results


async def get_safe_h20_manga(slug: str) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not valid_slug(slug):
        return None, "Invalid manga slug."
    data = await get_manga(slug)
    if data == CRASH or type(data) is int:
        return None, "Could not fetch manga details."
    info = data.get("manga", {})
    if blocked_text(info.get("title", ""), info.get("description", ""), slug):
        return None, "Blocked: this title/description appears to involve minors or unsafe terms."
    return info, None


async def build_h20_zip(chapter_id: str) -> tuple[Optional[str], Optional[bytes], str]:
    slug = manga_slug_from_chapter_id(chapter_id)
    info, err = await get_safe_h20_manga(slug)
    if err or not info:
        return None, None, err or "Could not verify this chapter."
    result = await build_chapter_zip(chapter_id)
    if result == CRASH or type(result) is int:
        return None, None, "Could not build chapter ZIP."
    filename, archive_bytes = result
    return filename, archive_bytes, info.get("title", slug)


def h20_search_keyboard(results: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = []
    for item in results:
        title = item.get("title") or item.get("slug")
        latest = item.get("latest_chapter", "")
        label = f"H20: {title[:42]} - {latest}" if latest else f"H20: {title[:55]}"
        rows.append([InlineKeyboardButton(label[:60], callback_data=f"h20m:{item.get('slug')}")])
    return InlineKeyboardMarkup(rows)


def h20_manga_keyboard(slug: str, chapters: List[Dict[str, str]], page: int = 0) -> InlineKeyboardMarkup:
    total = len(chapters)
    per_page = max(1, CHAPTERS_PER_PAGE)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    rows = []
    for chapter in chapters[page * per_page:(page + 1) * per_page]:
        cid = chapter.get("chapter_id", "")
        name = chapter.get("name", cid)
        rows.append([InlineKeyboardButton(name[:60], callback_data=f"h20c:{cid}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("Previous", callback_data=f"h20p:{slug}:{page-1}"))
    nav.append(InlineKeyboardButton(f"Page {page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next", callback_data=f"h20p:{slug}:{page+1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("Download first chapters", callback_data=f"h20all:{slug}")])
    return InlineKeyboardMarkup(rows)


def h20_destination_keyboard(chapter_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Send ZIP in Telegram", callback_data=f"h20tg:{chapter_id}")],
        [InlineKeyboardButton("Upload ZIP to Google Drive", callback_data=f"h20gd:{chapter_id}")],
    ])


def eh_callback(prefix: str, gallery_url: str) -> str:
    parts = gallery_parts_from_url(gallery_url)
    if not parts:
        return "noop"
    gallery_id, token, host = parts
    return f"{prefix}:{gallery_id}:{token}:{host}"


def eh_url_from_callback(data: str) -> Optional[str]:
    try:
        _, gallery_id, token, host = data.split(":", 3)
    except ValueError:
        return None
    return gallery_url_from_parts(gallery_id, token, host)


def eh_destination_keyboard(gallery_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Send ZIP in Telegram", callback_data=eh_callback("ehtg", gallery_url))],
        [InlineKeyboardButton("Upload ZIP to Google Drive", callback_data=eh_callback("ehgd", gallery_url))],
    ])


def eh_search_keyboard(results: List[Any]) -> InlineKeyboardMarkup:
    rows = []
    for item in results:
        if blocked_text(item.title, item.category, item.url):
            continue
        rows.append([InlineKeyboardButton(("EH: " + item.title)[:60], callback_data=eh_callback("eh", item.url))])
    return InlineKeyboardMarkup(rows)


def mixed_keyboard(h20_results: List[Dict[str, Any]], eh_results: List[Any]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup((h20_search_keyboard(h20_results).inline_keyboard + eh_search_keyboard(eh_results).inline_keyboard)[:20])


async def show_h20_manga(update: Update, slug: str, page: int = 0, edit: bool = False) -> None:
    target = update.callback_query.message if update.callback_query else update.effective_message
    info, err = await get_safe_h20_manga(slug)
    if err or not info:
        await target.reply_text(err or "Could not fetch manga details.")
        return
    chapters = info.get("chapters", [])
    total_pages = max(1, (len(chapters) + max(1, CHAPTERS_PER_PAGE) - 1) // max(1, CHAPTERS_PER_PAGE))
    text = "\n".join([
        "Source: hentai20",
        f"Title: {info.get('title', slug)}",
        f"Score: {info.get('score', '-')}",
        f"Chapters: {len(chapters)}",
        f"Page: {min(page + 1, total_pages)}/{total_pages}",
        "",
        "Select a chapter:",
    ])
    markup = h20_manga_keyboard(slug, chapters, page)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await target.reply_text(text, reply_markup=markup)


async def ask_h20_destination(update: Update, chapter_id: str) -> None:
    slug = manga_slug_from_chapter_id(chapter_id)
    info, err = await get_safe_h20_manga(slug)
    if err or not info:
        await update.effective_message.reply_text(err or "Could not verify this chapter.")
        return
    await update.effective_message.reply_text(
        f"Hentai20 chapter selected:\n{info.get('title', slug)}\n{chapter_id}\n\nChoose output:",
        reply_markup=h20_destination_keyboard(chapter_id),
    )


async def ask_eh_destination(update: Update, gallery_url: str) -> None:
    try:
        title = get_gallery_title(gallery_url)
    except Exception as exc:
        await update.effective_message.reply_text(f"Could not fetch EHentai gallery: {type(exc).__name__}: {exc}")
        return
    if blocked_text(title, gallery_url):
        await update.effective_message.reply_text("Blocked: this gallery appears to involve minors or unsafe terms.")
        return
    await update.effective_message.reply_text(
        f"EHentai gallery selected:\n{title}\n{gallery_url}\n\nChoose output:",
        reply_markup=eh_destination_keyboard(gallery_url),
    )


async def send_bytes_telegram(update: Update, filename: str, data: bytes, caption: str, status) -> None:
    if len(data) > MAX_TELEGRAM_FILE_BYTES:
        await status.edit_text(f"ZIP is too large for Telegram: {len(data) / 1024 / 1024:.1f} MB. Use Google Drive instead.")
        return
    bio = io.BytesIO(data)
    bio.name = filename
    await update.effective_message.reply_document(document=bio, filename=filename, caption=caption)
    await status.delete()


async def send_h20_tg(update: Update, chapter_id: str) -> None:
    status = await update.effective_message.reply_text(f"Building Hentai20 ZIP for {chapter_id} ...")
    filename, data, title = await build_h20_zip(chapter_id)
    if not filename or not data:
        await status.edit_text(title)
        return
    await send_bytes_telegram(update, filename, data, f"{title}\n{chapter_id}", status)


async def send_h20_gd(update: Update, chapter_id: str) -> None:
    status = await update.effective_message.reply_text(f"Building Hentai20 ZIP and uploading to Google Drive for {chapter_id} ...")
    filename, data, title = await build_h20_zip(chapter_id)
    if not filename or not data:
        await status.edit_text(title)
        return
    try:
        link = upload_chapter_zip(data, filename, title)
    except Exception as exc:
        await status.edit_text(f"Google Drive upload failed: {type(exc).__name__}: {exc}")
        return
    await status.edit_text(f"Uploaded to Google Drive:\n{link}")


async def send_eh_tg(update: Update, gallery_url: str) -> None:
    status = await update.effective_message.reply_text("Building EHentai gallery ZIP ...")
    try:
        filename, data, title = build_gallery_zip(gallery_url)
    except Exception as exc:
        await status.edit_text(f"Could not build EHentai ZIP: {type(exc).__name__}: {exc}")
        return
    if blocked_text(title, filename, gallery_url):
        await status.edit_text("Blocked: this gallery appears to involve minors or unsafe terms.")
        return
    await send_bytes_telegram(update, filename, data, f"{title}\n{gallery_url}", status)


async def send_eh_gd(update: Update, gallery_url: str) -> None:
    status = await update.effective_message.reply_text("Building EHentai gallery ZIP and uploading to Google Drive ...")
    try:
        filename, data, title = build_gallery_zip(gallery_url)
        if blocked_text(title, filename, gallery_url):
            await status.edit_text("Blocked: this gallery appears to involve minors or unsafe terms.")
            return
        link = upload_chapter_zip(data, filename, title)
    except Exception as exc:
        await status.edit_text(f"EHentai Google Drive upload failed: {type(exc).__name__}: {exc}")
        return
    await status.edit_text(f"Uploaded to Google Drive:\n{link}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await update.message.reply_text("Access denied.")
        return
    await update.message.reply_text(
        "Commands:\n"
        "/search name hentai20\n"
        "/search name ehentai\n"
        "/search name both\n"
        "/filter page=1 sort=latest type=manhwa status=completed genre=...\n"
        "/manga manga-slug\n"
        "/chapter chapter-id\n"
        "/all manga-slug\n\n"
        "Direct links from hentai20 and EHentai are detected automatically."
    )


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await update.message.reply_text("Access denied.")
        return
    query, source = parse_search_args(context.args)
    if not query:
        await update.message.reply_text("Usage: /search manga name hentai20|ehentai|both")
        return
    h20_results: List[Dict[str, Any]] = []
    eh_results: List[Any] = []
    if source in {"hentai20", "both"}:
        h20_results = await search_h20(query)
    if source in {"ehentai", "both"}:
        try:
            eh_results = search_galleries(query, DEFAULT_SEARCH_LIMIT)
        except Exception as exc:
            await update.message.reply_text(f"EHentai search failed: {type(exc).__name__}: {exc}")
            if source == "ehentai":
                return
    if not h20_results and not eh_results:
        await update.message.reply_text("No results found.")
        return
    lines = [f"Search results: {source}"]
    for i, item in enumerate(h20_results, 1):
        lines.append(f"H20 {i}. {item.get('title')} | {item.get('latest_chapter')} | slug: {item.get('slug')}")
    for i, item in enumerate(eh_results, 1):
        if not blocked_text(item.title, item.category, item.url):
            lines.append(f"EH {i}. {item.title} | {item.category}")
    markup = h20_search_keyboard(h20_results) if source == "hentai20" else eh_search_keyboard(eh_results) if source == "ehentai" else mixed_keyboard(h20_results, eh_results)
    await update.message.reply_text("\n".join(lines[:25]), reply_markup=markup)


async def filter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await update.message.reply_text("Access denied.")
        return
    params = {"page": "1"}
    for arg in context.args:
        if arg.isdigit():
            params["page"] = arg
        elif "=" in arg:
            key, value = arg.split("=", 1)
            key, value = key.strip().lower(), value.strip()
            if key == "genre":
                params["genre[]"] = value
            elif key == "type":
                params["type"] = value
            elif key in {"sort", "order"}:
                params["order"] = value
            elif key == "status":
                params["status"] = value
    data = await get_filter_mangas(endpoint="/manga/", params=params)
    if data == CRASH or type(data) is int:
        await update.message.reply_text("Could not fetch filter results.")
        return
    results = [item for item in data.get("mangas", [])[:DEFAULT_SEARCH_LIMIT] if not blocked_text(item.get("title", ""), item.get("slug", ""))]
    if not results:
        await update.message.reply_text("No results found.")
        return
    await update.message.reply_text("Filter results:", reply_markup=h20_search_keyboard(results))


async def manga_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await update.effective_message.reply_text("Access denied.")
        return
    slug = " ".join(context.args).strip().strip("/")
    if not slug:
        await update.effective_message.reply_text("Usage: /manga manga-slug")
        return
    await show_h20_manga(update, slug)


async def chapter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await update.message.reply_text("Access denied.")
        return
    kind, value = extract_h20_target(" ".join(context.args))
    if kind != "chapter" or not value:
        await update.message.reply_text("Usage: /chapter 69-university-chapter-35/")
        return
    await ask_h20_destination(update, value)


async def all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, slug: Optional[str] = None) -> None:
    if not allowed(update):
        await update.effective_message.reply_text("Access denied.")
        return
    slug = slug or " ".join(context.args).strip().strip("/")
    if not slug:
        await update.effective_message.reply_text("Usage: /all manga-slug")
        return
    info, err = await get_safe_h20_manga(slug)
    if err or not info:
        await update.effective_message.reply_text(err or "Could not fetch manga details.")
        return
    chapters = info.get("chapters", [])[:MAX_CHAPTERS_PER_ALL]
    await update.effective_message.reply_text(f"Downloading {len(chapters)} Hentai20 chapters to Telegram. Limit: {MAX_CHAPTERS_PER_ALL}")
    for chapter in chapters:
        cid = chapter.get("chapter_id")
        if cid:
            await send_h20_tg(update, cid)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not allowed(update):
        await q.message.reply_text("Access denied.")
        return
    data = q.data or ""
    if data == "noop":
        return
    if data.startswith("h20m:"):
        await show_h20_manga(update, data.removeprefix("h20m:"))
    elif data.startswith("h20p:"):
        _, slug, page = data.split(":", 2)
        await show_h20_manga(update, slug, int(page), edit=True)
    elif data.startswith("h20c:"):
        await ask_h20_destination(update, data.removeprefix("h20c:"))
    elif data.startswith("h20tg:"):
        await send_h20_tg(update, data.removeprefix("h20tg:"))
    elif data.startswith("h20gd:"):
        await send_h20_gd(update, data.removeprefix("h20gd:"))
    elif data.startswith("h20all:"):
        await all_cmd(update, context, data.removeprefix("h20all:"))
    elif data.startswith("eh:"):
        gallery_url = eh_url_from_callback(data)
        if gallery_url:
            await ask_eh_destination(update, gallery_url)
    elif data.startswith("ehtg:"):
        gallery_url = eh_url_from_callback(data)
        if gallery_url:
            await send_eh_tg(update, gallery_url)
    elif data.startswith("ehgd:"):
        gallery_url = eh_url_from_callback(data)
        if gallery_url:
            await send_eh_gd(update, gallery_url)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text if update.message else ""
    eh_url = extract_gallery_url(text)
    if eh_url:
        await ask_eh_destination(update, eh_url)
        return
    kind, value = extract_h20_target(text)
    if kind == "chapter" and value:
        await ask_h20_destination(update, value)
    elif kind == "manga" and value:
        await show_h20_manga(update, value)
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
    print("Telegram bot with Hentai20, EHentai, and Google Drive support started.", flush=True)
    app.run_polling()


if __name__ == "__main__":
    main()
