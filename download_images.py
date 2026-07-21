#!/usr/bin/env python3
"""Download all images from a webpage."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup
from requests.exceptions import ProxyError, Timeout

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".ico", ".avif"}
CSS_URL_PATTERN = re.compile(r'url\(["\']?(.*?)["\']?\)', re.IGNORECASE)
RAW_IMAGE_URL_PATTERN = re.compile(
    r"""(?:https?:)?(?:\\?/\\?/|//)[^\s"'<>\\]+?\.(?:jpg|jpeg|png|gif|webp|bmp|avif)"""
    r"""(?:\?[^\s"'<>\\]*)?""",
    re.IGNORECASE,
)
# Extensionless image endpoints common in CDN / Next.js apps, e.g. .../uuid/cover
EXTENSIONLESS_IMAGE_URL_PATTERN = re.compile(
    r"""(?:https?:)?(?:\\?/\\?/|//)[^\s"'<>\\]+"""
    r"""/(?:cover|thumbnail|thumb|avatar|icon|image|img|photo|banner|poster|og)"""
    r"""(?:\?[^\s"'<>\\]*)?""",
    re.IGNORECASE,
)
ATTR_SRC_PATTERN = re.compile(
    r"""(?i)(?:src|data-src|data-original|data-lazy-src|data-url|href)\s*=\s*["']([^"']+)["']"""
)
BAIDU_IMG_HOST_PATTERN = re.compile(
    r'https?://(?:img\d*\.baidu\.com|t\d+\.baidu\.com|hiphotos\.baidu\.com)[^\s"\'<>\\]+',
    re.IGNORECASE,
)
ZHIMG_URL_PATTERN = re.compile(
    r'https?://(?:pic[0-9]|picx|pica|pic1|pic2)\.zhimg\.com/[^\s"\'<>\\]+',
    re.IGNORECASE,
)
DEFAULT_WORKERS = 8
DEFAULT_RETRY_TIMES = 3
BAIDU_DEFAULT_WORDS = ("风景", "壁纸", "美女", "汽车", "美食")


def build_headers(
    referer: str | None = None,
    for_page: bool = False,
    cookie: str | None = None,
) -> dict[str, str]:
    if for_page:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
    else:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }
    if referer:
        headers["Referer"] = referer
    if cookie:
        headers["Cookie"] = cookie
    return headers


def parse_cookie_header(cookie: str | None) -> dict[str, str]:
    if not cookie:
        return {}
    pairs: dict[str, str] = {}
    for part in cookie.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name:
            pairs[name] = value.strip()
    return pairs


