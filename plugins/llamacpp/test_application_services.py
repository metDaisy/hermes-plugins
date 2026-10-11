"""Unit tests for cohesive llamacpp dashboard application modules."""
from __future__ import annotations

import unittest
import threading
import os
import tempfile
from pathlib import Path
from unittest.mock import patch


class ManagedEndpointConfigTests(unittest.TestCase):
    def test_strata_context_length_comes_from_generated_config(self) -> None:
        import json

        from dashboard.application.model_context import model_context_length

        with tempfile.TemporaryDirectory() as raw_root:
            config = Path(raw_root) / "strata.json"
            config.write_text(json.dumps({
                "args": ["--pack", "pack", "--max-context", "131072", "--kv", "int8"],
            }), encoding="utf-8")
            state = {
                "models": {
                    "strata-main": {
                        "runtime_kind": "strata",
                        "config_path": str(config),
                    },
                },
                "model_settings": {"strata-main": {"temp": "1"}},
            }

            self.assertEqual(model_context_length(state, "strata-main"), 131072)

    def test_desktop_leases_expire_after_owner_heartbeat_stops(self) -> None:
        from dashboard.application.desktop_leases import DesktopLeaseRegistry

        now = [100.0]
        leases = DesktopLeaseRegistry(timeout_seconds=8, clock=lambda: now[0])

        self.assertFalse(leases.should_shutdown())
        self.assertEqual(leases.touch("desktop-a"), 1)
        self.assertEqual(leases.touch("desktop-b"), 2)
        leases.release("desktop-a")
        self.assertFalse(leases.should_shutdown())
        now[0] += 9
        self.assertTrue(leases.should_shutdown())

    def test_desktop_lease_rejects_missing_or_oversized_client_id(self) -> None:
        from dashboard.application.desktop_leases import DesktopLeaseRegistry

        leases = DesktopLeaseRegistry()
        with self.assertRaisesRegex(ValueError, "client_id"):
            leases.touch("")
        with self.assertRaisesRegex(ValueError, "client_id"):
            leases.touch("x" * 129)

    def test_execution_profiles_store_main_and_compression_independently(self) -> None:
        from dashboard.application.execution_profiles import ExecutionProfileService

        state = {
            "active_model_id": "main-model",
            "runtime_kind": "official",
            "models": {"main-model": {}, "Ternary-Bonsai-small": {}},
        }
        service = ExecutionProfileService(
            load_state=lambda: state,
            save_state=lambda value: state.update(value),
            accepts=lambda kind, model_id: not (kind == "prism_ml" and model_id == "main-model"),
        )

        service.save("main", {"runtime_kind": "official", "model_id": "main-model"})
        result = service.save("compression", {"runtime_kind": "official", "model_id": "Ternary-Bonsai-small"})

        self.assertEqual(result["execution_mode"], "exclusive_swap")
        self.assertEqual(result["profiles"]["main"]["runtime_kind"], "official")
        self.assertEqual(result["profiles"]["compression"]["runtime_kind"], "prism_ml")
        self.assertEqual(result["profiles"]["main"]["logical_model"], "main-local")
        self.assertEqual(result["profiles"]["compression"]["logical_model"], "compression-local")

    def test_execution_profiles_allow_official_runtime_for_both_roles(self) -> None:
        from dashboard.application.execution_profiles import ExecutionProfileService

        state = {
            "models": {"main-model": {}, "small-model": {}},
            "runtime_kind": "official",
        }
        service = ExecutionProfileService(
            load_state=lambda: state,
            save_state=lambda value: state.update(value),
            accepts=lambda _kind, _model_id: True,
        )

        service.save("main", {"runtime_kind": "official", "model_id": "main-model"})
        result = service.save("compression", {"runtime_kind": "official", "model_id": "small-model"})

        self.assertEqual(result["profiles"]["main"]["runtime_kind"], "official")
        self.assertEqual(result["profiles"]["compression"]["runtime_kind"], "official")

    def test_execution_profiles_allow_one_strata_model_for_both_roles(self) -> None:
        from dashboard.application.execution_profiles import ExecutionProfileService

        state = {
            "models": {"strata-main": {"runtime_kind": "strata"}},
        }
        service = ExecutionProfileService(
            load_state=lambda: state,
            save_state=lambda value: state.update(value),
            accepts=lambda kind, model_id: kind == "strata" and model_id == "strata-main",
        )

        result = service.save_many({
            "main": {"model_id": "strata-main"},
            "compression": {"model_id": "strata-main"},
        })

        self.assertEqual(result["profiles"]["main"]["runtime_kind"], "strata")
        self.assertEqual(result["profiles"]["compression"]["runtime_kind"], "strata")
        self.assertEqual(state["execution_profiles"]["main"]["model_id"], "strata-main")
        self.assertEqual(state["execution_profiles"]["compression"]["model_id"], "strata-main")

    def test_execution_profiles_derive_runtime_from_model_name(self) -> None:
        from dashboard.application.execution_profiles import ExecutionProfileService

        for model_id, expected_runtime in (
            ("regular-model", "official"),
            ("Ternary-Bonsai-2-27B-PQ2_0", "prism_ml"),
        ):
            with self.subTest(model=model_id):
                state = {"models": {model_id: {}}}
                service = ExecutionProfileService(
                    load_state=lambda: state,
                    save_state=lambda value: state.update(value),
                    accepts=lambda _kind, _model_id: True,
                )
                result = service.save("main", {"runtime_kind": "prism_ml", "model_id": model_id})

                self.assertEqual(result["profiles"]["main"]["runtime_kind"], expected_runtime)

    def test_execution_profiles_migrate_saved_runtime_to_model_rule(self) -> None:
        from dashboard.application.execution_profiles import ExecutionProfileService

        state = {
            "models": {"Ternary-Bonsai-2-27B-PQ2_0": {}},
            "execution_profiles": {
                "main": {
                    "runtime_kind": "official",
                    "model_id": "Ternary-Bonsai-2-27B-PQ2_0",
                },
            },
        }
        service = ExecutionProfileService(
            load_state=lambda: state,
            save_state=lambda value: state.update(value),
            accepts=lambda _kind, _model_id: True,
        )

        result = service.snapshot()

        self.assertEqual(result["profiles"]["main"]["runtime_kind"], "prism_ml")

    def test_execution_profiles_allow_each_role_to_be_cleared_independently(self) -> None:
        from dashboard.application.execution_profiles import ExecutionProfileService

        state = {
            "active_model_id": "main-model",
            "models": {"main-model": {}, "small-model": {}},
            "execution_profiles": {
                "main": {"runtime_kind": "official", "model_id": "main-model", "preset_id": ""},
                "compression": {"runtime_kind": "official", "model_id": "small-model", "preset_id": ""},
            },
        }
        service = ExecutionProfileService(
            load_state=lambda: state,
            save_state=lambda value: state.update(value),
            accepts=lambda _kind, _model_id: True,
        )

        result = service.save("main", {"model_id": ""})

        self.assertFalse(result["profiles"]["main"]["configured"])
        self.assertTrue(result["profiles"]["compression"]["configured"])
        self.assertNotIn("main", state["execution_profiles"])
        self.assertIsNone(state["active_model_id"])

    def test_execution_profiles_validate_all_roles_before_atomic_write(self) -> None:
        from dashboard.application.execution_profiles import ExecutionProfileService

        state = {
            "models": {"main-model": {"runtime_kind": "official"}},
            "execution_profiles": {
                "main": {"runtime_kind": "official", "model_id": "old-main", "preset_id": ""},
            },
        }
        writes: list[dict[str, object]] = []
        service = ExecutionProfileService(
            load_state=lambda: state,
            save_state=lambda value: writes.append(dict(value)) or state.update(value),
            accepts=lambda _kind, _model_id: True,
        )

        with self.assertRaisesRegex(ValueError, "등록된 model_id"):
            service.save_many({
                "main": {"model_id": "main-model"},
                "compression": {"model_id": "missing-model"},
            })

        self.assertEqual(writes, [])
        self.assertEqual(state["execution_profiles"]["main"]["model_id"], "old-main")
        self.assertNotIn("compression", state["execution_profiles"])

    def test_compression_only_profile_skips_main_endpoint_projection(self) -> None:
        from dashboard.application.execution_profiles import ExecutionProfileCoordinator

        state: dict[str, object] = {}
        projection_calls: list[str] = []

        class Profiles:
            @staticmethod
            def save_many(_bindings):
                state["execution_profiles"] = {
                    "compression": {"runtime_kind": "official", "model_id": "small", "preset_id": ""},
                }
                return {
                    "profiles": {
                        "main": {"configured": False},
                        "compression": {
                            "runtime_kind": "official", "model_id": "small", "preset_id": "", "configured": True,
                        },
                    }
                }

        def mutate(callback):
            callback(state)
            return state

        coordinator = ExecutionProfileCoordinator(
            load_state=lambda: dict(state), mutate_state=mutate, profiles=lambda: Profiles(),
            sync_endpoint=lambda: projection_calls.append("sync") or {},
            unregister_endpoint=lambda: projection_calls.append("unregister"),
        )

        result = coordinator.save_many({"main": {"model_id": ""}, "compression": {"model_id": "small"}})

        self.assertTrue(result["profiles"]["compression"]["configured"])
        self.assertEqual(state["execution_profiles"]["compression"]["model_id"], "small")
        self.assertEqual(projection_calls, ["unregister"])
        self.assertIsNone(state["custom_endpoint"])

    def test_execution_profile_projection_failure_restores_related_runtime_state(self) -> None:
        from dashboard.application.execution_profiles import ExecutionProfileCoordinator

        old_profiles = {"main": {"runtime_kind": "official", "model_id": "old", "preset_id": ""}}
        state: dict[str, object] = {
            "execution_profiles": dict(old_profiles),
            "active_model_id": "old",
            "execution_mode": "native_router",
        }

        class Profiles:
            @staticmethod
            def save_many(_bindings):
                state["execution_profiles"] = {
                    "compression": {"runtime_kind": "official", "model_id": "small", "preset_id": ""},
                }
                state["active_model_id"] = None
                state["execution_mode"] = "exclusive_swap"
                return {
                    "profiles": {
                        "main": {"configured": False},
                        "compression": {
                            "runtime_kind": "official", "model_id": "small", "preset_id": "", "configured": True,
                        },
                    }
                }

        def mutate(callback):
            callback(state)
            return state

        coordinator = ExecutionProfileCoordinator(
            load_state=lambda: dict(state), mutate_state=mutate, profiles=lambda: Profiles(),
            sync_endpoint=lambda: (_ for _ in ()).throw(RuntimeError("projection failed")),
            unregister_endpoint=lambda: (_ for _ in ()).throw(RuntimeError("projection failed")),
        )

        with self.assertRaisesRegex(RuntimeError, "projection failed"):
            coordinator.save_many({"main": {"model_id": ""}, "compression": {"model_id": "small"}})

        self.assertEqual(state["execution_profiles"], old_profiles)
        self.assertEqual(state["active_model_id"], "old")
        self.assertEqual(state["execution_mode"], "native_router")

    def test_execution_profile_coordinator_rolls_back_all_written_profiles_when_projection_fails(self) -> None:
        from dashboard.application.execution_profiles import ExecutionProfileCoordinator

        old_profiles = {
            "main": {"runtime_kind": "official", "model_id": "old-main", "preset_id": ""},
        }
        new_profiles = {
            "main": {"runtime_kind": "official", "model_id": "new-main", "preset_id": ""},
            "compression": {"runtime_kind": "official", "model_id": "small", "preset_id": ""},
        }
        state: dict[str, object] = {"pid": 10, "execution_profiles": dict(old_profiles)}

        class Profiles:
            @staticmethod
            def save_many(_bindings):
                state["execution_profiles"] = dict(new_profiles)
                return {
                    "profiles": {
                        role: {**profile, "configured": True}
                        for role, profile in new_profiles.items()
                    }
                }

        def mutate(callback):
            callback(state)
            return state

        def fail_projection():
            state["pid"] = 99
            raise RuntimeError("projection failed")

        coordinator = ExecutionProfileCoordinator(
            load_state=lambda: dict(state),
            mutate_state=mutate,
            profiles=lambda: Profiles(),
            sync_endpoint=fail_projection,
            unregister_endpoint=lambda: None,
        )

        with self.assertRaisesRegex(RuntimeError, "projection failed"):
            coordinator.save_many({"main": {"model_id": "new-main"}, "compression": {"model_id": "small"}})

        self.assertEqual(state["pid"], 99)
        self.assertEqual(state["execution_profiles"], old_profiles)

    def test_execution_profile_start_route_uses_main_or_aux_from_one_server_action(self) -> None:
        from fastapi import HTTPException
        from dashboard.routes.execution_profile_routes import ExecutionProfileRouteContext, create_router

        events = []
        job = {"job_id": "job-1"}
        context = ExecutionProfileRouteContext(
            snapshot=lambda: {},
            save=lambda role, body: events.append(("save", role, body)) or {},
            save_many=lambda bindings: events.append(("save_many", bindings)) or {},
            start=lambda role: events.append(("start", role)),
            create_job=lambda kind, detail: events.append(("job", kind, detail)) or job,
            launch=lambda _job, run, _name: run(),
            finish=lambda _job, detail: events.append(("finish", detail)),
        )
        router = create_router(context)
        endpoint = next(route.endpoint for route in router.routes if route.path == "/profiles/start")
        direct = next(route.endpoint for route in router.routes if route.path == "/profiles/{role}/start")

        result = endpoint({
            "main_model_id": "large-model", "main_preset_id": "main-preset",
            "compression_model_id": "small-model", "compression_preset_id": "compress-preset",
        })

        self.assertEqual(result["job_id"], "job-1")
        self.assertEqual(events[0], (
            "save_many",
            {
                "main": {"model_id": "large-model", "preset_id": "main-preset"},
                "compression": {"model_id": "small-model", "preset_id": "compress-preset"},
            },
        ))
        self.assertEqual(events[2], ("start", "main"))

        events.clear()
        result = endpoint({"main_model_id": "", "compression_model_id": "small-model"})
        self.assertEqual(result["job_id"], "job-1")
        self.assertEqual(events[2], ("start", "compression"))

        with self.assertRaises(HTTPException) as rejected:
            endpoint({"main_model_id": "", "compression_model_id": ""})
        self.assertEqual(rejected.exception.status_code, 422)

        with self.assertRaises(HTTPException) as rejected:
            direct("compression", {"model_id": "small-model"})
        self.assertEqual(rejected.exception.status_code, 409)

    def test_profile_selection_survives_startup_failure_and_job_exposes_reason(self) -> None:
        from dashboard.routes.execution_profile_routes import ExecutionProfileRouteContext, create_router

        selected: dict[str, dict[str, object]] = {}
        job = {"job_id": "job-failed", "status": "running"}

        def save_many(bindings):
            selected.update(bindings)
            return {"profiles": bindings}

        def launch(_job, run, _name):
            try:
                run()
            except Exception as exc:  # noqa: BLE001 - mirrors JobManager failure projection
                job.update({"status": "error", "error": str(exc)})

        context = ExecutionProfileRouteContext(
            snapshot=lambda: {"profiles": selected},
            save=lambda role, body: save_many({role: body}),
            save_many=save_many,
            start=lambda _role: (_ for _ in ()).throw(RuntimeError("CUDA allocation failed")),
            create_job=lambda _kind, _detail: job,
            launch=launch,
            finish=lambda _job, _detail: None,
        )
        endpoint = next(
            route.endpoint for route in create_router(context).routes
            if route.path == "/profiles/start"
        )

        endpoint({"main_model_id": "main-model", "compression_model_id": "small-model"})

        self.assertEqual(selected["main"]["model_id"], "main-model")
        self.assertEqual(selected["compression"]["model_id"], "small-model")
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["error"], "CUDA allocation failed")

    def test_legacy_active_model_is_persisted_as_main_execution_profile(self) -> None:
        from dashboard.application.execution_profiles import ExecutionProfileService

        state = {
            "active_model_id": "legacy-main",
            "runtime_kind": "official",
            "models": {"legacy-main": {}},
        }

        def mutate(callback):
            callback(state)
            return state

        service = ExecutionProfileService(
            load_state=lambda: state,
            save_state=lambda value: state.update(value),
            accepts=lambda _kind, _model_id: True,
            mutate_state=mutate,
        )

        snapshot = service.snapshot()

        self.assertEqual(snapshot["profiles"]["main"]["model_id"], "legacy-main")
        self.assertEqual(state["execution_profiles"]["main"]["model_id"], "legacy-main")

    def test_execution_profiles_reject_incompatible_binding(self) -> None:
        from dashboard.application.execution_profiles import ExecutionProfileService

        state = {"models": {"model": {}}}
        service = ExecutionProfileService(
            load_state=lambda: state,
            save_state=lambda value: state.update(value),
            accepts=lambda _kind, _model_id: False,
        )

        with self.assertRaisesRegex(ValueError, "호환"):
            service.save("compression", {"runtime_kind": "prism_ml", "model_id": "model"})

    def test_execution_profile_uses_registered_runtime_for_custom_alias(self) -> None:
        from dashboard.application.execution_profiles import ExecutionProfileService

        state: dict[str, object] = {
            "models": {"my-compressor": {"runtime_kind": "prism_ml"}},
        }
        service = ExecutionProfileService(
            load_state=lambda: state,
            save_state=lambda value: state.update(value),
            accepts=lambda kind, _model_id: kind == "prism_ml",
        )

        result = service.save("compression", {"model_id": "my-compressor"})

        self.assertEqual(result["profiles"]["compression"]["runtime_kind"], "prism_ml")

    def test_execution_profiles_snapshot_distinct_preset_options_for_same_strata_model(self) -> None:
        from dashboard.application.execution_profiles import ExecutionProfileService

        state: dict[str, object] = {
            "models": {"strata-iq3_xxs": {"runtime_kind": "strata"}},
        }
        presets = {
            "main-preset": {"options": {"temp": "0.7", "top-p": "0.9"}},
            "compress-preset": {"options": {"temp": "0", "top-p": "1"}},
        }
        service = ExecutionProfileService(
            load_state=lambda: state,
            save_state=lambda value: state.update(value),
            accepts=lambda kind, _model_id: kind == "strata",
            resolve_preset=presets.get,
        )

        result = service.save_many({
            "main": {"model_id": "strata-iq3_xxs", "preset_id": "main-preset"},
            "compression": {"model_id": "strata-iq3_xxs", "preset_id": "compress-preset"},
        })

        self.assertEqual(result["profiles"]["main"]["preset_options"], {"temp": "0.7", "top-p": "0.9"})
        self.assertEqual(result["profiles"]["compression"]["preset_options"], {"temp": "0", "top-p": "1"})
        self.assertEqual(state["execution_profiles"]["main"]["preset_id"], "main-preset")
        self.assertEqual(state["execution_profiles"]["compression"]["preset_id"], "compress-preset")

    def test_execution_profile_rejects_unknown_preset(self) -> None:
        from dashboard.application.execution_profiles import ExecutionProfileService

        state: dict[str, object] = {
            "models": {"strata-iq3_xxs": {"runtime_kind": "strata"}},
        }
        service = ExecutionProfileService(
            load_state=lambda: state,
            save_state=lambda value: state.update(value),
            accepts=lambda _kind, _model_id: True,
            resolve_preset=lambda _preset_id: None,
        )

        with self.assertRaisesRegex(ValueError, "preset"):
            service.save("main", {"model_id": "strata-iq3_xxs", "preset_id": "missing"})

    def test_concurrent_execution_profile_saves_preserve_both_roles(self) -> None:
        import tempfile
        from dashboard.application.execution_profiles import ExecutionProfileService
        from dashboard.application.state_store import StateStore

        with tempfile.TemporaryDirectory() as raw_root:
            store = StateStore(
                Path(raw_root) / "state.json",
                lambda: {"models": {"main-model": {}, "small-model": {}}},
            )
            store.save({"models": {"main-model": {}, "small-model": {}}})
            service = ExecutionProfileService(store.load, store.save, lambda _kind, _model: True, store.mutate)
            barrier = threading.Barrier(2)

            def save(role: str, model_id: str) -> None:
                barrier.wait()
                service.save(role, {"runtime_kind": "official", "model_id": model_id})

            threads = [
                threading.Thread(target=save, args=("main", "main-model")),
                threading.Thread(target=save, args=("compression", "small-model")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            profiles = store.load()["execution_profiles"]
            self.assertEqual(profiles["main"]["model_id"], "main-model")
            self.assertEqual(profiles["compression"]["model_id"], "small-model")

    def test_replaces_only_the_plugin_managed_provider_block(self) -> None:
        from dashboard.application.profile_endpoint import ManagedEndpointConfig

        config = ManagedEndpointConfig(
            key="llamacpp-local",
            begin="# BEGIN llamacpp endpoint (managed)",
            end="# END llamacpp endpoint (managed)",
        )
        existing = (
            "providers:\n"
            "  other:\n"
            "    api: https://example.test\n"
            "  # BEGIN llamacpp endpoint (managed)\n"
            "    llamacpp-local:\n"
            "      name: stale\n"
            "  # END llamacpp endpoint (managed)\n"
        )

        updated = config.upsert(existing, "http://127.0.0.1:18434/v1", "model-a", 8192)

        self.assertIn("  other:\n", updated)
        self.assertIn('      api: "http://127.0.0.1:18434/v1"\n', updated)
        self.assertIn('      default_model: "model-a"\n', updated)
        self.assertNotIn("name: stale", updated)
        removed, changed = config.remove(updated)
        self.assertTrue(changed)
        self.assertIn("  other:\n", removed)
        self.assertNotIn("llamacpp-local", removed)

    def test_recovers_from_an_incomplete_managed_block(self) -> None:
        from dashboard.application.profile_endpoint import ManagedEndpointConfig

        config = ManagedEndpointConfig(
            key="llamacpp-local",
            begin="# BEGIN llamacpp endpoint (managed)",
            end="# END llamacpp endpoint (managed)",
        )
        incomplete = (
            "providers:\n"
            "  llamacpp-local:\n"
            "    api: http://127.0.0.1:18434/v1\n"
            "  # BEGIN llamacpp endpoint (managed)\n"
            "fallback_providers:\n"
            "  - provider: openai-codex\n"
        )

        removed, changed = config.remove(incomplete)

        self.assertTrue(changed)
        self.assertNotIn("BEGIN llamacpp endpoint", removed)
        self.assertIn("fallback_providers:\n", removed)
        self.assertIn("  - provider: openai-codex\n", removed)

        repaired = config.upsert(incomplete, "http://127.0.0.1:18434/v1", "model-a", 8192)
        self.assertIn("END llamacpp endpoint (managed)", repaired)
        self.assertIn('default_model: "model-a"', repaired)
        self.assertIn("fallback_providers:\n", repaired)

    def test_writes_main_and_compression_logical_models_in_one_provider(self) -> None:
        from dashboard.application.profile_endpoint import ManagedEndpointConfig

        config = ManagedEndpointConfig(
            key="llamacpp-local",
            begin="# BEGIN llamacpp endpoint (managed)",
            end="# END llamacpp endpoint (managed)",
        )

        updated = config.upsert_models(
            "providers:\n",
            "http://127.0.0.1:18380/v1",
            "main-local",
            {"main-local": 32768, "compression-local": 8192},
        )

        self.assertIn('      default_model: "main-local"', updated)
        self.assertIn('        "main-local":', updated)
        self.assertIn('          context_length: 32768', updated)
        self.assertIn('        "compression-local":', updated)
        self.assertIn('          context_length: 8192', updated)

    def test_remove_preserves_a_preexisting_empty_providers_root(self) -> None:
        from dashboard.application.profile_endpoint import ManagedEndpointConfig

        config = ManagedEndpointConfig(
            key="llamacpp-local",
            begin="# BEGIN llamacpp endpoint (managed)",
            end="# END llamacpp endpoint (managed)",
        )
        original = "providers:\n"

        updated = config.upsert(original, "http://127.0.0.1:18380/v1", "main-local", 32768)
        removed, changed = config.remove(updated)

        self.assertTrue(changed)
        self.assertEqual(removed, original)

    def test_remove_deletes_only_an_explicitly_owned_empty_providers_root(self) -> None:
        from dashboard.application.profile_endpoint import ManagedEndpointConfig

        config = ManagedEndpointConfig(
            key="llamacpp-local",
            begin="# BEGIN llamacpp endpoint (managed)",
            end="# END llamacpp endpoint (managed)",
        )
        original = "model:\n  default: remote\n"

        updated = config.upsert(original, "http://127.0.0.1:18380/v1", "main-local", 32768)
        removed, changed = config.remove(updated)

        self.assertTrue(changed)
        self.assertEqual(removed, original)

    def test_remove_keeps_owned_root_after_another_provider_is_added(self) -> None:
        from dashboard.application.profile_endpoint import ManagedEndpointConfig

        config = ManagedEndpointConfig(
            key="llamacpp-local",
            begin="# BEGIN llamacpp endpoint (managed)",
            end="# END llamacpp endpoint (managed)",
        )
        updated = config.upsert("", "http://127.0.0.1:18380/v1", "main-local", 32768)
        updated += "  other:\n    api: https://example.test\n"

        removed, changed = config.remove(updated)

        self.assertTrue(changed)
        self.assertEqual(removed, "providers:\n  other:\n    api: https://example.test\n")


class ServerOptionCatalogTests(unittest.TestCase):
    def test_parses_toggle_aliases_and_numeric_defaults(self) -> None:
        from dashboard.application.option_catalog import ServerOptionCatalog

        catalog = ServerOptionCatalog.parse_help(
            "--temp N                             temperature (default: 0.80)\n"
            "--mmproj-auto, --no-mmproj, --no-mmproj-auto\n"
            "                                        whether to use multimodal projector\n"
        )

        by_key = {option["key"]: option for option in catalog}
        self.assertEqual(by_key["temp"]["value_kind"], "number")
        self.assertFalse(by_key["no-mmproj"]["requires_value"])
        self.assertFalse(by_key["mmproj-auto"]["requires_value"])

    def test_parses_cache_type_allowed_values(self) -> None:
        from dashboard.application.option_catalog import ServerOptionCatalog

        catalog = ServerOptionCatalog.parse_help(
            "-ctk, --cache-type-k TYPE  KV cache data type for K allowed values: "
            "f32, f16, bf16, q8_0, q4_0, q4_1, iq4_nl, q5_0, q5_1 (default: f16)\n"
            "-ctv, --cache-type-v TYPE  KV cache data type for V allowed values: "
            "f32, f16, bf16, q8_0, q4_0, q4_1, iq4_nl, q5_0, q5_1 (default: f16)\n"
        )

        by_key = {option["key"]: option for option in catalog}
        expected = ["f32", "f16", "bf16", "q8_0", "q4_0", "q4_1", "iq4_nl", "q5_0", "q5_1"]
        self.assertEqual(by_key["cache-type-k"]["choices"], expected)
        self.assertEqual(by_key["cache-type-v"]["choices"], expected)

    def test_parses_llama_choice_descriptions_and_reasoning_effort(self) -> None:
        from dashboard.application.option_catalog import ServerOptionCatalog

        catalog = ServerOptionCatalog.parse_help(
            "--reasoning-format FORMAT               controls thoughts; one of:\n"
            "                                        - none: leave thoughts in content\n"
            "                                        - deepseek: extract reasoning\n"
            "                                        - deepseek-legacy: keep tags\n"
            "--reasoning-effort LEVEL                reasoning effort: 'default' to keep template default,\n"
            "                                        or a level such as 'minimal', 'low', 'medium', 'high', 'xhigh' or\n"
            "                                        'max' (default: default)\n"
            "--pooling {none,mean,cls,last,rank}     pooling type\n"
        )

        by_key = {option["key"]: option for option in catalog}
        self.assertEqual(by_key["reasoning-format"]["choices"], ["none", "deepseek", "deepseek-legacy"])
        self.assertEqual(by_key["reasoning-effort"]["choices"], ["minimal", "low", "medium", "high", "xhigh", "max"])
        self.assertIsNone(by_key["reasoning-effort"]["default_value"])
        self.assertEqual(by_key["pooling"]["choices"], ["none", "mean", "cls", "last", "rank"])


class RegisteredModelServiceTests(unittest.TestCase):
    def test_registered_local_paths_are_deduplicated_and_resolved(self) -> None:
        from dashboard.application.model_registry import RegisteredModelService

        with self.subTest("registered local model"):
            import tempfile
            from pathlib import Path

            with tempfile.TemporaryDirectory() as raw_root:
                model_path = Path(raw_root) / "model.gguf"
                model_path.write_bytes(b"gguf")
                state = {"active_model_id": "model", "models": {}}
                service = RegisteredModelService(lambda: state, lambda value: state.update(value), lambda _: ([], None))
                service.register("model", [model_path, model_path], owned=False)

                rows = service.rows()
                self.assertEqual(rows[0]["paths"], [str(model_path.resolve())])
                self.assertEqual(service.active_path("model"), model_path.resolve())

    def test_registration_preserves_runtime_kind_and_refuses_alias_overwrite(self) -> None:
        from dashboard.application.model_registry import RegisteredModelService

        state: dict[str, object] = {"models": {"same-name": {"paths": [], "owned": False}}}
        service = RegisteredModelService(
            load_state=lambda: state,
            save_state=lambda value: state.update(value),
            cached_files=lambda _repo: ([], None),
        )

        with self.assertRaisesRegex(RuntimeError, "이미 등록"):
            service.register("same-name", [Path("E:/models/model.gguf")], False)

        service.register(
            "custom-alias", [Path("E:/models/model.gguf")], False,
            hf_repo="owner/model-GGUF", hf_file="model.gguf", runtime_kind="official",
        )
        self.assertEqual(state["models"]["custom-alias"]["runtime_kind"], "official")

    def test_strata_registration_preserves_runtime_metadata_and_config_path(self) -> None:
        from dashboard.application.model_registry import RegisteredModelService

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            config = root / "strata-iq2_xs.json"
            config.write_text("{}\n", encoding="utf-8")
            state: dict[str, object] = {"models": {}}
            service = RegisteredModelService(
                load_state=lambda: state,
                save_state=lambda value: state.update(value),
                cached_files=lambda _repo: ([], None),
            )

            service.register(
                "strata-main", [config], False, runtime_kind="strata",
                metadata={
                    "runtime_root": str(root),
                    "python_path": str(root / ".venv" / "Scripts" / "python.exe"),
                    "config_path": str(config),
                    "worker_port": 18080,
                    "serving_model": "strata",
                },
            )

            row = service.rows()[0]
            self.assertEqual(row["runtime_kind"], "strata")
            self.assertEqual(row["paths"], [str(config.resolve())])
            self.assertEqual(row["worker_port"], 18080)
            self.assertEqual(service.active_path("strata-main"), config.resolve())

    def test_legacy_hf_registration_is_migrated_to_cached_local_paths(self) -> None:
        from dashboard.application.model_registry import RegisteredModelService

        with self.subTest("legacy Hugging Face registration"):
            import tempfile

            with tempfile.TemporaryDirectory() as raw_root:
                model_path = Path(raw_root) / "model.gguf"
                model_path.write_bytes(b"gguf")
                state = {"models": {"model": {"paths": [], "hf_repo": "owner/repo", "hf_file": "model.gguf"}}}
                service = RegisteredModelService(
                    lambda: state, lambda value: state.update(value),
                    lambda _repo: ([{"paths": ["model.gguf"]}], None),
                    lambda _repo, _paths: ([model_path], None),
                )

                self.assertEqual(service.active_path("model"), model_path.resolve())
                self.assertEqual(state["models"]["model"]["paths"], [str(model_path.resolve())])


class RuntimeRouteAdapterTests(unittest.TestCase):
    def test_keeps_runtime_control_and_inventory_paths(self) -> None:
        from dashboard.routes.runtime_routes import RuntimeRouteContext, create_router

        context = RuntimeRouteContext(
            status=lambda: {}, runtime_info=lambda: {}, save=lambda _body: {},
            hardware=lambda: {}, catalog=lambda: {}, open_runtime=lambda _body: {}, install=lambda _body: {},
        )
        paths = {route.path for route in create_router(context).routes}

        self.assertTrue({"/status", "/runtime", "/hardware", "/catalog", "/runtime/open", "/runtime/install"}.issubset(paths))


class StrataRuntimeServiceTests(unittest.TestCase):
    def test_detects_version_and_updates_clean_windows_checkout(self) -> None:
        from dashboard.application.strata_runtime import StrataRuntimeService

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Strata"
            root.mkdir()
            (root / "UPDATE.bat").write_text("@echo off\n", encoding="utf-8")
            state: dict[str, object] = {
                "models": {"strata-model": {"runtime_kind": "strata", "runtime_root": str(root)}}
            }
            commands: list[list[str]] = []

            def run(argv: list[str], _cwd: Path, _timeout: int) -> tuple[int, str]:
                commands.append(argv)
                if "status" in argv:
                    return 0, ""
                if "describe" in argv:
                    return 0, "v0.1.42\n"
                return 0, "updated"

            service = StrataRuntimeService(
                load_state=lambda: state,
                save_state=lambda value: state.update(value),
                run=run,
                platform_name=lambda: "Windows",
            )

            self.assertEqual(service.current(), {"version": "v0.1.42", "installed": True})
            self.assertEqual(service.update(), "v0.1.42")
            self.assertIn("UPDATE.bat", commands[-2][-1])
            self.assertEqual(state["strata_version"], "v0.1.42")
            self.assertEqual(state["models"]["strata-model"]["strata_version"], "v0.1.42")

    def test_resolves_latest_semver_tag_from_origin(self) -> None:
        from dashboard.application.strata_runtime import StrataRuntimeService

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Strata"
            root.mkdir()
            state = {"models": {"model": {"runtime_kind": "strata", "runtime_root": str(root)}}}

            def run(argv: list[str], _cwd: Path, _timeout: int) -> tuple[int, str]:
                if "ls-remote" in argv:
                    return 0, "aaa\trefs/tags/v0.1.9\nbbb\trefs/tags/v0.1.42\nccc\trefs/tags/preview\n"
                return 0, ""

            service = StrataRuntimeService(
                load_state=lambda: state, save_state=lambda _value: None,
                run=run, platform_name=lambda: "Windows",
            )

            self.assertEqual(service.latest(), {"tag": "v0.1.42"})

    def test_refuses_update_when_strata_checkout_has_tracked_changes(self) -> None:
        from dashboard.application.strata_runtime import StrataRuntimeService

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Strata"
            root.mkdir()
            (root / "UPDATE.bat").write_text("@echo off\n", encoding="utf-8")
            state = {"models": {"model": {"runtime_kind": "strata", "runtime_root": str(root)}}}

            def run(argv: list[str], _cwd: Path, _timeout: int) -> tuple[int, str]:
                return (0, " M settings.json\n") if "status" in argv else (0, "v0.1.42\n")

            service = StrataRuntimeService(
                load_state=lambda: state, save_state=lambda _value: None,
                run=run, platform_name=lambda: "Windows",
            )

            with self.assertRaisesRegex(RuntimeError, "tracked changes"):
                service.update()


class RuntimeManagementWorkflowTests(unittest.TestCase):
    def test_strata_runtime_update_routes_to_external_updater_only_while_stopped(self) -> None:
        from dashboard.application.runtime_management import RuntimeManagementWorkflow

        class Backend:
            key = "strata"

        state: dict[str, object] = {"pid": None}
        updates: list[str] = []
        workflow = RuntimeManagementWorkflow(
            load_state=lambda: state, save_state=lambda _value: None,
            get_backend=lambda _kind: Backend(), machine_root=Path("C:/runtime"),
            runtime_kind=lambda _state: "strata", runtime_info=lambda: {},
            pid_alive=lambda pid: pid == 42, open_directory=lambda _path: None,
            install_prism=lambda: {}, runtime_target=lambda *_: ("b1", "cpu"),
            create_job=lambda *_: {}, launch=lambda *_: None, finish=lambda *_: None,
            install_official=lambda *_: None, download_archive=lambda *_: None,
            update_strata=lambda: updates.append("updated") or {"job_id": "strata-job", "kind": "strata"},
        )

        self.assertEqual(workflow.install({"kind": "strata"})["job_id"], "strata-job")
        self.assertEqual(updates, ["updated"])
        state["pid"] = 42
        with self.assertRaisesRegex(RuntimeError, "stop the server"):
            workflow.install({"kind": "strata"})

    def test_selects_custom_runtime_and_projects_installed_state(self) -> None:
        from dashboard.application.runtime_management import RuntimeManagementWorkflow

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            executable = root / "llama-server.exe"
            executable.write_bytes(b"server")
            state: dict[str, object] = {}

            class Backend:
                key = "prism_ml"
                def managed_root(self, _machine_root): return root
                def resolve_executable(self, raw_path):
                    path = Path(raw_path)
                    if path == root:
                        return executable
                    raise RuntimeError("missing")

            workflow = RuntimeManagementWorkflow(
                load_state=lambda: state, save_state=lambda value: state.update(value),
                get_backend=lambda _kind: Backend(), machine_root=root,
                runtime_kind=lambda _state: "official", runtime_info=lambda: {"executable": str(executable), "mode": "custom"},
                pid_alive=lambda _pid: False, open_directory=lambda _path: None,
                install_prism=lambda: {"job_id": "prism"}, runtime_target=lambda *_: ("b1", "cpu"),
                create_job=lambda kind, detail: {"kind": kind, "detail": detail, "job_id": "job"},
                launch=lambda _job, _work, _name: None, finish=lambda _job, _detail: None,
                install_official=lambda *_args: None, download_archive=lambda *_args: None,
            )

            selected = workflow.select({"kind": "prism_ml", "path": str(root)})

            self.assertEqual(selected["mode"], "custom")
            self.assertEqual(state["runtime_kind"], "prism_ml")
            self.assertEqual(state["runtime_path"], str(root.resolve()))


class ParameterRouteAdapterTests(unittest.TestCase):
    def test_keeps_settings_and_preset_paths_with_search_projection(self) -> None:
        from dashboard.routes.parameter_routes import ParameterRouteContext, create_router

        class Parameters:
            def presets(self):
                return []

        option = {"key": "ctx-size", "name": "--ctx-size", "description": "context"}
        context = ParameterRouteContext(
            options=lambda: [option], executable=lambda: None, parameters=lambda: Parameters(),
            server_requires_restart=lambda: False, runtime_kind=lambda _state: "official", state=lambda: {},
        )
        routes = {route.path: route.endpoint for route in create_router(context).routes}

        self.assertTrue({"/settings", "/settings/{model_id}", "/presets", "/presets/{preset_id}/apply", "/presets/{preset_id}"}.issubset(routes))
        self.assertEqual(routes["/settings"]("ctx", 50)["options"], [option])

    def test_preset_edits_refresh_role_snapshots_and_delete_clears_them(self) -> None:
        from dashboard.routes.parameter_routes import ParameterRouteContext, create_router

        updates: list[tuple[str, dict[str, str] | None]] = []

        class Parameters:
            @staticmethod
            def rename_preset(preset_id, _name, options):
                return {"id": preset_id, "name": "Strata Main", "options": dict(options or {})}

            @staticmethod
            def delete_preset(_preset_id):
                return None

        context = ParameterRouteContext(
            options=lambda: [], executable=lambda: None, parameters=lambda: Parameters(),
            server_requires_restart=lambda: False, runtime_kind=lambda _state: "strata", state=lambda: {},
            refresh_profile_preset=lambda preset_id, options: updates.append((preset_id, options)),
        )
        routes = create_router(context).routes
        patch_preset = next(route.endpoint for route in routes if route.path == "/presets/{preset_id}" and "PATCH" in route.methods)
        delete_preset = next(route.endpoint for route in routes if route.path == "/presets/{preset_id}" and "DELETE" in route.methods)

        patch_preset("preset-1", {"options": {"temp": "0.7"}})
        delete_preset("preset-1")

        self.assertEqual(updates, [("preset-1", {"temp": "0.7"}), ("preset-1", None)])


class ServerRouteAdapterTests(unittest.TestCase):
    def test_keeps_server_and_job_paths_and_rejects_start_without_active_model(self) -> None:
        from dashboard.routes.server_routes import ServerRouteContext, create_router
        from fastapi import HTTPException

        context = ServerRouteContext(
            state=lambda: {"active_model_id": None}, stop=lambda: None, start=lambda: None,
            create_job=lambda kind, detail: {"kind": kind, "detail": detail, "job_id": "job"},
            launch=lambda _job, _work, _name: None, finish=lambda _job, _detail: None,
            recent_jobs=lambda: [], find_job=lambda _job_id: None, logs=lambda _limit, _role: {},
            metrics=lambda _window: {},
        )
        routes = {route.path: route.endpoint for route in create_router(context).routes}

        self.assertTrue({"/server", "/jobs", "/jobs/{job_id}", "/logs", "/metrics"}.issubset(routes))
        with self.assertRaises(HTTPException) as raised:
            routes["/server"]({"action": "start"})
        self.assertEqual(raised.exception.status_code, 400)

    def test_metrics_forwards_auxiliary_cursor(self) -> None:
        from dashboard.routes.server_routes import ServerRouteContext, create_router

        captured = []
        context = ServerRouteContext(
            state=lambda: {"active_model_id": None}, stop=lambda: None, start=lambda: None,
            create_job=lambda kind, detail: {"kind": kind, "detail": detail, "job_id": "job"},
            launch=lambda _job, _work, _name: None, finish=lambda _job, _detail: None,
            recent_jobs=lambda: [], find_job=lambda _job_id: None, logs=lambda _limit, _role: {},
            metrics=lambda window, cursor: captured.append((window, cursor)) or {"ok": True},
        )
        routes = {route.path: route.endpoint for route in create_router(context).routes}

        result = routes["/metrics"](60, "aux-5")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(captured, [(60, "aux-5")])

    def test_stop_requests_worker_and_coordinator_shutdown(self) -> None:
        from dashboard.routes.server_routes import ServerRouteContext, create_router

        events: list[str] = []
        context = ServerRouteContext(
            state=lambda: {"active_model_id": "model"}, stop=lambda: events.append("worker"), start=lambda: None,
            create_job=lambda kind, detail: {"kind": kind, "detail": detail, "job_id": "job"},
            launch=lambda _job, _work, _name: None, finish=lambda _job, _detail: None,
            recent_jobs=lambda: [], find_job=lambda _job_id: None, logs=lambda _limit, _role: {},
            metrics=lambda _window: {}, shutdown=lambda: events.append("coordinator"),
        )
        routes = {route.path: route.endpoint for route in create_router(context).routes}

        self.assertEqual(routes["/server"]({"action": "stop"}), {"ok": True, "server_running": False})
        self.assertEqual(events, ["worker", "coordinator"])


class CoordinatorEventBusTests(unittest.TestCase):
    def test_bounded_bus_reports_a_gap_and_keeps_newest_events(self) -> None:
        from dashboard.application.event_bus import CoordinatorEventBus

        bus = CoordinatorEventBus(max_events=2)
        bus.publish("log", refresh=["logs"])
        second = bus.publish("metrics", refresh=["metrics"])
        third = bus.publish("worker", refresh=["status", "metrics"])

        batch = bus.events_after(0)

        self.assertTrue(batch["gap"])
        self.assertEqual([event["id"] for event in batch["events"]], [second["id"], third["id"]])
        self.assertEqual(batch["cursor"], third["id"])

    def test_activity_classification_leaves_periodic_metrics_to_resource_sampler(self) -> None:
        from dashboard.application.event_bus import classify_activity_line

        self.assertEqual(classify_activity_line("ordinary llama.cpp info"), ("log", ["logs"]))
        self.assertEqual(
            classify_activity_line("prompt eval time = 100 ms / 10 tokens"),
            ("prompt_completed", ["logs"]),
        )
        self.assertEqual(
            classify_activity_line("llama_server: model loaded"),
            ("worker_state", ["logs", "status", "metrics"]),
        )

    def test_activity_pump_persists_stdout_and_publishes_invalidation_events(self) -> None:
        import io
        import tempfile
        from pathlib import Path
        from dashboard.application.event_bus import start_activity_pump

        class Process:
            stdout = io.BytesIO(b"ordinary info\nprompt eval time = 20 ms / 4 tokens\n")

        observed: list[tuple[str, list[str]]] = []
        with tempfile.TemporaryDirectory() as raw_root:
            log_path = Path(raw_root) / "activity.log"
            thread = start_activity_pump(
                Process(), log_path,
                lambda event_type, refresh: observed.append((event_type, refresh)),
            )
            self.assertTrue(thread.daemon)
            thread.join(timeout=2)

            self.assertFalse(thread.is_alive())
            self.assertEqual(log_path.read_text(encoding="utf-8"), "ordinary info\nprompt eval time = 20 ms / 4 tokens\n")
            self.assertEqual(observed, [
                ("log", ["logs"]),
                ("prompt_completed", ["logs"]),
                ("worker_output_closed", ["logs", "status", "metrics"]),
            ])


class ResourceHistoryTests(unittest.TestCase):
    def test_resource_sampler_does_not_lose_wake_during_capture(self) -> None:
        from dashboard.application.resource_history import ResourceSamplerSignal, start_resource_sampler

        signal = ResourceSamplerSignal()
        capture_started = threading.Event()
        release_capture = threading.Event()
        captured_twice = threading.Event()
        calls = 0

        def capture() -> dict[str, int]:
            nonlocal calls
            calls += 1
            if calls == 1:
                capture_started.set()
                release_capture.wait(1)
            if calls >= 2:
                captured_twice.set()
            return {"workerPid": 1, "active": 0, "queued": 0}

        stopped, thread = start_resource_sampler(
            capture,
            interval_seconds=lambda _sample: 30.0,
            signal=signal,
        )
        try:
            self.assertTrue(capture_started.wait(1))
            signal.wake()
            release_capture.set()
            self.assertTrue(captured_twice.wait(1), "wake during capture was lost")
        finally:
            stopped.set()
            thread.join(1)

    def test_resource_sampler_wakes_immediately_when_runtime_activity_changes(self) -> None:
        from dashboard.application.resource_history import ResourceSamplerSignal, start_resource_sampler

        signal = ResourceSamplerSignal()
        captured = threading.Event()
        calls: list[int] = []

        def capture() -> dict[str, int]:
            calls.append(len(calls))
            if len(calls) >= 2:
                captured.set()
            return {"workerPid": 1, "active": 0, "queued": 0}

        stopped, thread = start_resource_sampler(
            capture,
            interval_seconds=lambda _sample: 30.0,
            signal=signal,
        )
        try:
            self.assertEqual(len(calls), 1)
            signal.wake()
            self.assertTrue(captured.wait(1), "sampler did not wake on activity transition")
        finally:
            stopped.set()
            thread.join(1)

        self.assertFalse(thread.is_alive())

    def test_resource_sampler_uses_adaptive_intervals(self) -> None:
        from dashboard.application.resource_history import resource_sample_interval

        self.assertEqual(resource_sample_interval(None), 30.0)
        self.assertEqual(resource_sample_interval({"workerPid": 1, "active": 0, "queued": 0}), 10.0)
        self.assertEqual(resource_sample_interval({"workerPid": 1, "active": 1, "queued": 0}), 2.0)
        self.assertEqual(resource_sample_interval({"workerPid": 1, "active": 0, "queued": 1}), 2.0)

    def test_keeps_and_reads_two_second_samples_for_fourteen_days(self) -> None:
        import json
        import tempfile

        from dashboard.application.resource_history import ResourceHistoryStore

        day = 24 * 3600
        now = float(20 * day)
        with tempfile.TemporaryDirectory() as raw_root:
            path = Path(raw_root) / "resource-history.jsonl"
            store = ResourceHistoryStore(path)
            for at in (now - 15 * day, now - 13 * day, now):
                store.append({"at": at, "workerRssBytes": int(at)})

            recent = store.read(window_seconds=14 * day, now=now, max_points=10)
            store._compact(now)
            retained = [json.loads(line)["at"] for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual([row["at"] for row in recent], [now - 13 * day, now])
        self.assertEqual(retained, [now - 13 * day, now])

    def test_supports_five_second_metric_windows_for_two_second_samples(self) -> None:
        import tempfile

        from dashboard.application.resource_history import ResourceHistoryStore

        with tempfile.TemporaryDirectory() as raw_root:
            store = ResourceHistoryStore(Path(raw_root) / "resource-history.jsonl")
            for at in (94.0, 96.0, 98.0, 100.0):
                store.append({"at": at, "workerRssBytes": int(at)})

            recent = store.read(window_seconds=5, now=100.0, max_points=10)

        self.assertEqual([row["at"] for row in recent], [96.0, 98.0, 100.0])

    def test_reuses_parsed_history_when_switching_metric_windows(self) -> None:
        import tempfile
        from unittest.mock import patch

        from dashboard.application.resource_history import ResourceHistoryStore

        with tempfile.TemporaryDirectory() as raw_root:
            store = ResourceHistoryStore(Path(raw_root) / "resource-history.jsonl")
            for at in range(100, 401, 10):
                store.append({"at": float(at), "workerRssBytes": at})

            with patch.object(store, "_read_rows", wraps=store._read_rows) as read_rows:
                store.read(window_seconds=300, now=400.0)
                store.read(window_seconds=1800, now=400.0)
                store.read(window_seconds=86400, now=400.0)
                store.append({"at": 410.0, "workerRssBytes": 410})
                latest = store.read(window_seconds=300, now=410.0)

            self.assertEqual(read_rows.call_count, 1)
            self.assertEqual(latest[-1]["at"], 410.0)

    def test_persists_filters_and_downsamples_long_running_metrics(self) -> None:
        import tempfile

        from dashboard.application.resource_history import ResourceHistoryStore

        with tempfile.TemporaryDirectory() as raw_root:
            store = ResourceHistoryStore(Path(raw_root) / "resource-history.jsonl", retention_seconds=3600)
            for at, rss in ((100.0, 100), (200.0, 300), (250.0, 200), (300.0, 250)):
                store.append({"at": at, "workerRssBytes": rss, "mainGeneration": at / 10})

            recent = store.read(window_seconds=120, now=300.0, max_points=10)
            compact = store.read(window_seconds=3600, now=300.0, max_points=2)

            self.assertEqual([row["at"] for row in recent], [200.0, 250.0, 300.0])
            self.assertLessEqual(len(compact), 2)
            self.assertEqual(max(row["workerRssBytes"] for row in compact), 300)

    def test_reads_worker_rss_and_private_memory_without_guessing(self) -> None:
        from dashboard.application.resource_history import worker_memory_sample

        class Memory:
            rss = 12_000
            private = 8_000

        class Process:
            def memory_info(self):
                return Memory()

        result = worker_memory_sample(42, process_factory=lambda _pid: Process())

        self.assertEqual(result, {
            "workerPid": 42,
            "workerRssBytes": 12_000,
            "workerPrivateBytes": 8_000,
        })


class ModelRouteAdapterTests(unittest.TestCase):
    def test_keeps_model_paths_and_maps_workflow_failure_at_http_adapter(self) -> None:
        from dashboard.routes.model_routes import ModelRouteContext, create_router
        from fastapi import HTTPException

        registrations: list[tuple[str, list[str], str]] = []

        class Workflow:
            def search(self, _query, _limit):
                raise RuntimeError("offline")

            def register(self, repo_id, paths, alias=""):
                registrations.append((repo_id, paths, alias))
                return {"model_id": alias}

            def cached_files(self, _repo_id):
                return ([{"paths": ["model.gguf"]}], None)

            def suggested_alias(self, _repo_id):
                return "model-(2)"

        context = ModelRouteContext(
            lifecycle=lambda: object(), workflow=lambda: Workflow(),
            create_job=lambda kind, detail: {"kind": kind, "detail": detail, "job_id": "job"},
            launch=lambda _job, _work, _name: None, finish=lambda _job, _detail: None,
            begin_download=lambda _job, _detail: None,
        )
        router = create_router(context)
        paths = {route.path: route.endpoint for route in router.routes}

        self.assertTrue({"/activate", "/eject", "/models/{model_id}", "/models/{model_id}/registration", "/models/strata", "/search", "/repo", "/download-browsed", "/register", "/sideload", "/hf-models", "/hf-models/files", "/hf-models/delete"}.issubset(paths))
        with self.assertRaises(HTTPException) as raised:
            paths["/search"]("test", 20)
        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(
            paths["/register"]({"repo": "owner/model-GGUF", "paths": ["model.gguf"], "alias": "model-(2)"}),
            {"model_id": "model-(2)"},
        )
        self.assertEqual(registrations, [("owner/model-GGUF", ["model.gguf"], "model-(2)")])
        self.assertEqual(
            paths["/hf-models/files"]("owner/model-GGUF")["suggested_alias"],
            "model-(2)",
        )


class PrismRuntimeInstallerTests(unittest.TestCase):
    def test_resolves_latest_complete_release_and_installs_versioned_cpu_runtime(self) -> None:
        from dashboard.application.prism_runtime import PrismRuntimeInstaller

        import tempfile
        from pathlib import Path
        import zipfile

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root) / "prism"
            source = Path(raw_root) / "server.zip"
            with zipfile.ZipFile(source, "w") as package:
                package.writestr("llama-server.exe", b"server")
            state: dict[str, object] = {}

            installer = PrismRuntimeInstaller(
                root=root, load_state=lambda: state, save_state=lambda value: state.update(value),
                release_loader=lambda: [
                    {"tag_name": "prism-b123-aaaaaaa", "assets": [{"name": "llama-prism-b123-aaaaaaa-bin-win-cpu-x64.zip"}]},
                    {"tag_name": "prism-b124-bbbbbbb", "assets": [{"name": "unrelated.zip"}]},
                ],
                backend_detector=lambda: "cpu", platform_name=lambda: "windows", architecture=lambda: "x64",
            )
            job = {"job_id": "job"}

            self.assertEqual(installer.latest_target(), ("prism-b123-aaaaaaa", "cpu"))
            tag, backend = installer.install(
                "prism-b123-aaaaaaa", "cpu", job,
                download=lambda _url, destination, *_: destination.write_bytes(source.read_bytes()),
            )

            self.assertEqual((tag, backend), ("prism-b123-aaaaaaa", "cpu"))
            self.assertEqual(state["prism_release_tag"], "prism-b123-aaaaaaa")
            self.assertTrue((root / "prism-b123-aaaaaaa" / "cpu" / "llama-server.exe").is_file())
            self.assertTrue((root / "prism-b123-aaaaaaa" / "cpu" / "manifest.json").is_file())

    def test_migrates_legacy_demo_checkout_to_official_versioned_layout(self) -> None:
        from dashboard.application.prism_runtime import PrismRuntimeInstaller

        import tempfile

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root) / "prism"
            legacy = root / "bin" / "cuda"
            legacy.mkdir(parents=True)
            (legacy / "llama-server.exe").write_bytes(b"server")
            (legacy / "ggml-cuda.dll").write_bytes(b"cuda")
            (root / ".git" / "objects").mkdir(parents=True)
            (root / ".git" / "objects" / "legacy").write_bytes(b"old")
            (root / "README.md").write_text("legacy checkout", encoding="utf-8")
            state: dict[str, object] = {
                "runtime_kind": "prism_ml",
                "runtime_path": str(root),
                "custom_runtime_path": str(root),
                "prism_release_tag": "prism-b124-bbbbbbb",
                "prism_backend": "cuda",
                "pid": None,
            }

            installer = PrismRuntimeInstaller(
                root=root, load_state=lambda: state, save_state=lambda value: state.update(value),
                release_loader=lambda: [], backend_detector=lambda: "cpu",
                platform_name=lambda: "windows", architecture=lambda: "x64",
            )

            installed = installer.migrate_legacy_layout()

            expected = root / "prism-b124-bbbbbbb" / "cuda"
            self.assertEqual(installed, expected)
            self.assertEqual(Path(str(state["runtime_path"])), expected.resolve())
            self.assertTrue((expected / "llama-server.exe").is_file())
            self.assertTrue((expected / "ggml-cuda.dll").is_file())
            self.assertTrue((expected / "manifest.json").is_file())
            self.assertFalse((root / "bin").exists())
            self.assertFalse((root / ".git").exists())
            self.assertFalse((root / "README.md").exists())

    def test_selects_cuda_assets_directly_from_prism_llamacpp_release(self) -> None:
        from dashboard.application.prism_runtime import PrismRuntimeInstaller

        installer = PrismRuntimeInstaller(
            root=Path("runtime"), load_state=lambda: {}, save_state=lambda _value: None,
            release_loader=lambda: [], backend_detector=lambda: "cuda",
            platform_name=lambda: "windows", architecture=lambda: "x64",
        )

        self.assertEqual(installer.asset_names("prism-b10743-adfffbe", "cuda"), [
            "llama-prism-b10743-adfffbe-bin-win-cuda-12.4-x64.zip",
            "cudart-llama-bin-win-cuda-12.4-x64.zip",
        ])


class RuntimeInspectorTests(unittest.TestCase):
    def test_reconciles_dead_server_state_and_builds_runtime_status_projection(self) -> None:
        from dashboard.application.runtime_inspector import RuntimeInspector

        from pathlib import Path
        from types import SimpleNamespace
        state: dict[str, object] = {
            "runtime_kind": "official", "installed_tag": "b1", "installed_backend": "cuda",
            "pid": 42, "port": 18434, "custom_endpoint": {"key": "stale"}, "models": {},
        }
        unregistered: list[bool] = []
        inspector = RuntimeInspector(
            load_state=lambda: state, save_state=lambda value: state.update(value),
            runtime_kind=lambda current: str(current["runtime_kind"]),
            executable=lambda: Path("server.exe"), installed_target=lambda: ("b1", "cuda"),
            executable_in=lambda _root: Path("server.exe"), backend_detector=lambda: "cuda",
            backend=lambda _kind: SimpleNamespace(description="desc", repository="repo"),
            backend_view=lambda _kind, _state, _root: SimpleNamespace(label="official", managed_root="root", version="b1", install_action="install"),
            machine_root=Path("machine"), runtime_root=Path("runtime"), prism_root=Path("prism"),
            pid_alive=lambda _pid: False, health=lambda _port: False,
            unregister_endpoint=lambda: unregistered.append(True), devices=lambda: [{"id": "0"}],
            server_rows=lambda: [{"id": "model"}], models_root=Path("models"), model_presets=lambda: {},
        )

        status = inspector.status()

        self.assertFalse(status["server_running"])
        self.assertEqual(status["runtime_version"], "b1")
        self.assertEqual(status["devices"], [{"id": "0"}])
        self.assertIsNone(state["pid"])
        self.assertEqual(unregistered, [True])

    def test_keeps_endpoint_when_live_process_has_transient_health_failure(self) -> None:
        from dashboard.application.runtime_inspector import RuntimeInspector

        from pathlib import Path
        from types import SimpleNamespace
        state: dict[str, object] = {
            "runtime_kind": "official", "installed_tag": "b1", "installed_backend": "cuda",
            "pid": 42, "port": 18434, "custom_endpoint": {"key": "managed"}, "models": {},
        }
        unregistered: list[bool] = []
        inspector = RuntimeInspector(
            load_state=lambda: state, save_state=lambda value: state.update(value),
            runtime_kind=lambda current: str(current["runtime_kind"]),
            executable=lambda: Path("server.exe"), installed_target=lambda: ("b1", "cuda"),
            executable_in=lambda _root: Path("server.exe"), backend_detector=lambda: "cuda",
            backend=lambda _kind: SimpleNamespace(description="desc", repository="repo"),
            backend_view=lambda _kind, _state, _root: SimpleNamespace(label="official", managed_root="root", version="b1", install_action="install"),
            machine_root=Path("machine"), runtime_root=Path("runtime"), prism_root=Path("prism"),
            pid_alive=lambda _pid: True, health=lambda _port: False,
            unregister_endpoint=lambda: unregistered.append(True), devices=lambda: [{"id": "0"}],
            server_rows=lambda: [{"id": "model"}], models_root=Path("models"), model_presets=lambda: {},
        )

        status = inspector.status()

        self.assertFalse(status["server_running"])
        self.assertEqual(state["pid"], 42)
        self.assertEqual(state["custom_endpoint"], {"key": "managed"})
        self.assertEqual(unregistered, [])

    def test_does_not_infer_model_preset_from_equal_parameter_values(self) -> None:
        from dashboard.application.runtime_inspector import RuntimeInspector

        from pathlib import Path
        from types import SimpleNamespace
        general = {"ctx-size": "131072", "flash-attn": "on"}
        state: dict[str, object] = {
            "runtime_kind": "official", "installed_tag": "b1", "installed_backend": "cuda",
            "pid": 42, "port": 18434, "models": {}, "active_model_id": "tiel",
            "model_settings": {"tiel": dict(general)},
            "parameter_presets": {"general-id": {"name": "general", "options": dict(general)}},
            "model_presets": {},
        }
        inspector = RuntimeInspector(
            load_state=lambda: state, save_state=lambda value: state.update(value),
            runtime_kind=lambda current: str(current["runtime_kind"]),
            executable=lambda: Path("server.exe"), installed_target=lambda: ("b1", "cuda"),
            executable_in=lambda _root: Path("server.exe"), backend_detector=lambda: "cuda",
            backend=lambda _kind: SimpleNamespace(description="desc", repository="repo"),
            backend_view=lambda _kind, _state, _root: SimpleNamespace(label="official", managed_root="root", version="b1", install_action="install"),
            machine_root=Path("machine"), runtime_root=Path("runtime"), prism_root=Path("prism"),
            pid_alive=lambda _pid: True, health=lambda _port: True,
            unregister_endpoint=lambda: None, devices=lambda: [],
            server_rows=lambda: [{"id": "tiel"}], models_root=Path("models"), model_presets=lambda: {},
        )

        status = inspector.status()

        self.assertEqual(status["model_presets"], {})
        self.assertTrue(status["server_running"])


    def test_exposes_github_update_for_official_prism_and_strata_runtimes(self) -> None:
        from dashboard.application.runtime_inspector import RuntimeInspector

        state: dict[str, object] = {
            "runtime_kind": "prism_ml", "installed_tag": "b10976", "installed_backend": "cuda",
            "prism_release_tag": "prism-b10709-9a9394a", "pid": None, "models": {},
        }
        inspector = RuntimeInspector(
            load_state=lambda: state, save_state=lambda value: state.update(value),
            runtime_kind=lambda current: str(current["runtime_kind"]),
            executable=lambda: Path("server.exe"), installed_target=lambda: ("b10976", "cuda"),
            executable_in=lambda _root: Path("server.exe"), backend_detector=lambda: "cuda",
            backend=lambda _kind: type("Backend", (), {"description": "desc", "repository": "repo"})(),
            backend_view=lambda kind, current, _root: type("View", (), {
                "label": kind, "managed_root": "root",
                "version": current.get("prism_release_tag") if kind == "prism_ml" else current.get("installed_tag"),
                "install_action": "update",
            })(),
            machine_root=Path("machine"), runtime_root=Path("runtime"), prism_root=Path("prism"),
            pid_alive=lambda _pid: False, health=lambda _port: False,
            unregister_endpoint=lambda: None, devices=lambda: [], server_rows=lambda: [],
            models_root=Path("models"), model_presets=lambda: {},
            latest_official=lambda: "b10982", latest_prism=lambda: "prism-b10743-adfffbe",
            current_strata=lambda: {"version": "v0.1.41", "installed": True},
            latest_strata=lambda: "v0.1.42",
        )

        status = inspector.status()

        self.assertTrue(status["update_available"])
        self.assertEqual(status["latest_tag"], "prism-b10743-adfffbe")
        self.assertTrue(status["runtime_options"]["prism_ml"]["update_available"])
        self.assertTrue(status["runtime_options"]["official"]["update_available"])
        self.assertEqual(status["runtime_options"]["strata"]["version"], "v0.1.41")
        self.assertEqual(status["runtime_options"]["strata"]["latest_version"], "v0.1.42")
        self.assertTrue(status["runtime_options"]["strata"]["installed"])
        self.assertTrue(status["runtime_options"]["strata"]["update_available"])


class DeviceDiscoveryServiceTests(unittest.TestCase):
    def test_parses_llama_server_device_memory_into_bytes(self) -> None:
        from dashboard.application.device_discovery import DeviceDiscoveryService

        from pathlib import Path
        service = DeviceDiscoveryService(
            executable=lambda: Path("server.exe"),
            run=lambda _argv: (0, "Available devices:\n0: RTX (16 GiB, 12 GiB free)\n"),
        )

        self.assertEqual(service.devices(), [{
            "id": "0", "name": "RTX", "memory": "16 GiB, 12 GiB free",
            "vram_total_bytes": 16 * 1024 ** 3, "vram_free_bytes": 12 * 1024 ** 3,
        }])


class JobManagerTests(unittest.TestCase):
    def test_publishes_every_job_state_transition(self) -> None:
        from dashboard.application.job_manager import JobManager

        jobs: dict[str, dict[str, object]] = {}
        published: list[tuple[str, str]] = []
        manager = JobManager(
            jobs=jobs,
            lock=threading.RLock(),
            new_id=lambda: "job-1",
            now=lambda: 1.0,
            thread_factory=threading.Thread,
            notify=lambda job: published.append((str(job["job_id"]), str(job["status"]))),
        )

        job = manager.create("download", "starting")
        manager.finish(job, "done")
        manager.fail(job, RuntimeError("boom"))

        self.assertEqual(published, [("job-1", "running"), ("job-1", "done"), ("job-1", "error")])

    def test_tracks_recent_jobs_and_converts_background_failures_to_terminal_state(self) -> None:
        from dashboard.application.job_manager import JobManager

        jobs: dict[str, dict[str, object]] = {}

        class InlineThread:
            def __init__(self, target, **_): self._target = target
            def start(self): self._target()

        manager = JobManager(
            jobs=jobs, lock=__import__("threading").RLock(), new_id=iter(["first", "second"]).__next__,
            now=iter([1.0, 2.0]).__next__, thread_factory=InlineThread,
        )
        first = manager.create("download", "first")
        second = manager.create("runtime", "second")
        manager.finish(first, "done")
        manager.launch(second, "test-job", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        self.assertEqual([job["job_id"] for job in manager.recent()], ["second", "first"])
        self.assertEqual(manager.find("second")["status"], "error")
        self.assertEqual(manager.find("second")["error"], "boom")
        self.assertIsNone(manager.find("missing"))


class ModelLifecycleServiceTests(unittest.TestCase):
    def test_register_strata_binds_both_roles_and_requires_port_18080(self) -> None:
        from dashboard.application.model_lifecycle import ModelLifecycleService

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            python = root / ".venv" / "Scripts" / "python.exe"
            server = root / "serve" / "server.py"
            config = root / "strata.json"
            python.parent.mkdir(parents=True)
            server.parent.mkdir(parents=True)
            python.write_bytes(b"python")
            server.write_text("# server\n", encoding="utf-8")
            config.write_text("{}\n", encoding="utf-8")
            registrations: list[tuple[tuple, dict]] = []
            state: dict[str, object] = {}

            class Registry:
                def rows(self): return []
                def active_path(self, _model_id): return config
                def register(self, *args, **kwargs): registrations.append((args, kwargs))

            service = ModelLifecycleService(
                registry=Registry(), load_state=lambda: state, save_state=lambda value: state.update(value),
                runtime_kind=lambda _state: "official", accepts=lambda *_: True,
                server_running=lambda: False, stop_server=lambda: None,
                models_root=root, model_id=lambda path: path.stem,
            )

            result = service.register_strata("strata-main", root, config)

            self.assertEqual(result["worker_port"], 18080)
            self.assertEqual(registrations[0][1]["runtime_kind"], "strata")
            self.assertEqual(registrations[0][1]["metadata"]["python_path"], str(python.resolve()))
            self.assertEqual(state["execution_profiles"]["main"]["model_id"], "strata-main")
            self.assertEqual(state["execution_profiles"]["compression"]["model_id"], "strata-main")
            with self.assertRaisesRegex(RuntimeError, "18080"):
                service.register_strata("strata-unsafe", root, config, worker_port=8080)
            config.write_text('{"api_key": "[REDACTED]"}\n', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "API key"):
                service.register_strata("strata-auth", root, config)

    def test_unregister_removes_only_registration_and_clears_role_bindings(self) -> None:
        from dashboard.application.model_lifecycle import ModelLifecycleService

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "registered.gguf"
            model.write_bytes(b"model")
            state: dict[str, object] = {
                "active_model_id": "registered",
                "models": {"registered": {"paths": [str(model)], "owned": True}},
                "execution_profiles": {
                    "main": {"model_id": "registered", "runtime_kind": "official"},
                    "compression": {"model_id": "registered", "runtime_kind": "official"},
                },
            }

            class Registry:
                def rows(self): return [{"id": "registered", "paths": [str(model)]}]
                def active_path(self, _model_id): return model
                def register(self, *_args, **_kwargs): return None

            service = ModelLifecycleService(
                registry=Registry(), load_state=lambda: state, save_state=lambda value: state.update(value),
                runtime_kind=lambda _: "official", accepts=lambda *_: True,
                server_running=lambda: False, stop_server=lambda: None,
                models_root=root, model_id=lambda path: path.stem,
            )

            result = service.unregister("registered")

            self.assertEqual(result, {"ok": True, "model_id": "registered", "unregistered": True})
            self.assertTrue(model.exists())
            self.assertNotIn("registered", state["models"])
            self.assertIsNone(state["active_model_id"])
            self.assertEqual(state["execution_profiles"]["main"]["model_id"], "")
            self.assertEqual(state["execution_profiles"]["compression"]["model_id"], "")

    def test_activate_switch_delete_and_sideload_share_model_lifecycle_invariants(self) -> None:
        from dashboard.application.model_lifecycle import ModelLifecycleService

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model_a = root / "model-a.gguf"
            model_b = root / "model-b.gguf"
            model_a.write_bytes(b"a")
            model_b.write_bytes(b"b")
            state: dict[str, object] = {
                "active_model_id": "model-a",
                "models": {
                    "model-a": {"paths": [str(model_a)], "owned": False},
                    "model-b": {"paths": [str(model_b)], "owned": True},
                },
            }

            class Registry:
                def rows(self):
                    return [
                        {"id": "model-a", "paths": [str(model_a)], "size_bytes": 1, "size_label": "1 B", "active": True},
                        {"id": "model-b", "paths": [str(model_b)], "size_bytes": 1, "size_label": "1 B", "active": False},
                    ]
                def active_path(self, model_id):
                    if model_id == "model-a": return model_a
                    if model_id == "model-b": return model_b
                    raise RuntimeError("model is not registered: " + model_id)
                def register(self, model_id, paths, owned, **_):
                    state["models"][model_id] = {"paths": [str(path) for path in paths], "owned": owned}

            stopped: list[bool] = []
            service = ModelLifecycleService(
                registry=Registry(), load_state=lambda: state, save_state=lambda value: state.update(value),
                runtime_kind=lambda _: "official", accepts=lambda *_: True,
                server_running=lambda: True, stop_server=lambda: stopped.append(True),
                models_root=root, model_id=lambda path: path.stem,
            )

            with self.assertRaisesRegex(RuntimeError, "stop llama-server"):
                service.activate("model-b")
            service.eject("model-a")
            self.assertEqual(stopped, [True])
            self.assertIsNone(state["active_model_id"])
            service.delete("model-b")
            self.assertFalse(model_b.exists())
            self.assertNotIn("model-b", state["models"])
            side = root / "side.gguf"
            side.write_bytes(b"s")
            self.assertEqual(service.sideload(side)["model_id"], "side")


class PresetStoreMigrationTests(unittest.TestCase):
    def test_completed_migration_does_not_replace_sqlite_presets_with_stale_legacy_state(self) -> None:
        import tempfile
        from dashboard.application.preset_store import PresetStore

        with tempfile.TemporaryDirectory() as raw_root:
            store = PresetStore(Path(raw_root) / "presets.db")
            store.create("saved", "Saved", {"ctx-size": "131072"}, 1.0)
            self.assertTrue(store.migrate_legacy_state({
                "parameter_presets": {"stale": {"name": "Stale", "options": {}}},
                "model_presets": {},
            }))
            self.assertEqual(set(store.presets()), {"saved"})


class ParameterSettingsServiceTests(unittest.TestCase):
    class _MemoryPresetStore:
        def __init__(self, state):
            self.state = state
            self.state.setdefault("parameter_presets", {})
            self.state.setdefault("model_presets", {})

        def presets(self): return self.state["parameter_presets"]
        def get(self, preset_id): return self.presets().get(preset_id)

        def create(self, preset_id, name, options, now):
            value = {"name": name, "model_id": None, "options": options, "created_at": now, "updated_at": now}
            self.presets()[preset_id] = value
            return value

        def update(self, preset_id, name, options, now):
            value = self.get(preset_id)
            if value is None: return None
            if name is not None: value["name"] = name
            if options is not None: value["options"] = options
            value["updated_at"] = now
            return value

        def delete(self, preset_id):
            if preset_id not in self.presets(): return False
            self.presets().pop(preset_id)
            self.state["model_presets"] = {model_id: assigned for model_id, assigned in self.state["model_presets"].items() if assigned != preset_id}
            return True

        def assign(self, model_id, preset_id): self.state["model_presets"][model_id] = preset_id
    def test_apply_preset_replaces_twelve_model_parameters_with_six_preset_parameters(self) -> None:
        from dashboard.application.parameter_settings import ParameterSettingsService

        preset_options = {f"option-{index}": str(index) for index in range(6)}
        stored_options = {"model": {f"option-{index}": str(index) for index in range(12)}}
        state = {"parameter_presets": {"test2": {"name": "test2", "options": preset_options}}}
        catalog = [
            {"key": f"option-{index}", "requires_value": True, "choices": [], "value_kind": "integer"}
            for index in range(12)
        ]
        service = ParameterSettingsService(
            catalog=lambda: catalog, canonical=lambda _, value: str(value),
            load_options=lambda: stored_options, save_options=lambda value: stored_options.update(value),
            preset_store=self._MemoryPresetStore(state),
            error=lambda message: ValueError(message), now=lambda: 1.0, new_id=lambda: "new",
        )

        applied = service.apply_preset("test2", "model", runtime_kind="official", requires_restart=False)

        self.assertEqual(applied["options"], preset_options)
        self.assertEqual(stored_options["model"], preset_options)

    def test_prism_preset_apply_replaces_existing_values_and_omits_spec_options(self) -> None:
        from dashboard.application.parameter_settings import ParameterSettingsService

        state = {"parameter_presets": {"shared": {"name": "shared", "options": {"ctx-size": "8192", "spec-type": "draft"}}}}
        stored_options = {"model": {"no-mmproj": ""}}
        catalog = [
            {"key": "ctx-size", "requires_value": True, "choices": [], "value_kind": "integer"},
            {"key": "no-mmproj", "requires_value": False, "choices": [], "value_kind": "string"},
            {"key": "spec-type", "requires_value": True, "choices": [], "value_kind": "string"},
        ]
        service = ParameterSettingsService(
            catalog=lambda: catalog,
            canonical=lambda _, value: str(value),
            load_options=lambda: stored_options,
            save_options=lambda value: stored_options.update(value),
            preset_store=self._MemoryPresetStore(state),
            error=lambda message: ValueError(message),
            now=lambda: 1.0,
            new_id=lambda: "new",
        )

        applied = service.apply_preset("shared", "model", runtime_kind="prism_ml", requires_restart=False)

        self.assertEqual(applied["options"], {"ctx-size": "8192"})
        self.assertEqual(applied["omitted_options"], ["spec-type"])
        self.assertEqual(state["model_presets"], {"model": "shared"})

    def test_delete_preset_clears_explicit_model_assignments(self) -> None:
        from dashboard.application.parameter_settings import ParameterSettingsService

        state = {
            "parameter_presets": {"shared": {"name": "shared", "options": {}}},
            "model_presets": {"model-a": "shared", "model-b": "other"},
        }
        service = ParameterSettingsService(
            catalog=lambda: [], canonical=lambda _, value: str(value),
            load_options=lambda: {}, save_options=lambda _: None,
            preset_store=self._MemoryPresetStore(state),
            error=lambda message: ValueError(message), now=lambda: 1.0, new_id=lambda: "new",
        )

        service.delete_preset("shared")

        self.assertEqual(state["model_presets"], {"model-b": "other"})


class OfficialRuntimeServiceTests(unittest.TestCase):
    def test_resolves_complete_latest_build_and_installs_verified_runtime(self) -> None:
        from dashboard.application.official_runtime import OfficialRuntimeService

        import tempfile
        from pathlib import Path
        import zipfile

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root) / "runtimes"
            state: dict[str, object] = {"tag": "latest", "installed_tag": "", "installed_backend": ""}
            source_archive = Path(raw_root) / "source.zip"
            with zipfile.ZipFile(source_archive, "w") as package:
                package.writestr("bin/llama-server.exe", b"server")

            service = OfficialRuntimeService(
                root=root,
                load_state=lambda: state,
                save_state=lambda value: state.update(value),
                release_loader=lambda: [
                    {"tag_name": "b101", "assets": [{"name": "llama-b101-bin-win-cpu-x64.zip"}]},
                    {"tag_name": "b102", "assets": [{"name": "unrelated.zip"}]},
                ],
                backend_detector=lambda: "cpu",
                platform_name=lambda: "windows",
                architecture=lambda: "x64",
            )

            self.assertEqual(service.resolve_target(force_latest=True), ("b101", "cpu"))
            job: dict[str, object] = {}
            service.install(
                "b101", "cpu", job,
                download=lambda _url, destination, _job, _floor, _ceiling: destination.write_bytes(source_archive.read_bytes()),
            )

            self.assertEqual(state["installed_tag"], "b101")
            self.assertEqual(service.installed_target(), ("b101", "cpu"))
            self.assertTrue((root / "b101" / "cpu" / "manifest.json").is_file())


class ServerLifecycleServiceTests(unittest.TestCase):
    def test_stop_marker_does_not_create_a_missing_activity_log(self) -> None:
        from dashboard.application.server_lifecycle import ServerLifecycleService

        import tempfile

        with tempfile.TemporaryDirectory() as raw_root:
            log_path = Path(raw_root) / "missing.log"
            state: dict[str, object] = {"pid": 42}
            service = ServerLifecycleService(
                load_state=lambda: state,
                save_state=lambda value: state.update(value),
                pid_alive=lambda _pid: False,
                terminate=lambda _pid: None,
                unregister_endpoint=lambda: None,
                log_path=log_path,
            )

            service.stop(preserve_log=True)

            self.assertFalse(log_path.exists())

    def test_unexpected_worker_exit_publishes_state_change(self) -> None:
        from dashboard.application.server_lifecycle import ServerLifecycleService

        import tempfile

        state: dict[str, object] = {"pid": 42, "custom_endpoint": {"key": "llamacpp-local"}}
        changed = threading.Event()

        class Process:
            pid = 42

            @staticmethod
            def wait() -> None:
                return

        with tempfile.TemporaryDirectory() as raw_root:
            (Path(raw_root) / "server.log").write_text("", encoding="utf-8")
            service = ServerLifecycleService(
                load_state=lambda: state,
                save_state=lambda value: state.update(value),
                pid_alive=lambda _pid: False,
                terminate=lambda _pid: None,
                unregister_endpoint=lambda: None,
                log_path=Path(raw_root) / "server.log",
                on_state_change=changed.set,
            )
            service.watch(Process())

            self.assertTrue(changed.wait(1), "worker exit did not publish a state change")
            self.assertIsNone(state["pid"])
            self.assertIn("[proxy] --- proxy server stop ---", (Path(raw_root) / "server.log").read_text(encoding="utf-8"))

    def test_stop_clears_owned_state_unregisters_endpoint_and_removes_log(self) -> None:
        from dashboard.application.server_lifecycle import ServerLifecycleService

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as raw_root:
            log_path = Path(raw_root) / "llama-server.log"
            log_path.write_text("diagnostic", encoding="utf-8")
            state: dict[str, object] = {
                "pid": 42,
                "custom_endpoint": {"key": "llamacpp-local"},
                "active_role": "main",
                "transition_phase": "MAIN_READY",
                "transition_error": "stale",
            }
            stopped: list[int] = []
            unregistered: list[bool] = []
            service = ServerLifecycleService(
                load_state=lambda: state,
                save_state=lambda value: state.update(value),
                pid_alive=lambda pid: pid == 42,
                terminate=lambda pid: stopped.append(pid),
                unregister_endpoint=lambda: unregistered.append(True),
                log_path=log_path,
            )

            service.stop()

            self.assertEqual(stopped, [42])
            self.assertIsNone(state["pid"])
            self.assertIsNone(state["custom_endpoint"])
            self.assertIsNone(state["active_role"])
            self.assertEqual(state["transition_phase"], "IDLE")
            self.assertIsNone(state["transition_error"])
            self.assertEqual(unregistered, [True])
            self.assertFalse(log_path.exists())

    def test_stop_does_not_clear_worker_state_until_termination_returns(self) -> None:
        from dashboard.application.server_lifecycle import ServerLifecycleService

        import tempfile

        with tempfile.TemporaryDirectory() as raw_root:
            state: dict[str, object] = {"pid": 42, "custom_endpoint": {"key": "llamacpp-local"}}
            (Path(raw_root) / "server.log").write_text("", encoding="utf-8")
            termination_finished = threading.Event()
            observed: list[object] = []

            def terminate(_pid: int) -> None:
                observed.append(state["pid"])
                termination_finished.set()

            service = ServerLifecycleService(
                load_state=lambda: state,
                save_state=lambda value: state.update(value),
                pid_alive=lambda pid: pid == 42,
                terminate=terminate,
                unregister_endpoint=lambda: None,
                log_path=Path(raw_root) / "server.log",
            )

            service.stop(preserve_log=True, keep_endpoint=True)

            self.assertTrue(termination_finished.is_set())
            self.assertEqual(observed, [42])
            self.assertIsNone(state["pid"])
            self.assertIn("[proxy] --- proxy server stop ---", (Path(raw_root) / "server.log").read_text(encoding="utf-8"))

    def test_stop_preserves_live_state_when_identity_validation_refuses_termination(self) -> None:
        from dashboard.application.server_lifecycle import ServerLifecycleService
        import tempfile

        with tempfile.TemporaryDirectory() as raw_root:
            state: dict[str, object] = {"pid": 42, "custom_endpoint": {"key": "llamacpp-local"}}
            service = ServerLifecycleService(
                load_state=lambda: state,
                save_state=lambda value: state.update(value),
                pid_alive=lambda pid: pid == 42,
                terminate=lambda _pid: False,
                unregister_endpoint=lambda: None,
                log_path=Path(raw_root) / "server.log",
            )

            with self.assertRaisesRegex(RuntimeError, "unverified process identity"):
                service.stop(keep_endpoint=True)

            self.assertEqual(state["pid"], 42)
            self.assertEqual(state["custom_endpoint"], {"key": "llamacpp-local"})

    def test_stop_retries_a_transient_windows_log_lock_without_failing(self) -> None:
        from dashboard.application.server_lifecycle import ServerLifecycleService
        from unittest.mock import patch

        import tempfile

        with tempfile.TemporaryDirectory() as raw_root:
            log_path = Path(raw_root) / "llama-server.log"
            log_path.write_text("diagnostic", encoding="utf-8")
            state: dict[str, object] = {"pid": None, "custom_endpoint": {"key": "llamacpp-local"}}
            service = ServerLifecycleService(
                load_state=lambda: state,
                save_state=lambda value: state.update(value),
                pid_alive=lambda _pid: False,
                terminate=lambda _pid: None,
                unregister_endpoint=lambda: None,
                log_path=log_path,
            )

            with patch.object(Path, "unlink", side_effect=[PermissionError("locked"), None]) as unlink:
                service.stop()

            self.assertEqual(unlink.call_count, 2)
            self.assertIsNone(state["pid"])
            self.assertIsNone(state["custom_endpoint"])


class HuggingFaceCacheServiceTests(unittest.TestCase):
    def test_groups_cached_gguf_parts_and_excludes_projectors(self) -> None:
        from dashboard.application.huggingface_cache import HuggingFaceCacheService

        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as raw_root:
            snapshot = Path(raw_root)
            (snapshot / "model-00001-of-00002.gguf").write_bytes(b"a")
            (snapshot / "model-00002-of-00002.gguf").write_bytes(b"bc")
            (snapshot / "mmproj.gguf").write_bytes(b"ignore")
            service = HuggingFaceCacheService(
                find_cli=lambda: "hf",
                run=lambda _argv: (0, json.dumps([{"repo_id": "owner/repo", "snapshot_path": str(snapshot)}])),
            )

            groups, warning = service.cached_files("owner/repo")

            self.assertIsNone(warning)
            self.assertEqual(groups, [{"label": "model", "paths": ["model-00001-of-00002.gguf", "model-00002-of-00002.gguf"], "total_bytes": 3, "fit": "downloaded"}])

    def test_inventory_and_file_selection_include_complete_snapshot_omitted_by_hf_cli(self) -> None:
        from dashboard.application.huggingface_cache import HuggingFaceCacheService

        import tempfile

        with tempfile.TemporaryDirectory() as raw_root:
            hub = Path(raw_root) / "hub"
            repo = hub / "models--prism-ml--Ternary-Bonsai-2-27B-gguf"
            snapshot = repo / "snapshots" / "revision"
            snapshot.mkdir(parents=True)
            (repo / "refs").mkdir()
            (repo / "refs" / "main").write_text("revision", encoding="utf-8")
            (repo / "blobs").mkdir()
            (repo / "blobs" / "other-quant.downloadInProgress").write_bytes(b"partial")
            (snapshot / "Ternary-Bonsai-2-27B-PQ2_0.gguf").write_bytes(b"model")

            def run(argv):
                if "--revisions" in argv:
                    return 0, "[]"
                return 0, '[{"repo_id":"owner/Official-GGUF","size":"1.0G"}]'

            service = HuggingFaceCacheService(lambda: "hf", run, cache_root=lambda: hub)

            models, _executable, warning = service.downloaded_models()
            groups, files_warning = service.cached_files("prism-ml/Ternary-Bonsai-2-27B-gguf")

            self.assertIsNone(warning)
            self.assertEqual(
                [model["repo_id"] for model in models],
                ["owner/Official-GGUF", "prism-ml/Ternary-Bonsai-2-27B-gguf"],
            )
            self.assertIsNone(files_warning)
            self.assertEqual(groups[0]["paths"], ["Ternary-Bonsai-2-27B-PQ2_0.gguf"])


class JsonHttpClientTests(unittest.TestCase):
    def test_sends_hf_token_only_to_huggingface_hosts(self) -> None:
        from dashboard.application.json_http import JsonHttpClient

        seen: list[object] = []

        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self): return b'{"ok": true}'

        client = JsonHttpClient(lambda: "token", lambda request: (seen.append(request) or Response()), lambda _: None)

        self.assertEqual(client.get("https://huggingface.co/api/models/a"), {"ok": True})
        self.assertEqual(client.get("https://api.github.com/repos/ggml-org/llama.cpp"), {"ok": True})
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer token")
        self.assertIsNone(seen[1].get_header("Authorization"))


