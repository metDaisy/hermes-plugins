---
name: kwc-triage-authoring-runtime
description: Use when a KWC fresh agent authors staged Triage artifacts. Follow the current assignment exactly.
version: 1.0.0
author: "Amaazon project, Hermes Agent"
license: MIT
platforms: [windows, linux, macos]
metadata:
  hermes:
    tags: [project-management, triage, authoring, runtime]
    related_skills: [kwc-create-triage, kwc-orchestrator-contract]
requires_toolsets: [terminal, hermes-langchain]
---

# Staged Triage authoring runtime

This compact Skill is only for a KWC fresh-agent assignment. The current `kwc-assignment-v1` and its stage-specific `instruction` own the complete field schema, evidence bounds, and acceptance rules. Do not load the larger `kwc-create-triage` workflow Skill during this run and do not rediscover rules from helper source or historical artifacts.

## Invariants

1. Treat the assignment's `workspace_path`, board, Issue, generation, stage, `artifact_file`, `assignment_id`, `allowed_tools`, and `next_action` as immutable.
2. Work only on the current stage: `intake → discovery → source_confirmation → analysis → materialize`.
3. Write exactly the declared artifact schema to the exact repo-relative `artifact_file`. Do not add fields, choose another path, use Profile cache, or copy stale `.temp` content.
4. Use normal read/search tools only for evidence permitted by the current stage instruction. Do not read production/test source during intake; do not turn discovery snippets into confirmed evidence; do not expand beyond accepted discovery candidates during source confirmation.
5. Batch independent MCP searches and independent file reads in one tool turn. Serialize only true prerequisites such as project resolution before graph search or discovery acceptance before source confirmation.
6. Invoke only `next_action`, exactly once per accepted artifact. After a successful call, read `next_assignment` from the result. If its phase is still `triage`, continue immediately with that assignment without waiting for a user message. If the phase changes, stop.
7. KWC owns validation, canonical composition, current-state checks, native mutation, rollback, exact read-back, and phase transition. Never create/edit/archive Kanban cards manually.
8. A validator failure permits one consolidated correction of the exact reported fields. Do not loop, weaken evidence, remove independent requirements, or bypass the validator.

## Stage boundaries

- **intake:** read the named Issue once, its governing requirement/current-state documents, and Git identity. Before writing, make an in-memory **scope-to-requirement coverage checklist**: split every numbered scope and each sub-outcome whose owner or verification seam differs, then map every checklist row to exactly one change-requirement ID. A single Issue bullet may require multiple IDs. In particular, never combine an HTTP binding/input-format outcome with an application command/result boundary outcome; each needs its own requirement and later acceptance. Preserve atomic change requirements and independently testable observables.
- **discovery:** resolve the exact Codebase Memory project by workspace and actual Git HEAD, then batch candidate searches. Candidates are bounded locators, not findings. For a cross-module error-ownership target, candidate closure must include the public producer API plus the exact producer exception declaration/code owner instead of stopping at the throw site.
- **source confirmation:** batch-read accepted candidates and record only directly supported production/test/document claims. For cross-module error ownership, confirm the producer exception 선언과 public visibility, error code owner, consumer mapping, and nearest regression test before declaring the seam `undecided` or opening a Decision. Missing evidence remains `unknown`.
- **analysis:** preserve intake meaning; connect every target, gap, slice, observable, verification, contract, and document impact without inventing product choices. Write every descriptive claim copied into downstream Impl context, especially `confirmed_gaps[].evidence[].claim` and `confirmed_gaps[].rules[].claim`, as concrete Korean prose rather than an English sentence. Triage slice dependencies remain empty.
- **materialize:** perform no new research or rewriting; call the assigned creation tool once.

Completion means the KWC tool returned a non-Triage next assignment after verified native materialization, or a typed blocker was returned without unauthorized mutation.
