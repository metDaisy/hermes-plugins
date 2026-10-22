import copy
import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "scripts"))

from workflow import validate_implementation_admission


class ImplementationAdmissionContractTest(unittest.TestCase):
    def admission(self):
        return {
            "schema": "backend-implementation-admission-v2",
            "task_id": "t_a1b2c3d4",
            "issue": 42,
            "workspace": "C:/Users/example/project-coder",
            "card_schema": "backend-implementation-card-v6",
            "baseline": {
                "head_sha": "a" * 40,
                "dirty_paths": [],
            },
            "restart": None,
        }

    def test_accepts_clean_initial_v2_admission(self):
        self.assertEqual([], validate_implementation_admission(self.admission()))

    def test_rejects_legacy_v1_admission(self):
        admission = self.admission()
        admission["schema"] = "backend-implementation-admission-v1"

        self.assertIn(
            "IMPLEMENTATION_ADMISSION_SCHEMA",
            validate_implementation_admission(admission),
        )

    def test_rejects_dirty_initial_baseline(self):
        admission = self.admission()
        admission["baseline"]["dirty_paths"] = ["src/main/java/example/App.java"]

        self.assertIn(
            "INITIAL_ADMISSION_REQUIRES_CLEAN_BASELINE",
            validate_implementation_admission(admission),
        )

    def test_rejects_restart_when_baseline_does_not_match(self):
        admission = self.admission()
        admission["restart"] = {
            "schema": "restart-task-v1",
            "issue": 42,
            "delivery_branch": "feature/issue42",
            "workspace": admission["workspace"],
            "marker_baseline_sha": "b" * 40,
            "marker_snapshot_sha": "b" * 40,
            "current_head_sha": "c" * 40,
            "dirty_paths": ["src/main/java/example/App.java"],
            "path_attribution": [
                {
                    "path": "src/main/java/example/App.java",
                    "issue": 42,
                    "evidence": "Interrupted implementation owned by Issue #42.",
                    "attributed": True,
                }
            ],
            "recovery_task_id": admission["task_id"],
            "assignee": "coder",
            "active_recovery_task_ids": [admission["task_id"]],
            "allowed_scope": ["src/main/java/example/App.java"],
            "implementation_card_schema": "backend-implementation-card-v6",
            "required_checkpoint_schema": "backend-implementation-checkpoint-v1",
        }
        admission["baseline"] = {
            "head_sha": "d" * 40,
            "dirty_paths": ["src/main/java/example/App.java"],
        }

        errors = validate_implementation_admission(admission)

        self.assertIn("RESTART_ADMISSION_HEAD_MISMATCH", errors)


if __name__ == "__main__":
    unittest.main()
