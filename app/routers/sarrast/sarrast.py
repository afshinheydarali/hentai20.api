import html
import io
import os
import re
import zipfile
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import arabic_reshaper
import requests
from bidi.algorithm import get_display
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

from app.resources.errors import CRASH

SARRAST_BASE_URL = "https://sarrast.com"
SARRAST_HOSTS = {"sarrast.com", "www.sarrast.com"}
SARRAST_TIMEOUT = (10, 60)
SARRAST_IMAGE_TIMEOUT = (10, 120)
SARRAST_MAX_IMAGE_BYTES = int(os.getenv("SARRAST_MAX_IMAGE_MB", "50") or "50") * 1024 * 1024
SARRAST_MAX_ARCHIVE_BYTES = int(os.getenv("SARRAST_MAX_ARCHIVE_MB", "450") or "450") * 1024 * 1024
SARRAST_PROXY_URL = os.getenv("SARRAST_PROXY_URL", "").strip()
SARRAST_FONT_PATH = os.getenv("SARRAST_FONT_PATH", "").strip()

Image.MAX_IMAGE_PIXELS = None

HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def sarrast_proxies() -> Optional[Dict[str, str]]:
    if not SARRAST_PROXY_URL:
        return None
    return {"http": SARRAST_PROXY_URL, "https": SARRAST_PROXY_URL}


def normalize_sarrast_url(url: str) -> str:
    value = (url or "").strip()
    if not value:
        return ""
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value.rstrip("/")


def sarrast_path_parts(url: str) -> List[str]:
    parsed = urlparse(normalize_sarrast_url(url))
    return [part for part in parsed.path.split("/") if part]


def is_sarrast_url(url: str) -> bool:
    try:
        parsed = urlparse(normalize_sarrast_url(url))
        return parsed.hostname in SARRAST_HOSTS
    except Exception:
        return False


def is_sarrast_series_url(url: str) -> bool:
    if not is_sarrast_url(url):
        return False
    parts = sarrast_path_parts(url)
    return len(parts) == 2 and parts[0] == "series"


def is_sarrast_chapter_url(url: str) -> bool:
    if not is_sarrast_url(url):
        return False
    parts = sarrast_path_parts(url)
    return len(parts) == 3 and parts[0] == "series"


def sarrast_series_slug(url: str) -> str:
    parts = sarrast_path_parts(url)
    if len(parts) < 2 or parts[0] != "series":
        raise ValueError("Invalid Sarrast series/chapter URL")
    return parts[1]


def sarrast_chapter_slug(url: str) -> str:
    parts = sarrast_path_parts(url)
    if len(parts) != 3 or parts[0] != "series":
        raise ValueError("Invalid Sarrast chapter URL")
    return parts[2]


def sarrast_base_url(url: str) -> str:
    parsed = urlparse(normalize_sarrast_url(url))
    return f"{parsed.scheme}://{parsed.netloc}"


def unwrap_ouo_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if "s" in query and query["s"]:
            return unquote(query["s"][0])
    except Exception:
        pass
    return url


def chapter_number_from_slug(slug: str) -> int:
    match = re.match(r"^(\d+)", slug or "")
    return int(match.group(1)) if match else 999999


