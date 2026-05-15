import io
import json
import os
import re
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build as google_build
from googleapiclient.http import MediaIoBaseUpload
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.resources.errors import CRASH
from app.routers.hentai20.hentai20 import build_chapter_zip, get_filter_mangas, get_manga
from app.routers.sarrast.sarrast import (
    build_sarrast_chapter_zip,
    build_sarrast_chapters_from_chapter_api,
    is_sarrast_chapter_url,
    is_sarrast_series_url,
    load_sarrast_chapters,
    normalize_sarrast_url,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_USER_IDS = {
    int(x.strip())
    for x in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
}

MAX_TELEGRAM_FILE_MB = int(os.getenv("MAX_TELEGRAM_FILE_MB", "45") or "45")
MAX_TELEGRAM_FILE_BYTES = MAX_TELEGRAM_FILE_MB * 1024 * 1024
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")

DEFAULT_PAGE = int(os.getenv("DEFAULT_PAGE", "1") or "1")
DEFAULT_SEARCH_LIMIT = int(os.getenv("DEFAULT_SEARCH_LIMIT", "10") or "10")
MAX_CHAPTERS_PER_ALL = int(os.getenv("MAX_CHAPTERS_PER_ALL", "10") or "10")
MAX_CHAPTER_BUTTONS = int(os.getenv("MAX_CHAPTER_BUTTONS", "80") or "80")
MAX_CALLBACK_CACHE = int(os.getenv("MAX_CALLBACK_CACHE", "500") or "500")

GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()
GOOGLE_DRIVE_CREDENTIALS_FILE = (
    os.getenv("GOOGLE_DRIVE_CREDENTIALS_FILE", "").strip()
    or os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    or os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
)
GOOGLE_DRIVE_PUBLIC = os.getenv("GOOGLE_DRIVE_PUBLIC", "1").strip().lower() not in {"0", "false", "no", "off"}

BASE_URL = "https://hentai20.io"

SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_./-]{0,180}$")
CHAPTER_RE = re.compile(r"([a-zA-Z0-9][a-zA-Z0-9_-]*-chapter-[a-zA-Z0-9._-]+)/?")

BLOCKED_TERMS = {
    "underage",
    "minor",
    "child",
    "children",
    "kid",
    "kids",
    "loli",
    "shota",
    "junior high",
    "middle school",
    "elementary",
    "schoolgirl",
    "schoolboy",
    "15 years old",
    "14 years old",
    "13 years old",
    "12 years old",
    "11 years old",
    "10 years old",
}


def allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user) and (not ALLOWED_USER_IDS or user.id in ALLOWED_USER_IDS)


def valid_slug(value: str) -> bool:
    return bool(value and SLUG_RE.fullmatch(value)) and ".." not in value


def blocked_text(*values: str) -> bool:
    text = "\n".join(v or "" for v in values).lower()
    return any(term in text for term in BLOCKED_TERMS)


def add_callback_payload(context: ContextTypes.DEFAULT_TYPE, kind: str, payload: str) -> str:
    cache = context.user_data.setdefault("callback_payloads", {})
    seq = int(context.user_data.get("callback_seq", 0)) + 1

    if len(cache) > MAX_CALLBACK_CACHE:
        cache.clear()

    token = f"{kind}{seq}"
    cache[token] = payload
    context.user_data["callback_seq"] = seq
    return token


def get_callback_payload(context: ContextTypes.DEFAULT_TYPE, token: str) -> Optional[str]:
    cache = context.user_data.get("callback_payloads", {})
    payload = cache.get(token)
    return payload if isinstance(payload, str) else None


def upload_choice_keyboard(context: ContextTypes.DEFAULT_TYPE, source: str, payload: str) -> InlineKeyboardMarkup:
    token_payload = json.dumps({"source": source, "payload": payload}, ensure_ascii=False)
    token = add_callback_payload(context, "u", token_payload)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("ارسال در تلگرام", callback_data=f"tg:{token}"),
                InlineKeyboardButton("آپلود در Google Drive", callback_data=f"gd:{token}"),
            ]
        ]
    )


