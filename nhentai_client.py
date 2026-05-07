import io
import json
import re
import time
import zipfile
from dataclasses import dataclass
from html import unescape
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus, urlparse

import requests
from bs4 import BeautifulSoup

NH_BASE_URL = "https://nhentai.net"
NH_IMAGE_HOST = "https://i.nhentai.net"
NH_THUMB_HOST = "https://t.nhentai.net"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Referer": NH_BASE_URL + "/",
}
IMAGE_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif,image/*;q=0.8,*/*;q=0.5",
    "Referer": NH_BASE_URL + "/",
}

MAX_IMAGE_BYTES = 80 * 1024 * 1024
REQUEST_TIMEOUT = (8, 60)
IMAGE_EXTENSIONS = ["jpg", "png", "webp", "gif", "jpeg"]

BLOCKED_TERMS = {
    "underage", "minor", "child", "children", "kid", "kids", "loli", "lolicon",
    "shota", "shotacon", "junior high", "middle school", "elementary",
    "schoolgirl", "schoolboy", "12 years old", "13 years old", "14 years old",
    "15 years old", "10yo", "11yo", "12yo", "13yo", "14yo", "15yo",
}

EXT_BY_TYPE = {"j": "jpg", "p": "png", "g": "gif", "w": "webp"}


@dataclass
class NHResult:
    id: str
    title: str
    url: str


@dataclass
class NHGallery:
    id: str
    title: str
    url: str
    media_id: str
    pages: List[str]
    tags: List[str]


def _blocked(*values: str) -> bool:
    text = "\n".join(v or "" for v in values).lower()
    return any(term in text for term in BLOCKED_TERMS)


def _safe_filename(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]+", "-", name or "nhentai")
    name = re.sub(r"\s+", " ", name).strip()
    return name[:140] or "nhentai"


def extract_nhentai_id(text: str) -> Optional[str]:
    text = (text or "").strip()
    if not text:
        return None

    if text.isdigit():
        return text

    parsed = urlparse(text)
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        host = parsed.hostname.lower()
        if "nhentai.net" not in host:
            return None
        match = re.search(r"/g/(\d+)/?", parsed.path)
        if match:
            return match.group(1)

    match = re.search(r"nhentai\.net/g/(\d+)", text)
    if match:
        return match.group(1)

    return None


def _get(url: str) -> requests.Response:
    response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response


def search_nhentai(query: str, limit: int = 10) -> List[NHResult]:
    query = (query or "").strip()
    if not query:
        return []

    url = f"{NH_BASE_URL}/search/?q={quote_plus(query)}"
    response = _get(url)
    soup = BeautifulSoup(response.text, "html.parser")

    results: List[NHResult] = []
    seen: set[str] = set()

    for card in soup.select(".gallery"):
        link = card.select_one("a[href^='/g/']")
        if not link:
            continue
        href = link.get("href") or ""
        match = re.search(r"/g/(\d+)/?", href)
        if not match:
            continue
        gid = match.group(1)
        if gid in seen:
            continue

        title_node = card.select_one(".caption") or link
        title = title_node.get_text(" ", strip=True) or gid
        title = unescape(" ".join(title.split()))

        if _blocked(title):
            continue

        seen.add(gid)
        results.append(NHResult(id=gid, title=title, url=f"{NH_BASE_URL}/g/{gid}/"))
        if len(results) >= limit:
            break

    return results


def _extract_gallery_json(html: str) -> Optional[Dict[str, Any]]:
    patterns = [
        r"window\._gallery\s*=\s*JSON\.parse\((?P<json>\".*?\")\)",
        r"new N.gallery\((?P<json>\{.*?\})\)",
    ]
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.DOTALL)
        if not match:
            continue
        raw = match.group("json")
        try:
            if raw.startswith('"'):
                raw = json.loads(raw)
            return json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            continue
    return None


def _pages_from_json(data: Dict[str, Any]) -> tuple[Optional[str], List[str]]:
    media_id = str(data.get("media_id") or "").strip()
    pages = []
    images = data.get("images") or {}
    for page in images.get("pages") or []:
        ext = EXT_BY_TYPE.get(str(page.get("t") or "").lower(), "jpg")
        pages.append(ext)
    return media_id or None, pages


