#!/usr/bin/env python3
"""Archive the public 7gogo talk page and its posts using HTTP requests."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import html.parser
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, urlencode, urljoin, urlparse

import requests


SITE_URL = "https://7gogo.jp"
PUBLIC_API_ROOT = "https://api.7gogo.jp"
API_ROOT = "https://api.7gogo.jp/web/v2"
DEFAULT_TALK_ID = "nishino-nanase"
USER_API_URL = "https://api.7gogo.jp/web/v2/users"
ARCHIVES_ROOT = Path(__file__).resolve().with_name("archives")
TALKS_MAPPING_PATH = ARCHIVES_ROOT / "talks.json"
DEFAULT_USER_AVATAR_URL = (
    "https://stat.7gogo.jp/appimg_images/20200620/21/01/GQ/j/o01800180p.jpg"
)
LOCAL_DEFAULT_USER_AVATAR = "/data/images/default_user.jpg"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)
STAT_IMAGE_URL_RE = re.compile(r"https?://stat\.7gogo\.jp/[^\s\"'<>\\]+", re.IGNORECASE)
MOVIE_URL_RE = re.compile(r"https?://moviestat\.7gogo\.jp/[^\s\"'<>\\]+", re.IGNORECASE)
MOVIE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])output/[^\s\"'<>\\]+")
ARCHIVE_IMAGE_EXTENSIONS = {".json", ".html", ".htm", ".css", ".js", ".m3u8"}
LOCAL_IMAGE_PATH_RE = re.compile(r"(?:\.\./)+appimg_images/")
MOVIE_HOST = "moviestat.7gogo.jp"
MOVIE_BASE_URL = f"https://{MOVIE_HOST}/"


class ResourceParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("src"):
            self.urls.append(values["src"])
        elif tag == "link" and values.get("href"):
            rel = {part.lower() for part in (values.get("rel") or "").split()}
            href = values["href"]
            if (
                "stylesheet" in rel
                or "manifest" in rel
                or href.lower().split("?", 1)[0].endswith(".css")
            ):
                self.urls.append(href)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="output directory (default: archives/<talk-slug>)",
    )
    parser.add_argument("--delay", type=float, default=0.5, help="delay between API requests")
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="HTTP request timeout in seconds (default: 300)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="parallel workers for API phases (default: 4)",
    )
    parser.add_argument(
        "--phase",
        choices=("base", "details", "comments", "good", "retalks", "users", "support", "images", "videos", "all"),
        default="base",
        help="archive phase to run (default: base)",
    )
    parser.add_argument(
        "--talk-id",
        "--talk",
        dest="talk_ref",
        default=DEFAULT_TALK_ID,
        help="talk slug or full 7gogo URL (default: nishino-nanase)",
    )
    parser.add_argument(
        "--post-id",
        action="append",
        dest="post_ids",
        help="only process these postIds; may be repeated or comma-separated",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="overwrite existing JSON files",
    )
    parser.add_argument(
        "--refresh-base",
        action="store_true",
        help="with --phase all, refresh the page/resources/posts before detail scraping",
    )
    parser.add_argument(
        "--comment-probe-limit",
        type=int,
        default=2,
        help="limit used by the first comment request (default: 2)",
    )
    parser.add_argument(
        "--follower-max-pages",
        type=int,
        default=10,
        help="stop follower scraping after this many pages and resume later (default: 10; 0 = all)",
    )
    return parser.parse_args()


def normalize_talk_ref(value: str) -> tuple[str, str]:
    """Return the URL slug and page URL from a slug or a 7gogo URL."""
    value = value.strip()
    if not value:
        raise RuntimeError("talk ID or URL must not be empty")
    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        if parsed.netloc != "7gogo.jp":
            raise RuntimeError(f"unsupported talk URL host: {parsed.netloc}")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 1:
            raise RuntimeError(f"expected a talk URL like https://7gogo.jp/nishino-nanase: {value}")
        slug = parts[0]
    else:
        slug = value.strip("/")
        if "/" in slug:
            raise RuntimeError(f"expected a talk slug or full 7gogo URL: {value}")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", slug):
        raise RuntimeError(f"invalid talk slug: {slug}")
    return slug, f"{SITE_URL}/{slug}"


class HttpClient:
    def __init__(self, cookie_jar: Path | None = None, timeout: int = 60) -> None:
        self.cookie_jar = cookie_jar
        self.timeout = timeout
        self.session = requests.Session()

    def get(self, url: str, headers: list[str] | None = None) -> bytes:
        request_headers: dict[str, str] = {}
        for header in headers or []:
            name, separator, value = header.partition(":")
            if separator:
                request_headers[name.strip()] = value.strip()
        try:
            response = self.session.get(
                url,
                headers=request_headers,
                timeout=self.timeout,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"request failed for {url}: {exc}") from exc
        if response.status_code >= 400:
            detail = response.text.strip()[:500]
            raise RuntimeError(
                f"HTTP {response.status_code} for {url}: {detail or response.reason}"
            )
        return response.content

    def clone(self, cookie_jar: Path | None = None) -> "HttpClient":
        return HttpClient(cookie_jar, timeout=self.timeout)


def extract_dehydrated_states(page: bytes) -> dict:
    text = page.decode("utf-8")
    marker = re.search(r"window\.__DEHYDRATED_STATES__\s*=\s*", text)
    if not marker:
        raise RuntimeError("window.__DEHYDRATED_STATES__ was not found in the page")
    try:
        states, _ = json.JSONDecoder().raw_decode(text[marker.end() :])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"could not decode dehydrated state: {exc}") from exc
    return states


def talk_context_from_states(states: dict, fallback_slug: str) -> dict[str, str]:
    talk = states.get("page:talk:service:entity:talk") or {}
    custom = states.get("page:talk:service:entity:talkCustom") or {}
    owner = talk.get("owner") or {}
    members = states.get("page:talk:service:entity:talkMembers") or {}
    member_list = members.get("members") or []
    member_owner = member_list[0] if member_list else {}
    slug = str(talk.get("screenName") or fallback_slug)
    talk_id = str(custom.get("talkId") or slug)
    user_id = str(owner.get("userId") or member_owner.get("userId") or "")
    if not user_id:
        raise RuntimeError("the page did not contain a talk owner userId")
    return {
        "talkId": talk_id,
        "userId": user_id,
        "slug": slug,
        "url": f"{SITE_URL}/{slug}",
    }


def save_talk_mapping(context: dict[str, str]) -> None:
    path = TALKS_MAPPING_PATH
    mapping: dict[str, dict[str, str]] = {}
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            mapping = {str(key): value for key, value in existing.items() if isinstance(value, dict)}
    mapping[context["talkId"]] = context
    write_json(path, mapping)


def local_resource_path(url: str) -> Path:
    parsed = urlparse(url)
    host = parsed.netloc or "same-origin"
    path = parsed.path.lstrip("/") or "index"
    if parsed.query:
        path += "__query_" + re.sub(r"[^A-Za-z0-9._-]+", "_", parsed.query)
    if parsed.netloc and parsed.netloc != "7gogo.jp":
        return Path("external") / host / path
    return Path(path)


def save_resources(
    client: HttpClient, page: bytes, output: Path, page_url: str
) -> list[dict[str, object]]:
    resource_root = output
    original_root = output / "original"
    parser = ResourceParser()
    parser.feed(page.decode("utf-8", errors="replace"))
    urls = list(dict.fromkeys(urljoin(page_url, value) for value in parser.urls))
    manifest: list[dict[str, object]] = []
    for url in urls:
        relative = local_resource_path(url)
        destination = resource_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        record: dict[str, object] = {"url": url, "path": str(relative)}
        try:
            content = client.get(url, [f"User-Agent: {USER_AGENT}", "Accept: */*"])
            destination.write_bytes(content)
            backup = original_root / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(content)
            record["saved"] = True
            print(f"saved resource {relative}")
        except Exception as exc:  # Keep third-party resources from aborting the archive.
            record["saved"] = False
            record["error"] = str(exc)
            print(f"warning: could not save {url}: {exc}", file=sys.stderr)
        manifest.append(record)
    return manifest


def normalized_stat_image_urls(text: str) -> set[str]:
    normalized = html.unescape(text).replace("\\/", "/").replace("\\u002F", "/")
    return {
        match.group(0).rstrip(".,;:!?)]}")
        for match in STAT_IMAGE_URL_RE.finditer(normalized)
    }


def image_url_variants(url: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                url,
                url.replace("/", r"\/"),
                url.replace("/", r"\u002F"),
            )
        )
    )


def archive_image_path(url: str) -> Path:
    path = urlparse(url).path.lstrip("/")
    if not path or any(part in {".", ".."} for part in path.split("/")):
        raise RuntimeError(f"unsafe image path: {url}")
    return Path(path)


def collect_archive_image_references(
    output: Path,
) -> tuple[dict[Path, set[str]], dict[Path, str]]:
    references: dict[Path, set[str]] = {}
    image_urls: dict[Path, str] = {}
    for path in output.rglob("*"):
        if (
            not path.is_file()
            or "original" in path.parts
            or path.name == "dehydrated_states.json"
            or path.suffix.lower() not in ARCHIVE_IMAGE_EXTENSIONS
        ):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        urls = normalized_stat_image_urls(text)
        if not urls:
            continue
        for url in urls:
            image_path = archive_image_path(url)
            references.setdefault(path, set()).add(url)
            image_urls.setdefault(image_path, url)
    return references, image_urls


def normalize_archive_json_image_paths(output: Path) -> None:
    """Use stable talk-root paths for local images in saved JSON files."""
    prefix = f"/{output.name}/"
    for path in output.rglob("*.json"):
        if "original" in path.parts or path.name == "dehydrated_states.json":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        normalized = LOCAL_IMAGE_PATH_RE.sub(prefix + "appimg_images/", text)
        normalized = re.sub(
            r'(?<![A-Za-z0-9_/-])appimg_images/',
            prefix + "appimg_images/",
            normalized,
        )
        if normalized != text:
            path.write_text(normalized, encoding="utf-8")


def normalize_local_asset_paths(output: Path) -> None:
    """Make local image and video paths independent of the current SPA route."""
    prefix = f"/{output.name}/"
    for path in output.rglob("*"):
        if (
            not path.is_file()
            or "original" in path.parts
            or path.name == "dehydrated_states.json"
            or path.suffix.lower() not in ARCHIVE_IMAGE_EXTENSIONS
        ):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        normalized = re.sub(
            r'(?<![A-Za-z0-9_/-])(?:\.\./)*appimg_images/',
            prefix + "appimg_images/",
            text,
        )
        normalized = re.sub(
            r'(?<![A-Za-z0-9_/-])output/',
            prefix + "output/",
            normalized,
        )
        if normalized != text:
            path.write_text(normalized, encoding="utf-8")


def normalize_hls_playlist_paths(output: Path) -> None:
    """Use talk-root URLs for HLS segment references as well as playlists."""
    prefix = f"/{output.name}/"
    root = output.resolve()
    for path in output.rglob("*.m3u8"):
        if "original" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        changed = False
        lines: list[str] = []
        for line in text.splitlines(keepends=True):
            ending = ""
            value = line.rstrip("\r\n")
            if line.endswith("\r\n"):
                ending = "\r\n"
            elif line.endswith(("\r", "\n")):
                ending = line[-1]
            stripped = value.strip()
            if stripped and not stripped.startswith("#") and not stripped.startswith(("http://", "https://", "/")):
                child = (path.parent / stripped).resolve()
                if os.path.commonpath((str(root), str(child))) == str(root) and child.is_file():
                    value = value.replace(stripped, prefix + child.relative_to(root).as_posix())
                    changed = True
            lines.append(value + ending)
        if changed:
            path.write_text("".join(lines), encoding="utf-8")


def save_archive_images(
    client: HttpClient,
    output: Path,
    overwrite: bool,
    workers: int,
) -> None:
    """Download referenced stat images and rewrite each file to local paths."""
    if workers < 1:
        raise RuntimeError("--workers must be at least 1")
    normalize_archive_json_image_paths(output)
    normalize_local_asset_paths(output)
    normalize_hls_playlist_paths(output)
    references, image_urls = collect_archive_image_references(output)
    if not image_urls:
        print(f"no external images found in {output}")
        return

    def download_one(item: tuple[Path, str]) -> str:
        image_path, url = item
        destination = output / image_path
        if destination.exists() and not overwrite:
            return f"skip image {image_path} (already exists)"
        worker_client = client.clone()
        content = worker_client.get(
            url,
            [
                f"User-Agent: {USER_AGENT}",
                "Accept: image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            ],
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return f"saved image {image_path}"

    failures: list[str] = []
    items = sorted(image_urls.items(), key=lambda item: str(item[0]))
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as executor:
        futures = {executor.submit(download_one, item): item[0] for item in items}
        for future in as_completed(futures):
            image_path = futures[future]
            try:
                print(future.result())
            except Exception as exc:
                failures.append(f"{image_path}: {exc}")
                print(f"error image {image_path}: {exc}", file=sys.stderr)
    if failures:
        raise RuntimeError("image failures:\n" + "\n".join(sorted(failures)))

    rewritten = 0
    for path, urls in references.items():
        text = path.read_text(encoding="utf-8", errors="ignore")
        original = text
        for url in urls:
            image_path = archive_image_path(url)
            relative = f"/{output.name}/{image_path.as_posix()}"
            for variant in image_url_variants(url):
                text = text.replace(variant, relative)
        if text != original:
            path.write_text(text, encoding="utf-8")
            rewritten += 1
    print(
        f"localized {len(image_urls)} images in {rewritten} files under {output}"
    )
    normalize_local_asset_paths(output)


def movie_url_variants(url: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                url,
                url.replace("/", r"\/"),
                url.replace("/", r"\u002F"),
            )
        )
    )


def movie_absolute_url(value: str, base_url: str | None = None) -> str | None:
    value = html.unescape(value).replace("\\/", "/").replace("\\u002F", "/")
    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        if parsed.netloc.lower() != MOVIE_HOST:
            return None
        return value
    if value.startswith("output/"):
        return urljoin(MOVIE_BASE_URL, value)
    if base_url:
        candidate = urljoin(base_url, value)
        parsed = urlparse(candidate)
        if parsed.netloc.lower() == MOVIE_HOST:
            return candidate
    return None


def archive_media_path(url: str) -> Path:
    parsed = urlparse(url)
    path = parsed.path.lstrip("/")
    if not path or any(part in {".", ".."} for part in path.split("/")):
        raise RuntimeError(f"unsafe movie path: {url}")
    if parsed.query:
        path += "__query_" + re.sub(r"[^A-Za-z0-9._-]+", "_", parsed.query)
    return Path(path)


def collect_archive_movie_references(
    output: Path,
) -> tuple[dict[Path, dict[str, str]], dict[str, Path]]:
    references: dict[Path, dict[str, str]] = {}
    movie_urls: dict[str, Path] = {}
    for path in output.rglob("*"):
        if (
            not path.is_file()
            or "original" in path.parts
            or path.name == "dehydrated_states.json"
            or path.suffix.lower() not in ARCHIVE_IMAGE_EXTENSIONS
        ):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        normalized = html.unescape(text).replace("\\/", "/").replace("\\u002F", "/")
        tokens = set(MOVIE_URL_RE.findall(normalized))
        tokens.update(MOVIE_PATH_RE.findall(normalized))
        for token in tokens:
            absolute = movie_absolute_url(token)
            if absolute is None:
                continue
            local_path = archive_media_path(absolute)
            # Relative output/... values are already local after a successful run.
            if token.startswith("output/") and (output / local_path).exists():
                continue
            references.setdefault(path, {})[token] = absolute
            movie_urls.setdefault(absolute, local_path)
    return references, movie_urls


def hls_uri_references(text: str) -> list[str]:
    uris: list[str] = []
    seen: set[str] = set()
    uri_attribute = re.compile(r'URI="([^"]+)"')
    for line in text.splitlines():
        stripped = line.strip()
        candidates = [] if not stripped or stripped.startswith("#") else [stripped]
        candidates.extend(match.group(1) for match in uri_attribute.finditer(line))
        for value in candidates:
            if value and value not in seen:
                seen.add(value)
                uris.append(value)
    return uris


def media_relative_path(output: Path, media_path: Path) -> str:
    return f"/{output.name}/{media_path.as_posix()}"


def save_archive_videos(
    client: HttpClient,
    output: Path,
    overwrite: bool,
    workers: int,
) -> None:
    """Download movie URLs, HLS playlists, segments, and video thumbnails."""
    if workers < 1:
        raise RuntimeError("--workers must be at least 1")
    normalize_local_asset_paths(output)
    normalize_hls_playlist_paths(output)
    references, initial_urls = collect_archive_movie_references(output)
    if not initial_urls:
        print(f"no external videos or video thumbnails found in {output}")
        return

    resources: dict[str, Path] = dict(initial_urls)
    pending: list[str] = list(initial_urls)
    processed: set[str] = set()

    def register(url: str) -> Path:
        absolute = movie_absolute_url(url)
        if absolute is None:
            raise RuntimeError(f"unsupported movie URL: {url}")
        media_path = resources.setdefault(absolute, archive_media_path(absolute))
        if absolute not in processed and absolute not in pending:
            pending.append(absolute)
        return media_path

    failures: list[str] = []
    while pending:
        batch = pending[:]
        pending.clear()

        def download_one(url: str) -> tuple[str, bytes]:
            destination = output / resources[url]
            if destination.exists() and not overwrite:
                return url, destination.read_bytes()
            content = client.clone().get(
                url,
                [f"User-Agent: {USER_AGENT}", "Accept: */*"],
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            return url, content

        with ThreadPoolExecutor(max_workers=min(workers, len(batch))) as executor:
            futures = {executor.submit(download_one, url): url for url in batch}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    _, content = future.result()
                    processed.add(url)
                    destination = output / resources[url]
                    print(f"saved video resource {resources[url]}")
                    try:
                        playlist = content.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    if not (
                        resources[url].suffix.lower() == ".m3u8"
                        or playlist.lstrip().startswith("#EXTM3U")
                    ):
                        continue
                    rewritten = playlist
                    for uri in hls_uri_references(playlist):
                        child_url = movie_absolute_url(uri, url)
                        if child_url is None:
                            continue
                        child_path = register(child_url)
                        relative = f"/{output.name}/{child_path.as_posix()}"
                        for variant in movie_url_variants(uri):
                            rewritten = rewritten.replace(variant, relative)
                    if rewritten != playlist:
                        destination.write_text(rewritten, encoding="utf-8")
                except Exception as exc:
                    failures.append(f"{resources.get(url, url)}: {exc}")
                    print(f"error video resource {url}: {exc}", file=sys.stderr)

    if failures:
        raise RuntimeError("video failures:\n" + "\n".join(sorted(failures)))

    rewritten_files = 0
    for path, tokens in references.items():
        text = path.read_text(encoding="utf-8", errors="ignore")
        original = text
        for token, absolute in tokens.items():
            relative = media_relative_path(output, resources[absolute])
            for variant in tuple(dict.fromkeys(movie_url_variants(token) + movie_url_variants(absolute))):
                text = text.replace(variant, relative)
        if text != original:
            path.write_text(text, encoding="utf-8")
            rewritten_files += 1
    print(
        f"localized {len(resources)} video resources in {rewritten_files} files under {output}"
    )
    normalize_local_asset_paths(output)
    normalize_hls_playlist_paths(output)


def localize_page(
    page: bytes, manifest: list[dict[str, object]], resource_prefix: str = ""
) -> bytes:
    """Replace resource URLs with paths that work when index.html is opened locally."""
    text = page.decode("utf-8")
    for record in manifest:
        if not record.get("saved"):
            continue
        url = str(record["url"])
        relative = resource_prefix + str(record["path"])
        parsed = urlparse(url)
        source = parsed.path if parsed.netloc == "7gogo.jp" else url
        text = text.replace(source, relative)
    text = text.replace('href="/manifest.json"', f'href="{resource_prefix}manifest.json"')
    text = text.replace(
        '"API_HOST":"https:\\u002F\\u002Fapi.7gogo.jp"', '"API_HOST":""'
    )
    text = text.replace('"API_HOST":"https://api.7gogo.jp"', '"API_HOST":""')
    text = re.sub(
        r'"MOVIE_STATS_HOST":"https:(?:\\u002F|/){2}[^"]*"',
        '"MOVIE_STATS_HOST":""',
        text,
    )
    text = re.sub(
        r'"WEBSOCKET_HOST":"wss:(?:\\u002F|/){2}[^"]*"',
        '"WEBSOCKET_HOST":""',
        text,
    )
    text = localize_embedded_comment_users(text)
    text = localize_embedded_user_cover_images(text)
    return text.encode("utf-8")


def localize_embedded_user_cover_images(text: str) -> str:
    """Replace user cover images embedded in the server-rendered page state."""
    pattern = re.compile(
        r'("coverImage(?:Thumbnail)?Url"\s*:\s*)"[^"]*stat\.7gogo\.jp[^"]*"'
    )
    return pattern.sub(r'\1"/data/images/default_user.jpg"', text)


def localize_embedded_comment_users(text: str) -> str:
    """Localize comment-user images in the server-rendered dehydrated state."""
    marker = re.search(r"window\.__DEHYDRATED_STATES__\s*=\s*", text)
    if not marker:
        return text
    try:
        states, end = json.JSONDecoder().raw_decode(text[marker.end() :])
    except json.JSONDecodeError:
        return text

    changed = False
    payloads = []
    if isinstance(states, dict):
        for key in ("page:talk:service:entity:talkComments", "page:talk:service.comments"):
            payload = states.get(key)
            if payload is not None:
                payloads.append(payload)
        service = states.get("page:talk:service")
        if isinstance(service, dict) and isinstance(service.get("comments"), list):
            payloads.append(service["comments"])
    for payload in payloads:
        changed = replace_comment_thumbnails(payload) or changed
        changed = replace_comment_cover_images(payload) or changed
    if not changed:
        return text

    serialized = json.dumps(states, ensure_ascii=False, separators=(",", ":"))
    state_start = marker.end()
    return text[:state_start] + serialized + text[state_start + end :]


def patch_local_page(output: Path) -> None:
    """Repair an existing localized page without downloading it again."""
    path = output / "index.html"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    prefix = f"/{output.name}/"
    replacements = (
        ('href="/css/app.css"', f'href="{prefix}css/app.css"'),
        ('href="css/app.css"', f'href="{prefix}css/app.css"'),
        ('href="/css/app.mobile.css"', f'href="{prefix}css/app.mobile.css"'),
        ('href="css/app.mobile.css"', f'href="{prefix}css/app.mobile.css"'),
        ('src="/js/app.js"', f'src="{prefix}js/app.js"'),
        ('src="js/app.js"', f'src="{prefix}js/app.js"'),
        ('href="/js/app.js"', f'href="{prefix}js/app.js"'),
        ('href="js/app.js"', f'href="{prefix}js/app.js"'),
        ('src="/js/app.mobile.js"', f'src="{prefix}js/app.mobile.js"'),
        ('src="js/app.mobile.js"', f'src="{prefix}js/app.mobile.js"'),
        ('href="/js/app.mobile.js"', f'href="{prefix}js/app.mobile.js"'),
        ('href="js/app.mobile.js"', f'href="{prefix}js/app.mobile.js"'),
        ('href="/manifest.json"', f'href="{prefix}manifest.json"'),
        ('href="manifest.json"', f'href="{prefix}manifest.json"'),
        (
            'src="https://platform.twitter.com/widgets.js"',
            'src="external/platform.twitter.com/widgets.js"',
        ),
    )
    for source, replacement in replacements:
        text = text.replace(source, replacement)
    text = re.sub(r'href="/[A-Za-z0-9_-]+/users/', 'href="/users/', text)
    text = re.sub(
        r'<link\b(?=[^>]*\bhref="external/platform\.twitter\.com/widgets\.js")[^>]*>',
        "",
        text,
    )
    text = re.sub(
        r'<link\b(?=[^>]*\brel="preload"[^>]*\bhref="[^"]*/js/app(?:\.mobile)?\.js")[^>]*>',
        "",
        text,
    )
    text = re.sub(
        r'<script\b(?=[^>]*\bsrc="external/platform\.twitter\.com/widgets\.js")[^>]*>.*?</script>',
        "",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(r'<script\b[^>]*>\s*var twttr = window\.twttr.*?</script>', "", text, flags=re.DOTALL)
    text = re.sub(
        r'"MOVIE_STATS_HOST":"https:(?:\\u002F|/){2}[^"]*"',
        '"MOVIE_STATS_HOST":""',
        text,
    )
    text = re.sub(
        r'"WEBSOCKET_HOST":"wss:(?:\\u002F|/){2}[^"]*"',
        '"WEBSOCKET_HOST":""',
        text,
    )
    text = localize_embedded_comment_users(text)
    text = localize_embedded_user_cover_images(text)
    path.write_text(text, encoding="utf-8")


def patch_local_bundle(output: Path) -> None:
    """Make the editable bundle use local APIs and disable remote WebSocket startup."""
    path = output / "js" / "app.js"
    if not path.exists():
        raise RuntimeError(f"local app bundle is missing: {path}")
    text = path.read_text(encoding="utf-8")
    dynamic = (
        'API_HOST:(window.__7GOGO_CONFIG__&&window.__7GOGO_CONFIG__.API_HOST)'
        '||"https://api.7gogo.jp"'
    )
    empty = 'API_HOST:""'
    if dynamic in text:
        text = text.replace(dynamic, empty, 1)
    elif empty not in text:
        static = 'API_HOST:"https://api.7gogo.jp"'
        if static not in text:
            raise RuntimeError(f"could not locate API_HOST in local app bundle: {path}")
        text = text.replace(static, empty, 1)

    prefix = f"/{output.name}/"
    for source, replacement in (
        ('"/css/app.css"', f'"{prefix}css/app.css"'),
        ('"css/app.css"', f'"{prefix}css/app.css"'),
        ('"/css/app.mobile.css"', f'"{prefix}css/app.mobile.css"'),
        ('"css/app.mobile.css"', f'"{prefix}css/app.mobile.css"'),
        ('"/js/app.js"', f'"{prefix}js/app.js"'),
        ('"js/app.js"', f'"{prefix}js/app.js"'),
        ('"/js/app.mobile.js"', f'"{prefix}js/app.mobile.js"'),
        ('"js/app.mobile.js"', f'"{prefix}js/app.mobile.js"'),
        ('"/manifest.json"', f'"{prefix}manifest.json"'),
        ('"manifest.json"', f'"{prefix}manifest.json"'),
        (
            '"https://platform.twitter.com/widgets.js"',
            '"external/platform.twitter.com/widgets.js"',
        ),
    ):
        text = text.replace(source, replacement)
    # The archived page loads the app through its normal script tag. These
    # SSR-only preloads are inserted too early to be useful and cause browser
    # warnings after the Twitter integration has been disabled.
    text = text.replace(
        'this.renderLink("preload",n,"app-js",{as:"script"}),',
        "",
    )
    text = text.replace(
        'this.renderLink("preload","external/platform.twitter.com/widgets.js","twitter-widget-js",{as:"script"}),',
        "",
    )
    text = text.replace(
        ',this.renderScript("external/platform.twitter.com/widgets.js","twitter-widget-js",!0)',
        "",
    )
    text = re.sub(
        r',this\.renderInlineScript\("var twttr = window\.twttr[\s\S]*?,"twitter-preload-js"\)',
        "",
        text,
    )
    text = re.sub(r'MOVIE_STATS_HOST:"https?://[^"]*"', 'MOVIE_STATS_HOST:""', text)
    text = re.sub(r'WEBSOCKET_HOST:"wss?://[^"]*"', 'WEBSOCKET_HOST:""', text)
    text = re.sub(r'to:"/[A-Za-z0-9_-]+/users/', 'to:"/users/', text)

    # Keep SPA talk routes directory-like so relative archive assets resolve
    # correctly after navigating from one talk to another without a reload.
    talk_root_pattern = re.compile(
        r'to:"/"\+([A-Za-z_$][A-Za-z0-9_$]*)(?=[,}])'
    )
    text = talk_root_pattern.sub(r'to:"/"+\1+"/"', text)

    websocket_start = "M.default.connect();"
    websocket_disabled = "M.default.connect=function(){};"
    if websocket_start in text:
        text = text.replace(websocket_start, websocket_disabled, 1)
    elif websocket_disabled not in text:
        raise RuntimeError(f"could not locate WebSocket startup in local app bundle: {path}")

    text = text.replace("r.initializeSw();", "void 0;", 1)
    text = text.replace(
        'Promise.reject(new Error("[ServiceWorkerService.sendSwMessage] ILLEGAL_EXECUTION: called at server."))',
        "Promise.resolve()",
        1,
    )

    checksum_check = (
        "canReuseMarkup:function(e,t){var n=t.getAttribute(i.CHECKSUM_ATTR_NAME);"
        "return n=n&&parseInt(n,10),r(e)===n}"
    )
    checksum_disabled = "canReuseMarkup:function(e,t){return!1}"
    checksum_already_disabled = "canReuseMarkup:function(e,t){return!0}"
    if checksum_check in text:
        text = text.replace(checksum_check, checksum_disabled, 1)
    elif checksum_already_disabled in text:
        text = text.replace(checksum_already_disabled, checksum_disabled, 1)
    elif checksum_disabled not in text:
        raise RuntimeError(f"could not locate React checksum validation in local app bundle: {path}")

    reuse_markup_argument = "g=l(n),M=b&&!c&&!g,w="
    reuse_markup_disabled = "g=l(n),M=!1,w="
    if reuse_markup_argument in text:
        text = text.replace(reuse_markup_argument, reuse_markup_disabled, 1)
    elif reuse_markup_disabled not in text:
        raise RuntimeError(f"could not disable React markup reuse in local app bundle: {path}")

    checksum_error = 't.nodeType===x&&p("42",_)'
    checksum_error_disabled = "t.nodeType===x&&void 0"
    if checksum_error in text:
        text = text.replace(checksum_error, checksum_error_disabled, 1)
    elif checksum_error_disabled not in text:
        raise RuntimeError(f"could not disable React checksum error path in local app bundle: {path}")

    document_container_error = 't.nodeType===x&&p("43")'
    if document_container_error in text:
        text = text.replace(document_container_error, checksum_error_disabled, 1)
    elif text.count(checksum_error_disabled) < 2:
        raise RuntimeError(f"could not disable React document container error in local app bundle: {path}")

    path.write_text(text, encoding="utf-8")


def patch_all_local_bundles() -> None:
    """Apply compatibility patches to every already archived talk bundle."""
    if not ARCHIVES_ROOT.exists():
        return
    for bundle in ARCHIVES_ROOT.glob("*/js/app.js"):
        patch_local_bundle(bundle.parent.parent)


def post_id(item: dict) -> int | None:
    value = item.get("post", {}).get("postId")
    try:
        return int(value)
    except (TypeError, ValueError, AttributeError):
        return None


def api_headers(token: str, talk_id: str) -> list[str]:
    return [
        "Accept: */*",
        "Accept-Language: zh-CN,zh;q=0.9",
        f"Origin: {SITE_URL}",
        f"Referer: {SITE_URL}/{talk_id}",
        f"User-Agent: {USER_AGENT}",
        f"X-7gogo-WebAuth: {token}",
        "X-Requested-With: XMLHttpRequest",
    ]


def fetch_json(client: HttpClient, url: str, token: str, talk_id: str) -> dict:
    try:
        payload = json.loads(client.get(url, api_headers(token, talk_id)))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON response from {url}: {exc}") from exc
    if payload.get("error"):
        raise RuntimeError(f"API returned error for {url}: {payload['error']}")
    return payload


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_archive_context(output: Path, fallback_slug: str) -> tuple[str, list[dict], dict[str, str]]:
    state_path = output / "dehydrated_states.json"
    posts_path = output / "posts.json"
    if not state_path.exists() or not posts_path.exists():
        raise RuntimeError(
            f"{output} is missing base archive files; run with --phase base first"
        )
    states = json.loads(state_path.read_text(encoding="utf-8"))
    token = states.get("entity:credentials", {}).get("token")
    if not token:
        raise RuntimeError(f"credentials token is missing from {state_path}")
    posts = json.loads(posts_path.read_text(encoding="utf-8")).get("data") or []
    if not posts:
        raise RuntimeError(f"no posts found in {posts_path}")
    context = talk_context_from_states(states, fallback_slug)
    if not TALKS_MAPPING_PATH.exists():
        save_talk_mapping(context)
    return token, posts, context


def selected_post_ids(posts: list[dict], requested: list[str] | None) -> list[int]:
    available = {identifier for identifier in (post_id(item) for item in posts) if identifier is not None}
    if not requested:
        return sorted(available, reverse=True)
    selected: list[int] = []
    for group in requested:
        for value in group.split(","):
            value = value.strip()
            if not value:
                continue
            try:
                identifier = int(value)
            except ValueError as exc:
                raise RuntimeError(f"invalid --post-id value: {value}") from exc
            if identifier not in available:
                raise RuntimeError(f"postId={identifier} is not present in posts.json")
            if identifier not in selected:
                selected.append(identifier)
    return selected


def save_post_details(
    client: HttpClient,
    output: Path,
    token: str,
    talk_id: str,
    post_ids: list[int],
    overwrite: bool,
    delay: float,
    workers: int,
) -> None:
    if workers < 1:
        raise RuntimeError("--workers must be at least 1")
    directory = output / "data" / "posts"
    cookie_dir = client.cookie_jar.parent

    def fetch_one(identifier: int) -> str:
        destination = directory / f"{identifier}.json"
        if destination.exists() and not overwrite:
            return f"skip detail postId={identifier} (already exists)"
        url = f"{API_ROOT}/talks/{quote(talk_id, safe='')}/posts/{identifier}"
        worker_client = client.clone(cookie_dir / f"detail-{identifier}.cookies")
        payload = fetch_json(worker_client, url, token, talk_id)
        write_json(destination, payload)
        if delay:
            time.sleep(delay)
        return f"saved detail postId={identifier}"

    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fetch_one, identifier): identifier for identifier in post_ids}
        for future in as_completed(futures):
            identifier = futures[future]
            try:
                print(future.result())
            except Exception as exc:
                failures.append(f"postId={identifier}: {exc}")
                print(f"error detail postId={identifier}: {exc}", file=sys.stderr)
    if failures:
        raise RuntimeError("detail failures:\n" + "\n".join(sorted(failures)))


def save_post_comments(
    client: HttpClient,
    output: Path,
    token: str,
    talk_id: str,
    post_ids: list[int],
    overwrite: bool,
    delay: float,
    probe_limit: int,
    workers: int,
) -> None:
    if probe_limit < 1:
        raise RuntimeError("--comment-probe-limit must be at least 1")
    if workers < 1:
        raise RuntimeError("--workers must be at least 1")
    directory = output / "data" / "comments"
    cookie_dir = client.cookie_jar.parent

    def fetch_one(identifier: int) -> str:
        destination = directory / f"{identifier}.json"
        if destination.exists() and not overwrite:
            return f"skip comments postId={identifier} (already exists)"
        base_query = {"talkId": talk_id, "postId": identifier}
        endpoint = f"{API_ROOT}/talks/{quote(talk_id, safe='')}/posts/{identifier}/comments"
        probe_url = f"{endpoint}?{urlencode({**base_query, 'limit': probe_limit})}"
        worker_client = client.clone(cookie_dir / f"comments-{identifier}.cookies")
        probe = fetch_json(worker_client, probe_url, token, talk_id)
        probe_data = probe.get("data") or {}
        comment_count = probe_data.get("commentCount")
        if comment_count is None:
            raise RuntimeError("commentCount is missing from probe response")
        comment_count = int(comment_count)
        if comment_count < 0:
            raise RuntimeError(f"invalid commentCount={comment_count}")
        if delay:
            time.sleep(delay)
        if comment_count == 0:
            full = probe
        else:
            full_url = f"{endpoint}?{urlencode({**base_query, 'limit': comment_count})}"
            full = fetch_json(worker_client, full_url, token, talk_id)
        write_json(destination, full)
        actual = len((full.get("data") or {}).get("comments") or [])
        if delay:
            time.sleep(delay)
        return f"saved comments postId={identifier}: commentCount={comment_count}, received={actual}"

    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fetch_one, identifier): identifier for identifier in post_ids}
        for future in as_completed(futures):
            identifier = futures[future]
            try:
                print(future.result())
            except Exception as exc:
                failures.append(f"postId={identifier}: {exc}")
                print(f"error comments postId={identifier}: {exc}", file=sys.stderr)
    if failures:
        raise RuntimeError("comment failures:\n" + "\n".join(sorted(failures)))


def save_post_good(
    client: HttpClient,
    output: Path,
    token: str,
    talk_id: str,
    post_ids: list[int],
    overwrite: bool,
    delay: float,
    workers: int,
) -> None:
    """Save the default good-users page; this API exposes no usable paging state."""
    if workers < 1:
        raise RuntimeError("--workers must be at least 1")
    directory = output / "data" / "good"
    cookie_dir = client.cookie_jar.parent

    def fetch_one(identifier: int) -> str:
        destination = directory / f"{identifier}.json"
        if destination.exists() and not overwrite:
            return f"skip good postId={identifier} (already exists)"
        query = urlencode({"talkId": talk_id, "postId": identifier})
        url = f"{API_ROOT}/talks/{quote(talk_id, safe='')}/posts/{identifier}/good?{query}"
        worker_client = client.clone(cookie_dir / f"good-{identifier}.cookies")
        payload = fetch_json(worker_client, url, token, talk_id)
        write_json(destination, payload)
        if delay:
            time.sleep(delay)
        count = len((payload.get("data") or {}).get("goodUsers") or [])
        return f"saved good postId={identifier}: users={count}"

    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fetch_one, identifier): identifier for identifier in post_ids}
        for future in as_completed(futures):
            identifier = futures[future]
            try:
                print(future.result())
            except Exception as exc:
                failures.append(f"postId={identifier}: {exc}")
                print(f"error good postId={identifier}: {exc}", file=sys.stderr)
    if failures:
        raise RuntimeError("good failures:\n" + "\n".join(sorted(failures)))


def save_post_retalks(
    client: HttpClient,
    output: Path,
    token: str,
    talk_id: str,
    post_ids: list[int],
    overwrite: bool,
    delay: float,
    workers: int,
) -> None:
    """Fetch retalk pages by offset and save one merged response per post."""
    if workers < 1:
        raise RuntimeError("--workers must be at least 1")
    directory = output / "data" / "retalks"
    cookie_dir = client.cookie_jar.parent

    def fetch_one(identifier: int) -> str:
        destination = directory / f"{identifier}.json"
        if destination.exists() and not overwrite:
            return f"skip retalks postId={identifier} (already exists)"
        worker_client = client.clone(cookie_dir / f"retalks-{identifier}.cookies")
        merged: list[dict] = []
        offset = 0
        next_page = True
        last_payload: dict = {"data": {"next": False, "nextOffset": 0, "posts": []}, "error": None}
        while next_page:
            query = urlencode({"talkId": talk_id, "postId": identifier, "offset": offset})
            url = f"{API_ROOT}/talks/{quote(talk_id, safe='')}/posts/{identifier}/retalks?{query}"
            payload = fetch_json(worker_client, url, token, talk_id)
            data = payload.get("data") or {}
            page = data.get("posts") or []
            merged.extend(page)
            last_payload = payload
            next_page = bool(data.get("next"))
            next_offset = data.get("nextOffset")
            if not next_page:
                break
            if next_offset is None or int(next_offset) <= offset:
                raise RuntimeError(
                    f"invalid retalk pagination: offset={offset}, nextOffset={next_offset}"
                )
            offset = int(next_offset)
            if delay:
                time.sleep(delay)
        result = {
            "data": {
                "next": False,
                "nextOffset": len(merged),
                "posts": merged,
            },
            "error": last_payload.get("error"),
        }
        write_json(destination, result)
        if delay:
            time.sleep(delay)
        return f"saved retalks postId={identifier}: posts={len(merged)}"

    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fetch_one, identifier): identifier for identifier in post_ids}
        for future in as_completed(futures):
            identifier = futures[future]
            try:
                print(future.result())
            except Exception as exc:
                failures.append(f"postId={identifier}: {exc}")
                print(f"error retalks postId={identifier}: {exc}", file=sys.stderr)
    if failures:
        raise RuntimeError("retalks failures:\n" + "\n".join(sorted(failures)))


def follower_list(data: object) -> tuple[str, list[dict]]:
    """Return the list field used by the follower endpoint."""
    if isinstance(data, list):
        return "data", data
    if isinstance(data, dict):
        for key in ("list", "users", "followers"):
            value = data.get(key)
            if isinstance(value, list):
                return key, value
    raise RuntimeError("follower response does not contain a list")


def follower_item_id(item: dict) -> int:
    try:
        return int(item["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("follower response contains an item without a numeric id") from exc


def save_user_followers(
    client: HttpClient,
    output: Path,
    token: str,
    talk_id: str,
    user_id: str,
    overwrite: bool,
    delay: float,
    max_pages: int,
) -> str:
    """Fetch followers into one file and resume from metadata in that file."""
    if max_pages < 0:
        raise RuntimeError("--follower-max-pages must not be negative")
    directory = output / "data" / "profile"
    destination = directory / "follower.json"
    progress_key = "_archiveProgress"

    endpoint = f"{USER_API_URL}/{user_id}/follower"
    worker_client = client.clone(client.cookie_jar.parent / f"user-{user_id}-follower.cookies")
    merged: list[dict]
    page_count: int
    target_id: int | None
    template: dict | None
    list_key: str | None

    if destination.exists() and not overwrite:
        saved = json.loads(destination.read_text(encoding="utf-8"))
        progress = saved.get(progress_key)
        if not isinstance(progress, dict):
            return f"skip user {user_id}/follower (already exists)"
        list_key, existing = follower_list(saved.get("data"))
        merged = existing
        page_count = int(progress.get("pages") or 0)
        target_id = progress.get("nextTargetId")
        target_id = int(target_id) if target_id is not None else None
        template = dict(saved)
        template.pop(progress_key, None)
        print(f"resume user {user_id}/follower: pages={page_count}, items={len(merged)}")
    else:
        merged = []
        page_count = 0
        target_id = None
        template = None
        list_key = None

    while True:
        if max_pages and page_count >= max_pages:
            return f"paused user {user_id}/follower: pages={page_count}, items={len(merged)}"

        query = {"userId": user_id}
        if target_id is not None:
            query["targetId"] = target_id
        url = f"{endpoint}?{urlencode(query)}"
        payload = fetch_json(worker_client, url, token, talk_id)
        data = payload.get("data")
        current_key, page = follower_list(data)
        if list_key is None:
            list_key = current_key
        elif list_key != current_key:
            raise RuntimeError(
                f"follower response changed list field from {list_key} to {current_key}"
            )
        if template is None:
            template = payload
        if not page:
            break
        merged.extend(page)
        page_count += 1
        last_id = follower_item_id(page[-1])
        next_target_id = last_id + 1
        print(
            f"follower page={page_count}: received={len(page)}, "
            f"total={len(merged)}, nextTargetId={next_target_id}"
        )
        target_id = next_target_id
        if list_key == "data":
            partial = {"data": merged, "error": template.get("error")}
        else:
            result_data = dict(template.get("data") or {})
            result_data[list_key] = merged
            partial = {"data": result_data, "error": template.get("error")}
        partial[progress_key] = {
            "nextTargetId": target_id,
            "pages": page_count,
        }
        write_json(destination, partial)
        if delay:
            time.sleep(delay)

    if template is None or list_key is None:
        raise RuntimeError("follower response did not provide a usable data structure")
    if list_key == "data":
        result = {"data": merged, "error": template.get("error")}
    else:
        result_data = dict(template.get("data") or {})
        result_data[list_key] = merged
        result = {"data": result_data, "error": template.get("error")}
    write_json(destination, result)
    return f"saved user {user_id}/follower: pages={page_count}, items={len(merged)}"


def save_user_data(
    client: HttpClient,
    output: Path,
    token: str,
    talk_id: str,
    user_id: str,
    overwrite: bool,
    delay: float,
    workers: int,
    max_pages: int,
) -> None:
    """Save the public user endpoints as separate JSON files."""
    if not user_id:
        raise RuntimeError("the talk mapping did not contain a userId")
    if workers < 1:
        raise RuntimeError("--workers must be at least 1")

    endpoints = (
        ("mainTalk", "mainTalk"),
        ("profile", ""),
        ("talkCount", "talkCount"),
        ("ownerTalks", "ownerTalks"),
        ("groupTalks", "groupTalks"),
        ("followCount", "followCount"),
        ("followerCount", "followerCount"),
        ("follow", "follow"),
    )
    directory = output / "data" / "user"
    cookie_dir = client.cookie_jar.parent

    def fetch_one(item: tuple[str, str]) -> str:
        filename, endpoint = item
        destination_directory = output / "data" / "profile" if filename == "follow" else directory
        destination = destination_directory / f"{filename}.json"
        if destination.exists() and not overwrite:
            return f"skip user {user_id}/{filename} (already exists)"
        suffix = f"/{endpoint}" if endpoint else ""
        query = urlencode({"userId": user_id})
        url = f"{USER_API_URL}/{user_id}{suffix}?{query}"
        worker_client = client.clone(cookie_dir / f"user-{user_id}-{filename}.cookies")
        payload = fetch_json(worker_client, url, token, talk_id)
        write_json(destination, payload)
        if delay:
            time.sleep(delay)
        return f"saved user {user_id}/{filename}"

    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=min(workers, len(endpoints))) as executor:
        futures = {executor.submit(fetch_one, item): item[0] for item in endpoints}
        for future in as_completed(futures):
            filename = futures[future]
            try:
                print(future.result())
            except Exception as exc:
                failures.append(f"{filename}: {exc}")
                print(f"error user {user_id}/{filename}: {exc}", file=sys.stderr)
    if failures:
        raise RuntimeError("user failures:\n" + "\n".join(sorted(failures)))

    print(
        save_user_followers(
            client,
            output,
            token,
            talk_id,
            user_id,
            overwrite,
            delay,
            max_pages,
        )
    )


def save_support_data(
    client: HttpClient,
    output: Path,
    token: str,
    talk_id: str,
    overwrite: bool,
    delay: float,
) -> None:
    """Save the non-post API responses used during page initialization."""
    global_data = ARCHIVES_ROOT / "data"
    talk_data = output / "data"

    def fetch_one(destination: Path, url: str) -> dict:
        if destination.exists() and not overwrite:
            print(f"skip support {destination.name} (already exists)")
            return json.loads(destination.read_text(encoding="utf-8"))
        payload = fetch_json(client, url, token, talk_id)
        write_json(destination, payload)
        print(f"saved support {destination}")
        if delay:
            time.sleep(delay)
        return payload

    fetch_one(global_data / "client_setting.json", "https://api.7gogo.jp/v4/client_setting")
    packs = fetch_one(global_data / "stamp_packs.json", f"{API_ROOT}/stamp/packs")
    pack_ids = [str(item["packId"]) for item in (packs.get("data") or {}).get("packs") or []]
    stamp_query = urlencode({"packIds": ",".join(pack_ids)})
    fetch_one(global_data / "stamp_stamps.json", f"{API_ROOT}/stamp/packs/stamps?{stamp_query}")

    talk_path = quote(talk_id, safe="")
    talk_query = urlencode({"talkFields": "USER_STATUS,OWNER,LATEST_POST,REACTION"})
    profile_data = talk_data / "profile"
    requests = (
        (
            talk_data / "talk.json",
            f"https://api.7gogo.jp/v4/talk/{talk_path}?{talk_query}",
        ),
        (
            talk_data / "ui_setting.json",
            f"https://api.7gogo.jp/v4/talk/custom/{talk_path}/ui_setting",
        ),
        (
            profile_data / "members.json",
            f"{API_ROOT}/talks/{talk_path}/members?{urlencode({'talkId': talk_id})}",
        ),
        (
            profile_data / "watchers.json",
            f"{API_ROOT}/talks/{talk_path}/watchers?{urlencode({'talkId': talk_id})}",
        ),
        (
            profile_data / "comments.json",
            f"https://api.7gogo.jp/web/v3/talks/{talk_path}/comments?"
            f"{urlencode({'talkId': talk_id, 'limit': 20})}",
        ),
        (
            profile_data / "images.json",
            f"{API_ROOT}/talks/{talk_path}/images?"
            f"{urlencode({'talkId': talk_id, 'limit': 30})}",
        ),
    )
    for destination, url in requests:
        fetch_one(destination, url)

    save_public_catalog_data(client, token, talk_id, overwrite, delay)


def replace_comment_thumbnails(value: object) -> bool:
    """Replace every thumbnailUrl in a saved comment response in place."""
    changed = False
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "thumbnailUrl" and isinstance(item, str) and item != LOCAL_DEFAULT_USER_AVATAR:
                value[key] = LOCAL_DEFAULT_USER_AVATAR
                changed = True
            else:
                changed = replace_comment_thumbnails(item) or changed
    elif isinstance(value, list):
        for item in value:
            changed = replace_comment_thumbnails(item) or changed
    return changed


def replace_comment_cover_images(value: object) -> bool:
    """Replace cover images belonging to users in saved comments."""
    changed = False
    if isinstance(value, dict):
        users = []
        if "coverImageUrl" in value or "coverImageThumbnailUrl" in value:
            users.append(value)
        direct_user = value.get("user")
        if isinstance(direct_user, dict):
            users.append(direct_user)
        comment = value.get("comment")
        nested_user = comment.get("user") if isinstance(comment, dict) else None
        if isinstance(nested_user, dict):
            users.append(nested_user)
        for user in users:
            for field in ("coverImageUrl", "coverImageThumbnailUrl"):
                if isinstance(user.get(field), str) and user[field] != LOCAL_DEFAULT_USER_AVATAR:
                    user[field] = LOCAL_DEFAULT_USER_AVATAR
                    changed = True
        for item in value.values():
            changed = replace_comment_cover_images(item) or changed
    elif isinstance(value, list):
        for item in value:
            changed = replace_comment_cover_images(item) or changed
    return changed


def replace_nested_comment_thumbnails(value: object) -> bool:
    """Replace thumbnails under comment.user at any nesting depth."""
    changed = False
    if isinstance(value, dict):
        comment = value.get("comment")
        user = comment.get("user") if isinstance(comment, dict) else None
        if isinstance(user, dict) and isinstance(user.get("thumbnailUrl"), str):
            if user["thumbnailUrl"] != LOCAL_DEFAULT_USER_AVATAR:
                user["thumbnailUrl"] = LOCAL_DEFAULT_USER_AVATAR
                changed = True
        for item in value.values():
            changed = replace_nested_comment_thumbnails(item) or changed
    elif isinstance(value, list):
        for item in value:
            changed = replace_nested_comment_thumbnails(item) or changed
    return changed


def replace_post_body_user_images(value: object) -> bool:
    """Replace user and talk images nested inside post body entries."""
    def walk(item: object, inside_body: bool = False) -> bool:
        changed = False
        if isinstance(item, dict):
            if inside_body:
                talk = item.get("talk")
                if isinstance(talk, dict) and isinstance(talk.get("thumbnailUrl"), str):
                    if talk["thumbnailUrl"] != LOCAL_DEFAULT_USER_AVATAR:
                        talk["thumbnailUrl"] = LOCAL_DEFAULT_USER_AVATAR
                        changed = True
                user = item.get("user")
                if isinstance(user, dict):
                    for field in (
                        "thumbnailUrl",
                        "coverImageUrl",
                        "coverImageThumbnailUrl",
                    ):
                        if isinstance(user.get(field), str) and user[field] != LOCAL_DEFAULT_USER_AVATAR:
                            user[field] = LOCAL_DEFAULT_USER_AVATAR
                            changed = True
            for key, child in item.items():
                changed = walk(child, inside_body or key == "body") or changed
        elif isinstance(item, list):
            for child in item:
                changed = walk(child, inside_body) or changed
        return changed

    return walk(value)


def replace_post_comment_thumbnails(value: object) -> bool:
    """Replace thumbnails belonging to inline comments in a post detail."""
    if not isinstance(value, dict):
        return False
    post = ((value.get("data") or {}).get("posts") or {}).get("post")
    if not isinstance(post, dict):
        return False
    changed = replace_nested_comment_thumbnails(post)
    return replace_post_body_user_images(post) or changed


def replace_post_list_comment_thumbnails(value: object) -> bool:
    """Replace inline comment thumbnails in a talk's merged posts list."""
    if not isinstance(value, dict) or not isinstance(value.get("data"), list):
        return False
    changed = False
    for item in value["data"]:
        changed = replace_nested_comment_thumbnails(item) or changed
        post = item.get("post") if isinstance(item, dict) else None
        if isinstance(post, dict):
            changed = replace_post_body_user_images(post) or changed
    return changed


