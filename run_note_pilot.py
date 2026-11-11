#!/usr/bin/env python3
"""Sequential, opt-in NOTE pilot. Standard library only; dry-run by default."""
import argparse
from collections import Counter, defaultdict
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from cancer_pipeline.formats import validate_note

def validate_output(value):
    return validate_note(value, TEMPLATE, SUBTYPES)

from cancer_pipeline.gateway import (
    SetupError, check_live_config, decode_response, estimate_cost, make_payload,
    model_base_url, request_completion, resolve_key,
)


ROOT = Path(__file__).resolve().parent
TEMPLATE = json.loads((ROOT / 'schemas/note_output_format.json').read_text())
SUBTYPES = {r['name'] for r in json.loads((ROOT / 'schemas/subtypes_56.json').read_text())}
CATEGORIES = ['Malignant', 'Borderline', 'Benign', 'Other', 'Uncertain']
FIELDS = ['histologic_subtype', 'diagnostic_qualification', 'primary_origin_site',
          'primary_origin_certainty', 'temporal_status']




def compare(prediction, expected):
    """Order-independent normalized comparisons; original text is reviewed separately."""
    def bag(value, fields):
        return Counter(json.dumps([d[f] for f in fields], ensure_ascii=False)
                       for d in value['diagnoses'])
    scores = {key: prediction[key] == expected[key]
              for key in ['derived_classification', 'diagnosis_date', 'family_history_of_ovarian_cancer_documented']}
    scores.update({key + '_multiset_exact': bag(prediction, [key]) == bag(expected, [key]) for key in FIELDS})
    scores['normalized_diagnoses_exact'] = bag(prediction, FIELDS) == bag(expected, FIELDS)
    return scores




















def load_jsonl(path):
    values = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    ids = [v['case_id'] for v in values]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate case IDs in ' + path.name)
    return {v['case_id']: v for v in values}


