#!/usr/bin/env python3
"""Download all images from a webpage."""

from __future__ import annotations

import argparse
import hashlib
import mimetypes
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".ico", ".avif"}
CSS_URL_PATTERN = re.compile(r'url\(["\']?(.*?)["\']?\)', re.IGNORECASE)


def fetch_page(url: str, timeout: int) -> str:
    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
    )
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding
    return response.text


def parse_srcset(srcset: str) -> list[str]:
    urls = []
    for part in srcset.split(","):
        part = part.strip()
        if not part:
            continue
        url = part.split()[0]
        if url:
            urls.append(url)
    return urls


def extract_image_urls(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    found: set[str] = set()

    def add(raw_url: str | None) -> None:
        if not raw_url:
            return
        raw_url = raw_url.strip()
        if raw_url.startswith("data:"):
            return
        absolute = urljoin(base_url, raw_url)
        found.add(absolute)

    for img in soup.find_all("img"):
        add(img.get("src"))
        add(img.get("data-src"))
        add(img.get("data-original"))
        add(img.get("data-lazy-src"))
        for attr in img.attrs:
            if attr.startswith("data-") and attr not in {
                "data-src",
                "data-original",
                "data-lazy-src",
            }:
                value = img.get(attr)
                if isinstance(value, str) and value.startswith(("http://", "https://", "/")):
                    add(value)
        srcset = img.get("srcset")
        if srcset:
            for url in parse_srcset(srcset):
                add(url)

    for source in soup.find_all("source"):
        srcset = source.get("srcset")
        if srcset:
            for url in parse_srcset(srcset):
                add(url)
        add(source.get("src"))

    for tag in soup.find_all(style=True):
        style = tag.get("style", "")
        for match in CSS_URL_PATTERN.findall(style):
            add(match)

    for style_tag in soup.find_all("style"):
        if style_tag.string:
            for match in CSS_URL_PATTERN.findall(style_tag.string):
                add(match)

    for meta in soup.find_all("meta", property=True):
        prop = meta.get("property", "").lower()
        if prop in {"og:image", "og:image:url", "twitter:image"}:
            add(meta.get("content"))

    return sorted(found)


def guess_extension(url: str, content_type: str | None) -> str:
    path = urlparse(url).path
    ext = Path(unquote(path)).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return ext

    if content_type:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if ext:
            if ext == ".jpe":
                return ".jpg"
            return ext

    return ".jpg"


def make_filename(url: str, index: int, content_type: str | None) -> str:
    path = urlparse(url).path
    basename = Path(unquote(path)).name
    if basename and "." in basename:
        name = basename
        if len(name) > 120:
            stem = Path(name).stem[:80]
            suffix = Path(name).suffix
            name = f"{stem}{suffix}"
        return f"{index:03d}_{name}"

    ext = guess_extension(url, content_type)
    url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
    return f"{index:03d}_{url_hash}{ext}"


def download_image(url: str, output_dir: Path, index: int, timeout: int) -> str | None:
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            stream=True,
        )
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "")
        if content_type and not content_type.startswith("image/"):
            if "svg" in content_type:
                pass
            elif "octet-stream" not in content_type:
                print(f"  跳过（非图片）: {url} [{content_type}]")
                return None

        filename = make_filename(url, index, content_type)
        filepath = output_dir / filename

        if filepath.exists():
            stem = filepath.stem
            suffix = filepath.suffix
            filepath = output_dir / f"{stem}_{index}{suffix}"

        with open(filepath, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        return str(filepath)
    except requests.RequestException as exc:
        print(f"  下载失败: {url} ({exc})")
        return None


def default_output_dir(url: str) -> Path:
    parsed = urlparse(url)
    host = parsed.netloc.replace(":", "_") or "images"
    return Path("downloads") / host


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从网页下载所有图片到本地",
    )
    parser.add_argument("url", help="目标网页 URL")
    parser.add_argument(
        "-o",
        "--output",
        help="保存目录（默认: downloads/<域名>）",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=int,
        default=30,
        help="请求超时秒数（默认: 30）",
    )
    args = parser.parse_args()

    url = args.url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    output_dir = Path(args.output) if args.output else default_output_dir(url)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"正在获取页面: {url}")
    try:
        html = fetch_page(url, args.timeout)
    except requests.RequestException as exc:
        print(f"获取页面失败: {exc}", file=sys.stderr)
        return 1

    image_urls = extract_image_urls(html, url)
    if not image_urls:
        print("未在页面中找到图片。")
        return 0

    print(f"找到 {len(image_urls)} 张图片，保存到: {output_dir.resolve()}")
    success = 0
    for i, image_url in enumerate(image_urls, start=1):
        print(f"[{i}/{len(image_urls)}] {image_url}")
        result = download_image(image_url, output_dir, i, args.timeout)
        if result:
            print(f"  已保存: {result}")
            success += 1

    print(f"\n完成: 成功 {success}/{len(image_urls)}")
    return 0 if success > 0 or len(image_urls) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
