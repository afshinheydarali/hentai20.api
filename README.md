# Hentai20 API

A FastAPI scraper/downloader project with a Telegram bot workflow for packaging chapters as ZIP files and uploading them to Telegram, Google Drive, or NixFile.

## Features

- Hentai20 manga lookup, filtering, chapter reading, and ZIP generation.
- Telegram bot commands for search, manga/chapter selection, and chapter packaging.
- Sarrast support:
  - Send a Sarrast series link to the bot and receive inline chapter buttons.
  - Send a single Sarrast chapter link and download/render it directly.
  - Renders Persian overlay text onto the original chapter images.
  - Supports RAQM/Pillow RTL rendering when available.
  - Supports custom Sarrast font via `SARRAST_FONT_PATH`.
  - Supports Cloudflare/session access with `SARRAST_COOKIE_FILE` or `SARRAST_COOKIE`.
- Upload destinations from the bot:
  - Telegram document upload.
  - Google Drive upload using OAuth token JSON or service account credentials.
  - NixFile upload using automatic browser login for token refresh, then fast direct API upload.

## Getting Started

```bash
git clone https://github.com/afshinheydarali/hentai20.api.git
cd hentai20.api
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set the required values. Never commit `.env`, token files, cookies, or credentials.

## Running the API

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Running the Telegram Bot

```bash
python bot.py
```

Useful bot behavior:

- Send a normal Hentai20 chapter ID/link and select an upload destination.
- Send a Sarrast series URL such as `https://sarrast.com/series/<series-slug>` to receive chapter buttons.
- Send a Sarrast chapter URL such as `https://sarrast.com/series/<series-slug>/<chapter-slug>` to package it directly.
- After selecting a chapter, choose Telegram, Google Drive, or NixFile.

## Environment Configuration

See `.env.example` for all supported variables.

### Telegram

- `BOT_TOKEN`: Telegram bot token.
- `ALLOWED_USER_IDS`: Optional comma-separated Telegram user IDs. If empty, all users are allowed.
- `MAX_TELEGRAM_FILE_MB`: Telegram upload size limit.

### Sarrast

- `SARRAST_FONT_PATH`: Absolute path to the Mikhak `.woff2` font file.
- `SARRAST_COOKIE_FILE`: Netscape-format cookie file exported from a browser session.
- `SARRAST_COOKIE`: Optional raw cookie header alternative.
- `SARRAST_IMPERSONATE`: curl_cffi browser profile, for example `chrome124`.
- `SARRAST_LOG_FONT`: Set to `1` to log the font selected during render.
- `SARRAST_MAX_IMAGE_MB` and `SARRAST_MAX_ARCHIVE_MB`: Safety limits.

### Google Drive

Preferred for personal Drive uploads:

- `GOOGLE_OAUTH_TOKEN_JSON`: Full OAuth token JSON produced by the local token helper.
- `GDRIVE_ROOT_FOLDER_ID`: Drive folder ID for uploaded ZIPs.
- `GDRIVE_MAKE_PUBLIC`: `1` to make files public with link access.

Service account fallback is also supported through:

- `GOOGLE_DRIVE_CREDENTIALS_FILE`, `GOOGLE_SERVICE_ACCOUNT_FILE`, or `GOOGLE_APPLICATION_CREDENTIALS`.

### NixFile

NixFile upload flow is hybrid:

1. If `.nixfile-token.json` or `NIXFILE_ACCESS_TOKEN` is valid, the uploader uses the API directly.
2. If the token is missing or rejected, Selenium logs into `panel.nixfile.com`, extracts `accessToken`, saves it to `.nixfile-token.json`, then uploads with the API.

Required/important variables:

- `NIXFILE_USERNAME`: NixFile login username/phone/email.
- `NIXFILE_PASS`: NixFile password.
- `NIXFILE_FOLDER_ID`: Target folder ID. The root/home folder can be found from the file-manager API response.
- `NIXFILE_TOKEN_FILE`: Token cache file, usually `.nixfile-token.json`.
- `NIXFILE_HEADLESS`: `1` for headless browser login.
- `CHROME_BINARY` and `CHROMEDRIVER_PATH`: Required on some VPS/ARM setups.

NixFile public links are returned as:

```text
https://nixfile.com/f/<slug>/
```

## API Base URL

`http://127.0.0.1:8000` or `http://localhost:8000`

All Hentai20 endpoints are prefixed with `/hentai`.

## Endpoints

### Proxy Image

- **URL:** `/proxy/{image_url:path}`
- **Method:** GET
- **Description:** Proxy for image requests.

### Filter Mangas

- **URL:** `/filter`
- **Method:** GET
- **Query Parameters:**
  - `page` (str, default=`1`)
  - `genre` (str, optional)
  - `status` (str, optional)
  - `_type` (str, optional)
  - `sort` (str, optional)

### Get Manga Details

- **URL:** `/{manga_id}`
- **Method:** GET

### Read Chapter

- **URL:** `/read/{chapter_id}`
- **Method:** GET

## Security Notes

Do not commit these files or values:

- `.env`
- `.nixfile-token.json`
- `sarrast-cookies.txt`
- Google OAuth tokens or service account files
- Telegram bot token
- NixFile password/access token
- Generated ZIPs and `tmp_uploads/`

## Notes

- Sarrast may require a valid Cloudflare/browser session cookie on VPS environments.
- Pillow RAQM support improves Persian/RTL rendering quality.
- On ARM/aarch64 VPS environments, Selenium Manager may not work; set `CHROME_BINARY` and `CHROMEDRIVER_PATH` explicitly.
