"""Contract tests for the capacity-one KWC local-agent lease."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier


_ROOT = Path(__file__).resolve().parents[1]
from tests.support import load_module

LEASES = load_module("runtime.agent_lease_service")


class AgentLeaseServiceTests(unittest.TestCase):
    def _acquire(
        self,
        state_path: Path,
        *,
        lease_id: str = "lease-a",
        holder_id: str = "invocation-a",
        now: int = 1000,
        ttl_seconds: int = 60,
    ) -> dict:
        return LEASES.acquire_local_agent_lease(
            state_path=state_path,
            lease_id=lease_id,
            holder_id=holder_id,
            ttl_seconds=ttl_seconds,
            now_fn=lambda: now,
        )

    def test_first_holder_acquires_capacity_one_and_another_holder_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            first = self._acquire(state_path)
            second = self._acquire(
                state_path,
                lease_id="lease-b",
                holder_id="invocation-b",
                now=1001,
            )
            current = LEASES.read_local_agent_lease(
                state_path=state_path, now_fn=lambda: 1001
            )

        self.assertTrue(first["acquired"])
        self.assertFalse(first["takeover"])
        self.assertFalse(first["idempotent_replay"])
        self.assertEqual(first["resource"], "local-agent")
        self.assertFalse(second["acquired"])
        self.assertEqual(second["reason_code"], "LEASE_HELD")
        self.assertEqual(current["lease_id"], "lease-a")
        self.assertNotIn("holder_id", current)

    def test_identical_acquire_is_idempotent_but_same_lease_id_cannot_change_holder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            first = self._acquire(state_path)
            replay = self._acquire(state_path, now=1001)
            with self.assertRaisesRegex(ValueError, "different holder"):
                self._acquire(
                    state_path,
                    holder_id="invocation-b",
                    now=1001,
                )

        self.assertEqual(first["expires_at"], replay["expires_at"])
        self.assertTrue(replay["acquired"])
        self.assertTrue(replay["idempotent_replay"])

    def test_matching_heartbeat_extends_the_lease_and_wrong_holder_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._acquire(state_path)
            heartbeat = LEASES.heartbeat_local_agent_lease(
                state_path=state_path,
                lease_id="lease-a",
                holder_id="invocation-a",
                ttl_seconds=60,
                now_fn=lambda: 1030,
            )
            with self.assertRaisesRegex(ValueError, "does not own"):
                LEASES.heartbeat_local_agent_lease(
                    state_path=state_path,
                    lease_id="lease-b",
                    holder_id="invocation-b",
                    ttl_seconds=60,
                    now_fn=lambda: 1031,
                )

        self.assertEqual(heartbeat["heartbeat_at"], 1030)
        self.assertEqual(heartbeat["expires_at"], 1090)

    def test_expired_lease_is_taken_over_and_old_holder_cannot_heartbeat_or_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            first = self._acquire(state_path, ttl_seconds=10)
            takeover = self._acquire(
                state_path,
                lease_id="lease-b",
                holder_id="invocation-b",
                now=1010,
            )
            with self.assertRaisesRegex(ValueError, "does not own"):
                LEASES.heartbeat_local_agent_lease(
                    state_path=state_path,
                    lease_id="lease-a",
                    holder_id="invocation-a",
                    ttl_seconds=60,
                    now_fn=lambda: 1011,
                )
            with self.assertRaisesRegex(ValueError, "does not own"):
                LEASES.release_local_agent_lease(
                    state_path=state_path,
                    lease_id="lease-a",
                    holder_id="invocation-a",
                    now_fn=lambda: 1011,
                )

        self.assertEqual(first["generation"], 1)
        self.assertTrue(takeover["takeover"])
        self.assertEqual(takeover["generation"], 2)

    def test_expired_holder_cannot_revive_the_lease_before_takeover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._acquire(state_path, ttl_seconds=10)
            with self.assertRaisesRegex(ValueError, "expired"):
                LEASES.heartbeat_local_agent_lease(
                    state_path=state_path,
                    lease_id="lease-a",
                    holder_id="invocation-a",
                    ttl_seconds=60,
                    now_fn=lambda: 1010,
                )

    def test_release_is_idempotent_and_next_acquire_uses_a_new_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._acquire(state_path)
            released = LEASES.release_local_agent_lease(
                state_path=state_path,
                lease_id="lease-a",
                holder_id="invocation-a",
                now_fn=lambda: 1020,
            )
            replay = LEASES.release_local_agent_lease(
                state_path=state_path,
                lease_id="lease-a",
                holder_id="invocation-a",
                now_fn=lambda: 1021,
            )
            with self.assertRaisesRegex(ValueError, "released lease_id"):
                self._acquire(state_path, now=1022)
            next_holder = self._acquire(
                state_path,
                lease_id="lease-b",
                holder_id="invocation-b",
                now=1023,
            )

        self.assertEqual(released["status"], "released")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(next_holder["generation"], 2)
        self.assertFalse(next_holder["takeover"])

    def test_fractional_ttl_is_rejected_instead_of_being_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "ttl_seconds"):
                self._acquire(Path(tmp) / "state.db", ttl_seconds=1.5)

    def test_concurrent_acquire_grants_exactly_one_holder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            barrier = Barrier(2)

            def acquire(index: int) -> dict:
                barrier.wait()
                return self._acquire(
                    state_path,
                    lease_id=f"lease-{index}",
                    holder_id=f"invocation-{index}",
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(acquire, (1, 2)))

        self.assertEqual(sum(bool(result["acquired"]) for result in results), 1)
        self.assertEqual(
            sum(result.get("reason_code") == "LEASE_HELD" for result in results), 1
        )

    def test_store_uses_wal_and_contains_no_prompt_or_session_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._acquire(state_path)
            with closing(sqlite3.connect(state_path)) as conn:
                journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(agent_lease)").fetchall()
                }

        self.assertEqual(journal_mode.lower(), "wal")
        forbidden = {
            "prompt",
            "messages",
            "transcript",
            "reasoning",
            "raw_output",
            "command",
            "credentials",
            "session_key",
        }
        self.assertTrue(forbidden.isdisjoint(columns))


if __name__ == "__main__":
    unittest.main()
