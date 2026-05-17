import tempfile
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import bot_multi as base
from app.uploaders.rubika import upload_file as upload_to_rubika


def _rows_with_rubika(markup: InlineKeyboardMarkup, callback_data: str) -> InlineKeyboardMarkup:
    rows = [list(row) for row in markup.inline_keyboard]
    rows.append([InlineKeyboardButton("Upload ZIP to Rubika", callback_data=callback_data)])
    return InlineKeyboardMarkup(rows)


def h20_destination_keyboard(chapter_id: str, context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    markup = base.h20_destination_keyboard(chapter_id, context)
    return _rows_with_rubika(markup, base.put_cb(context, "b", chapter_id))


def eh_destination_keyboard(gallery_url: str) -> InlineKeyboardMarkup:
    markup = base.eh_destination_keyboard(gallery_url)
    return _rows_with_rubika(markup, base.eh_callback("ehrb", gallery_url))


def nh_destination_keyboard(gallery_id: str, context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    markup = base.nh_destination_keyboard(gallery_id, context)
    return _rows_with_rubika(markup, base.put_cb(context, "w", gallery_id))


def hk_destination_keyboard(url: str, context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    markup = base.hk_destination_keyboard(url, context)
    return _rows_with_rubika(markup, base.put_cb(context, "x", url))


base.h20_destination_keyboard = h20_destination_keyboard
base.eh_destination_keyboard = eh_destination_keyboard
base.nh_destination_keyboard = nh_destination_keyboard
base.hk_destination_keyboard = hk_destination_keyboard


async def upload_bytes_rubika(update: Update, filename: str, data: bytes, caption: str, status) -> None:
    await status.edit_text("Uploading ZIP to Rubika...")
    tmp_path = None

    try:
        upload_tmp_dir = Path("tmp_uploads")
        upload_tmp_dir.mkdir(parents=True, exist_ok=True)

        suffix = Path(filename).suffix or ".zip"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False, dir=upload_tmp_dir) as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)

        final_path = tmp_path.with_name(filename)
        try:
            tmp_path.replace(final_path)
            tmp_path = final_path
        except Exception:
            pass

        result = upload_to_rubika(tmp_path, caption=caption)
    except Exception as exc:
        await status.edit_text(f"Rubika upload failed: {type(exc).__name__}: {exc}")
        return
    finally:
        if tmp_path:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    await status.edit_text(f"Uploaded to Rubika:\n{result}")


async def send_h20_rb(update: Update, chapter_id: str) -> None:
    status = await update.effective_message.reply_text(f"Building Hentai20 ZIP and uploading to Rubika for {chapter_id} ...")
    filename, data, title = await base.build_h20_zip(chapter_id)
    if not filename or not data:
        await status.edit_text(title)
        return
    await upload_bytes_rubika(update, filename, data, f"{title}\n{chapter_id}", status)


async def send_eh_rb(update: Update, gallery_url: str) -> None:
    status = await update.effective_message.reply_text("Building EHentai gallery ZIP and uploading to Rubika ...")
    try:
        filename, data, title = base.build_gallery_zip(gallery_url)
        if base.blocked_text(title, filename, gallery_url):
            await status.edit_text("Blocked: this gallery appears to involve minors or unsafe terms.")
            return
    except Exception as exc:
        await status.edit_text(f"Could not build EHentai ZIP: {type(exc).__name__}: {exc}")
        return
    await upload_bytes_rubika(update, filename, data, f"{title}\n{gallery_url}", status)


async def send_nh_rb(update: Update, gallery_id: str) -> None:
    status = await update.effective_message.reply_text(f"Building nhentai ZIP and uploading to Rubika for {gallery_id} ...")
    try:
        filename, data, title = base.build_nhentai_zip(gallery_id)
        if base.blocked_text(title, filename, gallery_id):
            await status.edit_text("Blocked: this gallery appears to involve minors or unsafe terms.")
            return
    except Exception as exc:
        await status.edit_text(f"Could not build nhentai ZIP: {type(exc).__name__}: {exc}")
        return
    await upload_bytes_rubika(update, filename, data, f"{title}\nnhentai #{gallery_id}", status)


async def send_hk_rb(update: Update, url: str) -> None:
    status = await update.effective_message.reply_text("Building HentaiKun ZIP and uploading to Rubika ...")
    try:
        filename, data, title = base.build_hentaikun_zip(url)
        if base.blocked_text(title, filename, url):
            await status.edit_text("Blocked: this album appears to involve minors or unsafe terms.")
            return
    except Exception as exc:
        await status.edit_text(f"Could not build HentaiKun ZIP: {type(exc).__name__}: {exc}")
        return
    await upload_bytes_rubika(update, filename, data, f"{title}\n{url}", status)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not base.allowed(update):
        await update.message.reply_text("Access denied.")
        return
    await update.message.reply_text(
        "Commands:\n"
        "/search name hentai20\n"
        "/search name ehentai\n"
        "/search name nhentai\n"
        "/search name all\n"
        "/filter page=1 sort=latest type=manhwa status=completed genre=...\n"
        "/manga manga-slug\n"
        "/chapter chapter-id\n"
        "/all manga-slug\n\n"
        "Direct links from hentai20, EHentai, nhentai and HentaiKun are detected automatically.\n"
        "Upload destinations: Telegram, Google Drive, and Rubika."
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = q.data or ""

    if data.startswith("b"):
        await q.answer()
        if not base.allowed(update):
            await q.message.reply_text("Access denied.")
            return
        chapter_id = base.get_cb(context, data)
        if chapter_id:
            await send_h20_rb(update, chapter_id)
        return

    if data.startswith("w"):
        await q.answer()
        if not base.allowed(update):
            await q.message.reply_text("Access denied.")
            return
        gallery_id = base.get_cb(context, data)
        if gallery_id:
            await send_nh_rb(update, gallery_id)
        return

    if data.startswith("x"):
        await q.answer()
        if not base.allowed(update):
            await q.message.reply_text("Access denied.")
            return
        url = base.get_cb(context, data)
        if url:
            await send_hk_rb(update, url)
        return

    if data.startswith("ehrb:"):
        await q.answer()
        if not base.allowed(update):
            await q.message.reply_text("Access denied.")
            return
        gallery_url = base.eh_url_from_callback(data)
        if gallery_url:
            await send_eh_rb(update, gallery_url)
        return

    await base.callback_handler(update, context)


def main() -> None:
    if not base.BOT_TOKEN:
        raise RuntimeError("Please set BOT_TOKEN in .env or environment variables.")

    app = Application.builder().token(base.BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("search", base.search_cmd))
    app.add_handler(CommandHandler("filter", base.filter_cmd))
    app.add_handler(CommandHandler("manga", base.manga_cmd))
    app.add_handler(CommandHandler("chapter", base.chapter_cmd))
    app.add_handler(CommandHandler("all", base.all_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, base.text_handler))

    print("Telegram bot with Rubika uploader support started.", flush=True)
    app.run_polling()


if __name__ == "__main__":
    main()
