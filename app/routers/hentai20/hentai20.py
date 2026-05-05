from typing import Any, Dict, List, Union, Optional, Tuple
from urllib.parse import urlparse
import ipaddress
import socket
import io
import re
import zipfile

from bs4 import BeautifulSoup
from app.handlers.api_handler import ApiHandler
from app.resources.errors import CRASH
import requests

api = ApiHandler("https://hentai20.io")

ALLOWED_IMAGE_HOSTS = {
    "hentai20.io",
    "www.hentai20.io",
    "cdn.hentai20.io",
    "img.hentai1.io",
    "i0.wp.com",
    "i1.wp.com",
    "i2.wp.com",
    "i3.wp.com",
}

IMAGE_TIMEOUT = (5, 20)
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_ARCHIVE_BYTES = 250 * 1024 * 1024
IMAGE_ATTRS = ("data-src", "data-lazy-src", "data-original", "data-full", "src")
BLOCKED_IMAGE_PARTS = (
    "readerarea.svg",
    "/themes/",
    "/assets/",
    "/wp-includes/",
    "/wp-admin/",
)


def first_text(soup: BeautifulSoup, selector: str, default: str = "") -> str:
    element = soup.select_one(selector)
    return element.get_text(strip=True) if element else default


def first_attr(soup: BeautifulSoup, selector: str, attr: str, default: str = "") -> str:
    element = soup.select_one(selector)
    if not element:
        return default
    value = element.get(attr)
    return value if isinstance(value, str) else default


def safe_archive_name(value: str) -> str:
    value = value.strip().strip("/") or "chapter"
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", value)
    value = value.strip(".-_") or "chapter"
    return value[:120]


def normalize_image_url(value: Optional[str]) -> str:
    if not value:
        return ""
    value = value.strip()
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    return value


def image_from_srcset(srcset: Optional[str]) -> str:
    if not srcset:
        return ""
    candidates = []
    for part in srcset.split(","):
        chunks = part.strip().split()
        if chunks:
            candidates.append(chunks[0])
    return normalize_image_url(candidates[-1]) if candidates else ""


def is_real_panel_image(image_url: str) -> bool:
    lower_url = image_url.lower()
    if lower_url.startswith("data:"):
        return False
    if lower_url.endswith(".svg"):
        return False
    if "placeholder" in lower_url or "blank" in lower_url:
        return False
    if any(part in lower_url for part in BLOCKED_IMAGE_PARTS):
        return False
    parsed = urlparse(image_url)
    path = parsed.path.lower()
    return path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))


def extract_panel_images(soup: BeautifulSoup) -> List[str]:
    containers = [
        ".entry-content.entry-content-single",
        ".entry-content",
        ".reading-content",
        ".chapter-content",
        ".post-body",
        "article",
        "body",
    ]

    seen = set()
    images: List[str] = []

    for selector in containers:
        root = soup.select_one(selector)
        if not root:
            continue

        for img in root.select("img"):
            image_url = ""
            for attr in IMAGE_ATTRS:
                image_url = normalize_image_url(img.get(attr))
                if image_url:
                    break

            if not image_url:
                image_url = image_from_srcset(img.get("srcset") or img.get("data-srcset"))

            if not image_url or not is_real_panel_image(image_url):
                continue

            if image_url not in seen:
                seen.add(image_url)
                images.append(image_url)

        if images:
            break

    return images


def is_public_host(hostname: str) -> bool:
    try:
        addresses = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False

    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            return False
    return True


def is_allowed_image_url(image_url: str) -> bool:
    parsed = urlparse(image_url)
    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.hostname:
        return False
    hostname = parsed.hostname.lower()
    if hostname not in ALLOWED_IMAGE_HOSTS and not hostname.endswith(".hentai20.io"):
        return False
    return is_public_host(hostname)


async def get_panels(chapter_id: str) -> Union[Dict[str, Any], int]:
    response: Any = await api.get(endpoint=f"/{chapter_id}", html=True)

    if type(response) is int:
        return CRASH

    soup: BeautifulSoup = get_soup(response)
    chapter_title: str = first_text(soup, '.entry-title')
    panels: List[Dict[str, str]] = []

    for image_url in extract_panel_images(soup):
        panels.append({"image_url": image_url})

    return {
        "chapter_id": chapter_id,
        "chapter_title": chapter_title,
        "panels": panels,
    }