async def ask_upload_destination(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    source: str,
    payload: str,
    title: str,
) -> None:
    await update.effective_message.reply_text(
        f"کجا آپلود کنم؟\n{title}",
        reply_markup=upload_choice_keyboard(context, source, payload),
    )


def extract_chapter_id(text: str) -> Optional[str]:
    text = (text or "").strip()
    if not text:
        return None

    parsed = urlparse(text)
    if parsed.scheme in {"http", "https"}:
        path = parsed.path.strip("/")
        path = path.removeprefix("hentai/read/")
        path = path.removeprefix("hentai/download/")
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
        key = key.strip().lower()
        value = value.strip()

        if not value:
            continue

        if key in {"genre", "status", "type", "sort", "order"}:
            if key == "type":
                params["type"] = value
            elif key in {"sort", "order"}:
                params["order"] = value
            elif key == "genre":
                params["genre[]"] = value
            else:
                params[key] = value

    return params


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


async def search_mangas(query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> List[Dict[str, Any]]:
    url = f"{BASE_URL}/"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": BASE_URL + "/",
    }

    try:
        response = requests.get(url, params={"s": query}, headers=headers, timeout=(5, 20))
        response.raise_for_status()
    except Exception:
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    items = soup.select(".listupd .bsx > a")
    results = []

    for item in items[:limit]:
        image = item.select_one("img")
        latest = item.select_one(".epxs")
        score = item.select_one(".numscore")
        href = item.get("href") or ""
        chunks = href.split("/")
        slug = chunks[-2] if len(chunks) >= 2 else ""

        if not slug:
            continue

        results.append(
            {
                "title": item.get("title") or "",
                "slug": slug,
                "image_url": image.get("src") if image else "",
                "latest_chapter": latest.get_text(strip=True) if latest else "",
                "score": score.get_text(strip=True) if score else "",
            }
        )

    return results


@lru_cache(maxsize=1)
def get_drive_service():
    if not GOOGLE_DRIVE_CREDENTIALS_FILE:
        raise RuntimeError(
            "Google Drive credentials are missing. Set GOOGLE_DRIVE_CREDENTIALS_FILE or GOOGLE_SERVICE_ACCOUNT_FILE."
        )

    scopes = ["https://www.googleapis.com/auth/drive.file"]
    credentials = service_account.Credentials.from_service_account_file(
        GOOGLE_DRIVE_CREDENTIALS_FILE,
        scopes=scopes,
    )
    return google_build("drive", "v3", credentials=credentials, cache_discovery=False)


def upload_bytes_to_drive(filename: str, data: bytes, description: str = "") -> str:
    service = get_drive_service()

    metadata: Dict[str, Any] = {
        "name": filename,
        "description": description,
    }
    if GOOGLE_DRIVE_FOLDER_ID:
        metadata["parents"] = [GOOGLE_DRIVE_FOLDER_ID]

    media = MediaIoBaseUpload(
        io.BytesIO(data),
        mimetype="application/zip",
        resumable=False,
    )

    created = service.files().create(
        body=metadata,
        media_body=media,
        fields="id,name,webViewLink,webContentLink",
    ).execute()

    file_id = created["id"]

    if GOOGLE_DRIVE_PUBLIC:
        service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
            fields="id",
        ).execute()
        created = service.files().get(
            fileId=file_id,
            fields="id,name,webViewLink,webContentLink",
        ).execute()

    return created.get("webViewLink") or created.get("webContentLink") or f"https://drive.google.com/file/d/{file_id}/view"


async def build_package(source: str, payload: str) -> Optional[Tuple[str, bytes, str, str]]:
    if source == "h":
        ok, title = await safe_to_package(payload)
        if not ok:
            raise RuntimeError(title)

        result = await build_chapter_zip(payload)
        if result == CRASH or type(result) is int:
            return None

        filename, archive_bytes = result
        return filename, archive_bytes, title, payload

    if source == "s":
        chapter_url = normalize_sarrast_url(payload)
        result = build_sarrast_chapter_zip(chapter_url)
        if result == CRASH or type(result) is int:
            return None

        filename, archive_bytes, title = result
        return filename, archive_bytes, title, chapter_url

    raise RuntimeError("Unknown upload source")


