"""Knowledge backend abstraction for local vault/document recall."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import unquote

from utils import fast_safe_load


class QmdKnowledgeBackend:
    """Thin wrapper around qmd for local knowledge search and reads."""

    _MODE_TO_SUBCOMMAND = {
        "keyword": "search",
        # A structured vec query bypasses QMD's local LLM query-expansion
        # model.  Recall remains semantic, but the normal Hermes path does
        # not need to load a 1.7B generator for every lookup.
        "semantic": "query",
        "hybrid": "query",
    }

    _COMMAND_TIMEOUTS = {
        "keyword": 10,
        "semantic": 15,
        "hybrid": 25,
        "read": 10,
    }

    def __init__(
        self,
        *,
        collection: str = "obsidian",
        which: Callable[[str], Optional[str]] = shutil.which,
        runner: Callable[..., Any] = subprocess.run,
    ) -> None:
        self.collection = collection
        self._which = which
        self._runner = runner

    def is_available(self) -> bool:
        return bool(self._which("qmd"))

    def status(self) -> Dict[str, Any]:
        env = self._qmd_environment()
        return {
            "success": True,
            "backend": "qmd",
            "available": self.is_available(),
            "collection": self.collection,
            "data_dir": env.get("QMD_DATA_DIR", ""),
        }

    def search(self, query: str, limit: int = 5, mode: str = "semantic") -> Dict[str, Any]:
        requested_mode = (mode or "semantic").strip().lower()
        mode = requested_mode
        if mode not in self._MODE_TO_SUBCOMMAND:
            return {"success": False, "error": f"Invalid knowledge search mode: {mode}"}
        if not self.is_available():
            return {"success": False, "error": "qmd is not installed or not on PATH."}

        limit = max(1, min(int(limit), 10))
        path_result = self._search_exact_path(
            query=query,
            limit=limit,
            requested_mode=requested_mode,
        )
        if path_result is not None:
            return path_result

        result = self._search_with_mode(query=query, limit=limit, mode=mode)
        if result["success"]:
            result.setdefault("requested_mode", requested_mode)
            return result

        if mode == "hybrid":
            fallback = self._search_with_mode(query=query, limit=limit, mode="semantic")
            if fallback["success"]:
                fallback["requested_mode"] = requested_mode
                fallback["fallback_from"] = "hybrid"
                fallback["fallback_reason"] = result["error"]
            return fallback

        return result

    def _search_exact_path(
        self,
        *,
        query: str,
        limit: int,
        requested_mode: str,
    ) -> Optional[Dict[str, Any]]:
        """Resolve an exact collection-relative path before content search.

        QMD's text indexes do not treat document paths as searchable content. A
        user or model asking for ``AI/Interests.md`` should still get the same
        safe ``file`` ref that a semantic result would return, without turning
        this convenience into an arbitrary filesystem read.
        """
        ref = self._path_query_ref(query)
        if ref is None:
            return None

        result = self._run_command(["qmd", "get", ref], timeout=self._COMMAND_TIMEOUTS["read"])
        if not result["success"]:
            return None

        relative = ref.removeprefix(f"qmd://{self.collection}/")
        title = Path(relative).stem
        lines = [line.strip() for line in result["stdout"].splitlines() if line.strip()]
        for line in lines:
            if line.startswith("#"):
                title = line.lstrip("#").strip() or title
                break

        return {
            "success": True,
            "backend": "qmd",
            "mode": "keyword",
            "requested_mode": requested_mode,
            "path_match": True,
            "query": query,
            "limit": limit,
            "collection": self.collection,
            "results": [{
                "file": ref,
                "line": 1,
                "score": 1.0,
                "title": title,
                "snippet": " ".join(lines)[:240],
            }],
        }

    def _path_query_ref(self, query: str) -> Optional[str]:
        """Return a validated qmd ref only for an exact collection path query."""
        if not isinstance(query, str):
            return None
        candidate = query.strip()
        prefix = f"qmd://{self.collection}/"
        if candidate.startswith(prefix):
            ref = candidate
        elif not candidate.startswith("qmd://") and candidate.casefold().endswith(".md"):
            ref = f"{prefix}{candidate}"
        else:
            return None
        return ref if self._validate_read_ref(ref) is None else None

    def _search_with_mode(self, query: str, limit: int, mode: str) -> Dict[str, Any]:
        search_query = query
        search_options: List[str] = []
        if mode == "semantic":
            # QMD's structured endpoint accepts a single-line vec query and
            # skips its own expansion pass.  Normalize multiline user input
            # so a query cannot accidentally become malformed QMD syntax.
            search_query = f"vec: {query.replace(chr(10), ' ').replace(chr(13), ' ')}"
            search_options.append("--no-rerank")
        cmd = [
            "qmd",
            self._MODE_TO_SUBCOMMAND[mode],
            search_query,
            "-c",
            self.collection,
            "-n",
            str(limit),
            "--json",
            *search_options,
        ]
        result = self._run_command(cmd, timeout=self._COMMAND_TIMEOUTS.get(mode, 15))
        if result["success"] is False:
            return result

        parsed = self._parse_json_output(result["stdout"])
        if parsed is None:
            return {
                "success": False,
                "error": "qmd returned non-JSON output for knowledge search.",
                "raw_output": result["stdout"],
            }

        payload = parsed if isinstance(parsed, dict) else {"results": parsed}
        payload.setdefault("results", [])
        payload.update({
            "success": True,
            "backend": "qmd",
            "mode": mode,
            "query": query,
            "limit": limit,
            "collection": self.collection,
        })
        return payload

    def _validate_read_ref(self, ref: str) -> Optional[str]:
        """Keep reads inside the configured qmd collection namespace."""
        prefix = f"qmd://{self.collection}/"
        if not isinstance(ref, str) or not ref or not ref.startswith(prefix):
            return "knowledge_read accepts only refs from the configured collection."
        raw_relative = ref[len(prefix):]
        relative = raw_relative
        for _ in range(3):
            decoded = unquote(relative)
            if decoded == relative:
                break
            relative = decoded
        for candidate in (raw_relative, relative):
            if (
                not candidate
                or candidate.startswith("/")
                or "\\" in candidate
                or "\x00" in candidate
                or any(
                    len(part) >= 2
                    and part[0] in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                    and part[1] == ":"
                    for part in candidate.split("/")
                )
                or any(part in {"", ".", ".."} for part in candidate.split("/"))
            ):
                return "knowledge_read accepts only refs from the configured collection."
        if relative != raw_relative and "%" in relative:
            return "knowledge_read accepts only refs from the configured collection."

        collection_root = self._resolve_collection_root()
        if collection_root is None:
            return "knowledge_read accepts only refs from the configured collection."
        candidate_path = collection_root / relative
        try:
            if self._has_symlink_component(collection_root) or self._has_symlink_component(candidate_path):
                return "knowledge_read accepts only refs from the configured collection."
            canonical_root = collection_root.resolve(strict=True)
            if not canonical_root.is_dir():
                return "knowledge_read accepts only refs from the configured collection."
            canonical_candidate = candidate_path.resolve(strict=False)
            canonical_candidate.relative_to(canonical_root)
        except (OSError, RuntimeError, ValueError):
            return "knowledge_read accepts only refs from the configured collection."
        return None

    def _resolve_collection_root(self) -> Optional[Path]:
        """Resolve this profile's collection root from QMD's active index."""
        try:
            env = self._qmd_environment()
            data_dir = env.get("QMD_DATA_DIR", "").strip()
            if not data_dir:
                return None
            data_path = Path(os.path.abspath(os.path.expanduser(os.path.expandvars(data_dir))))
            config_home = env.get("XDG_CONFIG_HOME", "").strip()
            if not config_home:
                config_home = os.fspath(data_path.parent if data_path.name == "qmd" else data_path)
            index_path = Path(os.path.abspath(os.path.expanduser(config_home))) / "qmd" / "index.yml"
            with index_path.open(encoding="utf-8") as index_file:
                index = fast_safe_load(index_file) or {}
            collections = index.get("collections") if isinstance(index, dict) else None
            entry = collections.get(self.collection) if isinstance(collections, dict) else None
            raw_path = entry.get("path") if isinstance(entry, dict) else None
            if not isinstance(raw_path, str) or not raw_path.strip():
                return None
            collection_root = Path(os.path.expanduser(os.path.expandvars(raw_path.strip())))
            if not collection_root.is_absolute():
                collection_root = index_path.parent / collection_root
            return collection_root
        except Exception:
            return None

    @staticmethod
    def _has_symlink_component(path: Path) -> bool:
        """Return True when any existing component of *path* is a symlink."""
        current = Path(path.anchor)
        for part in path.parts:
            if part == path.anchor:
                continue
            current /= part
            try:
                if stat.S_ISLNK(os.lstat(current).st_mode):
                    return True
            except FileNotFoundError:
                continue
            except OSError:
                return True
        return False

    def read(self, ref: str) -> Dict[str, Any]:
        validation_error = self._validate_read_ref(ref)
        if validation_error:
            return {"success": False, "error": validation_error}
        if not self.is_available():
            return {"success": False, "error": "qmd is not installed or not on PATH."}
        result = self._run_command(["qmd", "get", ref], timeout=self._COMMAND_TIMEOUTS["read"])
        if result["success"] is False:
            return result
        return {
            "success": True,
            "ref": ref,
            "content": result["stdout"],
        }

    def _run_command(self, cmd: List[str], timeout: int) -> Dict[str, Any]:
        try:
            completed = self._runner(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                env=self._qmd_environment(),
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "qmd command timed out."}
        except Exception as exc:
            return {"success": False, "error": f"qmd command failed: {exc}"}

        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        if completed.returncode != 0:
            detail = stderr or stdout or f"exit code {completed.returncode}"
            return {"success": False, "error": f"qmd command failed: {detail}"}
        return {"success": True, "stdout": stdout, "stderr": stderr}

    @staticmethod
    def _configured_data_dir() -> str:
        """Resolve the active profile's QMD path when the process env has no override."""
        try:
            from hermes_cli.config import read_raw_config

            config = read_raw_config()
            knowledge = config.get("knowledge")
            if isinstance(knowledge, dict):
                data_dir = knowledge.get("qmd_data_dir")
                if isinstance(data_dir, str) and data_dir.strip():
                    return data_dir.strip()
        except Exception:
            # Knowledge availability must not make profile startup fail. The
            # caller receives the same unavailable-collection result as before.
            pass
        return ""

    @classmethod
    def _qmd_environment(cls) -> Dict[str, str]:
        """Map the active profile's persistent QMD path to QMD's XDG contract."""
        env = os.environ.copy()
        configured_data_dir = cls._configured_data_dir()
        data_dir = configured_data_dir or env.get("QMD_DATA_DIR", "").strip()
        if not data_dir:
            return env

        data_path = Path(os.path.abspath(os.path.expanduser(data_dir)))
        root = data_path.parent if data_path.name == "qmd" else data_path
        env["QMD_DATA_DIR"] = data_dir
        # Preserve explicit operator overrides, while making QMD_DATA_DIR
        # useful with stock QMD, which does not read that variable itself.
        if configured_data_dir:
            env["XDG_CACHE_HOME"] = os.fspath(root)
            env["XDG_CONFIG_HOME"] = os.fspath(root)
        else:
            env.setdefault("XDG_CACHE_HOME", os.fspath(root))
            env.setdefault("XDG_CONFIG_HOME", os.fspath(root))
        return env

    @staticmethod
    def _parse_json_output(stdout: str) -> Any:
        if not stdout:
            return []
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            return None
