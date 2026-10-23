import importlib.util
import subprocess
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[2]
    / "skills"
    / "kwc-update-current-state"
    / "scripts"
    / "current_state.py"
)
SPEC = importlib.util.spec_from_file_location("current_state_under_test", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class CurrentStateInspectorTest(unittest.TestCase):
    def test_parse_dirty_paths_preserves_dot_directories(self):
        status = " M .hermes/profiles/project-manager/SOUL.md\n?? .hermes/profiles/project-manager/skills/"
        self.assertEqual(
            [
                ".hermes/profiles/project-manager/SOUL.md",
                ".hermes/profiles/project-manager/skills/",
            ],
            MODULE.parse_dirty_paths(status),
        )

    def test_parse_dirty_paths_keeps_rename_destination(self):
        status = "R  .hermes/old.md -> .hermes/new.md\n"
        self.assertEqual([".hermes/new.md"], MODULE.parse_dirty_paths(status))