async def upload_package(update: Update, context: ContextTypes.DEFAULT_TYPE, source: str, payload: str, destination: str) -> None:
    message = update.effective_message
    status = await message.reply_text("در حال ساخت ZIP...")

    try:
        package = await build_package(source, payload)
    except Exception as exc:
        await status.edit_text(str(exc))
        return

    if not package:
        await status.edit_text("Could not build ZIP.")
        return

    filename, archive_bytes, title, label = package

    if destination == "telegram":
        if len(archive_bytes) > MAX_TELEGRAM_FILE_BYTES:
            if PUBLIC_BASE_URL and source == "h":
                await status.edit_text(f"ZIP is too large for Telegram.\nDownload:\n{PUBLIC_BASE_URL}/hentai/download/{label}")
            else:
                await status.edit_text(f"ZIP is too large for Telegram: {len(archive_bytes) / 1024 / 1024:.1f} MB")
            return

        bio = io.BytesIO(archive_bytes)
        bio.name = filename
        await message.reply_document(
            document=bio,
            filename=filename,
            caption=f"{title}\n{label}",
        )
        await status.delete()
        return

    if destination == "drive":
        await status.edit_text("در حال آپلود در Google Drive...")
        try:
            link = upload_bytes_to_drive(filename, archive_bytes, description=f"{title}\n{label}")
        except Exception as exc:
            await status.edit_text(f"Google Drive upload failed:\n{exc}")
            return

        await status.edit_text(f"✅ آپلود شد در Google Drive:\n{link}")
        return

    await status.edit_text("Unknown upload destination.")


async def send_zip(update: Update, chapter_id: str, title: str) -> None:
    message = update.effective_message
    status = await message.reply_text(f"Building ZIP for {chapter_id} ...")

    result = await build_chapter_zip(chapter_id)

    if result == CRASH or type(result) is int:
        await status.edit_text("Could not build chapter ZIP.")
        return

    filename, archive_bytes = result

    if len(archive_bytes) > MAX_TELEGRAM_FILE_BYTES:
        if PUBLIC_BASE_URL:
            await status.edit_text(
                f"ZIP is too large for Telegram.\nDownload:\n{PUBLIC_BASE_URL}/hentai/download/{chapter_id}"
            )
        else:
            await status.edit_text(
                f"ZIP is too large: {len(archive_bytes) / 1024 / 1024:.1f} MB"
            )
        return

    bio = io.BytesIO(archive_bytes)
    bio.name = filename

    await message.reply_document(
        document=bio,
        filename=filename,
        caption=f"{title}\n{chapter_id}",
    )

    await status.delete()


async def send_sarrast_zip(update: Update, chapter_url: str) -> None:
    await upload_package(update, None, "s", chapter_url, "telegram")