def replace_retalk_user_thumbnails(value: object) -> bool:
    """Localize retalk images and remove the talk UI settings payload."""
    if not isinstance(value, dict):
        return False
    posts = (value.get("data") or {}).get("posts")
    if not isinstance(posts, list):
        return False
    changed = False
    for item in posts:
        talk = item.get("talk") if isinstance(item, dict) else None
        if isinstance(talk, dict):
            if isinstance(talk.get("thumbnailUrl"), str):
                if talk["thumbnailUrl"] != LOCAL_DEFAULT_USER_AVATAR:
                    talk["thumbnailUrl"] = LOCAL_DEFAULT_USER_AVATAR
                    changed = True
            if talk.get("talkUiSetting") is not None:
                talk["talkUiSetting"] = None
                changed = True
        user = item.get("user") if isinstance(item, dict) else None
        if isinstance(user, dict):
            for field in ("thumbnailUrl", "coverImageUrl", "coverImageThumbnailUrl"):
                if isinstance(user.get(field), str) and user[field] != LOCAL_DEFAULT_USER_AVATAR:
                    user[field] = LOCAL_DEFAULT_USER_AVATAR
                    changed = True
    return changed


def replace_good_user_thumbnails(value: object) -> bool:
    """Replace user thumbnails in a likes response."""
    if not isinstance(value, dict):
        return False
    good_users = (value.get("data") or {}).get("goodUsers")
    if not isinstance(good_users, list):
        return False
    changed = False
    for item in good_users:
        user = item.get("user") if isinstance(item, dict) else None
        if isinstance(user, dict) and isinstance(user.get("thumbnailUrl"), str):
            if user["thumbnailUrl"] != LOCAL_DEFAULT_USER_AVATAR:
                user["thumbnailUrl"] = LOCAL_DEFAULT_USER_AVATAR
                changed = True
    return changed


