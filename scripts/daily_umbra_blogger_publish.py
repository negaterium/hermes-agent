#!/usr/bin/env python3
"""Idempotent Blogger insert, live readback, and exact archive writer.

The model supplies a JSON draft.  This script owns every provider-side write
and its verification so a long editorial turn cannot accidentally repeat an
insert or report success from an API response alone.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from hermes_constants import get_hermes_home

if __package__:
    from .daily_umbra_blogger_preflight import _publication_mode
else:
    from daily_umbra_blogger_preflight import _publication_mode

BLOG_URL = "https://negaterium.blogspot.com/"
PUBLISH_TZ = ZoneInfo("Europe/Bucharest")
ARCHIVE_DIR = Path("/root/obsidian-vault/AI/Umbra Blog Chronicles")

AMBIGUOUS_STATES = {
    "publish_attempted",
    "reconcile_required",
    "live_verified",
    "archive_verified",
    "completed",
}


def _load_connector():
    connector_dir = str(get_hermes_home() / "skills" / "productivity" / "google-workspace" / "scripts")
    if connector_dir not in sys.path:
        sys.path.insert(0, connector_dir)
    import google_api  # type: ignore

    return google_api


def _safe_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".replace("\n", " ")
    # Never allow an accidental credential-shaped diagnostic into cron output.
    for marker in ("access_token", "refresh_token", "client_secret", "token.json"):
        text = text.replace(marker, "[redacted]")
    return text[:500]


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


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
    return {
        "id": str(post.get("id", "")),
        "title": str(post.get("title", "")),
        "url": str(post.get("url", "")),
        "published": str(post.get("published", "")),
        "local_date": _local_date(post.get("published") or post.get("updated")),
        "status": str(post.get("status", "LIVE") or "LIVE").upper(),
    }


def _validate_editorial(brief: dict) -> None:
    evidence = brief.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("editorial evidence must include reviewed material")
    methods = {"full_source_review", "executed_experiment", "reviewed_specific_work"}
    for item in evidence:
        if (
            not isinstance(item, dict)
            or item.get("method") not in methods
            or item.get("reviewed") is not True
            or not isinstance(item.get("reference"), str)
            or not item["reference"].strip()
        ):
            raise ValueError("editorial evidence must be reviewed full sources, executed experiments, or specific works; snippets are discovery only")
    fields = ("subject", "reader_value", "lane", "format", "argument", "new_material")
    if any(not isinstance(brief.get(key), str) or not brief[key].strip() for key in fields):
        raise ValueError("editorial brief requires subject, reader_value, lane, format, argument, and new_material")
    if brief.get("review_passed") is not True:
        raise ValueError("editorial brief must pass substantive review before publication")


def _read_draft(path: Path) -> tuple[str, str, dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("draft must be a JSON object")
    if not isinstance(payload.get("editorial"), dict):
        raise ValueError("draft requires an editorial brief")
    _validate_editorial(payload["editorial"])
    title = str(payload.get("title", "")).strip()
    body = str(payload.get("body", payload.get("content", ""))).strip()
    if not title or not body:
        raise ValueError("draft requires non-empty title and body")
    if "\n" in title or "\r" in title or len(title) > 200:
        raise ValueError("title must be one line and at most 200 characters")
    if len(body) > 120_000:
        raise ValueError("body is unexpectedly large")
    lowered = body.lower()
    if "<script" in lowered or "javascript:" in lowered:
        raise ValueError("unsafe script content is not permitted")
    text = unescape(re.sub(r"<[^>]+>", " ", body))
    word_count = len(re.findall(r"\b[\w’'-]+\b", text))
    if word_count > 800:
        raise ValueError(f"body is {word_count} words; hard ceiling is 800")
    return title, body, {"word_count": word_count, **payload}


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _new_state(path: Path, **updates) -> dict:
    current = {}
    if path.exists():
        current = json.loads(path.read_text(encoding="utf-8"))
    current.update(updates)
    _atomic_json(path, current)
    return current


def _slugify(title: str) -> str:
    normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized.lower()).strip("-")
    return (slug[:90].rstrip("-") or "post")


def _archive_path(publish_date: str, title: str) -> Path:
    return ARCHIVE_DIR / f"{publish_date}--{_slugify(title)}.md"


def _target_blog_and_posts(service) -> tuple[dict, list[dict]]:
    blog = service.blogs().getByUrl(url=BLOG_URL).execute()
    blog_id = str(blog.get("id", ""))
    resolved_url = str(blog.get("url", ""))
    if not blog_id:
        raise RuntimeError("Blogger URL resolved without a blog ID")
    if resolved_url and resolved_url.rstrip("/") != BLOG_URL.rstrip("/"):
        raise RuntimeError(f"resolved blog URL mismatch: {resolved_url}")
    response = service.posts().list(
        blogId=blog_id,
        maxResults=100,
        fetchBodies=False,
        status="LIVE",
    ).execute()
    return {"id": blog_id, "name": str(blog.get("name", "")), "url": resolved_url or BLOG_URL}, response.get("items", []) or []


def _prior_ambiguous_run(publish_date: str) -> dict | None:
    state_dir = get_hermes_home() / "state" / "umbra-blogger"
    if not state_dir.exists():
        return None
    for path in sorted(state_dir.glob(f"{publish_date}-*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if record.get("state") in AMBIGUOUS_STATES:
            return {"path": str(path), "state": record.get("state"), "provider_id": record.get("provider_id")}
    return None


def _write_archive(path: Path, *, publish_date: str, title: str, provider_id: str, url: str, body: str, draft: dict) -> None:
    image_url = str(draft.get("image_url", "")).strip()
    image_source = str(draft.get("image_source", "")).strip()
    if image_url or image_source:
        if not image_url.startswith("https://") or not image_source.startswith("https://"):
            raise ValueError("image metadata must contain HTTPS image and source URLs")
    frontmatter = [
        "---",
        f"date: {publish_date}",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        "status: LIVE",
        f"blog: {json.dumps(BLOG_URL, ensure_ascii=False)}",
        f"provider_id: {json.dumps(provider_id, ensure_ascii=False)}",
        f"public_url: {json.dumps(url, ensure_ascii=False)}",
    ]
    if image_url and image_source:
        frontmatter.extend(
            [
                "image_policy: attributed",
                f"image_url: {json.dumps(image_url, ensure_ascii=False)}",
                f"image_source: {json.dumps(image_source, ensure_ascii=False)}",
            ]
        )
    else:
        frontmatter.append("image_policy: text-only")
    content = "\n".join(frontmatter) + "\n---\n\n" + body
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"archive collision: {path.name}")
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish one verified daily Umbra Blogger draft")
    parser.add_argument("--draft-file", required=True, type=Path)
    args = parser.parse_args()

    now = datetime.now(PUBLISH_TZ)
    publish_date = now.date().isoformat()
    run_id = f"{publish_date}-{uuid4().hex[:12]}"
    state_path = get_hermes_home() / "state" / "umbra-blogger" / f"{run_id}.json"

    try:
        if _publication_mode() == "review_only":
            _emit({"status": "BLOCKED", "stage": "editorial_gate", "date": publish_date, "reason": "editorial_pilot_review_required"})
            return 0
    except Exception as exc:
        _emit({"status": "FAILED", "stage": "editorial_gate", "date": publish_date, "error": _safe_error(exc)})
        return 0

    try:
        title, body, draft = _read_draft(args.draft_file)
    except Exception as exc:
        _emit({"status": "FAILED", "stage": "draft_validation", "date": publish_date, "error": _safe_error(exc)})
        return 0

    try:
        api = _load_connector()
        service = api.blogger_service()
        blog, raw_posts = _target_blog_and_posts(service)
        summaries = [_post_summary(post) for post in raw_posts]
        today_posts = [post for post in summaries if post["local_date"] == publish_date]
        if today_posts:
            _emit({"status": "BLOCKED", "stage": "duplicate_guard", "date": publish_date, "reason": "today_already_has_live_post", "posts": today_posts[:5]})
            return 0

        archive_path = _archive_path(publish_date, title)
        if archive_path.exists():
            _emit({"status": "BLOCKED", "stage": "duplicate_guard", "date": publish_date, "reason": "archive_collision", "archive": str(archive_path)})
            return 0

        prior = _prior_ambiguous_run(publish_date)
        if prior:
            _emit({"status": "RECONCILE_REQUIRED", "stage": "prior_run", "date": publish_date, "reason": "prior_publish_boundary_is_ambiguous", "run": prior})
            return 0

        record = {
            "run_id": run_id,
            "date": publish_date,
            "created_at": now.isoformat(timespec="seconds"),
            "state": "preflight",
            "blog_id": blog["id"],
            "title": title,
            "draft_file": str(args.draft_file),
            "editorial": draft["editorial"],
        }
        _atomic_json(state_path, record)
        _new_state(
            state_path,
            state="draft_saved",
            word_count=draft["word_count"],
            body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        )
    except Exception as exc:
        _emit({"status": "FAILED", "stage": "preflight", "date": publish_date, "error": _safe_error(exc)})
        return 0

    # Mark the irreversible boundary before calling Blogger.  If the process dies
    # after this point, the next run will reconcile instead of inserting blindly.
    try:
        _new_state(state_path, state="publish_attempted")
    except Exception as exc:
        _emit({"status": "FAILED", "stage": "state_before_insert", "date": publish_date, "error": _safe_error(exc)})
        return 0

    try:
        created = service.posts().insert(
            blogId=blog["id"],
            body={"title": title, "content": body},
            isDraft=False,
        ).execute()
    except Exception as exc:
        try:
            _new_state(state_path, state="reconcile_required", error=_safe_error(exc))
        except Exception:
            pass
        _emit({"status": "RECONCILE_REQUIRED", "stage": "insert", "date": publish_date, "reason": "Blogger insert boundary is uncertain", "error": _safe_error(exc)})
        return 0

    provider_id = str(created.get("id", ""))
    if not provider_id:
        try:
            _new_state(state_path, state="reconcile_required", reason="insert returned no provider ID")
        except Exception:
            pass
        _emit({"status": "RECONCILE_REQUIRED", "stage": "insert", "date": publish_date, "reason": "insert returned no provider ID"})
        return 0

    state_write_failed = False
    try:
        _new_state(
            state_path,
            provider_id=provider_id,
            provider_url=str(created.get("url", "")),
            state="publish_attempted",
        )
    except Exception:
        state_write_failed = True

    try:
        readback = service.posts().get(blogId=blog["id"], postId=provider_id, view="ADMIN").execute()
    except Exception as exc:
        try:
            _new_state(state_path, state="reconcile_required", provider_id=provider_id, error=_safe_error(exc))
        except Exception:
            pass
        _emit({"status": "RECONCILE_REQUIRED", "stage": "live_readback", "date": publish_date, "provider_id": provider_id, "reason": "authenticated readback failed", "error": _safe_error(exc)})
        return 0

    returned_title = str(readback.get("title", ""))
    returned_body = str(readback.get("content", ""))
    returned_url = str(readback.get("url", ""))
    returned_status = str(readback.get("status", "")).upper()
    checks = {
        "authenticated": True,
        "id_match": str(readback.get("id", "")) == provider_id,
        "title_match": returned_title == title,
        "body_match": returned_body == body,
        "status_live": returned_status == "LIVE",
        "public_url_present": bool(returned_url),
        "public_url_on_target_blog": returned_url.startswith(BLOG_URL.rstrip("/") + "/"),
    }
    if not all(checks.values()):
        try:
            _new_state(state_path, state="reconcile_required", provider_id=provider_id, checks=checks)
        except Exception:
            pass
        _emit({"status": "RECONCILE_REQUIRED", "stage": "live_readback", "date": publish_date, "provider_id": provider_id, "checks": checks})
        return 0

    try:
        _new_state(state_path, state="live_verified", provider_id=provider_id, public_url=returned_url, checks=checks)
    except Exception:
        state_write_failed = True

    try:
        _write_archive(
            archive_path,
            publish_date=publish_date,
            title=returned_title,
            provider_id=provider_id,
            url=returned_url,
            body=returned_body,
            draft=draft,
        )
        archived = archive_path.read_text(encoding="utf-8")
        archive_verified = (
            returned_title in archived
            and returned_url in archived
            and archived.endswith(returned_body)
        )
        if not archive_verified:
            raise ValueError("archive verification did not preserve title, URL, and exact HTML body")
    except Exception as exc:
        try:
            _new_state(state_path, state="live_verified", provider_id=provider_id, archive_error=_safe_error(exc))
        except Exception:
            pass
        _emit({"status": "LIVE_BUT_ARCHIVE_FAILED", "stage": "archive", "date": publish_date, "provider_id": provider_id, "title": returned_title, "url": returned_url, "error": _safe_error(exc)})
        return 0

    try:
        _new_state(state_path, state="archive_verified", archive=str(archive_path), provider_id=provider_id)
        _new_state(state_path, state="completed")
    except Exception:
        state_write_failed = True

    if state_write_failed:
        _emit({"status": "RECONCILE_REQUIRED", "stage": "state_after_publish", "date": publish_date, "provider_id": provider_id, "title": returned_title, "url": returned_url, "archive": str(archive_path), "reason": "publication and archive are verified but run-state persistence failed"})
        return 0

    _emit({"status": "PUBLISHED", "date": publish_date, "title": returned_title, "provider_id": provider_id, "url": returned_url, "archive": str(archive_path), "checks": checks})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