def manga_keyboard(slug: str, chapters: List[Dict[str, str]], context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    rows = []

    for chapter in chapters[:MAX_CHAPTER_BUTTONS]:
        chapter_id = chapter.get("chapter_id", "")
        name = chapter.get("name", chapter_id)
        token = add_callback_payload(context, "c", chapter_id)
        rows.append([InlineKeyboardButton(name, callback_data=token)])

    if len(chapters) > MAX_CHAPTER_BUTTONS:
        rows.append([InlineKeyboardButton(f"Only showing first {MAX_CHAPTER_BUTTONS} chapters", callback_data="noop")])

    all_token = add_callback_payload(context, "a", slug)
    rows.append([InlineKeyboardButton("Download first chapters", callback_data=all_token)])
    return InlineKeyboardMarkup(rows)


def sarrast_keyboard(chapters: List[Dict[str, Any]], context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    rows = []

    for chapter in chapters[:MAX_CHAPTER_BUTTONS]:
        number = chapter.get("number", "")
        title = chapter.get("title") or f"قسمت {number}"
        chapter_url = chapter.get("url", "")
        text = f"#{number} - {title}" if number else str(title)
        token = add_callback_payload(context, "s", chapter_url)
        rows.append([InlineKeyboardButton(text[:60], callback_data=token)])

    if len(chapters) > MAX_CHAPTER_BUTTONS:
        rows.append([InlineKeyboardButton(f"Only showing first {MAX_CHAPTER_BUTTONS} chapters", callback_data="noop")])

    return InlineKeyboardMarkup(rows)


def search_keyboard(results: List[Dict[str, Any]], context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    rows = []
    for item in results:
        title = item.get("title") or item.get("slug")
        slug = item.get("slug")
        latest = item.get("latest_chapter", "")
        text = f"{title[:45]} - {latest}" if latest else title[:60]
        token = add_callback_payload(context, "m", slug)
        rows.append([InlineKeyboardButton(text, callback_data=token)])
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await update.message.reply_text("Access denied.")
        return

    await update.message.reply_text(
        "Commands:\n"
        "/search name\n"
        "/filter page=1 sort=latest type=manhwa status=completed genre=...\n"
        "/manga manga-slug\n"
        "/chapter chapter-id\n"
        "/all manga-slug\n\n"
        "Upload options:\n"
        "After choosing a chapter, select Telegram or Google Drive.\n\n"
        "Sarrast:\n"
        "Send a Sarrast series link to choose chapters with buttons.\n"
        "Send a Sarrast chapter link to choose upload destination.\n\n"
        "Example:\n"
        "/search university\n"
        "/manga 69-university\n"
        "/chapter 69-university-chapter-35/"
    )


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
    for index, item in enumerate(results, start=1):
        lines.append(
            f"{index}. {item.get('title')} | {item.get('latest_chapter')} | slug: {item.get('slug')}"
        )

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=search_keyboard(results, context),
    )


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
    for index, item in enumerate(results, start=1):
        lines.append(
            f"{index}. {item.get('title')} | {item.get('latest_chapter')} | slug: {item.get('slug')}"
        )

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=search_keyboard(results, context),
    )


async def manga_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, slug: Optional[str] = None) -> None:
    if not allowed(update):
        await update.effective_message.reply_text("Access denied.")
        return

    slug = slug or (" ".join(context.args).strip().strip("/") if context.args else "")

    if not slug:
        await update.effective_message.reply_text("Usage: /manga manga-slug")
        return

    if not valid_slug(slug):
        await update.effective_message.reply_text("Invalid manga slug.")
        return

    data = await get_manga(slug)

    if data == CRASH or type(data) is int:
        await update.effective_message.reply_text("Could not fetch manga details.")
        return

    info = data.get("manga", {})

    if blocked_text(info.get("title", ""), info.get("description", ""), slug):
        await update.effective_message.reply_text(
            "Blocked: this title/description appears to involve minors or unsafe terms."
        )
        return

    chapters = info.get("chapters", [])
    lines = [
        f"Title: {info.get('title', slug)}",
        f"Score: {info.get('score', '-')}",
        f"Chapters: {len(chapters)}",
        "",
        "Select a chapter:",
    ]

    await update.effective_message.reply_text(
        "\n".join(lines),
        reply_markup=manga_keyboard(slug, chapters, context),
    )


async def sarrast_series(update: Update, context: ContextTypes.DEFAULT_TYPE, series_url: str) -> None:
    if not allowed(update):
        await update.effective_message.reply_text("Access denied.")
        return

    series_url = normalize_sarrast_url(series_url)
    status = await update.effective_message.reply_text("Loading Sarrast chapters...")
    chapters = load_sarrast_chapters(series_url)

    if chapters == CRASH or type(chapters) is int or not chapters:
        await status.edit_text("Could not load Sarrast chapters.")
        return

    await status.edit_text(
        f"Sarrast chapters: {len(chapters)}\nSelect a chapter:",
        reply_markup=sarrast_keyboard(chapters, context),
    )


async def chapter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await update.message.reply_text("Access denied.")
        return

    arg_text = " ".join(context.args).strip()
    if arg_text and is_sarrast_chapter_url(arg_text):
        await ask_upload_destination(update, context, "s", normalize_sarrast_url(arg_text), arg_text)
        return

    chapter_id = extract_chapter_id(arg_text)

    if not chapter_id:
        await update.message.reply_text("Usage: /chapter 69-university-chapter-35/")
        return

    ok, title = await safe_to_package(chapter_id)

    if not ok:
        await update.message.reply_text(title)
        return

    await ask_upload_destination(update, context, "h", chapter_id, title)


