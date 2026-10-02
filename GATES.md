# Gates: llama.cpp shared coordinator and cancellable proxy

OWNS: plugins/llamacpp/**, GATES.md

Scope: One machine-scoped coordinator serves every Hermes Desktop instance, forwards local inference through a cancellable stable endpoint, and preserves the requested exclusive main/compression design contract.

- [x] G1: concurrent coordinator startup converges on one versioned owner, stale startup leases recover, legacy coordinators are safely replaced, and the last Desktop lease shuts down owned processes
  CHECK: uv run --with fastapi --with uvicorn --with httpx --with psutil python -m unittest test_plugin_api.LlamaCppManagerTests.test_coordinator_start_lease_prevents_duplicate_spawn test_plugin_api.LlamaCppManagerTests.test_stale_coordinator_start_lease_is_reacquired_in_same_request test_plugin_api.LlamaCppManagerTests.test_coordinator_health_requires_expected_identity_and_build test_plugin_api.LlamaCppManagerTests.test_unversioned_legacy_coordinator_is_replaced_when_script_identity_matches test_plugin_api.LlamaCppManagerTests.test_coordinator_lifetime_lock_has_single_owner test_plugin_api.LlamaCppManagerTests.test_desktop_lifecycle_contract_stops_coordinator_after_last_lease test_plugin_api.LlamaCppManagerTests.test_worker_spawn_is_bound_to_coordinator_lifetime test_application_services.ManagedEndpointConfigTests.test_desktop_leases_expire_and_wait_for_active_execution -v && python -c "print('singleton coordinator verified')"
  EXPECT: singleton coordinator verified
  CWD: plugins/llamacpp
  EVIDENCE: automatic-evidence=v1; definition-sha256=00ab878d80bff40655916bdfc94537772c9cc7264f38eff899aa62cfa4e9ffba; exit=0; EXPECT=matched; output-sha256=e7441b896b727c98beb3b63d00baaabafbdcd4daa4a24e438d6c770d5b5d253b; output-bytes=1488; shell=C:\Windows\system32\cmd.exe; cwd=C:\Users\leee\IdeaProjects\hermes-plugins\plugins\llamacpp; path=5b5574a95663/62 entries

- [x] G2: provider registration uses the stable coordinator endpoint and Hermes-side disconnects close every request stream while cancelled Compression restores Main
  CHECK: uv run --with fastapi --with uvicorn --with httpx --with psutil python -m unittest test_plugin_api.LlamaCppManagerTests.test_custom_endpoint_routes_through_coordinator test_plugin_api.LlamaCppManagerTests.test_cancelled_proxy_stream_closes_upstream test_plugin_api.LlamaCppManagerTests.test_downstream_disconnect_cancels_upstream_without_stopping_worker test_plugin_api.LlamaCppManagerTests.test_profile_proxy_disconnect_propagates_through_coordinator_to_worker test_plugin_api.LlamaCppManagerTests.test_live_compression_disconnect_closes_worker_stream_and_restores_main test_plugin_api.LlamaCppManagerTests.test_stream_finalizer_runs_even_when_upstream_close_fails -v && python -c "print('cancellable inference proxy verified')"
  EXPECT: cancellable inference proxy verified
  CWD: plugins/llamacpp
  EVIDENCE: automatic-evidence=v1; definition-sha256=6bd835121c9caaacb777f0f40bdf07d8b2dec7312c46ea6ca4b38a07374cd322; exit=0; EXPECT=matched; output-sha256=8215a0096a6c6953082218e1a4e4a807ad029717593b8cd99af1df9189809722; output-bytes=1146; shell=C:\Windows\system32\cmd.exe; cwd=C:\Users\leee\IdeaProjects\hermes-plugins\plugins\llamacpp; path=5b5574a95663/62 entries

- [x] G7: compression exclusively swaps the worker, waits for termination, restores Main after completion/failure/cancellation, and keeps queue accounting exact
  CHECK: uv run --with fastapi --with uvicorn --with httpx --with psutil python -m unittest test_plugin_api.LlamaCppManagerTests.test_compression_execution_restores_main_after_stream_finishes test_plugin_api.LlamaCppManagerTests.test_cancelled_compression_finalizer_restores_main_before_releasing_transition test_plugin_api.LlamaCppManagerTests.test_compression_start_failure_attempts_main_restoration test_plugin_api.LlamaCppManagerTests.test_main_request_waits_until_compression_restores_main test_plugin_api.LlamaCppManagerTests.test_cancelled_queued_main_request_does_not_leak_queue_count test_plugin_api.LlamaCppManagerTests.test_server_termination_refuses_reused_worker_pid_identity test_application_services.ServerLifecycleServiceTests.test_stop_does_not_clear_worker_state_until_termination_returns -v && python -c "print('exclusive swap lifecycle verified')"
  EXPECT: exclusive swap lifecycle verified
  CWD: plugins/llamacpp
  EVIDENCE: automatic-evidence=v1; definition-sha256=587a20a3b2f8c3d04b3e1e2e42b4f508d6fc8906daf15995c6f9b744f4c072e6; exit=0; EXPECT=matched; output-sha256=aea17d1752b22efbecfc811bd7bfb437abb9090edba2919ac52b82165d29fb8e; output-bytes=1363; shell=C:\Windows\system32\cmd.exe; cwd=C:\Users\leee\IdeaProjects\hermes-plugins\plugins\llamacpp; path=5b5574a95663/62 entries

- [x] G3: llama.cpp plugin regression suite passes
  CHECK: uv run --with fastapi --with uvicorn --with httpx --with psutil python -m unittest discover -p test_*.py -v && python -c "print('llamacpp regression suite verified')"
  EXPECT: llamacpp regression suite verified
  CWD: plugins/llamacpp
  EVIDENCE: automatic-evidence=v1; definition-sha256=6f1a4ca11b67ed57c4fa549ba9a435834c67a35906a976a979319de8eeef0a7b; exit=0; EXPECT=matched; output-sha256=543f79dfe686b97324633b2edb0395d46945ad0a825751cdce76199d2009e447; output-bytes=16990; shell=C:\Windows\system32\cmd.exe; cwd=C:\Users\leee\IdeaProjects\hermes-plugins\plugins\llamacpp; path=5b5574a95663/62 entries

- [x] G4: changed Python and Desktop modules pass syntax and ESM parsing
  CHECK: uv run --with fastapi --with uvicorn --with httpx --with psutil python -m py_compile plugins/llamacpp/dashboard/plugin_api.py plugins/llamacpp/dashboard/coordinator_server.py plugins/llamacpp/dashboard/inference_proxy.py plugins/llamacpp/dashboard/backend_impl.py plugins/llamacpp/dashboard/child_lifecycle.py plugins/llamacpp/dashboard/application/coordinator_lock.py plugins/llamacpp/dashboard/application/desktop_leases.py plugins/llamacpp/dashboard/application/execution_profiles.py plugins/llamacpp/dashboard/application/profile_endpoint.py plugins/llamacpp/dashboard/application/server_lifecycle.py plugins/llamacpp/dashboard/application/server_startup.py plugins/llamacpp/dashboard/application/state_store.py plugins/llamacpp/dashboard/routes/execution_profile_routes.py plugins/llamacpp/verify_materialization.py plugins/llamacpp/verify_live_coordinator.py && cp plugins/llamacpp/desktop/plugin.js plugins/llamacpp/.plugin-check.mjs && node --check plugins/llamacpp/.plugin-check.mjs && rm plugins/llamacpp/.plugin-check.mjs && python -c "print('Syntax check passed')"
  EXPECT: Syntax check passed
  EVIDENCE: automatic-evidence=v1; definition-sha256=ecbc5c72de1b0c0a33483f166320db6091bfe62c731fdaad52af8a02218dcf7a; exit=0; EXPECT=matched; output-sha256=f5a28bc2637565574b30695e58453a6742f0fc432b322e11ea0366ec72ef1bc9; output-bytes=21; shell=C:\Windows\system32\cmd.exe; cwd=C:\Users\leee\IdeaProjects\hermes-plugins; path=5b5574a95663/62 entries

- [x] G5: source, profile materialization, and app Desktop copy report one synchronized bumped plugin version
  CHECK: python plugins/llamacpp/verify_materialization.py
  EXPECT: llamacpp materialization verified
  EVIDENCE: automatic-evidence=v1; definition-sha256=3e7f0d32dd561a4da248ed675979ba2b21e16b71411bfafff9d04e9e2431ac65; exit=0; EXPECT=matched; output-sha256=810d321a62303c43a4b33ddcc5a70bdbace4ff20a5db597b8caa8df48a8e11c9; output-bytes=63; shell=C:\Windows\system32\cmd.exe; cwd=C:\Users\leee\IdeaProjects\hermes-plugins; path=5b5574a95663/62 entries

- [x] G6: active versioned coordinator health and two profile API views converge on exactly one loopback coordinator listener without loading a model
  CHECK: uv run --with fastapi --with uvicorn --with httpx --with psutil python plugins/llamacpp/verify_live_coordinator.py
  EXPECT: shared coordinator live verification passed
  EVIDENCE: automatic-evidence=v1; definition-sha256=69e09cd16fef454afdf1268a39a2c495937413e99109e003cd124048a2d3a019; exit=0; EXPECT=matched; output-sha256=1be75b6a69fcc7e72590692236b5a1769711b28fe97d03c34841fd5d202b5fe5; output-bytes=76; shell=C:\Windows\system32\cmd.exe; cwd=C:\Users\leee\IdeaProjects\hermes-plugins; path=5b5574a95663/62 entries
