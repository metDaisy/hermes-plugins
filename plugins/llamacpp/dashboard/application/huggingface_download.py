"""Hugging Face download-worker execution and exact-path validation."""
from __future__ import annotations

import re
import shlex
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable


class HuggingFaceDownloadService:
    """Run ``hf download`` without overriding the user's HF storage policy."""

    _percent = re.compile(r"(?<!\d)(\d{1,3})\s*%")
    _ansi = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
    _human_path = re.compile(r"^path:\s*(.+)$", re.IGNORECASE)

    def __init__(self, start_process: Callable[[list[str]], Any], cwd: Callable[[], Path]) -> None:
        self._start_process = start_process
        self._cwd = cwd

    def download(self, executable: str, repo: str, path: str, observe_percent: Callable[[int], None] | None = None) -> Path:
        uri = f"hf://{repo}/{path}"
        worker = Path(__file__).with_name("hf_download_worker.py")
        process = self._start_process([self._python_executable(executable), str(worker), repo, path])
        stdout_chunks: list[str] = []
        stderr_tail = ""

        def read_available(stream: Any) -> Any:
            read1 = getattr(stream, "read1", None)
            return read1(4096) if callable(read1) else stream.read(4096)

        def consume_stdout() -> None:
            stream = process.stdout
            if stream is None:
                return
            while chunk := read_available(stream):
                stdout_chunks.append(chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk))

        def consume_stderr() -> None:
            nonlocal stderr_tail
            stream = process.stderr
            if stream is None:
                return
            while chunk := read_available(stream):
                text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
                stderr_tail = (stderr_tail + text)[-4096:]
                if observe_percent:
                    matches = self._percent.findall(stderr_tail)
                    if matches:
                        observe_percent(min(100, max(0, int(matches[-1]))))

        stdout_thread = threading.Thread(target=consume_stdout, daemon=True, name="llamacpp-hf-stdout")
        stderr_thread = threading.Thread(target=consume_stderr, daemon=True, name="llamacpp-hf-progress")
        stdout_thread.start()
        stderr_thread.start()
        try:
            returncode = process.wait(timeout=6 * 60 * 60)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            raise RuntimeError(f"hf download timed out for {uri}") from exc
        stdout_thread.join()
        stderr_thread.join()
        if returncode != 0:
            raise RuntimeError(f"hf download failed for {uri} (exit {returncode})")
        lines = [self._ansi.sub("", line).strip() for line in "".join(stdout_chunks).splitlines() if line.strip()]
        if not lines:
            raise RuntimeError(f"hf download returned no local path for {uri}")
        reported_text = lines[-1]
        for line in reversed(lines):
            match = self._human_path.match(line)
            if match:
                reported_text = match.group(1).strip()
                break
        reported = Path(reported_text)
        if not reported.is_absolute():
            reported = (self._cwd() / reported).resolve()
        if not reported.is_file():
            raise RuntimeError(f"hf download returned a missing local path for {uri}")
        return reported.resolve()

    @staticmethod
    def _python_executable(hf_executable: str) -> str:
        hf_path = Path(hf_executable)
        for name in ("python.exe", "python3", "python"):
            candidate = hf_path.with_name(name)
            if candidate.is_file():
                return str(candidate)
        try:
            first_line = hf_path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
        except (OSError, IndexError):
            first_line = ""
        if first_line.startswith("#!"):
            command = shlex.split(first_line[2:].strip())
            if command:
                return command[0]
        raise RuntimeError("could not resolve the Python environment that provides hf CLI")