async def all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, slug: Optional[str] = None) -> None:
    if not allowed(update):
        await update.effective_message.reply_text("Access denied.")
        return

    slug = slug or (" ".join(context.args).strip().strip("/") if context.args else "")

    if not slug:
        await update.effective_message.reply_text("Usage: /all manga-slug")
        return

    if not valid_slug(slug):
        await update.effective_message.reply_text("Invalid manga slug.")
        return

    data = await get_manga(slug)

    if data == CRASH or type(data) is int:
        await update.effective_message.reply_text("Could not fetch manga details.")
        return

    info = data.get("manga", {})

    if blocked_text(info.get("title", ""), info.get("description", ""), slug):
        await update.effective_message.reply_text(
            "Blocked: this title/description appears to involve minors or unsafe terms."
        )
        return

    chapters = info.get("chapters", [])[:MAX_CHAPTERS_PER_ALL]

    if not chapters:
        await update.effective_message.reply_text("No chapters found.")
        return

    await update.effective_message.reply_text(
        f"Downloading {len(chapters)} chapters to Telegram. Limit: {MAX_CHAPTERS_PER_ALL}"
    )

    for chapter in chapters:
        chapter_id = chapter.get("chapter_id")
        if not chapter_id:
            continue
        ok, title = await safe_to_package(chapter_id)
        if ok:
            await send_zip(update, chapter_id, title)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not allowed(update):
        await query.message.reply_text("Access denied.")
        return

    data = query.data or ""

    if data == "noop":
        return

    if data.startswith("tg:") or data.startswith("gd:"):
        destination = "telegram" if data.startswith("tg:") else "drive"
        token = data.split(":", 1)[1]
        raw_payload = get_callback_payload(context, token)
        if not raw_payload:
            await query.message.reply_text("This upload button expired. Please choose the chapter again.")
            return
        try:
            obj = json.loads(raw_payload)
            source = obj["source"]
            payload = obj["payload"]
        except Exception:
            await query.message.reply_text("Invalid upload payload. Please choose the chapter again.")
            return
        await upload_package(update, context, source, payload, destination)
        return

    if data.startswith("m"):
        slug = get_callback_payload(context, data)
        if not slug:
            await query.message.reply_text("This button expired. Please search again.")
            return
        await manga_cmd(update, context, slug=slug)
        return

    if data.startswith("c"):
        chapter_id = get_callback_payload(context, data)
        if not chapter_id:
            await query.message.reply_text("This button expired. Please open the manga again.")
            return
        ok, title = await safe_to_package(chapter_id)
        if not ok:
            await query.message.reply_text(title)
            return
        await ask_upload_destination(update, context, "h", chapter_id, title)
        return

    if data.startswith("a"):
        slug = get_callback_payload(context, data)
        if not slug:
            await query.message.reply_text("This button expired. Please open the manga again.")
            return
        await all_cmd(update, context, slug=slug)
        return

    if data.startswith("s"):
        chapter_url = get_callback_payload(context, data)
        if not chapter_url:
            await query.message.reply_text("This button expired. Please send the Sarrast series again.")
            return
        await ask_upload_destination(update, context, "s", chapter_url, chapter_url)
        return


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text if update.message else ""
    stripped = text.strip()

    if is_sarrast_series_url(stripped):
        await sarrast_series(update, context, stripped)
        return

    if is_sarrast_chapter_url(stripped):
        await ask_upload_destination(update, context, "s", normalize_sarrast_url(stripped), stripped)
        return

    chapter_id = extract_chapter_id(text)
    if chapter_id:
        ok, title = await safe_to_package(chapter_id)
        if not ok:
            await update.message.reply_text(title)
            return
        await ask_upload_destination(update, context, "h", chapter_id, title)
        return

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

    print("Telegram bot started.", flush=True)
    app.run_polling()


if __name__ == "__main__":
    main()
