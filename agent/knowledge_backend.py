"""Knowledge backend abstraction for local vault/document recall."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


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
        return {
            "success": True,
            "backend": "qmd",
            "available": self.is_available(),
            "collection": self.collection,
            "data_dir": os.getenv("QMD_DATA_DIR", ""),
        }

    def search(self, query: str, limit: int = 5, mode: str = "semantic") -> Dict[str, Any]:
        requested_mode = (mode or "semantic").strip().lower()
        mode = requested_mode
        if mode not in self._MODE_TO_SUBCOMMAND:
            return {"success": False, "error": f"Invalid knowledge search mode: {mode}"}
        if not self.is_available():
            return {"success": False, "error": "qmd is not installed or not on PATH."}

        limit = max(1, min(int(limit), 10))
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

    def read(self, ref: str) -> Dict[str, Any]:
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
    def _qmd_environment() -> Dict[str, str]:
        """Map Hermes' persistent QMD path to QMD's XDG path contract."""
        env = os.environ.copy()
        data_dir = env.get("QMD_DATA_DIR", "").strip()
        if not data_dir:
            return env

        data_path = Path(os.path.abspath(os.path.expanduser(data_dir)))
        root = data_path.parent if data_path.name == "qmd" else data_path
        # Preserve explicit operator overrides, while making QMD_DATA_DIR
        # useful with stock QMD, which does not read that variable itself.
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
