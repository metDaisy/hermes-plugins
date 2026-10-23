#!/usr/bin/env python3
"""Compatibility entrypoint for task-graph authoring commands."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    source_root = Path(__file__).resolve().parents[3]
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    from hermes_langchain.authoring.graph.implementation_batch import (
        _entry_point_symbol, _parse_json_values, _split_locator, apply_impl_completions,
        impl_batch_template, prepare_impl_fast_path, render_impl_batch, validate_impl_batch,
    )
    from hermes_langchain.authoring.graph.legacy_implementation_card import (
        APPLICABILITY, CARD_FIELDS, CONTRACT_DIMENSIONS, CURRENT_CARD_SCHEMA, FORBIDDEN_FIELDS,
        GENERIC_CONTEXT_TEXT, GENERIC_ENTRY_POINT_REASON, GENERIC_VERIFICATION_SCENARIO,
        TEST_LEVELS, _append_forbidden_fields, _error, _implementation_schema,
        _implementation_text, _implementation_triage_digest, _load_implementation_card,
        _load_triage_domain, _non_empty, _strings, _unexpected_fields, _validate_acceptance,
        _validate_context, _validate_frozen_triage, _validate_implementation_body,
        _validate_implementation_contract, _validate_implementation_v6, _validate_korean,
        _validate_korean_strings, _validate_slice, _validate_source, _validate_verification,
        template, validate,
    )
    from hermes_langchain.authoring.graph.native_authoring import (
        _created_task_id, _parent_ids, create_impl_batch_native, create_review_native,
        create_summary_native, prepare_review_fast_path, prepare_summary_fast_path,
        regenerate_summary_native, validate_review_fast_path,
    )
    from hermes_langchain.authoring.graph.task_graph_cli import main
    from hermes_langchain.authoring.graph.task_graph_io import _read_json, _temp_path, write_json
else:
    from .implementation_batch import (
        _entry_point_symbol, _parse_json_values, _split_locator, apply_impl_completions,
        impl_batch_template, prepare_impl_fast_path, render_impl_batch, validate_impl_batch,
    )
    from .legacy_implementation_card import (
        APPLICABILITY, CARD_FIELDS, CONTRACT_DIMENSIONS, CURRENT_CARD_SCHEMA, FORBIDDEN_FIELDS,
        GENERIC_CONTEXT_TEXT, GENERIC_ENTRY_POINT_REASON, GENERIC_VERIFICATION_SCENARIO,
        TEST_LEVELS, _append_forbidden_fields, _error, _implementation_schema,
        _implementation_text, _implementation_triage_digest, _load_implementation_card,
        _load_triage_domain, _non_empty, _strings, _unexpected_fields, _validate_acceptance,
        _validate_context, _validate_frozen_triage, _validate_implementation_body,
        _validate_implementation_contract, _validate_implementation_v6, _validate_korean,
        _validate_korean_strings, _validate_slice, _validate_source, _validate_verification,
        template, validate,
    )
    from .native_authoring import (
        _created_task_id, _parent_ids, create_impl_batch_native, create_review_native,
        create_summary_native, prepare_review_fast_path, prepare_summary_fast_path,
        regenerate_summary_native, validate_review_fast_path,
    )
    from .task_graph_cli import main
    from .task_graph_io import _read_json, _temp_path, write_json


if __name__ == "__main__":
    raise SystemExit(main())