def safe_archive_name(value: str) -> str:
    value = (value or "chapter").strip().strip("/")
    value = re.sub(r"[^\w.()\-\s\u0600-\u06FF]+", "-", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip(" .-_") or "chapter"
    return value[:140]


def sarrast_get(url: str, referer: Optional[str] = None, timeout: Tuple[int, int] = SARRAST_TIMEOUT) -> requests.Response:
    headers = dict(HEADERS_BASE)
    if referer:
        headers["Referer"] = referer
    response = requests.get(url, headers=headers, proxies=sarrast_proxies(), timeout=timeout)
    response.raise_for_status()
    return response


def sarrast_fetch_json(url: str, referer: Optional[str] = None) -> Dict[str, Any]:
    return sarrast_get(url, referer=referer).json()


def sarrast_fetch_text(url: str, referer: Optional[str] = None) -> str:
    return sarrast_get(url, referer=referer).text


def sarrast_fetch_bytes(url: str, referer: Optional[str] = None) -> bytes:
    response = sarrast_get(url, referer=referer, timeout=SARRAST_IMAGE_TIMEOUT)
    content_type = response.headers.get("content-type", "").lower()
    if content_type and not content_type.startswith("image/"):
        raise ValueError(f"Not an image response: {content_type}")
    content_length = response.headers.get("content-length")
    if content_length and int(content_length) > SARRAST_MAX_IMAGE_BYTES:
        raise ValueError("Image too large")
    data = response.content
    if len(data) > SARRAST_MAX_IMAGE_BYTES:
        raise ValueError("Image too large")
    return data


def load_sarrast_chapters(series_url: str) -> Union[List[Dict[str, Any]], int]:
    try:
        series_url = normalize_sarrast_url(series_url)
        if not is_sarrast_series_url(series_url):
            return CRASH

        page_html = sarrast_fetch_text(series_url, referer=SARRAST_BASE_URL + "/")
        soup = BeautifulSoup(page_html, "html.parser")
        base_url = sarrast_base_url(series_url)
        series_slug = sarrast_series_slug(series_url)
        seen = set()
        chapters: List[Dict[str, Any]] = []

        for link in soup.select('a[href]'):
            href = html.unescape(link.get("href") or "")
            href = unwrap_ouo_url(href)
            full_url = urljoin(series_url, href).rstrip("/")
            parts = sarrast_path_parts(full_url)
            if len(parts) != 3 or parts[0] != "series" or parts[1] != series_slug:
                continue
            chapter_slug = parts[2]
            if not re.match(r"^\d+", chapter_slug):
                continue
            chapter_url = f"{base_url}/series/{series_slug}/{chapter_slug}"
            if chapter_url in seen:
                continue
            seen.add(chapter_url)
            number = chapter_number_from_slug(chapter_slug)
            title = f"قسمت {number}"
            text = link.get_text(" ", strip=True)
            if text and re.search(r"#?\d+", text):
                title = text
            chapters.append({
                "number": number,
                "title": title,
                "slug": chapter_slug,
                "url": chapter_url,
            })

        chapters.sort(key=lambda item: (item["number"], item["slug"]))
        return chapters
    except Exception as exc:
        print(f"Sarrast chapter list failed: {exc}")
        return CRASH


def build_sarrast_chapters_from_chapter_api(chapter_url: str) -> Union[List[Dict[str, Any]], int]:
    try:
        chapter_url = normalize_sarrast_url(chapter_url)
        api_url = chapter_url.rstrip("/") + "/api"
        data = sarrast_fetch_json(api_url, referer=chapter_url)
        serie = data.get("serie") or {}
        posts = serie.get("posts") or []
        if not isinstance(posts, list):
            return CRASH
        base_url = sarrast_base_url(chapter_url)
        series_slug = serie.get("slug") or sarrast_series_slug(chapter_url)
        chapters = []
        for post in posts:
            if not post.get("visible", True):
                continue
            slug = post.get("slug")
            if not slug:
                continue
            number = chapter_number_from_slug(slug)
            chapters.append({
                "number": number,
                "title": post.get("title") or f"قسمت {number}",
                "slug": slug,
                "url": urljoin(base_url, f"/series/{series_slug}/{slug}"),
            })
        chapters.sort(key=lambda item: (item["number"], item["slug"]))
        return chapters
    except Exception as exc:
        print(f"Sarrast chapter API list failed: {exc}")
        return CRASH


def clean_html_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"</div>\s*<div[^>]*>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("\r", "")
    lines = [line.strip() for line in value.split("\n") if line.strip()]
    return "\n".join(lines)


def rtl_text(text: str) -> str:
    return get_display(arabic_reshaper.reshape(text))


@lru_cache(maxsize=256)
def find_font(size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        SARRAST_FONT_PATH,
        "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if path and Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def get_text_color(bg: str) -> str:
    try:
        value = bg.replace("#", "")
        r = int(value[0:2], 16)
        g = int(value[2:4], 16)
        b = int(value[4:6], 16)
        brightness = ((r * 299) + (g * 587) + (b * 114)) / 1000
        return "black" if brightness > 155 else "white"
    except Exception:
        return "black"


def normalize_image_url(chapter_url: str, file_obj: Dict[str, Any]) -> str:
    path = file_obj.get("path")
    if not path:
        raise ValueError("file object has no path")
    image_url = urljoin(chapter_url, path)
    parsed = urlparse(image_url)
    if parsed.hostname not in SARRAST_HOSTS:
        raise ValueError("image host is not allowed")
    return image_url


def get_file_height(file_obj: Dict[str, Any]) -> int:
    try:
        return int(file_obj.get("height") or 0)
    except Exception:
        return 0


def get_file_width(file_obj: Dict[str, Any]) -> int:
    try:
        return int(file_obj.get("width") or 720)
    except Exception:
        return 720


def get_translate_boxes(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    boxes = data.get("post", {}).get("translate", {}).get("html", [])
    return boxes if isinstance(boxes, list) else []


def get_editor_margin(data: Dict[str, Any]) -> float:
    try:
        return float(data.get("post", {}).get("translate", {}).get("editorDrawPanelMargin", 0) or 0)
    except Exception:
        return 0.0


def box_intersects_image(box: Dict[str, Any], image_start_y: float, image_end_y: float) -> bool:
    try:
        y1 = float(box.get("y", 0))
        y2 = float(box.get("endY", y1))
    except Exception:
        return False
    return y2 > image_start_y and y1 < image_end_y


def parse_radius(radius_value: Any, box_w: int, box_h: int) -> int:
    try:
        value = float(radius_value or 0)
    except Exception:
        value = 0
    return int(min(box_w, box_h) * value / 100)


def shrink_font_to_fit(draw: ImageDraw.ImageDraw, lines: List[str], box_w: int, box_h: int, start_size: int):
    size = max(8, start_size)
    while size >= 8:
        font = find_font(size)
        spacing = max(1, int(size * 0.20))
        total_h = 0
        max_w = 0
        for line in lines:
            shaped = rtl_text(line)
            bbox = draw.textbbox((0, 0), shaped, font=font)
            max_w = max(max_w, bbox[2] - bbox[0])
            total_h += bbox[3] - bbox[1]
        total_h += spacing * max(0, len(lines) - 1)
        if max_w <= box_w * 0.92 and total_h <= box_h * 0.90:
            return font, spacing
        size -= 1
    return find_font(8), 1


def render_text_on_box(box_img: Image.Image, content: str, bg: str, box_w: int, box_h: int) -> None:
    if not content:
        return
    draw = ImageDraw.Draw(box_img)
    lines = content.splitlines()
    line_count = max(1, len(lines))
    initial_font_size = max(8, int((box_h / line_count) * 0.62))
    font, spacing = shrink_font_to_fit(draw, lines, box_w, box_h, initial_font_size)
    text_fill = get_text_color(bg)
    line_render_data = []
    total_text_h = 0
    for line in lines:
        shaped = rtl_text(line)
        bbox = draw.textbbox((0, 0), shaped, font=font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        line_render_data.append((shaped, w, h))
        total_text_h += h
    total_text_h += spacing * max(0, len(lines) - 1)
    current_y = int((box_h - total_text_h) / 2)
    for shaped, w, h in line_render_data:
        draw.text((int((box_w - w) / 2), current_y), shaped, fill=text_fill, font=font)
        current_y += h + spacing


def draw_box(base_img: Image.Image, box: Dict[str, Any], image_start_y: float, scale_x: float, scale_y: float, editor_margin: float) -> None:
    try:
        raw_x = float(box.get("x", 0)) - editor_margin
        raw_end_x = float(box.get("endX", box.get("x", 0))) - editor_margin
        x = raw_x * scale_x
        y = (float(box.get("y", 0)) - image_start_y) * scale_y
        end_x = raw_end_x * scale_x
        end_y = (float(box.get("endY", box.get("y", 0))) - image_start_y) * scale_y
    except Exception:
        return
    if end_x <= x or end_y <= y:
        return

    box_w = max(1, int(end_x - x))
    box_h = max(1, int(end_y - y))
    bg = box.get("background") or "#ffffff"
    if not isinstance(bg, str) or not bg.startswith("#"):
        bg = "#ffffff"
    content = clean_html_text(str(box.get("content", "")))
    radius_px = parse_radius(box.get("radius"), box_w, box_h)
    try:
        angle = float(box.get("rotate") or 0)
    except Exception:
        angle = 0

    box_layer = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    layer_draw = ImageDraw.Draw(box_layer)
    layer_draw.rounded_rectangle([0, 0, box_w, box_h], radius=radius_px, fill=bg)
    render_text_on_box(box_layer, content, bg, box_w, box_h)

    if abs(angle) > 0.1:
        rotated = box_layer.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC)
        center_x = int(x + box_w / 2)
        center_y = int(y + box_h / 2)
        base_img.paste(rotated, (int(center_x - rotated.width / 2), int(center_y - rotated.height / 2)), rotated)
    else:
        base_img.paste(box_layer, (int(x), int(y)), box_layer)


def build_sarrast_chapter_zip(chapter_url: str) -> Union[Tuple[str, bytes, str], int]:
    try:
        chapter_url = normalize_sarrast_url(chapter_url)
        if not is_sarrast_chapter_url(chapter_url):
            return CRASH
        api_url = chapter_url.rstrip("/") + "/api"
        data = sarrast_fetch_json(api_url, referer=chapter_url)
        files = data.get("files") or []
        if not isinstance(files, list) or not files:
            return CRASH

        boxes = get_translate_boxes(data)
        editor_margin = get_editor_margin(data)
        serie = data.get("serie") or {}
        post = data.get("post") or {}
        series_slug = serie.get("slug") or sarrast_series_slug(chapter_url)
        post_slug = post.get("slug") or sarrast_chapter_slug(chapter_url)
        post_title = post.get("title") or post_slug
        title = f"{serie.get('title') or series_slug} - {post_title}"
        archive_name = safe_archive_name(f"{series_slug}-{post_slug}-{post_title}")
        heights = [get_file_height(item) for item in files]

        buffer = io.BytesIO()
        total_size = 0
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for image_index, target_file in enumerate(files):
                image_start_y = sum(heights[:image_index])
                image_height_declared = heights[image_index]
                image_end_y = image_start_y + image_height_declared
                image_url = normalize_image_url(chapter_url, target_file)
                raw = sarrast_fetch_bytes(image_url, referer=chapter_url)
                img = Image.open(BytesIO(raw)).convert("RGB")
                real_w, real_h = img.size
                declared_w = get_file_width(target_file) or 720
                scale_x = real_w / declared_w
                scale_y = real_h / image_height_declared if image_height_declared else 1.0
                boxes_for_image = [box for box in boxes if box_intersects_image(box, image_start_y, image_end_y)]
                for box in boxes_for_image:
                    draw_box(img, box, image_start_y, scale_x, scale_y, editor_margin)
                out = io.BytesIO()
                img.save(out, format="JPEG", quality=92, optimize=True)
                image_bytes = out.getvalue()
                total_size += len(image_bytes)
                if total_size > SARRAST_MAX_ARCHIVE_BYTES:
                    return CRASH
                archive.writestr(f"{image_index + 1:03d}.jpg", image_bytes)

        archive_bytes = buffer.getvalue()
        if not archive_bytes:
            return CRASH
        return f"{archive_name}.zip", archive_bytes, title
    except Exception as exc:
        print(f"Sarrast ZIP failed: {exc}")
        return CRASH