def write_private(path, value):
    with path.open('x', encoding='utf-8') as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def summarize(records):
    result = {}
    for model in sorted({r['model'] for r in records}):
        group = [r for r in records if r['model'] == model]
        metrics = {k: sum(bool(r.get('scores', {}).get(k)) for r in group) / len(group)
                   for k in ['derived_classification', 'diagnosis_date', 'normalized_diagnoses_exact',
                             'family_history_of_ovarian_cancer_documented'] + [f + '_multiset_exact' for f in FIELDS]}
        tp = sum(r['expected_category'] == 'Malignant' and r.get('predicted_category') == 'Malignant' for r in group)
        fn = sum(r['expected_category'] == 'Malignant' and r.get('predicted_category') != 'Malignant' for r in group)
        fp = sum(r['expected_category'] != 'Malignant' and r.get('predicted_category') == 'Malignant' for r in group)
        costs = [r.get('estimated_usd') for r in group]
        result[model] = {'attempted_cases': len(group), 'status_counts': dict(Counter(r['status'] for r in group)),
            'exact_match_fraction_all_attempts': metrics,
            'target_precision': tp / (tp + fp) if tp + fp else None,
            'target_recall': tp / (tp + fn) if tp + fn else None,
            'average_elapsed_seconds': sum(r['elapsed_seconds'] for r in group) / len(group),
            'prompt_tokens': sum(r.get('usage', {}).get('prompt_tokens') or 0 for r in group),
            'completion_tokens': sum(r.get('usage', {}).get('completion_tokens') or 0 for r in group),
            'estimated_usd_known_usage': sum(c for c in costs if c is not None),
            'cost_complete': all(c is not None for c in costs)}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'config.local.json')
    parser.add_argument('--inputs', type=Path, default=ROOT / 'data/note_inputs_20.jsonl')
    parser.add_argument('--labels', type=Path, default=ROOT / 'data/note_labels_20.jsonl')
    parser.add_argument('--prompt', type=Path, default=ROOT / 'prompts/note_v1.3.txt')
    parser.add_argument('--limit', type=int, default=1, help='Cases per model; default 1 for smoke testing')
    parser.add_argument('--models', nargs='+', help='Subset of model IDs from config')
    parser.add_argument('--structured-schema', type=Path, help='Strict JSON Schema; omitted preserves legacy JSON mode')
    parser.add_argument('--execute', action='store_true', help='Actually send requests; default is offline dry run')
    args = parser.parse_args()
    if not 1 <= args.limit <= 20:
        raise ValueError('--limit must be between 1 and 20 for this pilot')
    config_path = args.config if args.config.exists() else ROOT / 'config.example.json'
    config = json.loads(config_path.read_text())
    models = config['models']
    if args.models:
        if not set(args.models) <= {m['id'] for m in models}:
            raise ValueError('Requested model is not configured')
        models = [m for m in models if m['id'] in args.models]
    if not models or len({m['id'] for m in models}) != len(models):
        raise ValueError('Configure unique model IDs')
    inputs = load_jsonl(args.inputs)
    labels = load_jsonl(args.labels)
    if not set(inputs) <= set(labels):
        raise ValueError('Each input must have a matching reviewed label')
    cases = list(inputs.values())[:args.limit]
    if not cases or len(cases) * len(models) > 60:
        raise ValueError('Pilot requires 1 to 60 total requests')
    for case in cases:
        if set(case['input']) != {'metadata', 'full_text'} or not isinstance(case['input']['full_text'], str) or not case['input']['full_text'].strip():
            raise ValueError('Each input must contain only metadata and nonempty full_text')
        if validate_output(labels[case['case_id']]['output']):
            raise ValueError('Reference output is invalid: ' + case['case_id'])
    cap = config.get('max_completion_tokens', 3000)
    if type(cap) is not int or not 256 <= cap <= 16000:
        raise ValueError('max_completion_tokens must be 256 to 16000')
    prompt = args.prompt.read_text()
    schema = json.loads(args.structured_schema.read_text()) if args.structured_schema else None
    if schema is not None and (schema.get('type') != 'object' or set(schema.get('properties', {})) != set(TEMPLATE)):
        raise ValueError('Structured schema must match the NOTE output contract')
    print(json.dumps({'mode': 'execute' if args.execute else 'dry_run', 'cases_per_model': len(cases),
                      'models': [m['id'] for m in models], 'planned_requests': len(cases) * len(models),
                      'prompt_characters': len(prompt), 'max_completion_tokens': cap, 'output_mode': 'json_schema' if schema else 'json_object'}))
    if not args.execute:
        print('Offline checks passed. No requests sent, no key required.')
        return
    key = resolve_key(config)
    check_live_config(config, key)
    for model in models:
        check_live_config(dict(config, base_url=model_base_url(config, model)), key)
    os.umask(0o077)
    directory = ROOT / 'runs' / dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    manifest = {'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
        'inputs_sha256': hashlib.sha256(args.inputs.read_bytes()).hexdigest(),
        'labels_sha256': hashlib.sha256(args.labels.read_bytes()).hexdigest(),
        'case_ids': [c['case_id'] for c in cases], 'base_url': config['base_url'],
        'provider': config.get('provider', 'openai'),
        'resolved_model_urls': {m['id']: model_base_url(config, m) for m in models},
        'models': [{k: m.get(k) for k in ['id', 'model_version', 'reasoning_effort', 'rates_per_million']} for m in models], 'max_completion_tokens': cap, 'automatic_retries': 0,
        'evaluation_scope': 'User-reviewed development challenge set, not an unbiased holdout',
        'cost_note': 'Reference Standard pricing estimate, not a gateway invoice or a hard spending cap; no cache-write/regional charges included'}
    manifest['output_mode'] = 'json_schema' if schema else 'json_object'
    manifest['schema_sha256'] = hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest() if schema else None
    (directory / 'prompt.txt').write_text(prompt)
    if schema is not None:
        write_private(directory / 'schema.json', schema)
    write_private(directory / 'manifest.json', manifest)
    records = []
    for model in models:
        for case in cases:
            start = time.monotonic()
            expected = labels[case['case_id']]['output']
            record = {'case_id': case['case_id'], 'model': model['id'], 'expected_category': expected['derived_classification']}
            stop = False
            try:
                response, request_id = request_completion(model_base_url(config, model),
                    make_payload(model.get('model_version', model['id']), prompt, case['input'], cap, model.get('reasoning_effort'), schema=schema), key,
                    gateway=config.get('provider') == 'nyu_gateway')
                if not isinstance(response, dict):
                    raise ValueError('Invalid response object')
                status, prediction = decode_response(response)
                usage = response.get('usage')
                record.update(status=status, usage=usage if isinstance(usage, dict) else {}, request_id=request_id,
                              returned_model=response.get('model'), raw_response=response)
                if status == 'parsed':
                    errors = validate_output(prediction)
                    record.update(status='invalid_schema' if errors else 'ok', validation_errors=errors, output=prediction)
                    if not errors:
                        record.update(scores=compare(prediction, expected), predicted_category=prediction['derived_classification'])
                        # Metadata are program-owned; preserve model output above for diagnostics.
                        record['output_with_source_metadata'] = dict(prediction, mrn=case['input']['metadata'].get('pat_mrn_id'),
                            note_id=case['input']['metadata'].get('note_id'), note_signed_time=case['input']['metadata'].get('note_signed_time'))
                record['estimated_usd'] = estimate_cost(record['usage'], model.get('rates_per_million'))
            except urllib.error.HTTPError as error:
                record.update(status='http_error', http_status=error.code, estimated_usd=None)
                error.close()
                stop = error.code in [401, 403]
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, TypeError, KeyError) as error:
                # Error bodies/messages can echo private notes or credentials; do not log them.
                record.update(status='request_or_response_error', error_type=type(error).__name__, estimated_usd=None)
            record['elapsed_seconds'] = round(time.monotonic() - start, 3)
            records.append(record)
            with (directory / 'results.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
            print(case['case_id'], model['id'], record['status'], flush=True)
            if stop:
                write_private(directory / 'summary.json', summarize(records))
                print('Stopped after authorization failure; results preserved in', directory)
                return
    write_private(directory / 'summary.json', summarize(records))
    print('Results:', directory)


def cli():
    try:
        main()
        return 0
    except SetupError as error:
        print('Setup failed:', str(error))
        return 1
    except (ValueError, OSError) as error:
        # Untrusted parser/OS errors may echo private data; only show their type.
        print('Setup failed:', type(error).__name__, '(check input paths and local configuration; no secrets printed)')
        return 1


if __name__ == '__main__':
    raise SystemExit(cli())