async def build_chapter_zip(chapter_id: str) -> Union[Tuple[str, bytes], int]:
    panel_data = await get_panels(chapter_id)
    if panel_data == CRASH or type(panel_data) is int:
        return CRASH

    panels = panel_data.get("panels", [])
    if not panels:
        return CRASH

    archive_name = safe_archive_name(panel_data.get("chapter_title") or chapter_id)
    buffer = io.BytesIO()
    total_size = 0

    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, panel in enumerate(panels, start=1):
            image_url = panel.get("image_url", "")
            image_bytes = download_image_from_url(image_url)
            if not image_bytes:
                continue

            total_size += len(image_bytes)
            if total_size > MAX_ARCHIVE_BYTES:
                return CRASH

            suffix = urlparse(image_url).path.rsplit(".", 1)[-1].lower()
            if suffix not in {"jpg", "jpeg", "png", "webp", "gif"}:
                suffix = "jpg"
            archive.writestr(f"{index:03d}.{suffix}", image_bytes)

    archive_bytes = buffer.getvalue()
    if not archive_bytes:
        return CRASH

    return f"{archive_name}.zip", archive_bytes


async def get_manga(manga_id) -> Union[Dict[str, Any], int]:
    response: Any = await api.get(endpoint=f"/manga/{manga_id}", html=True)

    if type(response) is int:
        return CRASH

    soup: BeautifulSoup = get_soup(response)
    title = first_attr(soup, '.attachment-.size-.wp-post-image', "alt")
    image_url = first_attr(soup, '.attachment-.size-.wp-post-image', "src")
    description = first_text(soup, '.entry-content.entry-content-single > p')
    score = first_text(soup, '.num')
    chapter_eles = soup.select('.eph-num > a')

    if not title and not image_url:
        return CRASH

    ticks = {"score": score}
    tick_eles = soup.select(".imptdt")

    for tick in tick_eles:
        tick_type = tick.text.replace("Posted On", "created_at").replace("Updated On", "updated_at").split(" ")[0].lower().strip()

        if tick_type == "type":
            type_element = tick.select_one("a")
            if type_element:
                ticks["type"] = type_element.get_text(strip=True)

        if tick_type in ["updated_at", "created_at", "author"]:
            value_element = tick.select_one("i")
            if value_element:
                ticks[tick_type] = value_element.get_text(strip=True)

    chapters: List[Dict[str, str]] = []

    for chapter_ele in chapter_eles[1:]:
        href: Any = chapter_ele.get("href")
        name_ele = chapter_ele.select_one(".chapternum")
        date_ele = chapter_ele.select_one(".chapterdate")
        if not href or not name_ele:
            continue
        chapter_id = href.replace("https://hentai20.io/", "")

        chapters.append({
            "name": name_ele.get_text(strip=True),
            "chapter_id": chapter_id,
            "date": date_ele.get_text(strip=True) if date_ele else "",
        })

    return {
        "manga": {
            "manga_id": manga_id,
            "image_url": image_url,
            "title": title,
            **ticks,
            "description": description,
            "chapters": chapters,
        }
    }


async def get_filter_mangas(params: Dict[str, str], **kwargs) -> Union[Dict[str, Any], int]:
    response: Any = await api.get(**kwargs, params=params, html=True)
    page = params["page"]

    if type(response) is int:
        return CRASH

    soup: BeautifulSoup = get_soup(response)
    mangas: List[Dict[str, Any]] = []
    items: List = soup.select('.listupd .bsx > a')

    for manga in items:
        image_ele = manga.select_one("img")
        latest_ele = manga.select_one(".epxs")
        score_ele = manga.select_one(".numscore")
        href = manga.get("href") or ""
        href_chunks = href.split("/")
        slug = href_chunks[-2] if len(href_chunks) >= 2 else ""

        mangas.append({
            "title": manga.get("title") or "",
            "image_url": image_ele.get("src") if image_ele else "",
            "colored": bool(manga.select(".colored")),
            "slug": slug,
            "latest_chapter": latest_ele.get_text(strip=True) if latest_ele else "",
            "score": score_ele.get_text(strip=True) if score_ele else "",
        })

    return {
        "mangas": mangas,
        "pagination": {"page": page}
    }


def get_soup(html) -> BeautifulSoup:
    return BeautifulSoup(html, 'html.parser')


def download_image_from_url(image_url: Optional[str]) -> Union[None, bytes]:
    if not image_url or not is_allowed_image_url(image_url):
        return None

    headers = {
        'Referer': 'https://hentai20.io/',
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    }

    try:
        response = requests.get(image_url, headers=headers, timeout=IMAGE_TIMEOUT, stream=True)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if not content_type.startswith("image/"):
            return None

        content_length = response.headers.get("content-length")
        if content_length and int(content_length) > MAX_IMAGE_BYTES:
            return None

        chunks = []
        total = 0
        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_IMAGE_BYTES:
                return None
            chunks.append(chunk)
        return b"".join(chunks)
    except Exception as e:
        print(e)
        return None