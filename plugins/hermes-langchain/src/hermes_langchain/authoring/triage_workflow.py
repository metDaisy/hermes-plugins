"""Sequential, resumable Triage authoring stages owned by KWC."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from ..paths import PLUGIN_ROOT
from . import artifact_paths, triage_domain


_STAGE_SCHEMAS = {
    "intake": "kwc-triage-intake-authoring-v1",
    "discovery": "kwc-triage-discovery-v2",
    "source_confirmation": "kwc-triage-source-confirmation-v2",
    "analysis": "kwc-triage-analysis-authoring-v1",
}
_CANDIDATE_KINDS = {"production", "test", "document"}
_SOURCE_DISPOSITIONS = {"implemented", "partial", "absent", "unknown"}
_PUBLIC_CONTRACT_STATUSES = {"published", "approved-unimplemented", "undecided"}
_GIT_HEAD = re.compile(r"^[0-9a-fA-F]{40,64}$")


def _load_sibling(unique_name: str, filename: str) -> Any:
    del unique_name
    if filename != "triage_domain.py":
        raise ImportError(f"unknown Triage dependency: {filename}")
    return triage_domain


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"artifact is not readable JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError("artifact root must be an object")
    return value


def _workspace_path(assignment: dict[str, Any]) -> Path:
    workspace = Path(str(assignment.get("workspace_path") or "")).resolve()
    if not workspace.is_dir():
        raise ValueError("assignment workspace is unavailable")
    return workspace


def _resolve_artifact(workspace: Path, artifact_file: Path | str) -> Path:
    path = Path(artifact_file)
    path = (workspace / path).resolve() if not path.is_absolute() else path.resolve()
    try:
        path.relative_to(workspace)
    except ValueError as exc:
        raise ValueError("artifact_file must stay inside assignment workspace") from exc
    if not path.is_file():
        raise ValueError("artifact_file does not exist")
    return path


def _write_json(path: Path, payload: dict[str, Any]) -> dict[str, str]:
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    data = text.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {
        "path": path.as_posix(),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _relative_metadata(workspace: Path, metadata: dict[str, str]) -> dict[str, str]:
    return {
        "path": Path(metadata["path"]).resolve().relative_to(workspace).as_posix(),
        "sha256": metadata["sha256"],
    }


def _output_dir(
    workspace: Path,
    *,
    board: Any,
    issue_number: int,
    generation: int,
) -> Path:
    return artifact_paths.triage_output_dir(
        workspace,
        board=board,
        issue_number=issue_number,
        generation=generation,
    )


def _default_inspection(workspace: Path) -> dict[str, Any]:
    script = (
        PLUGIN_ROOT
        / "skills"
        / "kwc-update-current-state"
        / "scripts"
        / "current_state.py"
    )
    if not script.is_file():
        raise ValueError(f"current-state inspector is unavailable: {script}")
    spec = importlib.util.spec_from_file_location("_kwc_current_state_inspector", script)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load current-state inspector from {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.inspect(workspace, workspace / "docs/current-state.md")


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _git_head(workspace: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    head = completed.stdout.strip()
    if completed.returncode != 0 or _GIT_HEAD.fullmatch(head) is None:
        raise ValueError("CODEBASE_MEMORY_GIT_HEAD_UNAVAILABLE")
    return head.lower()


def resolve_codebase_memory_project(
    *,
    workspace: Path,
    projects: Any,
    git_head_fn: Callable[[Path], str] = _git_head,
) -> dict[str, Any]:
    workspace = Path(workspace).resolve()
    if not workspace.is_dir():
        return {
            "status": "unavailable",
            "project": None,
            "reason": "workspace-unavailable",
            "git_head": None,
        }
    head = _required_text(git_head_fn(workspace), "git_head").lower()
    if _GIT_HEAD.fullmatch(head) is None:
        raise ValueError("CODEBASE_MEMORY_GIT_HEAD_INVALID")
    if not isinstance(projects, list):
        raise ValueError("CODEBASE_MEMORY_PROJECTS_INVALID")

    matches: list[str] = []
    workspace_candidates = 0
    for raw in projects:
        project = _exact_fields(
            raw,
            {"project", "root_path", "root_exists", "git_head"},
            "CODEBASE_MEMORY_PROJECT",
        )
        name = _required_text(project["project"], "codebase_memory.project")
        root_exists = project["root_exists"]
        if not isinstance(root_exists, bool):
            raise ValueError("CODEBASE_MEMORY_ROOT_EXISTS_INVALID")
        root = Path(_required_text(project["root_path"], "codebase_memory.root_path")).resolve()
        actual_exists = root.is_dir()
        if root_exists != actual_exists:
            raise ValueError("CODEBASE_MEMORY_ROOT_EXISTENCE_MISMATCH")
        if not actual_exists:
            continue
        try:
            same_workspace = root.samefile(workspace)
        except OSError:
            same_workspace = False
        if not same_workspace:
            continue
        workspace_candidates += 1
        candidate_head = str(project["git_head"] or "").strip().lower()
        if _GIT_HEAD.fullmatch(candidate_head) and candidate_head == head:
            matches.append(name)

    if len(matches) == 1:
        return {
            "status": "available",
            "project": matches[0],
            "reason": "workspace-and-head-match",
            "git_head": head,
        }
    reason = "ambiguous-workspace-match" if len(matches) > 1 else (
        "git-head-mismatch" if workspace_candidates else "no-workspace-match"
    )
    return {
        "status": "unavailable",
        "project": None,
        "reason": reason,
        "git_head": head,
    }


def _exact_fields(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label}_FIELDS")
    return value


def _target_ids(intake: dict[str, Any]) -> list[str]:
    return [str(item["id"]) for item in intake.get("investigation_targets", [])]


def _candidate_base(locator: str) -> str:
    return locator.split("#", 1)[0].replace("\\", "/")


def _module_from_path(value: Any) -> str | None:
    path = _candidate_base(str(value))
    marker = "/java/"
    if marker not in path:
        return None
    package_parts = path.split(marker, 1)[1].split("/")
    layer_names = {"application", "domain", "presentation", "infra", "infrastructure"}
    layer_index = next(
        (index for index, part in enumerate(package_parts) if part in layer_names),
        None,
    )
    if layer_index is None or layer_index == 0:
        return None
    return package_parts[layer_index - 1] or None


def _producer_evidence_locators(target: dict[str, Any], producer: Any) -> set[str]:
    producer_name = str(producer or "").casefold()
    return {
        _normalized_locator(item["locator"])
        for item in target.get("evidence", [])
        if isinstance(item, dict)
        and item.get("kind") == "production"
        and isinstance(item.get("locator"), str)
        and "#" in item["locator"]
        and str(_module_from_path(item["locator"]) or "").casefold() == producer_name
    }


def _scope_mutation_base(scope_entry: Any) -> str | None:
    if not isinstance(scope_entry, str):
        return None
    match = re.match(
        r"\s*(?:create|modify|move-source|move-target|delete)\s*:\s*(src/main/java/\S+)",
        scope_entry,
        re.IGNORECASE,
    )
    if match is None:
        return None
    return _candidate_base(match.group(1).rstrip(".,;"))


def _canonicalize_discovery_authoring(value: Any, workspace: Path) -> Any:
    discovery = copy.deepcopy(value)
    if not isinstance(discovery, dict):
        return discovery

    fallbacks = discovery.get("fallbacks")
    if isinstance(fallbacks, dict):
        normalized_fallbacks: list[Any] = []
        for kind, details in fallbacks.items():
            if not isinstance(details, dict) or details.get("used") is not True:
                continue
            normalized_fallbacks.append({
                "kind": str(kind),
                "reason": details.get("reason"),
                "target_ids": details.get("target_ids"),
            })
        discovery["fallbacks"] = normalized_fallbacks

    codebase_memory = discovery.get("codebase_memory")
    if not isinstance(codebase_memory, dict):
        return discovery
    resolution = codebase_memory.get("resolution")
    resolution_head = resolution.get("git_head") if isinstance(resolution, dict) else None
    projects = codebase_memory.get("projects")
    if not isinstance(projects, list):
        return discovery
    normalized_projects: list[Any] = []
    for project in projects:
        if not isinstance(project, dict):
            normalized_projects.append(project)
            continue
        project_name = project.get("project") or project.get("name")
        root_path = project.get("root_path")
        if not isinstance(project_name, str) or not isinstance(root_path, str):
            normalized_projects.append(project)
            continue
        root_exists = project.get("root_exists")
        if not isinstance(root_exists, bool):
            root_exists = Path(root_path).resolve(strict=False).exists()
        git_head = project.get("git_head") or resolution_head
        normalized_projects.append({
            "project": project_name,
            "root_path": root_path,
            "root_exists": root_exists,
            "git_head": git_head,
        })
    codebase_memory["projects"] = normalized_projects
    return discovery


def _normalized_locator(value: Any) -> str:
    return _required_text(value, "locator").replace("\\", "/")


def _document_path(value: Any) -> str:
    return _normalized_locator(value).split("#", 1)[0]


def _validate_document_impact_coverage(
    analysis_body: dict[str, Any], confirmation: dict[str, Any]
) -> None:
    baseline = analysis_body.get("baseline", {})
    required = {
        _document_path(locator)
        for field in ("requirement_locators", "current_state_locators")
        for locator in baseline.get(field, [])
    }
    required.update(
        _document_path(item["locator"])
        for target in confirmation.get("targets", [])
        for item in target.get("evidence", [])
        if item.get("kind") == "document"
    )

    impacts = analysis_body.get("document_impacts", [])
    actual = {_document_path(item["document"]) for item in impacts}
    missing = sorted(required - actual)
    if missing:
        raise ValueError("DOCUMENT_IMPACT_COVERAGE:" + ",".join(missing))

    for impact in impacts:
        document = _document_path(impact["document"])
        if impact.get("disposition") == "not-applicable":
            continue
        evidence_documents = {
            _document_path(item["locator"])
            for item in impact.get("evidence", [])
            if _document_path(item["locator"]).startswith("docs/")
        }
        if document not in evidence_documents:
            raise ValueError("DOCUMENT_IMPACT_EVIDENCE_MISMATCH:" + document)


def _canonicalize_analysis_authoring(value: Any) -> Any:
    analysis = copy.deepcopy(value)

    def strip_source_kinds(item: Any) -> None:
        if isinstance(item, dict):
            if set(item) == {"locator", "claim", "kind"}:
                item.pop("kind")
            for child in item.values():
                strip_source_kinds(child)
        elif isinstance(item, list):
            for child in item:
                strip_source_kinds(child)

    strip_source_kinds(analysis)
    if not isinstance(analysis, dict):
        return analysis

    amendments = analysis.get("intake_amendments")
    if isinstance(amendments, dict) and isinstance(amendments.get("intent_entries"), list):
        aliases = {
            "intent.change_requirements": "change_requirements",
            "intent.preserved_observables": "preserved_observables",
            "intent.quality_gates": "quality_gates",
        }
        value_fields = {
            "change_requirements": "outcome",
            "preserved_observables": "behavior",
            "quality_gates": "outcome",
        }
        for amendment in amendments["intent_entries"]:
            if not isinstance(amendment, dict):
                continue
            if amendment.get("section") in aliases:
                amendment["section"] = aliases[amendment["section"]]
            section = amendment.get("section")
            changes = amendment.get("changes")
            field = value_fields.get(section)
            if field is not None and isinstance(changes, str) and changes.strip():
                amendment["changes"] = {field: changes}
            elif (
                field is not None
                and isinstance(changes, list)
                and len(changes) == 1
                and isinstance(changes[0], str)
                and changes[0].strip()
            ):
                amendment["changes"] = {field: changes[0]}

    rows = analysis.get("verification_matrix")
    if not isinstance(rows, list):
        return analysis
    canonical_rows: list[Any] = []
    by_observable: dict[str, dict[str, Any]] = {}
    verification_aliases: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("observable_id"), str):
            canonical_rows.append(row)
            continue
        observable_id = row["observable_id"]
        retained = by_observable.get(observable_id)
        if retained is None:
            by_observable[observable_id] = row
            canonical_rows.append(row)
            continue
        duplicate_id = row.get("id")
        retained_id = retained.get("id")
        if isinstance(duplicate_id, str) and isinstance(retained_id, str):
            verification_aliases[duplicate_id] = retained_id
        retained_required = retained.get("required_verification")
        duplicate_required = row.get("required_verification")
        if isinstance(retained_required, list) and isinstance(duplicate_required, list):
            retained_required.extend(
                item for item in duplicate_required if item not in retained_required
            )
        retained_evidence = retained.get("evidence")
        duplicate_evidence = row.get("evidence")
        if isinstance(retained_evidence, list) and isinstance(duplicate_evidence, list):
            retained_evidence.extend(item for item in duplicate_evidence if item not in retained_evidence)
    analysis["verification_matrix"] = canonical_rows

    def remap_verification_ids(item: Any) -> None:
        if isinstance(item, dict):
            ids = item.get("verification_ids")
            if isinstance(ids, list):
                remapped: list[Any] = []
                for identifier in ids:
                    normalized = verification_aliases.get(identifier, identifier)
                    if normalized not in remapped:
                        remapped.append(normalized)
                item["verification_ids"] = remapped
            for child in item.values():
                remap_verification_ids(child)
        elif isinstance(item, list):
            for child in item:
                remap_verification_ids(child)

    remap_verification_ids(analysis)
    return analysis


def _confirmed_locators(output_dir: Path) -> set[str]:
    confirmation = _load_json(output_dir / "triage-source-confirmation.json")
    locators = {
        _normalized_locator(item["locator"])
        for target in confirmation.get("targets", [])
        if isinstance(target, dict)
        for item in target.get("evidence", [])
        if isinstance(item, dict)
    }
    locators.update(
        _normalized_locator(item["locator"])
        for target in confirmation.get("targets", [])
        if isinstance(target, dict) and isinstance(target.get("public_contract"), dict)
        for item in target["public_contract"].get("approval_evidence", [])
        if isinstance(item, dict)
    )
    return locators


def _validate_production_seam_confirmation(
    confirmation: dict[str, Any], analysis: dict[str, Any]
) -> None:
    confirmed = {
        _normalized_locator(item["locator"])
        for target in confirmation.get("targets", [])
        if isinstance(target, dict)
        for item in target.get("evidence", [])
        if isinstance(item, dict) and isinstance(item.get("locator"), str)
    }
    confirmed_bases = {_candidate_base(locator) for locator in confirmed}
    required_states = "\n".join(
        str(gap.get("required_state") or "")
        for gap in analysis.get("confirmed_gaps", [])
        if isinstance(gap, dict)
    )
    invalid: list[str] = []
    for row in analysis.get("verification_matrix", []):
        if not isinstance(row, dict) or not isinstance(row.get("production_seam"), dict):
            continue
        locator = _normalized_locator(row["production_seam"].get("locator"))
        if locator in confirmed:
            continue
        base, separator, member = locator.partition("#")
        class_name = Path(base).stem
        planned_symbol = f"{class_name}#{member}" if separator and member else ""
        if base in confirmed_bases and planned_symbol and planned_symbol in required_states:
            continue
        invalid.append(locator)
    if invalid:
        raise ValueError("UNCONFIRMED_PRODUCTION_SEAM:" + ",".join(sorted(invalid)))


def _source_locators(value: Any) -> set[str]:
    locators: set[str] = set()
    if isinstance(value, dict):
        if set(value) == {"locator", "claim"} and isinstance(value.get("locator"), str):
            locators.add(_normalized_locator(value["locator"]))
        for item in value.values():
            locators.update(_source_locators(item))
    elif isinstance(value, list):
        for item in value:
            locators.update(_source_locators(item))
    return locators


def _validate_public_contract_analysis(
    confirmation: dict[str, Any], analysis: dict[str, Any]
) -> None:
    dispositions = {
        item.get("target_id"): item
        for item in analysis.get("target_dispositions", [])
        if isinstance(item, dict)
    }
    contracts = [
        item for item in analysis.get("contract_changes", []) if isinstance(item, dict)
    ]
    decisions = [
        item for item in analysis.get("open_decisions", []) if isinstance(item, dict)
    ]
    for target in confirmation.get("targets", []):
        if not isinstance(target, dict) or not isinstance(target.get("public_contract"), dict):
            continue
        target_id = str(target.get("target_id"))
        public_contract = target["public_contract"]
        status = public_contract.get("status")
        if status == "approved-unimplemented":
            target_evidence = {
                _normalized_locator(item["locator"])
                for item in target.get("evidence", [])
                if isinstance(item, dict)
            }
            approval_evidence = {
                _normalized_locator(item["locator"])
                for item in public_contract.get("approval_evidence", [])
                if isinstance(item, dict)
            }
            matching = []
            for contract in contracts:
                evidence = {
                    _normalized_locator(item["locator"])
                    for item in contract.get("evidence", [])
                    if isinstance(item, dict)
                }
                if (
                    contract.get("consumer") == public_contract.get("consumer")
                    and contract.get("producer") == public_contract.get("producer")
                    and evidence & target_evidence
                    and evidence & approval_evidence
                ):
                    matching.append(contract)
            disposition = dispositions.get(target_id)
            if (
                not matching
                or not isinstance(disposition, dict)
                or disposition.get("disposition") != "gap"
                or not disposition.get("finding_ids")
            ):
                raise ValueError(f"MISSING_APPROVED_CONTRACT_CHANGE:{target_id}")
        elif status == "published":
            producer_locators = _producer_evidence_locators(
                target, public_contract.get("producer")
            )
            producer_bases = {_candidate_base(locator) for locator in producer_locators}
            for slice_body in analysis.get("implementation_slices", []):
                if not isinstance(slice_body, dict):
                    continue
                for scope_entry in slice_body.get("scope", []):
                    mutation_base = _scope_mutation_base(scope_entry)
                    if mutation_base in producer_bases:
                        raise ValueError(
                            "PUBLISHED_CONTRACT_PRODUCER_MUTATION:"
                            f"{target_id}:{mutation_base}"
                        )
        elif status == "undecided" and not decisions:
            raise ValueError(f"MISSING_PUBLIC_CONTRACT_DECISION:{target_id}")


def _analysis_evidence_locators(analysis: dict[str, Any]) -> set[str]:
    locators: set[str] = set()
    for gap in analysis.get("confirmed_gaps", []):
        if isinstance(gap, dict):
            for item in gap.get("evidence", []):
                if isinstance(item, dict):
                    locators.add(_normalized_locator(item.get("locator")))
    for verification in analysis.get("verification_matrix", []):
        if not isinstance(verification, dict):
            continue
        for item in verification.get("evidence", []):
            if isinstance(item, dict):
                locators.add(_normalized_locator(item.get("locator")))
    for contract in analysis.get("contract_changes", []):
        if isinstance(contract, dict):
            for item in contract.get("evidence", []):
                if isinstance(item, dict):
                    locators.add(_normalized_locator(item.get("locator")))
    return locators


def _verified_stage_artifact(
    workspace: Path, stage: str, metadata: Any
) -> Path:
    if not isinstance(metadata, dict) or set(metadata) != {"path", "sha256"}:
        raise ValueError(f"TRIAGE_ARTIFACT_METADATA_INVALID:{stage}")
    path = _resolve_artifact(workspace, metadata.get("path"))
    expected = _required_text(metadata.get("sha256"), f"{stage}.sha256")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError(f"TRIAGE_ARTIFACT_DIGEST_MISMATCH:{stage}")
    return path


def _validate_repo_path(workspace: Path, value: Any, field: str) -> str:
    text = _required_text(value, field).replace("\\", "/")
    path = (workspace / _candidate_base(text)).resolve()
    try:
        path.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(f"{field} escapes workspace") from exc
    if not path.is_file():
        raise ValueError(f"{field} does not exist: {text}")
    return text


def _canonical_intake(output_dir: Path) -> dict[str, Any]:
    return _load_json(output_dir / "triage-intake.json")


def _identity(output_dir: Path) -> dict[str, Any]:
    return _load_json(output_dir / "triage-identity.json")


def _advance_intake(
    assignment: dict[str, Any],
    payload: dict[str, Any],
    workspace: Path,
    output_dir: Path,
    inspection_fn: Callable[..., dict[str, Any]] | None,
) -> dict[str, str]:
    _exact_fields(payload, {"schema", "identity", "intake"}, "INTAKE")
    identity = _exact_fields(
        payload["identity"],
        {
            "issue_number",
            "title",
            "issue_url",
            "planning_sha",
            "current_state_sha",
            "requirement_locators",
            "current_state_locators",
        },
        "IDENTITY",
    )
    if int(identity["issue_number"]) != int(assignment["issue_number"]):
        raise ValueError("ISSUE_IDENTITY_MISMATCH")
    inspector = inspection_fn or _default_inspection
    inspection = inspector(workspace)
    if not isinstance(inspection, dict):
        raise ValueError("CURRENT_STATE_INSPECTION_INVALID")
    if inspection.get("inspection_sha") != identity.get("planning_sha"):
        raise ValueError("PLANNING_SHA_MISMATCH")
    if inspection.get("snapshot_sha") != identity.get("current_state_sha"):
        raise ValueError("CURRENT_STATE_SHA_MISMATCH")

    domain = _load_sibling("_kwc_triage_workflow_domain", "triage_domain.py")
    module = domain.build_triage_module()
    triage_identity = domain.TriageIdentity(**identity)
    intake = module.compose_intake.execute(triage_identity, payload["intake"])
    admission = domain.current_state_admission(inspection)
    if admission is not None:
        intake["baseline"]["current_state"]["freshness"] = admission
    errors = module.validator.validate_repository_locators(intake, workspace)
    errors.extend(module.validator.validate_current_state_inspection(intake, inspection))
    if errors:
        raise ValueError("INVALID_TRIAGE_INTAKE:" + ",".join(errors))

    _write_json(output_dir / "triage-identity.json", identity)
    _write_json(output_dir / "current-state-inspection.json", inspection)
    return _relative_metadata(workspace, _write_json(output_dir / "triage-intake.json", intake))


def _advance_discovery(
    payload: dict[str, Any],
    workspace: Path,
    issue_number: int,
    output_dir: Path,
    git_head_fn: Callable[[Path], str],
) -> dict[str, str]:
    payload = _canonicalize_discovery_authoring(payload, workspace)
    _exact_fields(
        payload,
        {"schema", "targets", "tool_usage", "fallbacks", "codebase_memory"},
        "DISCOVERY",
    )
    intake = _canonical_intake(output_dir)
    expected_ids = _target_ids(intake)
    targets = payload["targets"]
    if not isinstance(targets, list) or [item.get("target_id") for item in targets if isinstance(item, dict)] != expected_ids:
        raise ValueError("TARGET_COVERAGE")
    usage = _exact_fields(payload["tool_usage"], {"codebase_memory", "semble"}, "TOOL_USAGE")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in usage.values()):
        raise ValueError("TOOL_USAGE_INVALID")
    fallbacks = payload["fallbacks"]
    if not isinstance(fallbacks, list):
        raise ValueError("FALLBACKS_INVALID")
    for fallback in fallbacks:
        _exact_fields(fallback, {"kind", "reason", "target_ids"}, "DISCOVERY_FALLBACK")
        _required_text(fallback["kind"], "fallback.kind")
        _required_text(fallback["reason"], "fallback.reason")
        target_ids = fallback["target_ids"]
        if (
            not isinstance(target_ids, list)
            or not target_ids
            or any(target_id not in expected_ids for target_id in target_ids)
        ):
            raise ValueError("DISCOVERY_FALLBACK_TARGETS")
    codebase_memory = _exact_fields(
        payload["codebase_memory"], {"projects", "resolution"}, "CODEBASE_MEMORY"
    )
    expected_resolution = resolve_codebase_memory_project(
        workspace=workspace,
        projects=codebase_memory["projects"],
        git_head_fn=git_head_fn,
    )
    if codebase_memory["resolution"] != expected_resolution:
        raise ValueError("CODEBASE_MEMORY_RESOLUTION_MISMATCH")
    intake_targets = {
        str(item.get("id")): item
        for item in intake.get("investigation_targets", [])
        if isinstance(item, dict)
    }
    for target in targets:
        _exact_fields(target, {"target_id", "candidates"}, "DISCOVERY_TARGET")
        candidates = target["candidates"]
        if not isinstance(candidates, list) or not candidates:
            raise ValueError("DISCOVERY_CANDIDATES_REQUIRED")
        target_id = str(target["target_id"])
        path_counts: dict[str, int] = {}
        bounded_candidates: list[dict[str, Any]] = []
        for candidate in candidates:
            _exact_fields(candidate, {"path", "symbol", "kind", "reason"}, "DISCOVERY_CANDIDATE")
            _validate_repo_path(workspace, candidate["path"], "candidate.path")
            path = _candidate_base(candidate["path"])
            path_counts[path] = path_counts.get(path, 0) + 1
            if candidate["kind"] not in _CANDIDATE_KINDS:
                raise ValueError("DISCOVERY_CANDIDATE_KIND")
            _required_text(candidate["reason"], "candidate.reason")
            if path_counts[path] <= 2:
                bounded_candidates.append(candidate)
        if len(bounded_candidates) > 8:
            raise ValueError(f"DISCOVERY_CANDIDATE_LIMIT:{target_id}")
        intake_target = intake_targets.get(target_id, {})
        question = str(intake_target.get("question") or "").lower()
        if any(
            marker in question
            for marker in ("producer", "cross-module", "상대 module", "다른 module")
        ):
            consumer_module = _module_from_path(intake_target.get("locator"))
            has_producer = any(
                candidate.get("kind") == "production"
                and _module_from_path(candidate.get("path")) not in {None, consumer_module}
                for candidate in bounded_candidates
            )
            if consumer_module is not None and not has_producer:
                raise ValueError(f"DISCOVERY_PRODUCER_CANDIDATE_REQUIRED:{target_id}")
        if any(
            marker in question
            for marker in (
                "repository",
                "persistence",
                "transaction",
                "pessimistic",
                "lock",
                "영속",
                "트랜잭션",
                "잠금",
                "동시성",
            )
        ):
            has_repository = any(
                candidate.get("kind") == "production"
                and "repository" in str(candidate.get("path") or "").lower()
                for candidate in bounded_candidates
            )
            if not has_repository:
                raise ValueError(f"DISCOVERY_PERSISTENCE_REPOSITORY_REQUIRED:{target_id}")
            has_repository_test = any(
                candidate.get("kind") == "test"
                and "repository" in str(candidate.get("path") or "").lower()
                for candidate in bounded_candidates
            )
            if not has_repository_test:
                raise ValueError(f"DISCOVERY_PERSISTENCE_TEST_REQUIRED:{target_id}")
        target["candidates"] = bounded_candidates
    questions = " ".join(
        str(item.get("question") or "").lower() for item in intake_targets.values()
    )
    candidate_paths = {
        _candidate_base(candidate["path"])
        for target in targets
        for candidate in target["candidates"]
    }
    glossary = "docs/domain-glossary.md"
    if (
        (workspace / glossary).is_file()
        and any(marker in questions for marker in ("error ownership", "error contract", "code/message", "예외 소유"))
        and glossary not in candidate_paths
    ):
        raise ValueError(f"DISCOVERY_GOVERNING_DOCUMENT_REQUIRED:{glossary}")
    backend_targets = any(
        str(item.get("locator") or "").replace("\\", "/").startswith("src/main/java/")
        for item in intake_targets.values()
    )
    required_backend_documents = []
    if backend_targets:
        required_backend_documents.append("docs/backend-development-guide.md")
    quality_gates = intake.get("intent", {}).get("quality_gates", [])
    if backend_targets and isinstance(quality_gates, list) and quality_gates:
        required_backend_documents.append("docs/backend-test-guide.md")
    for document in required_backend_documents:
        if (workspace / document).is_file() and document not in candidate_paths:
            raise ValueError(f"DISCOVERY_GOVERNING_DOCUMENT_REQUIRED:{document}")
    return _relative_metadata(workspace, _write_json(output_dir / "triage-discovery.json", payload))


def _advance_source_confirmation(
    payload: dict[str, Any], workspace: Path, issue_number: int, output_dir: Path
) -> dict[str, str]:
    _exact_fields(payload, {"schema", "targets", "open_questions"}, "SOURCE_CONFIRMATION")
    intake = _canonical_intake(output_dir)
    discovery = _load_json(output_dir / "triage-discovery.json")
    expected_ids = _target_ids(intake)
    targets = payload["targets"]
    if not isinstance(targets, list) or [item.get("target_id") for item in targets if isinstance(item, dict)] != expected_ids:
        raise ValueError("TARGET_COVERAGE")
    candidates = {
        _candidate_base(item["path"])
        for target in discovery["targets"]
        for item in target["candidates"]
    }
    intake_targets = {
        str(item.get("id")): item
        for item in intake.get("investigation_targets", [])
        if isinstance(item, dict)
    }
    approved_source_locators = _source_locators(intake.get("intent"))
    for target in targets:
        _exact_fields(
            target,
            {"target_id", "disposition", "rationale", "evidence", "public_contract"},
            "SOURCE_TARGET",
        )
        if target["disposition"] not in _SOURCE_DISPOSITIONS:
            raise ValueError("SOURCE_DISPOSITION")
        _required_text(target["rationale"], "target.rationale")
        evidence = target["evidence"]
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("SOURCE_EVIDENCE_REQUIRED")
        for item in evidence:
            _exact_fields(item, {"locator", "claim", "kind"}, "SOURCE_EVIDENCE")
            locator = _validate_repo_path(workspace, item["locator"], "evidence.locator")
            target_id = str(target["target_id"])
            if _candidate_base(locator) not in candidates:
                raise ValueError(f"UNDISCOVERED_EVIDENCE:{target_id}:{locator}")
            if item["kind"] not in _CANDIDATE_KINDS:
                raise ValueError("SOURCE_EVIDENCE_KIND")
            _required_text(item["claim"], "evidence.claim")
        target_id = str(target["target_id"])
        question = str(intake_targets.get(target_id, {}).get("question") or "").lower()
        if all(marker in question for marker in ("request", "response", "service", "signature")):
            def confirms_role(item: dict[str, Any], markers: tuple[str, ...]) -> bool:
                locator = str(item.get("locator") or "").replace("\\", "/")
                text = f"{locator} {item.get('claim', '')}".casefold()
                return (
                    re.search(r"src/main/java/[^\s]+\.java(?:#[A-Za-z_$][\w$]*)?$", locator)
                    is not None
                    and any(marker in text for marker in markers)
                )

            if not any(confirms_role(item, ("request", "요청")) for item in evidence):
                raise ValueError(f"SOURCE_HTTP_DTO_REQUEST_EVIDENCE_REQUIRED:{target_id}")
            if not any(
                confirms_role(item, ("response", "result", "응답", "결과"))
                for item in evidence
            ):
                raise ValueError(f"SOURCE_HTTP_DTO_RESPONSE_EVIDENCE_REQUIRED:{target_id}")
            if not any(
                (
                    (match := re.search(
                        r"src/main/java/[^\s/]+(?:/[^\s/]+)*/([A-Za-z_$][\w$]*)\.java#([A-Za-z_$][\w$]*)$",
                        str(item.get("locator") or "").replace("\\", "/"),
                    ))
                    is not None
                    and match.group(1) != match.group(2)
                )
                for item in evidence
            ):
                raise ValueError(
                    f"SOURCE_HTTP_DTO_OPERATION_SIGNATURE_EVIDENCE_REQUIRED:{target_id}"
                )
        public_contract = target["public_contract"]
        if public_contract is not None:
            _exact_fields(
                public_contract,
                {"consumer", "producer", "status", "approval_evidence"},
                "PUBLIC_CONTRACT",
            )
            _required_text(public_contract["consumer"], "public_contract.consumer")
            _required_text(public_contract["producer"], "public_contract.producer")
            status = public_contract["status"]
            if status not in _PUBLIC_CONTRACT_STATUSES:
                raise ValueError("PUBLIC_CONTRACT_STATUS")
            if status == "approved-unimplemented" and target["disposition"] not in {"partial", "absent"}:
                raise ValueError("APPROVED_CONTRACT_DISPOSITION")
            if status == "published" and not _producer_evidence_locators(
                target, public_contract.get("producer")
            ):
                raise ValueError(
                    f"PUBLISHED_CONTRACT_PRODUCER_EVIDENCE_REQUIRED:{target_id}"
                )
            if status == "undecided" and target["disposition"] not in {"partial", "absent", "unknown"}:
                raise ValueError("UNDECIDED_CONTRACT_DISPOSITION")
            approval_evidence = public_contract["approval_evidence"]
            if not isinstance(approval_evidence, list) or not approval_evidence:
                raise ValueError("PUBLIC_CONTRACT_APPROVAL_EVIDENCE_REQUIRED")
            for item in approval_evidence:
                _exact_fields(item, {"locator", "claim"}, "PUBLIC_CONTRACT_APPROVAL_EVIDENCE")
                locator = _normalized_locator(item["locator"])
                if locator not in approved_source_locators:
                    raise ValueError("UNAPPROVED_CONTRACT_EVIDENCE")
                _required_text(item["claim"], "approval_evidence.claim")
    if not isinstance(payload["open_questions"], list) or any(
        not isinstance(item, str) or not item.strip() for item in payload["open_questions"]
    ):
        raise ValueError("OPEN_QUESTIONS_INVALID")
    return _relative_metadata(
        workspace, _write_json(output_dir / "triage-source-confirmation.json", payload)
    )


def _advance_analysis(
    payload: dict[str, Any], workspace: Path, issue_number: int, output_dir: Path
) -> dict[str, str]:
    _exact_fields(payload, {"schema", "analysis"}, "ANALYSIS")
    intake = _canonical_intake(output_dir)
    identity = _identity(output_dir)
    domain = _load_sibling("_kwc_triage_workflow_domain", "triage_domain.py")
    module = domain.build_triage_module()
    analysis = _canonicalize_analysis_authoring(payload["analysis"])
    analysis_body = module.compose_analysis.execute(intake, analysis)
    confirmation = _load_json(output_dir / "triage-source-confirmation.json")
    _validate_public_contract_analysis(confirmation, analysis)
    _validate_production_seam_confirmation(confirmation, analysis)
    _validate_document_impact_coverage(analysis_body, confirmation)
    unconfirmed = sorted(
        _analysis_evidence_locators(analysis) - _confirmed_locators(output_dir)
    )
    if unconfirmed:
        raise ValueError("UNCONFIRMED_ANALYSIS_EVIDENCE:" + ",".join(unconfirmed))
    errors = module.validator.validate_repository_locators(analysis_body, workspace)
    if errors:
        raise ValueError("INVALID_TRIAGE_ANALYSIS:" + ",".join(errors))
    assembled = {
        "schema": "kwc-triage-authoring-v1",
        "identity": identity,
        "intake": {"intent": intake["intent"], "investigation_targets": intake["investigation_targets"]},
        "analysis": analysis,
    }
    return _relative_metadata(
        workspace, _write_json(output_dir / "triage-authoring.json", assembled)
    )


def advance_stage(
    *,
    assignment: dict[str, Any],
    artifact_file: Path | str,
    inspection_fn: Callable[..., dict[str, Any]] | None = None,
    git_head_fn: Callable[[Path], str] = _git_head,
) -> dict[str, Any]:
    if assignment.get("phase") != "triage":
        raise ValueError("assignment is not in triage phase")
    stage = str(assignment.get("triage_stage") or "")
    expected_schema = _STAGE_SCHEMAS.get(stage)
    if expected_schema is None:
        raise ValueError("triage stage is not advanceable")
    workspace = _workspace_path(assignment)
    artifact = _resolve_artifact(workspace, artifact_file)
    expected_artifact_file = assignment.get("artifact_file")
    if expected_artifact_file:
        expected_artifact = (workspace / str(expected_artifact_file)).resolve()
        if artifact != expected_artifact:
            raise ValueError("artifact_file does not match assignment")
    payload = _load_json(artifact)
    if payload.get("schema") != expected_schema:
        raise ValueError(f"artifact schema must be {expected_schema}")
    issue_number = int(assignment["issue_number"])
    generation = int(assignment["generation"])
    output_dir = _output_dir(
        workspace,
        board=assignment["board"],
        issue_number=issue_number,
        generation=generation,
    )

    if stage == "intake":
        metadata = _advance_intake(
            assignment, payload, workspace, output_dir, inspection_fn
        )
    elif stage == "discovery":
        metadata = _advance_discovery(
            payload, workspace, issue_number, output_dir, git_head_fn
        )
    elif stage == "source_confirmation":
        metadata = _advance_source_confirmation(payload, workspace, issue_number, output_dir)
    else:
        metadata = _advance_analysis(payload, workspace, issue_number, output_dir)
    return {"ok": True, "stage": stage, "artifact": metadata}


def materialization_arguments(assignment: dict[str, Any]) -> dict[str, str]:
    if assignment.get("phase") != "triage" or assignment.get("triage_stage") != "materialize":
        raise ValueError("triage assignment is not ready for materialization")
    workspace = _workspace_path(assignment)
    issue_number = int(assignment["issue_number"])
    generation = int(assignment["generation"])
    output_dir = _output_dir(
        workspace,
        board=assignment["board"],
        issue_number=issue_number,
        generation=generation,
    )
    artifacts = assignment.get("stage_artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != set(_STAGE_SCHEMAS):
        raise ValueError("TRIAGE_ARTIFACT_SET_INCOMPLETE")
    verified = {
        stage: _verified_stage_artifact(workspace, stage, metadata)
        for stage, metadata in artifacts.items()
    }
    authoring = verified["analysis"]
    inspection = output_dir / "current-state-inspection.json"
    if not authoring.is_file() or not inspection.is_file():
        raise ValueError("triage materialization artifacts are incomplete")
    return {
        "authoring_file": authoring.relative_to(workspace).as_posix(),
        "current_state_inspection_file": inspection.relative_to(workspace).as_posix(),
    }