class HuggingFaceModelWorkflowTests(unittest.TestCase):
    def test_local_models_lists_every_cached_repository_regardless_of_runtime(self) -> None:
        from dashboard.application.huggingface_model_workflow import HuggingFaceModelWorkflow

        cached = [
            {"repo_id": "owner/Regular-GGUF", "size": "2 GB"},
            {"repo_id": "prism-ml/Ternary-Bonsai-2-27B-gguf", "size": "7 GB"},
        ]
        workflow = HuggingFaceModelWorkflow(
            load_state=lambda: {"runtime_kind": "prism_ml"},
            runtime_kind=lambda current: str(current["runtime_kind"]),
            accepts=lambda *_: True,
            cache_models=lambda: (cached, "hf", None),
            cached_files=lambda _repo: ([], None),
            cached_paths=lambda _repo, _paths: ([], None),
            http_json=lambda _url: [],
            download=lambda *_args, **_kwargs: None,
            register=lambda *_args, **_kwargs: None,
            remove_cache=lambda _repo: True,
            model_id=lambda path: path.stem,
            visible_repositories=lambda _kind, repositories: [
                repo for repo in repositories if repo.startswith("prism-ml/")
            ],
        )

        result = workflow.local_models()

        self.assertEqual(result["models"], cached)
        self.assertEqual(result["runtime_kind"], "prism_ml")

    def test_groups_remote_files_filters_search_and_registers_only_cached_selection(self) -> None:
        from dashboard.application.huggingface_model_workflow import HuggingFaceModelWorkflow

        state: dict[str, object] = {"runtime_kind": "official", "prism_release_tag": "", "models": {}}
        registered: list[tuple[object, ...]] = []
        workflow = HuggingFaceModelWorkflow(
            load_state=lambda: state,
            runtime_kind=lambda current: str(current["runtime_kind"]),
            accepts=lambda _kind, repo, paths, _version: repo != "blocked/repo" and bool(paths),
            cache_models=lambda: ([{"repo_id": "owner/repo", "size": "1 GB"}], "hf", None),
            cached_files=lambda repo: ([{"label": "model", "paths": ["model.gguf"], "total_bytes": 3, "fit": "downloaded"}], None),
            cached_paths=lambda repo, paths: ([Path("E:/gguf/models") / path for path in paths], None),
            http_json=lambda url: (
                [{"path": "model.gguf", "size": 3}]
                if "/tree/main" in url else [
                    {"id": "owner/repo", "downloads": 12},
                    {"id": "blocked/repo", "downloads": 99},
                ]
            ),
            download=lambda *_: None,
            register=lambda *args, **kwargs: registered.append(args),
            remove_cache=lambda _repo: True,
            model_id=lambda path: path.stem,
        )

        self.assertEqual(workflow.search("model", 20), {"hits": [{"repo": "owner/repo", "downloads": 12}]})
        self.assertEqual(workflow.repository("owner/repo"), {"files": [{"label": "model", "paths": ["model.gguf"], "total_bytes": 3, "fit": "available"}]})
        registered_model = workflow.register("owner/repo", ["model.gguf"])
        self.assertEqual(registered_model["model_id"], "repo")
        self.assertEqual(registered[0][0], "repo")
        self.assertEqual(registered[0][1], [Path("E:/gguf/models/model.gguf")])
        with self.assertRaisesRegex(RuntimeError, "HF cache"):
            workflow.register("owner/repo", ["missing.gguf"])

    def test_register_route_publishes_inventory_refresh_event(self) -> None:
        from dashboard.routes.model_routes import ModelRouteContext, create_router
        from fastapi.testclient import TestClient
        from fastapi import FastAPI

        events: list[tuple[str, list[str]]] = []

        class Workflow:
            @staticmethod
            def register(repo_id, paths, alias=""):
                return {"model_id": alias or repo_id, "paths": paths}

        context = ModelRouteContext(
            lifecycle=lambda: object(), workflow=lambda: Workflow(),
            create_job=lambda kind, detail: {"kind": kind, "detail": detail, "job_id": "job"},
            launch=lambda *_args: None, finish=lambda *_args: None,
            begin_download=lambda *_args: None,
            publish=lambda event_type, refresh: events.append((event_type, list(refresh))),
        )
        app = FastAPI()
        app.include_router(create_router(context))

        response = TestClient(app).post(
            "/register",
            json={"repo": "owner/repo", "paths": ["model.gguf"], "alias": "local-model"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(events, [("model_registered", ["status", "inventory"])])

    def test_register_uses_model_runtime_instead_of_selected_dashboard_runtime(self) -> None:
        from dashboard.application.huggingface_model_workflow import HuggingFaceModelWorkflow

        accepted_kinds: list[str] = []
        registered: list[tuple[object, ...]] = []
        workflow = HuggingFaceModelWorkflow(
            load_state=lambda: {"runtime_kind": "prism_ml", "prism_release_tag": "prism-test"},
            runtime_kind=lambda current: str(current["runtime_kind"]),
            accepts=lambda kind, _repo, _paths, _version: accepted_kinds.append(kind) is None or kind == "official",
            cache_models=lambda: ([], "hf", None),
            cached_files=lambda _repo: ([{"label": "Qwen", "paths": ["Qwen-Q6_K.gguf"], "total_bytes": 3, "fit": "downloaded"}], None),
            cached_paths=lambda _repo, paths: ([Path("E:/gguf/models") / path for path in paths], None),
            http_json=lambda _url: [],
            download=lambda *_args, **_kwargs: None,
            register=lambda *args, **kwargs: registered.append(args),
            remove_cache=lambda _repo: True,
            model_id=lambda path: path.stem,
            registration_runtime_kind=lambda model_id: "prism_ml" if model_id.startswith("Ternary-Bonsai") else "official",
        )

        result = workflow.register("owner/Qwen-GGUF", ["Qwen-Q6_K.gguf"])

        self.assertEqual(result["model_id"], "Qwen")
        self.assertEqual(accepted_kinds, ["official"])
        self.assertEqual(registered[0][0], "Qwen")

    def test_suggests_repo_alias_and_suffixes_existing_names_for_repeat_registration(self) -> None:
        from dashboard.application.huggingface_model_workflow import HuggingFaceModelWorkflow

        state: dict[str, object] = {
            "runtime_kind": "official",
            "models": {
                "Ornith-1.5-35B-A3B": {},
                "Ornith-1.5-35B-A3B-(2)": {},
            },
        }
        registered: list[tuple[object, ...]] = []

        def register(model_id, *args, **kwargs):
            registered.append((model_id, *args))
            state["models"][model_id] = {"paths": []}

        workflow = HuggingFaceModelWorkflow(
            load_state=lambda: state,
            runtime_kind=lambda current: str(current["runtime_kind"]),
            accepts=lambda *_args: True,
            cache_models=lambda: ([], "hf", None),
            cached_files=lambda _repo: ([{"label": "Ornith-Q6", "paths": ["Ornith-Q6.gguf"], "total_bytes": 3, "fit": "downloaded"}], None),
            cached_paths=lambda _repo, paths: ([Path("E:/gguf/models") / path for path in paths], None),
            http_json=lambda _url: [], download=lambda *_args, **_kwargs: None,
            register=register, remove_cache=lambda _repo: True, model_id=lambda path: path.stem,
            registration_runtime_kind=lambda _model_id: "official",
        )

        self.assertEqual(
            workflow.suggested_alias("ornith-ai/Ornith-1.5-35B-A3B-GGUF"),
            "Ornith-1.5-35B-A3B-(3)",
        )
        result = workflow.register(
            "ornith-ai/Ornith-1.5-35B-A3B-GGUF", ["Ornith-Q6.gguf"],
            alias="Ornith-1.5-35B-A3B",
        )
        self.assertEqual(result["model_id"], "Ornith-1.5-35B-A3B-(3)")
        self.assertEqual(registered[0][0], "Ornith-1.5-35B-A3B-(3)")


class HuggingFaceDownloadServiceTests(unittest.TestCase):
    def test_worker_emits_monotonic_progress_and_reserves_completion(self) -> None:
        from dashboard.application import hf_download_worker

        import io
        from unittest.mock import patch

        output = io.StringIO()
        hf_download_worker._LAST_PERCENT = -1
        with patch("sys.stderr", output):
            hf_download_worker._emit_percent(1, 4)
            hf_download_worker._emit_percent(1, 4)
            hf_download_worker._emit_percent(4, 4)

        self.assertEqual(
            output.getvalue().splitlines(),
            ["HERMES_PROGRESS:25%", "HERMES_PROGRESS:99%"],
        )

    def test_validates_the_exact_cli_reported_path(self) -> None:
        from dashboard.application.huggingface_download import HuggingFaceDownloadService

        import tempfile
        from pathlib import Path

        class Stream:
            def __init__(self, value): self.value = value
            def read(self, _): value, self.value = self.value, b""; return value

        class Process:
            stdout = None
            stderr = None
            def wait(self, timeout=None): return 0

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "model.gguf"
            model.write_bytes(b"gguf")
            scripts = root / "Scripts"
            scripts.mkdir()
            hf = scripts / "hf.exe"
            hf.write_bytes(b"")
            (scripts / "python.exe").write_bytes(b"")
            process = Process()
            process.stdout = Stream((str(model) + "\n").encode())
            process.stderr = Stream(b"")
            service = HuggingFaceDownloadService(lambda _argv: process, cwd=lambda: Path(raw_root))

            self.assertEqual(service.download(str(hf), "owner/repo", "model.gguf"), model.resolve())

    def test_enables_cli_progress_and_parses_human_reported_path(self) -> None:
        from dashboard.application.huggingface_download import HuggingFaceDownloadService

        import tempfile
        from pathlib import Path

        class Stream:
            def __init__(self, value): self.value = value
            def read(self, _): value, self.value = self.value, b""; return value

        class AvailableStream(Stream):
            def read(self, _): return b""
            def read1(self, _): value, self.value = self.value, b""; return value

        class Process:
            stdout = None
            stderr = None
            def wait(self, timeout=None): return 0

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "model.gguf"
            model.write_bytes(b"gguf")
            scripts = root / "Scripts"
            scripts.mkdir()
            hf = scripts / "hf.exe"
            python = scripts / "python.exe"
            hf.write_bytes(b"")
            python.write_bytes(b"")
            process = Process()
            process.stdout = Stream((f"✓ Downloaded\n  path: {model}\n").encode())
            process.stderr = AvailableStream(b"HERMES_PROGRESS:42%\n")
            argv: list[str] = []
            progress: list[int] = []

            def start_process(command: list[str]):
                argv.extend(command)
                return process

            service = HuggingFaceDownloadService(start_process, cwd=lambda: Path(raw_root))

            self.assertEqual(
                service.download(str(hf), "owner/repo", "model.gguf", progress.append),
                model.resolve(),
            )
            self.assertEqual(argv[0], str(python))
            self.assertTrue(argv[1].endswith("hf_download_worker.py"))
            self.assertEqual(argv[2:], ["owner/repo", "model.gguf"])
            self.assertEqual(progress, [42])


class ExecutionLeaseCoordinatorTests(unittest.TestCase):
    def test_main_lease_publishes_queue_and_active_transitions(self) -> None:
        import asyncio
        from dashboard.application.execution_lease import ExecutionLeaseCoordinator

        counts: list[tuple[int, int]] = []
        roles: list[str] = []

        async def exercise() -> None:
            coordinator = ExecutionLeaseCoordinator(
                ensure_role=lambda role: _record_async(roles, role),
                role_configured=lambda _role: True,
                abort_transition=lambda _role, _error: _noop_async(),
                publish_counts=lambda queued, active: _record_async(counts, (queued, active)),
            )
            finish = await coordinator.begin("main")
            self.assertEqual(counts, [(1, 0), (0, 1)])
            await finish()

        async def _record_async(target: list, value: object) -> None:
            target.append(value)

        async def _noop_async() -> None:
            return None

        asyncio.run(exercise())
        self.assertEqual(roles, ["main"])
        self.assertEqual(counts[-1], (0, 0))


class OwnedProcessTests(unittest.TestCase):
    def test_termination_escalates_to_kill_and_confirms_exit(self) -> None:
        from dashboard.application.owned_process import terminate_owned_process

        events: list[object] = []

        class Process:
            def terminate(self) -> None:
                events.append("terminate")

            def wait(self, timeout: int) -> None:
                events.append(("wait", timeout))
                if timeout == 10:
                    raise TimeoutError("still running")

            def kill(self) -> None:
                events.append("kill")

            def poll(self) -> int | None:
                return 0

        self.assertTrue(terminate_owned_process(Process()))
        self.assertEqual(events, ["terminate", ("wait", 10), "kill", ("wait", 5)])


class ServerStartupServiceTests(unittest.TestCase):
    def test_startup_failure_detail_prefers_safe_raw_native_error(self) -> None:
        from dashboard.application.server_startup import _startup_failure_detail

        detail = _startup_failure_detail({
            "raw_lines": [
                "prompt: secret user text",
                "error: CUDA allocation failed for C:/models/private/model.gguf",
            ],
            "lines": ["[Main] 서버 초기화 중"],
        })

        self.assertIn("CUDA allocation failed", detail)
        self.assertIn("[PATH]", detail)
        self.assertNotIn("secret user text", detail)
        self.assertNotIn("C:/models/private", detail)

    def test_requires_an_active_model_before_resolving_runtime(self) -> None:
        from dashboard.application.server_startup import ServerStartupService

        service = ServerStartupService(
            load_state=lambda: {"active_model_id": None}, save_state=lambda _: None,
            executable=lambda: None, stop=lambda preserve_log=False: None,
            active_path=lambda _: None, load_options=lambda: {}, runtime_kind=lambda _: "official",
            backend=lambda _: None, serving_model_name=lambda model_id: model_id, option_args=lambda _: [], watch=lambda _: None,
            health=lambda _: False, register_endpoint=lambda _, __: {}, log_tail=lambda _: {"lines": []},
            log_path=None, spawn=lambda *_: None, now=lambda: 0.0, sleep=lambda _: None,
        )

        with self.assertRaisesRegex(RuntimeError, "select a model"):
            service.start()

    def test_writes_llama_server_version_as_the_first_startup_log(self) -> None:
        from dashboard.application.server_startup import ServerStartupService

        import tempfile

        class Backend:
            @staticmethod
            def build_command(*_args, **_kwargs):
                return ["llama-server"]

        class Process:
            pid = os.getpid()

            @staticmethod
            def poll():
                return None

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "model.gguf"
            model.write_bytes(b"gguf")
            log_path = root / "activity.log"
            state = {"active_model_id": "model", "models": {"model": {}}}
            service = ServerStartupService(
                load_state=lambda: state, save_state=lambda value: state.update(value),
                executable=lambda: root / "llama-server.exe", stop=lambda **_: None,
                active_path=lambda _model_id: model, load_options=lambda: {"model": {}},
                runtime_kind=lambda _state: "official", backend=lambda _kind: Backend(),
                serving_model_name=lambda _model_id: "served", option_args=lambda _options: [],
                watch=lambda _process: None, health=lambda _port: True,
                register_endpoint=lambda _port, _model: {}, log_tail=lambda _limit: {"lines": []},
                log_path=log_path, spawn=lambda *_args: Process(),
                now=lambda: 0.0, sleep=lambda _seconds: None,
                server_version=lambda _executable: "0.6.0-dev (build 11515, commit 3d65c90d0)",
            )

            service.start()

            lines = log_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], "[Server] llama-server version: 0.6.0-dev (build 11515, commit 3d65c90d0)")
            self.assertIn("--- llama-server start: model=model, port=18434 ---", lines[1])

    def test_uses_a_stable_serving_alias_for_command_and_endpoint(self) -> None:
        from dashboard.application.server_startup import ServerStartupService

        import tempfile

        class Backend:
            def __init__(self): self.command = []
            def build_command(self, _executable, _port, _model_id, _entry, _options, model_path=None):
                self.command = ["llama-server", "--model", str(model_path)]
                return self.command

        class Process:
            pid = os.getpid()
            def poll(self): return None

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "Ternary-Bonsai-2-27B-PQ2_0.gguf"
            model.write_bytes(b"gguf")
            log_path = root / "llama-server.log"
            state: dict[str, object] = {
                "active_model_id": "Ternary-Bonsai-2-27B-PQ2_0",
                "models": {"Ternary-Bonsai-2-27B-PQ2_0": {}},
            }
            endpoint_models: list[str] = []
            backend = Backend()
            service = ServerStartupService(
                load_state=lambda: state, save_state=lambda value: state.update(value),
                executable=lambda: root / "llama-server.exe", stop=lambda **_: None,
                active_path=lambda _model_id: model, load_options=lambda: {"Ternary-Bonsai-2-27B-PQ2_0": {}},
                runtime_kind=lambda _state: "prism_ml", backend=lambda _kind: backend,
                serving_model_name=lambda _model_id: "Ternary-Bonsai-2-27B",
                option_args=lambda _: [], watch=lambda _: None, health=lambda _: True,
                register_endpoint=lambda _port, model_name: endpoint_models.append(model_name) or {"model": model_name},
                log_tail=lambda _: {"lines": []}, log_path=log_path,
                spawn=lambda *_: Process(), now=lambda: 0.0, sleep=lambda _: None,
            )

            service.start()

            self.assertEqual(backend.command[-2:], ["--alias", "Ternary-Bonsai-2-27B"])
            self.assertEqual(endpoint_models, ["Ternary-Bonsai-2-27B"])

    def test_strata_launch_controls_worker_port_and_working_directory(self) -> None:
        from dashboard.application.server_startup import ServerStartupService
        from dashboard.backends.base import WorkerLaunch

        class Backend:
            key = "strata"

            @staticmethod
            def build_launch(executable, _port, _model_id, _entry, _options,
                             _serving_model, _option_args, model_path=None):
                return WorkerLaunch(
                    [str(executable), "-m", "serve.server", "--config", str(model_path), "--port", "18080"],
                    executable.parent.parent.parent,
                    18080,
                    "Strata",
                    startup_timeout=180,
                    environment={},
                )

        class Process:
            pid = os.getpid()

            @staticmethod
            def poll():
                return None

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            python = root / ".venv" / "Scripts" / "python.exe"
            config = root / "strata.json"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"python")
            config.write_text("{}\n", encoding="utf-8")
            state = {
                "active_model_id": "strata-main",
                "active_role": "main",
                "models": {"strata-main": {"runtime_kind": "strata"}},
            }
            spawned: list[tuple[list[str], Path, dict[str, str] | None]] = []
            health_ports: list[int] = []

            service = ServerStartupService(
                load_state=lambda: state, save_state=lambda value: state.update(value),
                executable=lambda: python, stop=lambda **_: None,
                active_path=lambda _model_id: config, load_options=lambda: {"strata-main": {"port": "18434"}},
                runtime_kind=lambda _state: "strata", backend=lambda _kind: Backend(),
                serving_model_name=lambda _model_id: "strata-main", option_args=lambda _options: [],
                watch=lambda _process: None,
                health=lambda port: health_ports.append(port) or True,
                register_endpoint=lambda port, model: {"port": port, "model": model},
                log_tail=lambda _limit: {"lines": []}, log_path=root / "activity.log",
                spawn=lambda command, _log, cwd, environment=None: spawned.append((command, cwd, environment)) or Process(),
                now=lambda: 0.0, sleep=lambda _seconds: None,
            )

            service.start()

            self.assertEqual(spawned[0][1], root)
            self.assertNotIn("STRATA_API_KEY", spawned[0][2])
            self.assertEqual(health_ports, [18080])
            self.assertEqual(state["port"], 18080)
            self.assertEqual(state["custom_endpoint"], {"port": 18080, "model": "strata-main"})

    def test_worker_early_exit_surfaces_native_failure_reason(self) -> None:
        import sys
        import tempfile
        from dashboard.application.server_startup import ServerStartupService

        class Backend:
            @staticmethod
            def build_command(*_args, **_kwargs):
                return [sys.executable]

        class Process:
            pid = os.getpid()

            @staticmethod
            def poll():
                return 1

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "model.gguf"
            model.write_bytes(b"gguf")
            state = {"active_model_id": "model", "models": {"model": {}}}

            def mutate(callback):
                callback(state)
                return state

            service = ServerStartupService(
                load_state=lambda: state, save_state=lambda value: state.update(value),
                executable=lambda: Path(sys.executable), stop=lambda **_kwargs: None,
                active_path=lambda _model_id: model, load_options=lambda: {"model": {}},
                runtime_kind=lambda _state: "official", backend=lambda _kind: Backend(),
                serving_model_name=lambda _model_id: "served", option_args=lambda _options: [],
                watch=lambda _process: None, health=lambda _port: False,
                register_endpoint=lambda _port, _model: {},
                log_tail=lambda _limit: {
                    "raw_lines": ["error: CUDA allocation failed"],
                    "lines": ["[Main] 서버 초기화 중"],
                },
                log_path=root / "server.log", spawn=lambda *_args: Process(),
                now=lambda: 0.0, sleep=lambda _seconds: None, mutate_state=mutate,
            )

            with self.assertRaisesRegex(RuntimeError, "CUDA allocation failed"):
                service.start()

    def test_identity_capture_failure_terminates_owned_process_before_state_write(self) -> None:
        import sys
        import tempfile
        import types
        from unittest.mock import patch
        from dashboard.application.server_startup import ServerStartupService

        class Backend:
            def build_command(self, *_args, **_kwargs):
                return ["llama-server"]

        class Process:
            pid = 99

            def __init__(self) -> None:
                self.terminated = False
                self.waited = False

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout=None) -> None:
                self.waited = True

            def poll(self):
                return 1

        class IdentityLookupFails:
            def __init__(self, _pid: int) -> None:
                raise RuntimeError("identity unavailable")

        process = Process()
        writes: list[dict[str, object]] = []
        fake_psutil = types.SimpleNamespace(Process=IdentityLookupFails)
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "model.gguf"
            model.write_bytes(b"gguf")
            state = {"active_model_id": "model", "models": {"model": {}}}
            service = ServerStartupService(
                load_state=lambda: state, save_state=lambda value: writes.append(dict(value)),
                executable=lambda: root / "llama-server.exe", stop=lambda **_: None,
                active_path=lambda _: model, load_options=lambda: {"model": {}},
                runtime_kind=lambda _: "official", backend=lambda _: Backend(),
                serving_model_name=lambda _: "served", option_args=lambda _: [], watch=lambda _: None,
                health=lambda _: False, register_endpoint=lambda *_: {}, log_tail=lambda _: {"lines": []},
                log_path=root / "server.log", spawn=lambda *_: process,
                now=lambda: 0.0, sleep=lambda _: None,
            )

            with patch.dict(sys.modules, {"psutil": fake_psutil}):
                with self.assertRaisesRegex(RuntimeError, "identity"):
                    service.start()

        self.assertTrue(process.terminated)
        self.assertTrue(process.waited)
        self.assertEqual(writes, [])

    def test_state_persistence_failure_terminates_verified_owned_process(self) -> None:
        import sys
        import tempfile
        import types
        from unittest.mock import patch
        from dashboard.application.server_startup import ServerStartupService

        class Backend:
            def build_command(self, *_args, **_kwargs):
                return ["llama-server"]

        class Process:
            pid = 99

            def __init__(self) -> None:
                self.terminated = False
                self.waited = False

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout=None) -> None:
                self.waited = True

            def poll(self):
                return None

        class Identity:
            def __init__(self, _pid: int) -> None:
                return

            def create_time(self) -> float:
                return 100.0

            def exe(self) -> str:
                return "C:/runtime/llama-server.exe"

        process = Process()
        fake_psutil = types.SimpleNamespace(Process=Identity)
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "model.gguf"
            model.write_bytes(b"gguf")
            state = {"active_model_id": "model", "models": {"model": {}}}
            service = ServerStartupService(
                load_state=lambda: state, save_state=lambda _value: None,
                executable=lambda: Path("C:/runtime/llama-server.exe"), stop=lambda **_: None,
                active_path=lambda _: model, load_options=lambda: {"model": {}},
                runtime_kind=lambda _: "official", backend=lambda _: Backend(),
                serving_model_name=lambda _: "served", option_args=lambda _: [], watch=lambda _: None,
                health=lambda _: False, register_endpoint=lambda *_: {}, log_tail=lambda _: {"lines": []},
                log_path=root / "server.log", spawn=lambda *_: process,
                now=lambda: 0.0, sleep=lambda _: None,
                mutate_state=lambda _callback: (_ for _ in ()).throw(OSError("state write failed")),
            )

            with patch.dict(sys.modules, {"psutil": fake_psutil}):
                with self.assertRaisesRegex(OSError, "state write failed"):
                    service.start()

        self.assertTrue(process.terminated)
        self.assertTrue(process.waited)

    def test_endpoint_state_write_failure_terminates_worker_and_clears_pid(self) -> None:
        import sys
        import tempfile
        import types
        from unittest.mock import patch
        from dashboard.application.server_startup import ServerStartupService

        class Backend:
            def build_command(self, *_args, **_kwargs):
                return ["llama-server"]

        class Process:
            pid = 99

            def __init__(self) -> None:
                self.terminated = False
                self.waited = False

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout=None) -> None:
                self.waited = True

            def poll(self):
                return None

        class Identity:
            def __init__(self, _pid: int) -> None:
                return

            def create_time(self) -> float:
                return 100.0

            def exe(self) -> str:
                return "C:/runtime/llama-server.exe"

        process = Process()
        state = {"active_model_id": "model", "models": {"model": {}}}
        mutations = 0
        stops: list[dict[str, object]] = []

        def mutate(callback) -> None:
            nonlocal mutations
            mutations += 1
            if mutations == 2:
                raise OSError("endpoint state write failed")
            callback(state)

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "model.gguf"
            model.write_bytes(b"gguf")
            service = ServerStartupService(
                load_state=lambda: state, save_state=lambda _value: None,
                executable=lambda: Path("C:/runtime/llama-server.exe"),
                stop=lambda **kwargs: stops.append(kwargs),
                active_path=lambda _: model, load_options=lambda: {"model": {}},
                runtime_kind=lambda _: "official", backend=lambda _: Backend(),
                serving_model_name=lambda _: "served", option_args=lambda _: [], watch=lambda _: None,
                health=lambda _: True, register_endpoint=lambda *_: {"model": "served"},
                log_tail=lambda _: {"lines": []}, log_path=root / "server.log",
                spawn=lambda *_: process, now=lambda: 0.0, sleep=lambda _: None,
                mutate_state=mutate,
            )

            with patch.dict(sys.modules, {"psutil": types.SimpleNamespace(Process=Identity)}):
                with self.assertRaisesRegex(OSError, "endpoint state write failed"):
                    service.start()

        self.assertTrue(process.terminated)
        self.assertTrue(process.waited)
        self.assertIsNone(state.get("pid"))
        self.assertEqual(stops, [
            {"preserve_log": True, "keep_endpoint": True},
            {"preserve_log": True},
        ])

    def test_cleanup_failure_retains_worker_identity_state(self) -> None:
        import sys
        import tempfile
        import types
        from unittest.mock import patch
        from dashboard.application.server_startup import ServerStartupService

        class Backend:
            def build_command(self, *_args, **_kwargs):
                return ["llama-server"]

        class Process:
            pid = 99

            def terminate(self) -> None:
                raise OSError("terminate failed")

            def kill(self) -> None:
                raise OSError("kill failed")

            def wait(self, timeout=None) -> None:
                raise TimeoutError("still running")

            def poll(self):
                return None

        class Identity:
            def __init__(self, _pid: int) -> None:
                return

            def create_time(self) -> float:
                return 100.0

            def exe(self) -> str:
                return "C:/runtime/llama-server.exe"

        process = Process()
        state = {"active_model_id": "model", "models": {"model": {}}}
        mutations = 0

        def mutate(callback) -> None:
            nonlocal mutations
            mutations += 1
            if mutations == 2:
                raise OSError("endpoint state write failed")
            callback(state)

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "model.gguf"
            model.write_bytes(b"gguf")
            service = ServerStartupService(
                load_state=lambda: state, save_state=lambda _value: None,
                executable=lambda: Path("C:/runtime/llama-server.exe"), stop=lambda **_kwargs: None,
                active_path=lambda _: model, load_options=lambda: {"model": {}},
                runtime_kind=lambda _: "official", backend=lambda _: Backend(),
                serving_model_name=lambda _: "served", option_args=lambda _: [], watch=lambda _: None,
                health=lambda _: True, register_endpoint=lambda *_: {"model": "served"},
                log_tail=lambda _: {"lines": []}, log_path=root / "server.log",
                spawn=lambda *_: process, now=lambda: 0.0, sleep=lambda _: None,
                mutate_state=mutate,
            )

            with patch.dict(sys.modules, {"psutil": types.SimpleNamespace(Process=Identity)}):
                with self.assertRaisesRegex(RuntimeError, "retaining worker ownership state"):
                    service.start()

        self.assertEqual(state.get("pid"), 99)
        self.assertEqual(state.get("worker_identity", {}).get("pid"), 99)


