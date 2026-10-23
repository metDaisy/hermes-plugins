"""Command handlers for the task-graph CLI adapter."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable

from .contract_common import serialize_task_body
from .graph_contract import requirement_diff, validate_graph, validate_native_readback, validate_review_graph
from .implementation_batch import (
    _parse_json_values, apply_impl_completions, impl_batch_template, prepare_impl_fast_path,
    render_impl_batch, validate_impl_batch,
)
from .legacy_implementation_card import _validate_implementation_v6, template, validate
from .native_authoring import (
    create_impl_batch_native, create_review_native, create_summary_native,
    prepare_review_fast_path, prepare_summary_fast_path, regenerate_summary_native,
)
from .task_graph_io import _read_json, _temp_path, write_json

def _handle_template(args: Any) -> int:
    write_json(args.output, template(args.issue, args.issue_url))
    return 0


def _handle_impl_batch_template(args: Any) -> int:
    try:
        write_json(args.output, impl_batch_template(_read_json(args.triage), generation=args.generation, triage_id=args.triage_id))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(json.dumps({'valid': False, 'errors': [f'IMPL_BATCH_TEMPLATE_FAILED:{exc}']}, ensure_ascii=False))
        return 2
    return 0


def _handle_prepare_impl_fast_path(args: Any) -> int:
    try:
        base = ['hermes', '--profile', args.profile, 'kanban', '--board', args.board]
        show = subprocess.run([*base, 'show', args.triage_id, '--json'], check=True, capture_output=True, text=True, encoding='utf-8')
        listing = subprocess.run([*base, 'list', '--json'], check=True, capture_output=True, text=True, encoding='utf-8')
        show_values = _parse_json_values(show.stdout)
        if len(show_values) != 1 or not isinstance(show_values[0], dict):
            raise ValueError(f'SOURCE_TRIAGE_READBACK_COUNT:{len(show_values)}')
        active_tasks = [item for item in _parse_json_values(listing.stdout) if isinstance(item, dict)]
        prepared = prepare_impl_fast_path(show_values[0], active_tasks, triage_id=args.triage_id, issue=args.issue, generation=args.generation)
        write_json(args.output_dir / 'source-triage.json', show_values[0])
        write_json(args.output_dir / 'active-board.json', active_tasks)
        write_json(args.output_dir / 'triage-body.json', prepared['triage_body'])
        write_json(args.batch_output, prepared['batch'])
        print(json.dumps(prepared['summary'], ensure_ascii=False))
    except (OSError, json.JSONDecodeError, subprocess.CalledProcessError, TypeError, ValueError) as exc:
        print(json.dumps({'valid': False, 'errors': [f'PREPARE_IMPL_FAST_PATH_FAILED:{exc}']}, ensure_ascii=False))
        return 2
    return 0


def _handle_prepare_review_fast_path(args: Any) -> int:
    try:
        base = ['hermes', '--profile', args.profile, 'kanban', '--board', args.board]
        listing = subprocess.run([*base, 'list', '--json'], check=True, capture_output=True, text=True, encoding='utf-8')
        active_tasks = [item for item in _parse_json_values(listing.stdout) if isinstance(item, dict)]
        bundle = prepare_review_fast_path(active_tasks, triage_id=args.triage_id, implementation_ids=args.implementation_ids, issue=args.issue, generation=args.generation)
        write_json(args.bundle_output, bundle)
        write_json(args.body_output, bundle['review_body_template'])
        print(json.dumps({'valid': True, 'title': bundle['title'], 'source_count': 1 + len(bundle['implementation_cards']), 'native_calls': 1, 'bundle': str(args.bundle_output), 'body': str(args.body_output)}, ensure_ascii=False))
    except (OSError, json.JSONDecodeError, subprocess.CalledProcessError, TypeError, ValueError) as exc:
        print(json.dumps({'valid': False, 'errors': [f'PREPARE_REVIEW_FAST_PATH_FAILED:{exc}']}, ensure_ascii=False))
        return 2
    return 0


def _handle_create_review_native(args: Any) -> int:
    try:
        report = create_review_native(_read_json(args.bundle), _read_json(args.body), profile=args.profile, board=args.board, body_file=args.body, workspace_path=args.workspace, dry_run=args.dry_run)
        write_json(args.output, report)
        print(json.dumps(report, ensure_ascii=False))
    except (OSError, json.JSONDecodeError, subprocess.CalledProcessError, TypeError, ValueError) as exc:
        print(json.dumps({'valid': False, 'errors': [f'CREATE_REVIEW_NATIVE_FAILED:{exc}']}, ensure_ascii=False))
        return 2
    return 0


def _handle_prepare_summary_fast_path(args: Any) -> int:
    try:
        base = ['hermes', '--profile', args.profile, 'kanban', '--board', args.board]
        listing = subprocess.run([*base, 'list', '--json'], check=True, capture_output=True, text=True, encoding='utf-8')
        active_tasks = [item for item in _parse_json_values(listing.stdout) if isinstance(item, dict)]
        bundle = prepare_summary_fast_path(active_tasks, triage_id=args.triage_id, issue=args.issue, generation=args.generation)
        write_json(args.bundle_output, bundle)
        write_json(args.body_output, bundle['body'])
        print(json.dumps({'valid': True, 'title': bundle['title'], 'parent_ids': bundle['parent_ids'], 'native_calls': 1, 'bundle': str(args.bundle_output), 'body': str(args.body_output)}, ensure_ascii=False))
    except (OSError, json.JSONDecodeError, subprocess.CalledProcessError, TypeError, ValueError) as exc:
        print(json.dumps({'valid': False, 'errors': [f'PREPARE_SUMMARY_FAST_PATH_FAILED:{exc}']}, ensure_ascii=False))
        return 2
    return 0


def _handle_create_summary_native(args: Any) -> int:
    try:
        report = create_summary_native(_read_json(args.bundle), profile=args.profile, board=args.board, body_file=args.body, workspace_path=args.workspace, dry_run=args.dry_run)
        write_json(args.output, report)
        print(json.dumps(report, ensure_ascii=False))
    except (OSError, json.JSONDecodeError, subprocess.CalledProcessError, TypeError, ValueError) as exc:
        print(json.dumps({'valid': False, 'errors': [f'CREATE_SUMMARY_NATIVE_FAILED:{exc}']}, ensure_ascii=False))
        return 2
    return 0


def _handle_regenerate_summary_native(args: Any) -> int:
    try:
        report = regenerate_summary_native(profile=args.profile, board=args.board, triage_id=args.triage_id, issue=args.issue, generation=args.generation, workspace_path=args.workspace, bundle_file=args.bundle_output, body_file=args.body_output)
        write_json(args.output, report)
        print(json.dumps(report, ensure_ascii=False))
    except (OSError, json.JSONDecodeError, subprocess.CalledProcessError, TypeError, ValueError) as exc:
        print(json.dumps({'valid': False, 'errors': [f'REGENERATE_SUMMARY_NATIVE_FAILED:{exc}']}, ensure_ascii=False))
        return 2
    return 0


def _handle_apply_impl_completions(args: Any) -> int:
    try:
        write_json(args.output, apply_impl_completions(_read_json(args.batch), _read_json(args.completions)))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(json.dumps({'valid': False, 'errors': [f'APPLY_IMPL_COMPLETIONS_FAILED:{exc}']}, ensure_ascii=False))
        return 2
    return 0


def _handle_render_impl_batch(args: Any) -> int:
    try:
        rendered = render_impl_batch(_read_json(args.batch))
        for filename, body in rendered.items():
            write_json(args.output_dir / filename, body)
        print(json.dumps({'valid': True, 'count': len(rendered), 'files': list(rendered)}, ensure_ascii=False))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(json.dumps({'valid': False, 'errors': [f'RENDER_IMPL_BATCH_FAILED:{exc}']}, ensure_ascii=False))
        return 2
    return 0


def _handle_create_impl_batch_native(args: Any) -> int:
    try:
        report = create_impl_batch_native(_read_json(args.batch), profile=args.profile, board=args.board, triage_id=args.triage_id, body_dir=args.body_dir, workspace_path=args.workspace)
        write_json(args.output, report)
        print(json.dumps({'valid': True, 'created': [{'id': item['id'], 'title': item['title']} for item in report['created']], 'triage_status': report['triage']['status']}, ensure_ascii=False))
    except (OSError, json.JSONDecodeError, subprocess.CalledProcessError, TypeError, ValueError) as exc:
        print(json.dumps({'valid': False, 'errors': [f'CREATE_IMPL_BATCH_NATIVE_FAILED:{exc}']}, ensure_ascii=False))
        return 2
    return 0


def _handle_requirement_diff(args: Any) -> int:
    try:
        write_json(args.output, requirement_diff(Path.cwd(), args.base, args.revised, args.paths))
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(json.dumps({'valid': False, 'errors': [f'REQUIREMENT_DIFF_FAILED:{exc}']}, ensure_ascii=False))
        return 2
    return 0


def _handle_render_body(args: Any) -> int:
    try:
        graph = _read_json(args.graph)
        cards = graph.get('cards') if isinstance(graph, dict) else None
        matches = [card for card in cards or [] if isinstance(card, dict) and card.get('key') == args.card_key]
        if len(matches) != 1:
            raise ValueError(f'CARD_KEY_MATCH_COUNT:{args.card_key}:{len(matches)}')
        destination = _temp_path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(serialize_task_body(matches[0].get('body')).encode('utf-8'))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(json.dumps({'valid': False, 'errors': [f'RENDER_BODY_FAILED:{exc}']}, ensure_ascii=False))
        return 2
    return 0


def _handle_validation(args: Any) -> int:
    if args.command in {'validate-graph', 'validate-review-graph', 'validate-native-readback'}:
        source = args.graph
    elif args.command == 'validate-impl-batch':
        source = args.batch
    else:
        source = args.card
    try:
        payload = _read_json(source)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(json.dumps({'valid': False, 'errors': [f'READ_INPUT_FAILED:{exc}']}, ensure_ascii=False))
        return 2
    source_review_result = None
    if args.command in {'validate-graph', 'validate-native-readback'} and args.source_review_result is not None:
        try:
            source_review_result = _read_json(args.source_review_result)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            print(json.dumps({'valid': False, 'errors': [f'READ_INPUT_FAILED:{exc}']}, ensure_ascii=False))
            return 2
    if args.command == 'validate-native-readback':
        try:
            readback = _read_json(args.readback)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            print(json.dumps({'valid': False, 'errors': [f'READ_INPUT_FAILED:{exc}']}, ensure_ascii=False))
            return 2
        errors = validate_graph(payload, phase=args.phase, validate_implementation=validate, source_review_result=source_review_result)
        errors.extend(validate_native_readback(payload, readback))
    elif args.command == 'validate-graph':
        errors = validate_graph(payload, phase=args.phase, validate_implementation=validate, source_review_result=source_review_result)
    elif args.command == 'validate-review-graph':
        errors = validate_review_graph(payload, validate_implementation=validate)
    elif args.command == 'validate-impl-batch':
        errors = validate_impl_batch(payload)
    elif args.command == 'validate-implementation-v6':
        errors = _validate_implementation_v6(payload)
    else:
        errors = validate(payload)
    print(json.dumps({'valid': not errors, 'errors': errors}, ensure_ascii=False))
    return 1 if errors else 0


_HANDLERS: dict[str, Callable[[Any], int]] = {
    'template': _handle_template,
    'impl-batch-template': _handle_impl_batch_template,
    'prepare-impl-fast-path': _handle_prepare_impl_fast_path,
    'prepare-review-fast-path': _handle_prepare_review_fast_path,
    'create-review-native': _handle_create_review_native,
    'prepare-summary-fast-path': _handle_prepare_summary_fast_path,
    'create-summary-native': _handle_create_summary_native,
    'regenerate-summary-native': _handle_regenerate_summary_native,
    'apply-impl-completions': _handle_apply_impl_completions,
    'render-impl-batch': _handle_render_impl_batch,
    'create-impl-batch-native': _handle_create_impl_batch_native,
    'requirement-diff': _handle_requirement_diff,
    'render-body': _handle_render_body,
    'validate': _handle_validation,
    'validate-implementation-v6': _handle_validation,
    'validate-impl-batch': _handle_validation,
    'validate-graph': _handle_validation,
    'validate-review-graph': _handle_validation,
    'validate-native-readback': _handle_validation,
}


def execute(args: Any) -> int:
    return _HANDLERS[args.command](args)
