#!/usr/bin/env python3
"""Serve archived 7gogo talks and emulate the archived API responses."""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import re
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit


USER_FILES = {
    "": "profile.json",
    "mainTalk": "mainTalk.json",
    "talkCount": "talkCount.json",
    "ownerTalks": "ownerTalks.json",
    "groupTalks": "groupTalks.json",
    "followCount": "followCount.json",
    "followerCount": "followerCount.json",
    "follow": "follow.json",
    "follower": "follower.json",
}
ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="listen address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="listen port (default: 8080)")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent / "archives",
        help="web root containing data and talk directories (default: archives)",
    )
    return parser.parse_args()


def load_mapping(root: Path) -> dict[str, dict[str, str]]:
    path = root / "talks.json"
    if not path.exists():
        path = root.parent / "talks.json"
    if not path.exists():
        raise RuntimeError(f"mapping file not found: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"mapping file must contain an object: {path}")
    return {str(key): item for key, item in value.items() if isinstance(item, dict)}


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def post_identifier(item: dict) -> int | None:
    try:
        return int(item.get("post", {}).get("postId"))
    except (AttributeError, TypeError, ValueError):
        return None


class ArchiveHandler(BaseHTTPRequestHandler):
    server_version = "7gogoArchive/1.0"

    @property
    def root(self) -> Path:
        return self.server.archive_root  # type: ignore[attr-defined]

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.add_cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Requested-With, X-7gogo-WebAuth")
        self.end_headers()

    def do_HEAD(self) -> None:
        self.dispatch(head_only=True)

    def do_GET(self) -> None:
        self.dispatch(head_only=False)

    def do_POST(self) -> None:
        self.dispatch(head_only=False)

    def dispatch(self, head_only: bool) -> None:
        path = urlsplit(self.path).path
        try:
            if path == "/" or path == "":
                self.serve_root(head_only)
                return
            if path == "/ws/sock":
                self.send_response(HTTPStatus.NOT_FOUND)
                self.send_header("Connection", "close")
                self.end_headers()
                return
            if path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.add_cors_headers()
                self.end_headers()
                return
            api_parts = self.api_parts(path)
            if api_parts is not None:
                self.serve_api(api_parts, head_only)
                return
            self.serve_static(path, head_only)
        except FileNotFoundError as exc:
            if self.api_parts(path) is not None:
                self.send_json({"data": None, "error": {"message": str(exc)}}, HTTPStatus.NOT_FOUND, head_only)
            else:
                self.send_error(HTTPStatus.NOT_FOUND, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            # Browsers may cancel a response while navigating or retrying media.
            return
        except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
            self.send_json({"data": None, "error": {"message": str(exc)}}, HTTPStatus.BAD_REQUEST, head_only)

    def add_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK, head_only: bool = False) -> None:
        body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self.send_response(status)
        self.add_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def api_parts(self, path: str) -> list[str] | None:
        clean = path.lstrip("/")
        for prefix in ("api.7gogo.jp/", "api/", ""):
            for version in ("web/v2/", "web/v3/", "v4/"):
                if clean.startswith(prefix + version):
                    rest = clean[len(prefix) :]
                    return [unquote(part) for part in rest.split("/") if part]
        return None

    def serve_root(self, head_only: bool) -> None:
        mapping = load_mapping(self.root)
        links = []
        for context in sorted(mapping.values(), key=lambda item: item.get("slug", "")):
            slug = context.get("slug", "")
            if slug:
                links.append(f'<li><a href="/{html.escape(slug)}/">{html.escape(slug)}</a></li>')
        body = (
            "<!doctype html><meta charset=utf-8><title>7gogo archive</title>"
            "<h1>7gogo archive</h1><ul>" + "".join(links) + "</ul>"
        ).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.add_cors_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def resolve_talk(self, talk_id: str) -> tuple[dict[str, str], Path]:
        if not talk_id:
            raise RuntimeError("talkId is required")
        mapping = load_mapping(self.root)
        context = mapping.get(talk_id)
        if context is None:
            for candidate in mapping.values():
                if candidate.get("slug") == talk_id:
                    context = candidate
                    break
        if context is None:
            direct_root = (self.root / talk_id).resolve()
            if direct_root.is_dir():
                context = {"talkId": talk_id, "slug": talk_id, "userId": ""}
            else:
                raise FileNotFoundError(f"unknown talkId: {talk_id}")
        slug = context.get("slug", "")
        if not ID_RE.fullmatch(slug):
            raise RuntimeError(f"invalid talk slug in mapping: {slug}")
        talk_root = (self.root / slug).resolve()
        if os.path.commonpath((str(self.root.resolve()), str(talk_root))) != str(self.root.resolve()):
            raise RuntimeError("talk directory escapes archive root")
        return context, talk_root

    def find_user_talk(self, user_id: str) -> tuple[dict[str, str], Path]:
        mapping = load_mapping(self.root)
        for context in mapping.values():
            if context.get("userId") == user_id:
                slug = context.get("slug", "")
                if not ID_RE.fullmatch(slug):
                    raise RuntimeError(f"invalid talk slug in mapping: {slug}")
                return context, (self.root / slug).resolve()
        raise FileNotFoundError(f"unknown userId: {user_id}")

    def serve_api(self, parts: list[str], head_only: bool) -> None:
        query = parse_qs(urlsplit(self.path).query)
        if parts[:2] == ["web", "v2"] and parts[2:] == ["popular", "posts"]:
            self.send_json({"data": [], "error": None}, head_only=head_only)
            return
        if parts[:2] == ["web", "v2"] and parts[2:5] == ["ranking", "official", "talks"]:
            if parts[5:] not in (["daily"], ["total"]):
                raise FileNotFoundError("unknown ranking API path")
            self.serve_global_list(f"ranking_{parts[5]}.json", query, head_only)
            return
        if parts[:2] == ["web", "v2"] and parts[2:] == ["token", "security", "publish"]:
            self.serve_token_publish(head_only)
            return
        if parts[:2] == ["web", "v2"] and parts[2:] == ["stamp", "packs"]:
            self.serve_json_file(self.root / "data" / "stamp_packs.json", head_only)
            return
        if parts[:2] == ["web", "v2"] and parts[2:] == ["stamp", "packs", "stamps"]:
            self.serve_json_file(self.root / "data" / "stamp_stamps.json", head_only)
            return
        if parts[:2] == ["web", "v2"] and len(parts) >= 3 and parts[2] == "talks":
            self.serve_talk_api(parts, query, head_only)
            return
        if parts[:2] == ["web", "v3"] and len(parts) == 5 and parts[2] == "talks" and parts[4] == "comments":
            self.serve_talk_collection(parts, query, "comments.json", head_only)
            return
        if parts[:2] == ["web", "v2"] and len(parts) >= 3 and parts[2] == "users":
            self.serve_user_api(parts, query, head_only)
            return
        if parts[:1] == ["v4"]:
            self.serve_v4_api(parts, head_only)
            return
        raise FileNotFoundError("unknown API path")

    def serve_token_publish(self, head_only: bool) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length:
            self.rfile.read(content_length)
        token = ""
        mapping = load_mapping(self.root)
        for context in mapping.values():
            slug = context.get("slug", "")
            state_path = self.root / slug / "dehydrated_states.json"
            if state_path.exists():
                states = read_json(state_path)
                token = str((states.get("entity:credentials") or {}).get("token") or "")
                break
        self.send_json({"token": token}, head_only=head_only)

    def serve_v4_api(self, parts: list[str], head_only: bool) -> None:
        if parts == ["v4", "client_setting"]:
            self.serve_json_file(self.root / "data" / "client_setting.json", head_only)
            return
        if parts == ["v4", "hashtag", "list", "pickup"]:
            self.send_json({"data": [], "error": None}, head_only=head_only)
            return
        if parts == ["v4", "recommend", "talk", "list"]:
            self.serve_global_list(
                "recommend_talks.json", parse_qs(urlsplit(self.path).query), head_only
            )
            return
        if parts == ["v4", "stamp", "packs"]:
            self.serve_json_file(self.root / "data" / "stamp_packs.json", head_only)
            return
        if len(parts) == 5 and parts[1:3] == ["talk", "custom"] and parts[4] == "ui_setting":
            _, talk_root = self.resolve_talk(parts[3])
            self.serve_json_file(talk_root / "data" / "ui_setting.json", head_only)
            return
        if len(parts) == 3 and parts[1] == "talk":
            _, talk_root = self.resolve_talk(parts[2])
            self.serve_json_file(talk_root / "data" / "talk.json", head_only)
            return
        raise FileNotFoundError("unknown v4 API path")

    def serve_talk_api(self, parts: list[str], query: dict[str, list[str]], head_only: bool) -> None:
        if len(parts) < 5:
            raise FileNotFoundError("invalid talk API path")
        talk_id = parts[3]
        query_talk_id = query.get("talkId", [talk_id])[0]
        if query_talk_id != talk_id:
            raise ValueError("talkId in the path and query string do not match")
        _, talk_root = self.resolve_talk(talk_id)
        if len(parts) == 5 and parts[4] in {"members", "watchers", "images"}:
            filename = {
                "members": "members.json",
                "watchers": "watchers.json",
                "images": "images.json",
            }[parts[4]]
            payload = read_json(talk_root / "data" / "profile" / filename)
            if parts[4] == "images":
                payload = self.limit_list(payload, query)
            self.send_json(payload, head_only=head_only)
            return
        if parts[4] != "posts":
            raise FileNotFoundError("unknown talk API resource")

        if len(parts) == 5:
            self.serve_post_list(talk_root, query, head_only)
            return
        post_id = parts[5]
        if not ID_RE.fullmatch(post_id):
            raise RuntimeError(f"invalid postId: {post_id}")
        query_post_id = query.get("postId", [post_id])[0]
        if query_post_id != post_id:
            raise ValueError("postId in the path and query string do not match")
        if len(parts) == 6:
            self.serve_json_file(talk_root / "data" / "posts" / f"{post_id}.json", head_only)
            return
        if len(parts) != 7:
            raise FileNotFoundError("unknown post API resource")
        filename = {"comments": "comments", "good": "good", "retalks": "retalks"}.get(parts[6])
        if filename is None:
            raise FileNotFoundError("unknown post API resource")
        payload = read_json(talk_root / "data" / filename / f"{post_id}.json")
        if filename == "comments":
            payload = self.limit_comments(payload, query)
        self.send_json(payload, head_only=head_only)

    def serve_post_list(self, talk_root: Path, query: dict[str, list[str]], head_only: bool) -> None:
        payload = read_json(talk_root / "posts.json")
        posts = payload.get("data") or []
        target_values = query.get("targetId")
        if target_values:
            target = int(target_values[0])
            selected = [item for item in posts if (post_identifier(item) or 0) <= target]
            selected = selected[:100]
        else:
            selected = posts[:10]
        self.send_json({"data": selected, "error": payload.get("error")}, head_only=head_only)

    def limit_comments(self, payload: dict, query: dict[str, list[str]]) -> dict:
        data = dict(payload.get("data") or {})
        comments = data.get("comments") or []
        limit = int(query.get("limit", ["2"])[0])
        if limit < 0:
            raise ValueError("limit must not be negative")

        target_values = query.get("targetId")
        if target_values:
            target_id = int(target_values[0])
            direction = query.get("direction", ["PREV"])[0].upper()
            if direction == "PREV":
                comments = [
                    item
                    for item in comments
                    if int((item.get("comment") or {}).get("commentId", 0)) < target_id
                ]
            elif direction == "NEXT":
                comments = [
                    item
                    for item in comments
                    if int((item.get("comment") or {}).get("commentId", 0)) > target_id
                ]
        data["comments"] = comments[:limit]
        return {"data": data, "error": payload.get("error")}

    def serve_talk_collection(
        self,
        parts: list[str],
        query: dict[str, list[str]],
        filename: str,
        head_only: bool,
    ) -> None:
        talk_id = parts[3]
        query_talk_id = query.get("talkId", [talk_id])[0]
        if query_talk_id != talk_id:
            raise ValueError("talkId in the path and query string do not match")
        _, talk_root = self.resolve_talk(talk_id)
        payload = read_json(talk_root / "data" / "profile" / filename)
        self.send_json(self.limit_list(payload, query), head_only=head_only)

    def limit_list(self, payload: dict, query: dict[str, list[str]]) -> dict:
        if "limit" not in query:
            return payload
        limit = int(query["limit"][0])
        if limit < 0:
            raise ValueError("limit must not be negative")
        data = payload.get("data")
        if not isinstance(data, list):
            return payload
        return {"data": data[:limit], "error": payload.get("error")}

    def serve_user_api(
        self, parts: list[str], query: dict[str, list[str]], head_only: bool
    ) -> None:
        if len(parts) < 4:
            raise FileNotFoundError("invalid user API path")
        user_id = parts[3]
        query_user_id = query.get("userId", [user_id])[0]
        if query_user_id != user_id:
            raise ValueError("userId in the path and query string do not match")
        _, user_root = self.find_user_talk(user_id)
        endpoint = parts[4] if len(parts) >= 5 else ""
        filename = USER_FILES.get(endpoint)
        if filename is None or len(parts) > 5:
            raise FileNotFoundError("unknown user API resource")
        directory = "profile" if endpoint in {"follow", "follower"} else "user"
        payload = read_json(user_root / "data" / directory / filename)
        if endpoint == "follower":
            payload = self.limit_followers(payload, query)
        self.send_json(payload, head_only=head_only)

    def limit_followers(self, payload: dict, query: dict[str, list[str]]) -> dict:
        """Serve follower pages using the archived id-ordered list."""
        followers = payload.get("data")
        if not isinstance(followers, list):
            return payload

        page_size = 10
        target_values = query.get("targetId")
        if not target_values:
            start = 0
        else:
            target_id = int(target_values[0])
            start = len(followers)
            for index, item in enumerate(followers):
                try:
                    item_id = int(item.get("id"))
                except (AttributeError, TypeError, ValueError):
                    continue
                if item_id <= target_id:
                    start = index
                    break

        result = dict(payload)
        result["data"] = followers[start : start + page_size]
        result.pop("_archiveProgress", None)
        return result

    def serve_json_file(self, path: Path, head_only: bool) -> None:
        self.send_json(read_json(path), head_only=head_only)

    def serve_global_list(
        self, filename: str, query: dict[str, list[str]], head_only: bool
    ) -> None:
        path = self.root / "data" / filename
        if path.exists():
            payload = read_json(path)
        elif filename in {"ranking_daily.json", "ranking_total.json"}:
            # Ranking data can be served from the current archive without caching it.
            payload = self.build_talk_catalog()
        else:
            raise FileNotFoundError(f"global API data not found: {filename}")
        if "limit" in query and isinstance(payload.get("data"), list):
            limit = int(query["limit"][0])
            if limit < 0:
                raise ValueError("limit must not be negative")
            payload = dict(payload)
            payload["data"] = payload["data"][:limit]
        if filename in {"ranking_daily.json", "ranking_total.json"}:
            payload = dict(payload)
            payload["data"] = [
                {key: value for key, value in item.items() if key != "talkCode"}
                if isinstance(item, dict)
                else item
                for item in payload.get("data", [])
            ]
        self.send_json(payload, head_only=head_only)

    def build_talk_catalog(self) -> dict:
        """Build a ranking response without writing a cache file."""
        entries: list[dict[str, str]] = []
        for key, context in load_mapping(self.root).items():
            slug = str(context.get("slug") or key)
            talk_id = str(context.get("talkId") or key)
            profile: dict = {}
            talk: dict = {}
            profile_path = self.root / slug / "data" / "user" / "profile.json"
            talk_path = self.root / slug / "data" / "talk.json"
            if profile_path.exists():
                profile = read_json(profile_path).get("data") or {}
            if talk_path.exists():
                talk = read_json(talk_path).get("data") or {}
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

    def serve_static(self, path: str, head_only: bool) -> None:
        parts = [unquote(part) for part in path.split("/") if part]
        if not parts:
            self.serve_root(head_only)
            return
        if parts[0] == "users":
            self.serve_user_static(parts, head_only)
            return
        if len(parts) >= 3 and parts[1] == "users":
            asset_index = next(
                (
                    index
                    for index, part in enumerate(parts)
                    if part in {"appimg_images", "output", "css", "js", "external"}
                    or part == "manifest.json"
                ),
                None,
            )
            if asset_index is not None and asset_index >= 3:
                _, talk_root = self.resolve_talk(parts[0])
                relative = Path(*parts[asset_index:])
                candidate = (talk_root / relative).resolve()
                if os.path.commonpath((str(talk_root), str(candidate))) != str(talk_root):
                    raise RuntimeError("static path escapes talk directory")
                self.serve_static_file(candidate, head_only)
                return
        mapping = load_mapping(self.root)
        slug = parts[0]
        mapped_slugs = {context.get("slug") for context in mapping.values()}
        if slug not in mapped_slugs and not (self.root / slug).is_dir():
            raise FileNotFoundError(f"unknown archive path: {path}")
        if len(parts) == 1 and not path.endswith("/"):
            location = path + "/"
            query = urlsplit(self.path).query
            if query:
                location += "?" + query
            self.send_response(HTTPStatus.PERMANENT_REDIRECT)
            self.send_header("Location", location)
            self.end_headers()
            return
        relative = Path(*parts[1:]) if len(parts) > 1 else Path("index.html")
        candidate = (self.root / slug / relative).resolve()
        archive_root = (self.root / slug).resolve()
        if os.path.commonpath((str(archive_root), str(candidate))) != str(archive_root):
            raise RuntimeError("static path escapes archive directory")
        if candidate.is_dir():
            candidate /= "index.html"
        if not candidate.is_file():
            # Let the client-side router handle extensionless talk routes.
            if "." not in parts[-1]:
                candidate = archive_root / "index.html"
            else:
                raise FileNotFoundError(f"static file not found: {path}")
        self.serve_static_file(candidate, head_only)

    def serve_static_file(self, candidate: Path, head_only: bool) -> None:
        if not candidate.is_file():
            raise FileNotFoundError(f"static file not found: {candidate}")
        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.add_cors_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def serve_user_static(self, parts: list[str], head_only: bool) -> None:
        """Serve the SPA user route and resolve its relative talk assets."""
        if len(parts) == 2 and ID_RE.fullmatch(parts[1]):
            _, talk_root = self.find_user_talk(parts[1])
            self.serve_static_file(talk_root / "index.html", head_only)
            return

        asset_index = next(
            (
                index
                for index, part in enumerate(parts)
                if part in {"appimg_images", "output", "css", "js", "external"}
                or part == "manifest.json"
            ),
            None,
        )
        if asset_index is None:
            raise FileNotFoundError("unknown user route")
        relative = Path(*parts[asset_index:])
        roots: list[Path] = []
        if len(parts) >= 3 and ID_RE.fullmatch(parts[1]):
            _, talk_root = self.find_user_talk(parts[1])
            roots.append(talk_root)
        else:
            mapping = load_mapping(self.root)
            for context in mapping.values():
                slug = context.get("slug", "")
                if ID_RE.fullmatch(slug):
                    roots.append((self.root / slug).resolve())

        for talk_root in roots:
            candidate = (talk_root / relative).resolve()
            if os.path.commonpath((str(talk_root), str(candidate))) != str(talk_root):
                continue
            if candidate.is_file():
                self.serve_static_file(candidate, head_only)
                return
        raise FileNotFoundError(f"user asset not found: {'/'.join(parts)}")

    def log_message(self, format: str, *args: object) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args))


class ArchiveServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], root: Path) -> None:
        self.archive_root = root.resolve()
        super().__init__(address, ArchiveHandler)


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    load_mapping(root)
    server = ArchiveServer((args.host, args.port), root)
    print(f"serving {root} at http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
