"""Isolated Hugging Face download worker with machine-readable progress."""
from __future__ import annotations

import sys
from pathlib import Path

_LAST_PERCENT = -1


def _emit_percent(current: float, total: float | None) -> None:
    """Emit monotonic transfer progress while reserving 100% for job completion."""
    global _LAST_PERCENT
    if not total or total <= 0:
        return
    percent = max(0, min(99, int(current * 100 / total)))
    if percent <= _LAST_PERCENT:
        return
    _LAST_PERCENT = percent
    print(f"HERMES_PROGRESS:{percent}%", file=sys.stderr, flush=True)


def main(repo_id: str, filename: str) -> None:
    from huggingface_hub import hf_hub_download
    from tqdm.auto import tqdm as base_tqdm

    class ProgressTqdm(base_tqdm):
        def update(self, n: float = 1) -> bool | None:
            result = super().update(n)
            _emit_percent(float(self.n), float(self.total) if self.total else None)
            return result

    downloaded = hf_hub_download(
        repo_id=repo_id,
        filename=filename,
        library_name="hermes-llamacpp-plugin",
        tqdm_class=ProgressTqdm,
    )
    print(str(Path(downloaded).resolve()), flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: hf_download_worker.py <repo_id> <filename>")
    main(sys.argv[1], sys.argv[2])