def save_default_user_avatar(client: HttpClient, overwrite: bool) -> None:
    """Download the shared avatar and localize comment thumbnails."""
    destination = ARCHIVES_ROOT / "data" / "images" / "default_user.jpg"
    if overwrite or not destination.exists():
        content = client.get(
            DEFAULT_USER_AVATAR_URL,
            [
                f"User-Agent: {USER_AGENT}",
                "Accept: image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            ],
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        print(f"saved default user avatar {destination}")
    else:
        print(f"skip default user avatar (already exists): {destination}")

    updated = 0
    for comment_path in ARCHIVES_ROOT.glob("*/data/comments/*.json"):
        try:
            payload = json.loads(comment_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"could not read comment file {comment_path}: {exc}") from exc
        changed = replace_comment_thumbnails(payload)
        changed = replace_comment_cover_images(payload) or changed
        if changed:
            write_json(comment_path, payload)
            updated += 1
    for post_path in ARCHIVES_ROOT.glob("*/data/posts/*.json"):
        try:
            payload = json.loads(post_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"could not read post file {post_path}: {exc}") from exc
        if replace_post_comment_thumbnails(payload):
            write_json(post_path, payload)
            updated += 1
    for posts_path in ARCHIVES_ROOT.glob("*/posts.json"):
        try:
            payload = json.loads(posts_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"could not read posts file {posts_path}: {exc}") from exc
        if replace_post_list_comment_thumbnails(payload):
            write_json(posts_path, payload)
            updated += 1
    for retalk_path in ARCHIVES_ROOT.glob("*/data/retalks/*.json"):
        try:
            payload = json.loads(retalk_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"could not read retalk file {retalk_path}: {exc}") from exc
        if replace_retalk_user_thumbnails(payload):
            write_json(retalk_path, payload)
            updated += 1
    for good_path in ARCHIVES_ROOT.glob("*/data/good/*.json"):
        try:
            payload = json.loads(good_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"could not read good file {good_path}: {exc}") from exc
        if replace_good_user_thumbnails(payload):
            write_json(good_path, payload)
            updated += 1
    for profile_path in (
        *ARCHIVES_ROOT.glob("*/data/profile/comments.json"),
        *ARCHIVES_ROOT.glob("*/data/profile/follower.json"),
    ):
        try:
            payload = json.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"could not read profile file {profile_path}: {exc}") from exc
        changed = replace_comment_thumbnails(payload)
        if profile_path.name in {"comments.json", "follower.json"}:
            changed = replace_comment_cover_images(payload) or changed
        if changed:
            write_json(profile_path, payload)
            updated += 1
    print(f"localized thumbnails in {updated} files")


def build_talk_catalog(archive_root: Path) -> dict:
    """Build the small talk objects consumed by the ranking pages."""
    if not TALKS_MAPPING_PATH.exists():
        return {"data": [], "error": None}
    mapping = json.loads(TALKS_MAPPING_PATH.read_text(encoding="utf-8"))
    entries: list[dict[str, str]] = []
    for key, context in mapping.items():
        if not isinstance(context, dict):
            continue
        slug = str(context.get("slug") or key)
        talk_id = str(context.get("talkId") or key)
        profile_path = archive_root / slug / "data" / "user" / "profile.json"
        talk_path = archive_root / slug / "data" / "talk.json"
        profile: dict = {}
        talk: dict = {}
        if profile_path.exists():
            try:
                profile_payload = json.loads(profile_path.read_text(encoding="utf-8"))
                profile = profile_payload.get("data") or {}
            except (OSError, json.JSONDecodeError):
                profile = {}
        if talk_path.exists():
            try:
                talk_payload = json.loads(talk_path.read_text(encoding="utf-8"))
                talk = talk_payload.get("data") or {}
            except (OSError, json.JSONDecodeError):
                talk = {}
        entries.append(
            {
                "name": str(talk.get("name") or profile.get("name") or slug),
                "thumbnailUrl": str(
                    profile.get("thumbnailUrl") or talk.get("thumbnailPath") or ""
                ),
                "talkId": talk_id,
            }
        )
    return {"data": entries, "error": None}


def save_public_catalog_data(
    client: HttpClient,
    token: str,
    talk_id: str,
    overwrite: bool,
    delay: float,
) -> None:
    """Save public catalog data and derive rankings from talks.json."""
    global_data = ARCHIVES_ROOT / "data"
    recommendation_path = global_data / "recommend_talks.json"
    if recommendation_path.exists() and not overwrite:
        print(f"skip support {recommendation_path.name} (already exists)")
    else:
        payload = fetch_json(
            client,
            f"{PUBLIC_API_ROOT}/v4/recommend/talk/list?{urlencode({'limit': 20})}",
            token,
            talk_id,
        )
        write_json(recommendation_path, payload)
        print(f"saved support {recommendation_path}")
        if delay:
            time.sleep(delay)

    ranking = build_talk_catalog(ARCHIVES_ROOT)
    for filename in ("ranking_daily.json", "ranking_total.json"):
        destination = global_data / filename
        # Rankings are derived from the complete talks.json mapping. Rebuild
        # them on every talk run so adding a second talk cannot leave stale
        # one-entry ranking files behind.
        write_json(destination, ranking)
        print(f"saved support {destination}")


def fetch_posts(
    client: HttpClient,
    initial_posts: list[dict],
    token: str,
    talk_id: str,
    delay: float,
) -> list[dict]:
    by_id: dict[int, dict] = {}
    without_id: list[dict] = []
    for item in initial_posts:
        identifier = post_id(item)
        if identifier is None:
            without_id.append(item)
        else:
            by_id[identifier] = item

    initial_ids = [identifier for identifier in (post_id(item) for item in initial_posts) if identifier]
    if not initial_ids:
        raise RuntimeError("no postId was found in the initial page")

    target_id = min(initial_ids) - 1
    seen_targets: set[int] = set()
    while target_id >= 1:
        if target_id in seen_targets:
            raise RuntimeError(f"pagination repeated targetId={target_id}")
        seen_targets.add(target_id)
        url = (
            f"{API_ROOT}/talks/{quote(talk_id, safe='')}/posts?"
            f"talkId={quote(talk_id, safe='')}&targetId={target_id}&direction=PREV"
        )
        payload = json.loads(
            client.get(
                url,
                api_headers(token, talk_id),
            )
        )
        data = payload.get("data") or []
        ids = [identifier for identifier in (post_id(item) for item in data) if identifier is not None]
        new_count = 0
        for item in data:
            identifier = post_id(item)
            if identifier is None:
                without_id.append(item)
            elif identifier not in by_id:
                by_id[identifier] = item
                new_count += 1
        print(
            f"targetId={target_id}: received={len(data)}, new={new_count}, "
            f"total={len(by_id)}"
        )
        if not data or not ids or min(ids) <= 1:
            break
        if new_count == 0:
            raise RuntimeError(f"pagination returned no new posts at targetId={target_id}")
        target_id = min(ids) - 1
        if delay:
            time.sleep(delay)

    merged = sorted(by_id.values(), key=lambda item: post_id(item) or -1, reverse=True)
    merged.extend(without_id)
    return merged


def run_base_archive(
    client: HttpClient,
    output: Path,
    talk_slug: str,
    page_url: str,
    delay: float,
) -> tuple[str, list[dict], dict[str, str]]:
    page = client.get(
        page_url,
        [f"User-Agent: {USER_AGENT}", "Accept: text/html,application/xhtml+xml"],
    )
    original = output / "original"
    original.mkdir(parents=True, exist_ok=True)
    (original / "index.html").write_bytes(page)
    states = extract_dehydrated_states(page)
    write_json(output / "dehydrated_states.json", states)
    context = talk_context_from_states(states, talk_slug)
    save_talk_mapping(context)
    token = states.get("entity:credentials", {}).get("token")
    if not token:
        raise RuntimeError("credentials token was not found")
    initial_posts = states.get("page:talk:service:entity:posts", {}).get("posts", [])
    if not initial_posts:
        raise RuntimeError("initial posts were not found")
    print(f"saved index.html ({len(page)} bytes), initial posts={len(initial_posts)}")

    manifest = save_resources(client, page, output, page_url)
    (output / "index.html").write_bytes(
        localize_page(page, manifest, resource_prefix=f"/{output.name}/")
    )
    patch_local_bundle(output)
    patch_local_page(output)
    merged = fetch_posts(client, initial_posts, token, context["talkId"], delay)
    write_json(output / "posts.json", {"data": merged, "error": None})
    print(f"wrote {output / 'posts.json'} with {len(merged)} posts")
    return token, merged, context


def main() -> int:
    args = parse_args()
    if args.timeout < 1:
        raise RuntimeError("--timeout must be at least 1")
    talk_slug, page_url = normalize_talk_ref(args.talk_ref)
    output = args.output or ARCHIVES_ROOT / talk_slug
    output.mkdir(parents=True, exist_ok=True)
    patch_all_local_bundles()
    with tempfile.TemporaryDirectory(prefix="7gogo-cookies-") as temporary:
        cookie_jar = Path(temporary) / "cookies.txt"
        client = HttpClient(cookie_jar, timeout=args.timeout)

        base_files_exist = (output / "dehydrated_states.json").exists() and (output / "posts.json").exists()
        if args.phase == "base" or (args.phase == "all" and (args.refresh_base or not base_files_exist)):
            token, posts, context = run_base_archive(
                client, output, talk_slug, page_url, args.delay
            )
        else:
            token, posts, context = load_archive_context(output, talk_slug)
        if (output / "js" / "app.js").exists():
            patch_local_bundle(output)
        patch_local_page(output)
        talk_id = context["talkId"]
        user_id = context["userId"]

        if args.phase in ("details", "comments", "good", "retalks", "all"):
            post_ids = selected_post_ids(posts, args.post_ids)
            print(f"selected {len(post_ids)} postIds")
            if args.phase in ("details", "all"):
                save_post_details(
                    client, output, token, talk_id, post_ids,
                    args.overwrite, args.delay, args.workers
                )
            if args.phase in ("comments", "all"):
                save_post_comments(
                    client,
                    output,
                    token,
                    talk_id,
                    post_ids,
                    args.overwrite,
                    args.delay,
                    args.comment_probe_limit,
                    args.workers,
                )
            if args.phase in ("good", "all"):
                save_post_good(
                    client, output, token, talk_id, post_ids,
                    args.overwrite, args.delay, args.workers
                )
            if args.phase in ("retalks", "all"):
                save_post_retalks(
                    client, output, token, talk_id, post_ids,
                    args.overwrite, args.delay, args.workers
                )
        if args.phase in ("users", "all"):
            save_user_data(
                client,
                output,
                token,
                talk_id,
                user_id,
                args.overwrite,
                args.delay,
                args.workers,
                args.follower_max_pages,
            )
        if args.phase in ("support", "all"):
            save_support_data(
                client,
                output,
                token,
                talk_id,
                args.overwrite,
                args.delay,
            )
        save_default_user_avatar(client, args.overwrite)
        if args.phase in ("images", "all"):
            patch_local_page(output)
            save_archive_images(client, output, args.overwrite, args.workers)
        if args.phase in ("videos", "all"):
            save_archive_videos(client, output, args.overwrite, args.workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