class MmprojAutoTests(unittest.TestCase):
    """Custom mmproj-auto behavior: discover a projector gguf next to the model
    and emit an explicit --mmproj flag (stock --mmproj-auto is a no-op for --model)."""

    def test_find_prefers_base_mmproj_then_falls_back_to_any(self):
        import tempfile
        from dashboard.application.server_startup import _find_mmproj_near as find

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "Ternary-Bonsai-2-27B-PQ2_0.gguf"
            projector = root / "Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf"
            decoy = root / "Qwen-1.5-14B-mmproj.gguf"
            for p in (model, projector, decoy):
                p.write_bytes(b"gguf")
            self.assertEqual(find(model), projector)

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "model.gguf"
            projector = root / "model-mmproj.gguf"
            model.write_bytes(b"gguf")
            projector.write_bytes(b"gguf")
            self.assertEqual(find(model), projector)

    def test_find_none_without_projector_or_missing_model(self):
        import tempfile
        from dashboard.application.server_startup import _find_mmproj_near as find

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "model.gguf"
            model.write_bytes(b"gguf")
            self.assertIsNone(find(model))
        self.assertIsNone(find(None))

    def _command(self, options, model):
        """Return the exact command list that ServerStartupService.start() assembles
        and passes to spawn. The caller creates the model file (and any sibling
        projector) on disk; we just feed that real path in. start() extends the
        recorded command list in place, so it is exactly what would be spawned."""
        import tempfile
        from dashboard.application.server_startup import ServerStartupService

        root = Path(tempfile.mkdtemp(prefix="llama_startup_"))
        state = {"active_model_id": "model", "models": {"model": {}}}
        recorded = {}

        class Backend:
            def build_command(self, executable, port, model_id, entry, opts, model_path=None):
                command = [executable, "--model", str(model_path)]
                recorded["command"] = command
                return command

        service = ServerStartupService(
            load_state=lambda: state, save_state=lambda _: None,
            executable=lambda: root / "llama-server.exe", stop=lambda **_: None,
            active_path=lambda _model_id: model,
            load_options=lambda: {"model": options},
            runtime_kind=lambda _state: "prism_ml",
            backend=lambda _kind: Backend(),
            serving_model_name=lambda _model_id: "served", option_args=lambda _: [],
            watch=lambda _: None, health=lambda _: True,
            register_endpoint=lambda _port, _name: {}, log_tail=lambda _: {"lines": []},
            log_path=root / "llama-server.log",
            spawn=lambda *_: _FakeProcess(), now=lambda: 0.0, sleep=lambda _: None,
        )
        service.start()
        return recorded["command"]

    def test_emits_mmproj_when_auto_enabled_and_projector_present(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "Ternary-Bonsai-2-27B-PQ2_0.gguf"
            projector = root / "Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf"
            for p in (model, projector):
                p.write_bytes(b"gguf")
            cmd = self._command({"mmproj-auto": "", "ctx-size": "8192"}, model)
            self.assertIn("--mmproj", cmd)
            self.assertIn(str(projector), cmd)
            self.assertNotIn("--mmproj-auto", cmd)
            self.assertEqual(cmd[cmd.index("--mmproj") + 1], str(projector))

    def test_omits_mmproj_when_auto_disabled(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "model.gguf"
            projector = root / "model-mmproj.gguf"
            for p in (model, projector):
                p.write_bytes(b"gguf")
            cmd = self._command({"ctx-size": "8192"}, model)
            self.assertNotIn("--mmproj", cmd)
            self.assertNotIn("--mmproj-auto", cmd)

    def test_respects_no_mmproj_override(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "model.gguf"
            projector = root / "model-mmproj.gguf"
            for p in (model, projector):
                p.write_bytes(b"gguf")
            cmd = self._command({"mmproj-auto": "", "no-mmproj": ""}, model)
            self.assertNotIn("--mmproj", cmd)

    def test_none_when_no_projector(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "model.gguf"
            model.write_bytes(b"gguf")
            cmd = self._command({"mmproj-auto": ""}, model)
            self.assertNotIn("--mmproj", cmd)
            self.assertNotIn("--mmproj-auto", cmd)


class ExecutionTransitionTests(unittest.TestCase):
    def test_official_worker_is_not_reused_across_roles(self) -> None:
        from dashboard import backend_impl

        state = {
            "execution_mode": "exclusive_swap",
            "active_role": "main",
            "active_model_id": "shared",
            "active_runtime_kind": "official",
        }
        profiles = {
            "main": {"configured": True, "model_id": "shared", "runtime_kind": "official", "preset_id": ""},
            "compression": {"configured": True, "model_id": "shared", "runtime_kind": "official", "preset_id": ""},
        }

        def mutate(change):
            change(state)
            return state

        startup = unittest.mock.MagicMock()
        with patch.object(backend_impl, "execution_profiles", return_value={"profiles": profiles}), \
                patch.object(backend_impl, "_state", side_effect=lambda: dict(state)), \
                patch.object(backend_impl, "_mutate_state", side_effect=mutate), \
                patch.object(backend_impl, "_active_runtime_kind", return_value="official"), \
                patch.object(backend_impl, "_is_server_running", return_value=True), \
                patch.object(backend_impl, "_server_startup", return_value=startup), \
                patch.object(backend_impl, "_transition_log"):
            result = backend_impl.ensure_execution_role("compression")

        self.assertFalse(result["already_running"])
        startup.start.assert_called_once()

    def test_strata_execution_profile_accepts_registered_json_config(self) -> None:
        from dashboard import backend_impl

        state = {
            "models": {
                "strata-main": {
                    "runtime_kind": "strata",
                    "paths": ["E:/gguf/models/strata-iq2_xs.json"],
                },
            },
        }
        with patch.object(backend_impl, "_state", return_value=state):
            self.assertTrue(backend_impl._execution_profile_accepts("strata", "strata-main"))

    def test_same_strata_worker_is_reused_when_switching_to_compression_role(self) -> None:
        from dashboard import backend_impl

        state = {
            "execution_mode": "exclusive_swap",
            "active_role": "main",
            "active_model_id": "strata-main",
            "active_runtime_kind": "strata",
        }
        profiles = {
            "main": {"configured": True, "model_id": "strata-main", "runtime_kind": "strata", "preset_id": ""},
            "compression": {"configured": True, "model_id": "strata-main", "runtime_kind": "strata", "preset_id": ""},
        }

        def mutate(change):
            change(state)
            return state

        with patch.object(backend_impl, "execution_profiles", return_value={"profiles": profiles}), \
                patch.object(backend_impl, "_state", side_effect=lambda: dict(state)), \
                patch.object(backend_impl, "_mutate_state", side_effect=mutate), \
                patch.object(backend_impl, "_active_runtime_kind", return_value="strata"), \
                patch.object(backend_impl, "_is_server_running", return_value=True), \
                patch.object(backend_impl, "_server_startup") as startup, \
                patch.object(backend_impl, "_transition_log"):
            result = backend_impl.ensure_execution_role("compression")

        self.assertTrue(result["already_running"])
        self.assertEqual(state["active_role"], "compression")
        self.assertEqual(state["transition_phase"], "COMPRESSING")
        startup.assert_not_called()

    def test_sync_transition_wait_is_notified_when_main_becomes_idle(self) -> None:
        from dashboard import backend_impl

        state = {"active_main_requests": 1}
        observed_active = threading.Event()

        def read_state() -> dict[str, int]:
            if state["active_main_requests"]:
                observed_active.set()
            return dict(state)

        def mutate_state(change) -> None:
            change(state)

        with patch.object(backend_impl, "_state", side_effect=read_state), \
                patch.object(backend_impl, "_mutate_state", side_effect=mutate_state), \
                patch.object(backend_impl, "publish_event"):
            waiter = threading.Thread(target=backend_impl._wait_for_main_idle)
            waiter.start()
            self.assertTrue(observed_active.wait(1))
            self.assertTrue(waiter.is_alive())
            backend_impl.update_execution_queue(0, 0)
            waiter.join(1)

        self.assertFalse(waiter.is_alive())


class _FakeProcess:
    pid = os.getpid()

    def poll(self):
        return None

if __name__ == "__main__":
    unittest.main()
