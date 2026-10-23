"""Subprocess half of the M6 restart/resume acceptance test."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hermes_langchain import controller


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("before", "after"))
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("artifacts", type=Path)
    parser.add_argument("hermes_executable")
    parser.add_argument("hermes_source_root", type=Path)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("isolated_home", type=Path)
    parser.add_argument("base_url")
    args = parser.parse_args()

    runtime = controller.ControllerRuntime.open(
        workflow=controller.profile_independent_e2e_workflow(),
        executor=controller.HermesProcessExecutor(
            hermes_executable=args.hermes_executable,
            hermes_source_root=args.hermes_source_root,
            model="fixture-model",
            provider="openai",
            workspace=args.workspace,
            scratch_dir=args.checkpoint.parent / "scratch",
            isolated_home=args.isolated_home,
            runtime_environment={
                "OPENAI_API_KEY": "fixture-key",
                "OPENAI_BASE_URL": args.base_url,
            },
        ),
        checkpoint_path=args.checkpoint,
        artifact_store_path=args.artifacts,
    )
    try:
        if args.phase == "before":
            runtime.start(
                run_id="run-cross-process",
                initial_input={"request": "Build it", "unrelated": "must not leak"},
            )
            state = runtime.resume(run_id="run-cross-process")
        else:
            state = runtime.resume(run_id="run-cross-process")
    finally:
        runtime.close()
    print(json.dumps(state, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()