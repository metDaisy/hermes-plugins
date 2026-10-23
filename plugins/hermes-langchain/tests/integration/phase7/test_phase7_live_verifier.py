from __future__ import annotations

import unittest

from tests.integration.phase7.phase7_live_verifier import (
    digest_issue_boards,
    select_issue_boards,
    select_plugin_states,
)


class Phase7LiveVerifierTests(unittest.TestCase):
    def test_select_plugin_states_keeps_only_cutover_plugins(self) -> None:
        plugins = [
            {"name": "other", "status": "enabled", "version": "1", "source": "bundled"},
            {"name": "hermes-langchain", "status": "enabled", "version": "0.29.0", "source": "git"},
            {"name": "kanban-workflow-controller", "status": "disabled", "version": "0.27.0", "source": "user"},
        ]

        self.assertEqual(
            {
                "hermes-langchain": {"status": "enabled", "version": "0.29.0"},
                "kanban-workflow-controller": {"status": "disabled", "version": "0.27.0"},
            },
            select_plugin_states(plugins),
        )

    def test_issue_board_digest_is_order_independent_and_ignores_paths(self) -> None:
        boards = [
            {
                "slug": "issue-151-b",
                "archived": False,
                "counts": {"todo": 1},
                "total": 1,
                "db_path": "C:/private/b.db",
            },
            {
                "slug": "other",
                "archived": False,
                "counts": {},
                "total": 0,
                "db_path": "C:/private/other.db",
            },
            {
                "slug": "issue-151-a",
                "archived": False,
                "counts": {"done": 1},
                "total": 1,
                "db_path": "C:/private/a.db",
            },
        ]

        selected = select_issue_boards(boards)

        self.assertEqual(["issue-151-a", "issue-151-b"], [item["slug"] for item in selected])
        self.assertEqual(digest_issue_boards(selected), digest_issue_boards(list(reversed(selected))))
        self.assertNotIn("db_path", selected[0])


if __name__ == "__main__":
    unittest.main()