def _pages_from_thumbnails(soup: BeautifulSoup) -> tuple[Optional[str], List[str]]:
    pages: List[str] = []
    media_id: Optional[str] = None
    thumbs = soup.select("#thumbnail-container img, .thumb-container img, .gallerythumb img")

    for img in thumbs:
        src = img.get("data-src") or img.get("src") or ""
        match = re.search(r"/galleries/(\d+)/(\d+)t?\.([a-zA-Z0-9]+)", src)
        if not match:
            continue
        media_id = media_id or match.group(1)
        ext = match.group(3).lower()
        pages.append(ext if ext in IMAGE_EXTENSIONS else "jpg")

    return media_id, pages


def get_nhentai_gallery(gallery_id: str) -> NHGallery:
    gallery_id = str(gallery_id).strip()
    if not gallery_id.isdigit():
        raise ValueError("Invalid nhentai gallery id")

    url = f"{NH_BASE_URL}/g/{gallery_id}/"
    response = _get(url)
    soup = BeautifulSoup(response.text, "html.parser")

    title_node = soup.select_one("h1.title") or soup.select_one("#info h1") or soup.select_one("title")
    title = title_node.get_text(" ", strip=True) if title_node else f"nhentai-{gallery_id}"
    title = re.sub(r"\s*-\s*nhentai.*$", "", title, flags=re.I).strip() or f"nhentai-{gallery_id}"

    tags = [node.get_text(" ", strip=True) for node in soup.select(".tag-container .tag .name, .tags a.tag .name")]

    if _blocked(title, " ".join(tags), gallery_id):
        raise ValueError("Blocked: this gallery appears to involve minors or unsafe terms.")

    data = _extract_gallery_json(response.text)
    media_id: Optional[str] = None
    pages: List[str] = []

    if data:
        media_id, pages = _pages_from_json(data)

    if not media_id or not pages:
        media_id, pages = _pages_from_thumbnails(soup)

    if not media_id or not pages:
        raise RuntimeError("Could not parse nhentai gallery pages.")

    return NHGallery(id=gallery_id, title=title, url=url, media_id=media_id, pages=pages, tags=tags)


def _image_url(gallery: NHGallery, index: int, ext: str) -> str:
    return f"{NH_IMAGE_HOST}/galleries/{gallery.media_id}/{index}.{ext}"


def _candidate_extensions(preferred: str) -> List[str]:
    preferred = (preferred or "jpg").lower()
    candidates = [preferred]
    candidates.extend(ext for ext in IMAGE_EXTENSIONS if ext != preferred)
    return candidates


def _download_image_with_fallback(gallery: NHGallery, index: int, preferred_ext: str) -> tuple[str, bytes]:
    last_error: Optional[Exception] = None

    for ext in _candidate_extensions(preferred_ext):
        url = _image_url(gallery, index, ext)
        try:
            response = requests.get(url, headers=IMAGE_HEADERS, timeout=REQUEST_TIMEOUT, stream=True)
            if response.status_code == 404:
                response.close()
                continue
            response.raise_for_status()

            content_type = response.headers.get("Content-Type", "").lower()
            if content_type and "image" not in content_type:
                response.close()
                continue

            data = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                data.extend(chunk)
                if len(data) > MAX_IMAGE_BYTES:
                    response.close()
                    raise RuntimeError(f"Image page {index} is too large")
            response.close()

            if not data:
                continue
            return ext, bytes(data)
        except Exception as exc:
            last_error = exc
            try:
                response.close()
            except Exception:
                pass
            continue

    if last_error:
        raise RuntimeError(f"Could not download nhentai page {index}: {last_error}")
    raise RuntimeError(f"Could not download nhentai page {index}: no extension matched")


def build_nhentai_zip(gallery_id_or_url: str) -> tuple[str, bytes, str]:
    gallery_id = extract_nhentai_id(gallery_id_or_url) or gallery_id_or_url
    gallery = get_nhentai_gallery(gallery_id)

    archive = io.BytesIO()
    safe_title = _safe_filename(gallery.title)

    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("metadata.txt", f"Source: nhentai\nID: {gallery.id}\nTitle: {gallery.title}\nURL: {gallery.url}\n")

        for index, preferred_ext in enumerate(gallery.pages, start=1):
            ext, data = _download_image_with_fallback(gallery, index, preferred_ext)
            zf.writestr(f"{index:03}.{ext}", data)
            time.sleep(0.2)

    return f"nhentai-{gallery.id}-{safe_title}.zip", archive.getvalue(), gallery.title