def is_zhihu_site(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host.endswith("zhihu.com")


def request_get_with_proxy_fallback(
    url: str,
    timeout: int,
    stream: bool = False,
    referer: str | None = None,
    for_page: bool = False,
    cookie: str | None = None,
) -> requests.Response:
    """Try normal request first, then retry without env proxy if needed."""
    headers = build_headers(referer, for_page=for_page, cookie=cookie)
    try:
        return requests.get(
            url,
            headers=headers,
            timeout=timeout,
            stream=stream,
        )
    except ProxyError:
        session = requests.Session()
        session.trust_env = False
        return session.get(
            url,
            headers=headers,
            timeout=timeout,
            stream=stream,
        )


def fetch_page(url: str, timeout: int, cookie: str | None = None) -> str:
    referer = None
    if is_zhihu_site(url):
        referer = "https://www.zhihu.com/"
    response = request_get_with_proxy_fallback(
        url=url,
        timeout=timeout,
        for_page=True,
        referer=referer,
        cookie=cookie,
    )
    if response.status_code == 403 and is_zhihu_site(url):
        hint = (
            "知乎拒绝访问（403）。请在浏览器登录知乎后，把 Cookie 粘贴到页面的 Cookie 输入框再试。"
            "获取方式：打开该文章 → F12 → Network → 刷新 → 点选文档请求 → Request Headers 里复制 cookie。"
        )
        if not cookie:
            raise requests.HTTPError(hint, response=response)
        raise requests.HTTPError(
            hint + " 当前 Cookie 可能已过期，请重新复制。",
            response=response,
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


def looks_like_image_url(url: str) -> bool:
    lowered = url.lower()
    path = urlparse(lowered).path
    if any(path.endswith(ext) for ext in IMAGE_EXTENSIONS):
        return True
    if re.search(
        r"/(?:cover|thumbnail|thumb|avatar|icon|image|img|photo|banner|poster|og)(?:/|$|\?)",
        lowered,
    ):
        return True
    if "/_next/image" in lowered or "format=image" in lowered or "mime=image" in lowered:
        return True
    if "fm=253" in lowered or "f=jpeg" in lowered or "f=webp" in lowered:
        return True
    return False


def unwrap_next_image_url(url: str) -> str:
    """Expand Next.js /_next/image?url=... back to the original image URL."""
    parsed = urlparse(url)
    if "/_next/image" not in parsed.path:
        return url
    query = parse_qs(parsed.query)
    raw = query.get("url", [None])[0]
    if not raw:
        return url
    decoded = unquote(raw)
    if decoded.startswith("/"):
        return f"{parsed.scheme}://{parsed.netloc}{decoded}"
    if decoded.startswith(("http://", "https://")):
        return decoded
    return url


def normalize_embedded_url(raw_url: str) -> str | None:
    if not raw_url:
        return None
    url = raw_url.strip().strip("\\\"'")
    url = url.replace(r"\/", "/").replace(r"\u0026", "&").replace("&amp;", "&")
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith(("http://", "https://")):
        return None
    # Drop obvious non-image assets even if extension matched poorly.
    lowered = url.lower()
    if any(
        lowered.endswith(ext)
        for ext in (".css", ".js", ".mp4", ".mp3", ".m3u8", ".woff", ".woff2", ".ttf", ".eot", ".html", ".htm")
    ):
        return None
    url = unwrap_next_image_url(url)
    return url


def extract_images_with_playwright(
    page_url: str,
    timeout: int = 30,
    wait_ms: int = 8000,
    scroll_rounds: int = 12,
    cookie: str | None = None,
) -> list[str]:
    """Render page in headless Chromium and collect image URLs from DOM."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "当前页面是 JS 动态加载，需要安装 playwright："
            "pip install playwright && python -m playwright install chromium"
        ) from exc

    timeout_ms = max(timeout, 1) * 1000
    cookie_pairs = parse_cookie_header(cookie)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 1600},
            locale="zh-CN",
        )
        if cookie_pairs:
            host = urlparse(page_url).hostname or ""
            context.add_cookies(
                [
                    {
                        "name": name,
                        "value": value,
                        "domain": "." + ".".join(host.split(".")[-2:]) if host.count(".") >= 1 else host,
                        "path": "/",
                    }
                    for name, value in cookie_pairs.items()
                ]
            )
        page = context.new_page()
        try:
            page.goto(page_url, wait_until="networkidle", timeout=timeout_ms)
        except Exception:
            page.goto(page_url, wait_until="domcontentloaded", timeout=timeout_ms)

        page.wait_for_timeout(min(wait_ms, timeout_ms))
        try:
            page.wait_for_selector(
                'img[src*="cover"], img[src*="catai.wiki"], img[src*="zhimg.com"], img[src*="static."]',
                timeout=timeout_ms,
            )
        except Exception:
            pass

        # Scroll to trigger lazy-loaded images.
        for _ in range(max(1, scroll_rounds)):
            page.evaluate("window.scrollBy(0, Math.max(800, window.innerHeight))")
            page.wait_for_timeout(700)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1200)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(500)

        urls = page.evaluate(
            """() => {
              const out = new Set();
              for (const img of document.querySelectorAll('img')) {
                const candidates = [
                  img.currentSrc,
                  img.src,
                  img.getAttribute('data-src'),
                  img.getAttribute('data-original'),
                  img.getAttribute('data-actualsrc'),
                  img.getAttribute('data-lazy-src'),
                ];
                for (const value of candidates) {
                  if (value && !value.startsWith('data:')) out.add(value);
                }
                const srcset = img.getAttribute('srcset');
                if (srcset) {
                  for (const part of srcset.split(',')) {
                    const u = part.trim().split(' ')[0];
                    if (u) out.add(u);
                  }
                }
              }
              for (const el of document.querySelectorAll('[style*="background"]')) {
                const style = el.getAttribute('style') || '';
                const match = style.match(/url\\((['"]?)(.*?)\\1\\)/i);
                if (match && match[2] && !match[2].startsWith('data:')) out.add(match[2]);
              }
              return [...out];
            }"""
        )
        browser.close()

    absolute: set[str] = set()
    for raw in urls or []:
        if not isinstance(raw, str):
            continue
        normalized = normalize_embedded_url(urljoin(page_url, raw.strip()))
        if normalized:
            absolute.add(normalized)
    return sorted(absolute)


def extract_raw_image_urls(html: str, base_url: str | None = None) -> set[str]:
    found: set[str] = set()

    def add(raw: str | None, require_image_shape: bool = False) -> None:
        if not raw:
            return
        if raw.startswith("data:"):
            return
        absolute = urljoin(base_url, raw) if base_url else raw
        url = normalize_embedded_url(absolute)
        if not url:
            return
        if require_image_shape and not looks_like_image_url(url):
            return
        found.add(url)

    for match in RAW_IMAGE_URL_PATTERN.findall(html):
        add(match)
    for match in EXTENSIONLESS_IMAGE_URL_PATTERN.findall(html):
        add(match)
    for match in BAIDU_IMG_HOST_PATTERN.findall(html):
        add(match)
    for match in ZHIMG_URL_PATTERN.findall(html):
        add(match)
    # Catch attribute values even when URL has no file extension.
    for match in ATTR_SRC_PATTERN.findall(html):
        add(match, require_image_shape=True)

    return found


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
        add(img.get("data-actualsrc"))
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

    # JS-heavy sites often embed image URLs as plain strings / attributes.
    found.update(extract_raw_image_urls(html, base_url=base_url))

    return sorted(found)


def is_baidu_image_site(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host == "image.baidu.com" or host.endswith(".image.baidu.com")


def baidu_search_word(url: str) -> str | None:
    query = parse_qs(urlparse(url).query)
    for key in ("word", "wd", "queryWord"):
        values = query.get(key)
        if values and values[0].strip():
            return values[0].strip()
    return None


def fetch_baidu_acjson_images(word: str, timeout: int, session: requests.Session, rn: int = 60) -> list[str]:
    q = quote(word)
    api = (
        "https://image.baidu.com/search/acjson"
        "?tn=resultjson_com&logid=1&ipn=rj&ct=201326592&is=&fp=result&fr="
        f"&word={q}&queryWord={q}&cl=2&lm=-1&ie=utf-8&oe=utf-8&adpicid=&st=-1"
        "&z=&ic=0&hd=&latest=&copyright=&s=&se=&tab=&width=&height=&face=0"
        f"&istype=2&qc=&nc=1&expermode=&nojc=&istype=2&pn=0&rn={rn}&gsm=1e"
    )
    headers = build_headers("https://image.baidu.com/")
    headers.update(
        {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
        }
    )
    response = session.get(api, headers=headers, timeout=timeout)
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError:
        # Some responses contain slightly invalid JSON; try a soft parse.
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError:
            return []

    if isinstance(payload, dict) and payload.get("antiFlag"):
        return []

    urls: list[str] = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        for key in ("middleURL", "hoverURL", "thumbURL", "replaceUrl"):
            value = item.get(key)
            if isinstance(value, str) and value.startswith("http"):
                urls.append(value)
                break
    return urls


def extract_baidu_image_urls(page_url: str, html: str, timeout: int) -> list[str]:
    """Baidu Image is JS-rendered; pull from HTML strings + acjson API."""
    found: set[str] = set(extract_raw_image_urls(html))

    session = requests.Session()
    # Seed cookies first; Baidu often blocks anonymous API calls.
    try:
        session.get(
            "https://image.baidu.com/",
            headers=build_headers("https://image.baidu.com/"),
            timeout=timeout,
        )
    except requests.RequestException:
        pass

    words: list[str] = []
    word = baidu_search_word(page_url)
    if word:
        words.append(word)
    else:
        words.extend(BAIDU_DEFAULT_WORDS)

    for item in words:
        try:
            found.update(fetch_baidu_acjson_images(item, timeout=timeout, session=session))
        except requests.RequestException:
            continue

    return sorted(found)


def is_placeholder_image(url: str) -> bool:
    lowered = url.lower()
    name = urlparse(lowered).path.rsplit("/", 1)[-1]
    if "fyloading" in lowered or "placeholder" in lowered or "spacer" in lowered:
        return True
    if "ads.trafficjunky" in lowered or "doubleclick" in lowered:
        return True
    if name in {"loading.gif", "loading.png", "blank.gif", "pixel.gif"}:
        return True
    return False


def meaningful_image_urls(urls: list[str]) -> list[str]:
    return [u for u in urls if not is_placeholder_image(u)]


def should_use_browser_render(page_url: str, static_urls: list[str]) -> bool:
    host = urlparse(page_url).netloc.lower()
    meaningful = meaningful_image_urls(static_urls)
    if host.endswith(("aiaha.xyz", "catai.wiki")):
        return True
    if is_zhihu_site(page_url) and len(meaningful) <= 2:
        return True
    if len(meaningful) <= 2:
        return True
    return False


def collect_image_urls(
    page_url: str,
    html: str,
    timeout: int,
    cookie: str | None = None,
) -> tuple[list[str], str]:
    """Return (urls, source_label)."""
    if is_baidu_image_site(page_url):
        return meaningful_image_urls(extract_baidu_image_urls(page_url, html, timeout)), "baidu-api"

    static_urls = extract_image_urls(html, page_url)
    if not should_use_browser_render(page_url, static_urls):
        return meaningful_image_urls(static_urls), "html"

    try:
        rendered_urls = extract_images_with_playwright(
            page_url,
            timeout=timeout,
            cookie=cookie,
        )
    except Exception as exc:  # noqa: BLE001
        return meaningful_image_urls(static_urls), f"html(browser-failed:{exc})"

    merged = sorted(set(static_urls) | set(rendered_urls))
    cleaned = meaningful_image_urls(merged)
    if meaningful_image_urls(rendered_urls):
        return cleaned, "browser"
    return cleaned, "html+browser"


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


def is_timeout_error(exc: Exception) -> bool:
    if isinstance(exc, Timeout):
        return True
    lowered = str(exc).lower()
    return "timed out" in lowered or "timeout" in lowered


def format_download_error(exc: Exception, retry_times: int = 0) -> str:
    text = str(exc)
    lowered = text.lower()
    if "403" in lowered:
        return "HTTP 403（防盗链/权限拒绝）"
    if "404" in lowered:
        return "HTTP 404（图片不存在）"
    if "401" in lowered:
        return "HTTP 401（需要登录）"
    if "nameresolutionerror" in lowered or "nodename nor servname" in lowered:
        return "DNS 解析失败"
    if "timed out" in lowered or "timeout" in lowered:
        if retry_times > 0:
            return f"请求超时（已重试 {retry_times} 次）"
        return "请求超时"
    if "proxyerror" in lowered:
        return "代理连接失败"
    if hasattr(exc, "response") and getattr(exc, "response", None) is not None:
        status = getattr(exc.response, "status_code", None)
        if status:
            return f"HTTP {status}"
    return text[:120]


def download_image(
    url: str,
    output_dir: Path,
    index: int,
    timeout: int,
    referer: str | None = None,
    retry_times: int = DEFAULT_RETRY_TIMES,
    cookie: str | None = None,
) -> tuple[str | None, str | None]:
    """Return (saved_path, error_reason). Exactly one of them is set."""
    last_exc: requests.RequestException | None = None

    for attempt in range(retry_times + 1):
        try:
            response = request_get_with_proxy_fallback(
                url=url,
                timeout=timeout,
                stream=True,
                referer=referer,
                cookie=cookie,
            )
            response.raise_for_status()

            content_type = response.headers.get("Content-Type", "")
            if content_type and not content_type.startswith("image/"):
                if "svg" in content_type:
                    pass
                elif "octet-stream" not in content_type:
                    reason = f"非图片类型（{content_type.split(';')[0]}）"
                    print(f"  跳过: {url} [{reason}]")
                    return None, reason

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

            return str(filepath), None
        except requests.RequestException as exc:
            last_exc = exc
            if is_timeout_error(exc) and attempt < retry_times:
                print(f"  超时重试 ({attempt + 1}/{retry_times}): {url}")
                continue
            break

    reason = format_download_error(
        last_exc or RuntimeError("未知错误"),
        retry_times=retry_times if last_exc and is_timeout_error(last_exc) else 0,
    )
    print(f"  下载失败: {url} ({reason})")
    return None, reason


def default_output_dir() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path("downloads") / f"default-{stamp}"


def normalize_page_url(url: str) -> str:
    normalized_url = url.strip()
    if not normalized_url.startswith(("http://", "https://")):
        normalized_url = "https://" + normalized_url
    return normalized_url


def iter_download(
    url: str,
    output: str | None = None,
    timeout: int = 30,
    workers: int = DEFAULT_WORKERS,
    retry_times: int = DEFAULT_RETRY_TIMES,
    cookie: str | None = None,
):
    """Yield progress events while downloading images."""
    normalized_url = normalize_page_url(url)
    output_dir = Path(output) if output else default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    resolved_output = str(output_dir.resolve())
    cookie = (cookie or "").strip() or None

    if is_zhihu_site(normalized_url) and not cookie:
        yield {
            "type": "status",
            "message": "检测到知乎链接：未提供 Cookie 时很容易被 403 拦截，建议先粘贴浏览器 Cookie。",
            "output_dir": resolved_output,
        }

    yield {
        "type": "status",
        "message": f"正在获取页面: {normalized_url}",
        "output_dir": resolved_output,
    }

    html = fetch_page(normalized_url, timeout, cookie=cookie)
    yield {
        "type": "status",
        "message": "正在解析图片（必要时会用浏览器渲染 JS 页面）...",
        "output_dir": resolved_output,
    }
    image_urls, source = collect_image_urls(
        normalized_url,
        html,
        timeout,
        cookie=cookie,
    )
    total = len(image_urls)

    yield {
        "type": "found",
        "message": (
            f"找到 {total} 张图片，保存到: {resolved_output} "
            f"（来源: {source}，{workers} 线程，超时重试 {retry_times} 次）"
        ),
        "total": total,
        "output_dir": resolved_output,
        "url": normalized_url,
    }

    parsed = urlparse(normalized_url)
    referer = f"{parsed.scheme}://{parsed.netloc}/"

    if total == 0:
        result = {
            "url": normalized_url,
            "output_dir": resolved_output,
            "total_found": 0,
            "success_count": 0,
            "fail_count": 0,
            "saved_files": [],
            "failures": [],
        }
        yield {"type": "done", "message": "未在页面中找到图片。", "result": result}
        return

    saved_files: list[str] = []
    failures: list[dict[str, str]] = []
    stats_lock = threading.Lock()
    completed = 0

    def download_task(index: int, image_url: str) -> tuple[int, str, str | None, str | None]:
        saved_path, error = download_image(
            image_url,
            output_dir,
            index,
            timeout,
            referer=referer,
            retry_times=retry_times,
            cookie=cookie,
        )
        return index, image_url, saved_path, error

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        future_map = {}
        for i, image_url in enumerate(image_urls, start=1):
            yield {
                "type": "item_start",
                "current": i,
                "total": total,
                "success": len(saved_files),
                "fail": len(failures),
                "url": image_url,
                "message": f"[{i}/{total}] 排队下载: {image_url}",
            }
            future_map[executor.submit(download_task, i, image_url)] = (i, image_url)

        for future in as_completed(future_map):
            index, image_url, saved_path, error = future.result()
            with stats_lock:
                completed += 1
                current = completed
                if saved_path:
                    saved_files.append(saved_path)
                else:
                    failures.append({"url": image_url, "reason": error or "未知错误"})

            if saved_path:
                yield {
                    "type": "item_ok",
                    "current": current,
                    "total": total,
                    "success": len(saved_files),
                    "fail": len(failures),
                    "url": image_url,
                    "path": saved_path,
                    "message": f"[{index}/{total}] 已保存: {saved_path}",
                }
            else:
                reason = error or "未知错误"
                yield {
                    "type": "item_fail",
                    "current": current,
                    "total": total,
                    "success": len(saved_files),
                    "fail": len(failures),
                    "url": image_url,
                    "reason": reason,
                    "message": f"[{index}/{total}] 失败: {reason} | {image_url}",
                }

    saved_files.sort()

    result = {
        "url": normalized_url,
        "output_dir": resolved_output,
        "total_found": total,
        "success_count": len(saved_files),
        "fail_count": len(failures),
        "saved_files": saved_files,
        "failures": failures,
    }
    yield {
        "type": "done",
        "message": (
            f"完成：找到 {total} 张，成功 {len(saved_files)} 张，失败 {len(failures)} 张。"
        ),
        "result": result,
    }


def run_download(
    url: str,
    output: str | None = None,
    timeout: int = 30,
    workers: int = DEFAULT_WORKERS,
    retry_times: int = DEFAULT_RETRY_TIMES,
    cookie: str | None = None,
) -> dict:
    result = None
    for event in iter_download(
        url=url,
        output=output,
        timeout=timeout,
        workers=workers,
        retry_times=retry_times,
        cookie=cookie,
    ):
        if event.get("type") == "done":
            result = event["result"]
    if result is None:
        raise RuntimeError("下载流程未正常结束")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从网页下载所有图片到本地",
    )
    parser.add_argument("url", help="目标网页 URL")
    parser.add_argument(
        "-o",
        "--output",
        help="保存目录（默认: downloads/default-当前时间）",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=int,
        default=30,
        help="请求超时秒数（默认: 30）",
    )
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"并发下载线程数（默认: {DEFAULT_WORKERS}）",
    )
    parser.add_argument(
        "-r",
        "--retries",
        type=int,
        default=DEFAULT_RETRY_TIMES,
        help=f"超时重试次数（默认: {DEFAULT_RETRY_TIMES}）",
    )
    parser.add_argument(
        "-c",
        "--cookie",
        default="",
        help="可选 Cookie（知乎等站点防爬时需要）",
    )
    args = parser.parse_args()

    print(f"正在获取页面: {args.url}")
    try:
        result = run_download(
            args.url,
            args.output,
            args.timeout,
            workers=args.workers,
            retry_times=args.retries,
            cookie=args.cookie or None,
        )
    except requests.RequestException as exc:
        print(f"获取页面失败: {exc}", file=sys.stderr)
        return 1

    if result["total_found"] == 0:
        print("未在页面中找到图片。")
        return 0

    print(f"找到 {result['total_found']} 张图片，保存到: {result['output_dir']}")
    for index, file_path in enumerate(result["saved_files"], start=1):
        print(f"  [{index}] 已保存: {file_path}")

    if result["failures"]:
        print(f"\n失败 {result['fail_count']} 张：")
        for item in result["failures"]:
            print(f"  - {item['reason']}: {item['url']}")

    print(f"\n完成: 成功 {result['success_count']}/{result['total_found']}")
    return 0 if result["success_count"] > 0 or result["total_found"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
