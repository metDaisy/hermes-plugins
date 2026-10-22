"""Fresh Hermes process adapter for profile-independent controller steps."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from .agent_contracts import AgentResult, StepInvocation

_MAX_CONTEXT_CHARS = 32_000
_MAX_QUERY_CHARS = 48_000
_MAX_STREAM_CHARS = 1_000_000
_NO_TOOLS_TOOLSET = "context_engine"
_ALLOWED_RUNTIME_ENV_KEYS = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT",
    }
)
_ALLOWED_RUNTIME_ENV_SUFFIXES = (
    "_ACCESS_TOKEN",
    "_API_KEY",
    "_API_TOKEN",
    "_API_URL",
    "_API_VERSION",
    "_AUTH_TOKEN",
    "_BASE_URL",
    "_ENDPOINT",
)
_BASE_ENV_KEYS = (
    "LANG",
    "LC_ALL",
    "PATH",
    "PATHEXT",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "WINDIR",
)


def validate_process_invocation_budget(invocation: StepInvocation) -> None:
    """Fail when an invocation cannot cross the fresh-process query boundary."""

    HermesProcessExecutor._query(invocation)


class HermesProcessExecutor:
    """Execute each step in a fresh ``hermes chat`` process and isolated Hermes home."""

    def __init__(
        self,
        *,
        hermes_executable: str,
        hermes_source_root: Path,
        model: str,
        provider: str,
        workspace: Path,
        scratch_dir: Path,
        isolated_home: Path,
        runtime_environment: Mapping[str, str] | None = None,
        run_fn: Callable[..., Any] = subprocess.run,
    ) -> None:
        executable = str(hermes_executable or "").strip()
        resolved_model = str(model or "").strip()
        resolved_provider = str(provider or "").strip()
        resolved_source_root = Path(hermes_source_root).resolve()
        resolved_workspace = Path(workspace).resolve()
        resolved_isolated_home = Path(isolated_home).resolve()
        if not executable:
            raise ValueError("Hermes executable is required")
        if not resolved_model:
            raise ValueError("Hermes model is required")
        if not resolved_provider:
            raise ValueError("Hermes provider is required")
        if not (resolved_source_root / "cli.py").is_file() or not (
            resolved_source_root / "hermes_cli"
        ).is_dir():
            raise ValueError("Hermes source root is required")
        if not resolved_workspace.is_dir():
            raise ValueError("Hermes workspace is required")
        if resolved_isolated_home == resolved_workspace or resolved_workspace in resolved_isolated_home.parents:
            raise ValueError("Hermes isolated home must be outside the workspace")
        self._hermes_executable = executable
        self._model = resolved_model
        self._provider = resolved_provider
        self._workspace = resolved_workspace
        self._scratch_dir = Path(scratch_dir).resolve()
        self._isolated_home_root = resolved_isolated_home
        self._source_dotenv_keys = self._source_dotenv_keys_from(resolved_source_root)
        self._runtime_environment = self._validated_runtime_environment(
            runtime_environment or {}
        )
        self._run_fn = run_fn

    @property
    def workspace(self) -> Path:
        """Return the exact workspace used as process cwd and ``--in`` target."""

        return self._workspace

    def invoke(self, invocation: StepInvocation) -> AgentResult:
        if invocation.working_directory is not None and (
            Path(invocation.working_directory).resolve() != self._workspace
        ):
            raise ValueError("Hermes invocation workspace does not match executor workspace")
        query = self._query(invocation)
        query_path = self._write_query(query)
        invocation_home: Path | None = None
        try:
            self._isolated_home_root.mkdir(parents=True, exist_ok=True)
            invocation_home = Path(
                tempfile.mkdtemp(prefix="hermes-step-", dir=self._isolated_home_root)
            )
            completed = self._run_fn(
                self._command(invocation, query_path),
                cwd=str(self._workspace),
                env=self._environment(invocation_home),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                shell=False,
            )
        finally:
            self._cleanup_invocation_resources(query_path, invocation_home)
        return self._result(completed)

    def _command(self, invocation: StepInvocation, query_path: Path) -> list[str]:
        command = [
            self._hermes_executable,
            "chat",
            "--oneshot",
            "--quiet",
            "--ignore-rules",
            "--ignore-user-config",
            "--model",
            self._model,
            "--provider",
            self._provider,
            "--toolsets",
            ",".join(invocation.allowed_toolsets) or _NO_TOOLS_TOOLSET,
            "--source",
            "tool",
            "--format",
            "stream-json",
            "--no-restore-cwd",
            "--in",
            str(self._workspace),
            "--query-file",
            str(query_path),
        ]
        return command

    @staticmethod
    def _cleanup_invocation_home(invocation_home: Path) -> None:
        try:
            shutil.rmtree(invocation_home)
        except OSError as exc:
            raise RuntimeError("Hermes invocation home cleanup failed") from exc
        if invocation_home.exists():
            raise RuntimeError("Hermes invocation home cleanup failed")

    @classmethod
    def _cleanup_invocation_resources(
        cls,
        query_path: Path,
        invocation_home: Path | None,
    ) -> None:
        failures: list[tuple[str, BaseException]] = []
        try:
            query_path.unlink(missing_ok=True)
        except OSError as exc:
            failures.append(("query-file", exc))
        if invocation_home is not None:
            try:
                cls._cleanup_invocation_home(invocation_home)
            except RuntimeError as exc:
                failures.append(("invocation-home", exc))
        if failures:
            resources = " and ".join(name for name, _exc in failures)
            raise RuntimeError(f"Hermes {resources} cleanup failed") from failures[-1][1]

    def _environment(self, isolated_home: Path) -> dict[str, str]:
        environment = {key: os.environ[key] for key in _BASE_ENV_KEYS if key in os.environ}
        environment.update({key: "" for key in self._source_dotenv_keys})
        environment.update(self._runtime_environment)
        user_home = isolated_home / "user"
        appdata = user_home / "AppData" / "Roaming"
        local_appdata = user_home / "AppData" / "Local"
        temp_dir = isolated_home / "tmp"
        hermes_home = isolated_home / "hermes"
        managed_dir = isolated_home / "managed"
        for directory in (
            user_home,
            appdata,
            local_appdata,
            temp_dir,
            hermes_home,
            managed_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        (hermes_home / ".env").write_text("\n", encoding="utf-8")
        environment.update(
            {
                "APPDATA": str(appdata),
                "HERMES_HOME": str(hermes_home),
                "HERMES_MANAGED_DIR": str(managed_dir),
                "HOME": str(user_home),
                "LOCALAPPDATA": str(local_appdata),
                "TEMP": str(temp_dir),
                "TMP": str(temp_dir),
                "TMPDIR": str(temp_dir),
                "USERPROFILE": str(user_home),
            }
        )
        prefill_override = isolated_home / "disabled-prefill-messages.json"
        prefill_override.write_text("[]\n", encoding="utf-8")
        environment["HERMES_PREFILL_MESSAGES_FILE"] = str(prefill_override)
        return environment

    @staticmethod
    def _validated_runtime_environment(values: Mapping[str, str]) -> dict[str, str]:
        environment: dict[str, str] = {}
        for raw_name, raw_value in values.items():
            name = str(raw_name or "").strip()
            if not name or name.upper().startswith("HERMES_") or not (
                name in _ALLOWED_RUNTIME_ENV_KEYS
                or name.endswith(_ALLOWED_RUNTIME_ENV_SUFFIXES)
            ):
                raise ValueError("Hermes runtime environment contains a forbidden key")
            environment[name] = str(raw_value)
        return environment

    @staticmethod
    def _source_dotenv_keys_from(source_root: Path) -> tuple[str, ...]:
        dotenv_path = source_root / ".env"
        if not dotenv_path.is_file():
            return ()
        try:
            parsed = dotenv_values(dotenv_path, encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise ValueError("Hermes source dotenv is unreadable") from exc
        return tuple(str(key) for key in parsed if str(key))

    def _write_query(self, query: str) -> Path:
        self._scratch_dir.mkdir(parents=True, exist_ok=True)
        query_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix="controller-step-",
                suffix=".txt",
                dir=self._scratch_dir,
                delete=False,
            ) as stream:
                query_path = Path(stream.name)
                stream.write(query)
            return query_path
        except BaseException:
            if query_path is not None:
                try:
                    query_path.unlink(missing_ok=True)
                except OSError as cleanup_exc:
                    raise RuntimeError("Hermes partial query-file cleanup failed") from cleanup_exc
            raise

    @classmethod
    def _query(cls, invocation: StepInvocation) -> str:
        envelope = {
            "protocol": "hermes-langchain/controller-step-v1",
            "run_id": invocation.run_id,
            "invocation_id": invocation.invocation_id,
            "step_id": invocation.step_id,
            "input": invocation.input,
            "acceptance_criteria": list(invocation.acceptance_criteria),
            "output_schema": invocation.output_schema,
            "skills": list(invocation.skills),
            "final_response_contract": "Return only one JSON object matching output_schema.",
        }
        context = json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(context) > _MAX_CONTEXT_CHARS:
            raise ValueError("Hermes projected context exceeds 32000 characters")
        query = f"{invocation.goal}\n\n{context}"
        if len(query) > _MAX_QUERY_CHARS:
            raise ValueError("Hermes step query exceeds 48000 characters")
        return query

    @classmethod
    def _result(cls, completed: Any) -> AgentResult:
        stdout = getattr(completed, "stdout", "")
        if not isinstance(stdout, str) or len(stdout) > _MAX_STREAM_CHARS:
            raise RuntimeError("Hermes process returned an invalid event stream")
        results: list[Mapping[str, Any]] = []
        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError("Hermes process returned an invalid event stream") from exc
            if not isinstance(event, Mapping):
                raise RuntimeError("Hermes process returned an invalid event stream")
            if event.get("type") == "result":
                results.append(event)
        if len(results) != 1:
            raise RuntimeError("Hermes process returned no unique terminal result")
        terminal = results[0]
        if int(getattr(completed, "returncode", 1)) != 0 or terminal.get("exit_code") != 0:
            raise RuntimeError("Hermes process step failed")
        session_id = str(terminal.get("session_id") or "").strip()
        if not session_id:
            raise RuntimeError("Hermes process returned no stable session identity")
        raw_output = terminal.get("text")
        try:
            output = json.loads(raw_output) if isinstance(raw_output, str) else None
        except json.JSONDecodeError as exc:
            raise ValueError("Hermes process result must be a valid JSON object") from exc
        if not isinstance(output, dict):
            raise ValueError("Hermes process result must be a valid JSON object")
        return AgentResult(agent_id=session_id, output=output)
