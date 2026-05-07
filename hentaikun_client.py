import io
import re
import time
import zipfile
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

HK_DOMAIN = "hentaikun.com"
HK_BASE_URL = "https://hentaikun.com"
REQUEST_TIMEOUT = (8, 60)
MAX_IMAGE_BYTES = 80 * 1024 * 1024

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Referer": HK_BASE_URL + "/",
}
IMAGE_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif,image/*;q=0.8,*/*;q=0.5",
    "Referer": HK_BASE_URL + "/",
}

BLOCKED_TERMS = {
    "underage", "minor", "child", "children", "kid", "kids", "loli", "lolicon",
    "shota", "shotacon", "junior high", "middle school", "elementary",
    "schoolgirl", "schoolboy", "12 years old", "13 years old", "14 years old",
    "15 years old", "10yo", "11yo", "12yo", "13yo", "14yo", "15yo",
}


@dataclass
class HentaiKunGallery:
    title: str
    url: str
    album: str
    images: List[str]


def _blocked(*values: str) -> bool:
    text = "\n".join(v or "" for v in values).lower()
    return any(term in text for term in BLOCKED_TERMS)


def _safe_filename(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]+", "-", name or "hentaikun")
    name = re.sub(r"\s+", " ", name).strip()
    return name[:140] or "hentaikun"


def extract_hentaikun_url(text: str) -> Optional[str]:
    text = (text or "").strip()
    if not text:
        return None

    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None

    host = parsed.hostname.lower().removeprefix("www.")
    if host != HK_DOMAIN:
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 3:
        return None

    return f"https://{HK_DOMAIN}/" + "/".join(parts[:3]) + "/"


def _get(url: str) -> requests.Response:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host != HK_DOMAIN:
        raise ValueError(f"Blocked non-HentaiKun URL: {url}")
    response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response


def _absolute(url: str, base: str) -> str:
    return urljoin(base, (url or "").strip())


def _album_from_url(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    return parts[2] if len(parts) >= 3 else "hentaikun"


def _title_from_soup(soup: BeautifulSoup, fallback: str) -> str:
    node = soup.select_one("h1") or soup.select_one(".entry-title") or soup.select_one("title")
    title = node.get_text(" ", strip=True) if node else fallback
    title = re.sub(r"\s*-\s*HentaiKun.*$", "", title, flags=re.I).strip()
    return title or fallback


def get_hentaikun_gallery(url: str) -> HentaiKunGallery:
    normalized = extract_hentaikun_url(url)
    if not normalized:
        raise ValueError("Invalid HentaiKun album URL")

    album = _album_from_url(normalized)
    first = _get(normalized)
    soup = BeautifulSoup(first.text, "html.parser")
    title = _title_from_soup(soup, album)

    if _blocked(title, album, normalized):
        raise ValueError("Blocked: this gallery appears to involve minors or unsafe terms.")

    chapter_links = []
    for link in soup.select("a.readchap"):
        href = _absolute(link.get("href") or "", normalized)
        if href and href not in chapter_links:
            chapter_links.append(href)

    chapter_links.reverse()
    if not chapter_links:
        chapter_links = [normalized]

    image_urls: List[str] = []
    seen: set[str] = set()

    for chapter_url in chapter_links:
        chapter_res = _get(chapter_url)
        chapter_soup = BeautifulSoup(chapter_res.text, "html.parser")
        page_urls = []

        for option in chapter_soup.select(".form-control option"):
            value = (option.get("value") or "").strip()
            if value.startswith("http"):
                page_urls.append(value)

        if not page_urls:
            page_urls = [chapter_url]

        for page_url in page_urls:
            page_res = _get(page_url)
            page_soup = BeautifulSoup(page_res.text, "html.parser")
            for img in page_soup.select("img.image_show"):
                src = _absolute(img.get("src") or img.get("data-src") or "", page_url)
                if not src or src in seen:
                    continue
                parsed = urlparse(src)
                if parsed.scheme not in {"http", "https"}:
                    continue
                seen.add(src)
                image_urls.append(src)
            time.sleep(0.1)

    if not image_urls:
        raise RuntimeError("Could not find HentaiKun images.")

    return HentaiKunGallery(title=title, url=normalized, album=album, images=image_urls)


def _download_image(url: str, referer: str) -> tuple[str, bytes]:
    headers = dict(IMAGE_HEADERS)
    headers["Referer"] = referer
    response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, stream=True)
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "").lower()
    if content_type and "image" not in content_type:
        response.close()
        raise RuntimeError(f"Unexpected non-image response: {url}")

    ext = urlparse(url).path.rsplit(".", 1)[-1].lower()
    if not re.fullmatch(r"[a-z0-9]{2,5}", ext):
        ext = "jpg"

    data = bytearray()
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        data.extend(chunk)
        if len(data) > MAX_IMAGE_BYTES:
            response.close()
            raise RuntimeError("Image is too large")
    response.close()

    if not data:
        raise RuntimeError(f"Empty image response: {url}")

    return ext, bytes(data)


def build_hentaikun_zip(url: str) -> tuple[str, bytes, str]:
    gallery = get_hentaikun_gallery(url)
    archive = io.BytesIO()
    safe_title = _safe_filename(gallery.title)

    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("metadata.txt", f"Source: hentaikun\nTitle: {gallery.title}\nURL: {gallery.url}\nImages: {len(gallery.images)}\n")

        for index, image_url in enumerate(gallery.images, start=1):
            ext, data = _download_image(image_url, gallery.url)
            zf.writestr(f"{index:03}.{ext}", data)
            time.sleep(0.15)

    return f"hentaikun-{gallery.album}-{safe_title}.zip", archive.getvalue(), gallery.title
