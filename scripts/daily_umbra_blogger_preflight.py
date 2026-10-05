#!/usr/bin/env python3
"""Bounded, read-only preflight for the daily Umbra Blogger job.

The editorial agent receives this compact JSON as cron script output.  All
provider access stays in the established Google Workspace connector; this
script never writes to Blogger.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

from hermes_constants import get_hermes_home

BLOG_URL = "https://negaterium.blogspot.com/"
PUBLISH_TZ = ZoneInfo("Europe/Bucharest")
ARCHIVE_DIR = Path("/root/obsidian-vault/AI/Umbra Blog Chronicles")
MAX_BODY_CHARS = 6500


def _policy_path() -> Path:
    return get_hermes_home() / "state" / "blog-editorial-reset" / "publication-policy.json"


def _publication_mode() -> str:
    policy = json.loads(_policy_path().read_text(encoding="utf-8-sig"))
    mode = policy.get("publication_mode")
    if mode not in {"review_only", "live"}:
        raise ValueError("editorial publication mode must be review_only or live")
    return mode


class _PostText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag in {"p", "h1", "h2", "h3", "li", "blockquote", "br", "figcaption"}:
            self.parts.append("\n")
        if tag == "a":
            for key, value in attrs:
                if key == "href" and value and value.startswith("https://"):
                    self.links.append(value)

    def handle_data(self, data):
        self.parts.append(data)


def _load_connector():
    connector_dir = str(get_hermes_home() / "skills" / "productivity" / "google-workspace" / "scripts")
    if connector_dir not in sys.path:
        sys.path.insert(0, connector_dir)
    import google_api  # type: ignore

    return google_api


def _safe_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".replace("\n", " ")
    return text[:400]


def _local_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(PUBLISH_TZ).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _post_summary(post: dict) -> dict:
    published = post.get("published") or post.get("updated") or ""
    parser = _PostText()
    parser.feed(str(post.get("content", "")))
    text = "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())
    truncated = len(text) > MAX_BODY_CHARS
    if truncated:
        text = text[:MAX_BODY_CHARS - 1800] + "\n[body excerpt omitted]\n" + text[-1800:]
    return {
        "id": str(post.get("id", "")),
        "title": str(post.get("title", "")),
        "url": str(post.get("url", "")),
        "published": str(post.get("published", "")),
        "local_date": _local_date(published),
        "body_text": text,
        "body_truncated": truncated,
        "source_urls": list(dict.fromkeys(parser.links))[:20],
    }


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def main() -> int:
    now = datetime.now(PUBLISH_TZ)
    publish_date = now.date().isoformat()

    try:
        if _publication_mode() == "review_only":
            _emit({"status": "BLOCKED", "date": publish_date, "reason": "editorial_pilot_review_required"})
            return 0
        api = _load_connector()
        service = api.blogger_service()
        blog = service.blogs().getByUrl(url=BLOG_URL).execute()
        blog_id = str(blog.get("id", ""))
        resolved_url = str(blog.get("url", ""))
        if not blog_id:
            raise RuntimeError("Blogger URL resolved without a blog ID")
        if resolved_url and resolved_url.rstrip("/") != BLOG_URL.rstrip("/"):
            raise RuntimeError(f"resolved blog URL mismatch: {resolved_url}")

        response = service.posts().list(
            blogId=blog_id,
            maxResults=10,
            fetchBodies=True,
            status="LIVE",
            orderBy="PUBLISHED",
        ).execute()
        posts = response.get("items", []) or []
        summaries = [_post_summary(post) for post in posts]
        today_posts = [post for post in summaries if post["local_date"] == publish_date]
        archive_collisions = []
        if ARCHIVE_DIR.exists():
            archive_collisions = sorted(
                path.name
                for path in ARCHIVE_DIR.glob(f"{publish_date}--*.md")
                if path.is_file()
            )

        status = "READY"
        reason = None
        if today_posts:
            status = "BLOCKED"
            reason = "today_already_has_live_post"
        elif archive_collisions:
            status = "BLOCKED"
            reason = "today_archive_collision"

        payload = {
            "status": status,
            "date": publish_date,
            "current_local": now.isoformat(timespec="seconds"),
            "blog": {
                "id": blog_id,
                "name": str(blog.get("name", "")),
                "url": resolved_url or BLOG_URL,
                "posts": blog.get("posts", {}).get("totalItems", 0),
            },
            "today_live_posts": today_posts[:5],
            "archive_collisions": archive_collisions[:5],
            "recent_live_posts": summaries,
            "max_posts_for_date": 1,
        }
        if reason:
            payload["reason"] = reason
        _emit(payload)
        return 0
    except Exception as exc:  # Keep cron output machine-readable and secret-free.
        _emit(
            {
                "status": "FAILED",
                "stage": "preflight",
                "date": publish_date,
                "current_local": now.isoformat(timespec="seconds"),
                "error": _safe_error(exc),
            }
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
