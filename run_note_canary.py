#!/usr/bin/env python3
"""Bounded NOTE canary. Dry-run by default. A stopped request requires reconciliation."""
import argparse
from contextlib import closing
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import urllib.error

from run_note_pilot import (
    ROOT, SetupError, check_live_config, decode_response, estimate_cost, make_payload,
    request_completion, resolve_key, validate_output,
)

GATEWAY = 'https://kong-api.prod1.nyumc.org'
MODEL = 'gpt-6-luna/v1.0.0'
MAX_CASES = 56


class CanaryError(ValueError):
    """Only fixed, credential-free errors may be shown."""


def plan(cases, prompt, model, cap):
    if not 1 <= len(cases) <= MAX_CASES:
        raise CanaryError('Canary requires 1 to 56 notes.')
    if model['model_version'] != MODEL or model.get('reasoning_effort') != 'low':
        raise CanaryError('Canary requires the benchmarked Luna deployment and low reasoning.')
    if type(cap) is not int or not 256 <= cap <= 3000:
        raise CanaryError('Canary completion cap must be between 256 and 3000.')
    rates = model['rates_per_million']
    if any(type(rates.get(k)) not in (int, float) or not math.isfinite(rates[k]) or rates[k] <= 0
           for k in ('input', 'cached_input', 'output')) or rates['cached_input'] > rates['input']:
        raise CanaryError('Invalid configured reference rates.')
    ids, notes, sources = set(), set(), set()
    requests = []
    for case in cases:
        source = case['input']
        metadata = source['metadata']
        text = source['full_text']
        ids_for_case = (case['case_id'], metadata['pat_mrn_id'], metadata['note_id'], case['source_file'])
        if (set(source) != {'metadata', 'full_text'}
                or set(metadata) != {'pat_mrn_id', 'note_id', 'note_signed_time'}
                or any(not isinstance(v, str) or not v.strip() for v in ids_for_case)
                or not isinstance(text, str) or not text.strip()
                or type(case['source_row']) is not int or case['source_row'] < 1
                or not isinstance(metadata['note_signed_time'], str) or not metadata['note_signed_time'].strip()):
            raise CanaryError('Malformed note input, identifiers, or source locator.')
        case_id, mrn, note_id, source_file = ids_for_case
        source_key = (source_file, case['source_row'])
        if case_id in ids or (mrn, note_id) in notes or source_key in sources:
            raise CanaryError('Duplicate case ID, note identity, or source row.')
        ids.add(case_id)
        notes.add((mrn, note_id))
        sources.add(source_key)
        payload = make_payload(MODEL, prompt, source, cap, 'low')
        # Reserve by UTF-8 payload bytes plus framing allowance, with no cache discount.
        prompt_limit = len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) + 1024
        if prompt_limit > 256_000:
            raise CanaryError('Request exceeds the canary input size limit.')
        requests.append((case, payload, prompt_limit))
    reserve = math.fsum((limit * rates['input'] + cap * rates['output']) / 1_000_000
                        for _, _, limit in requests)
    if not math.isfinite(reserve):
        raise CanaryError('Invalid cost reserve.')
    return requests, dict(rates), reserve


def normalize_prediction(value):
    errors = validate_output(value)
    if not errors or set(errors) != {'Absent subtype must have null qualification'}:
        return value, []
    diagnoses = [dict(d) for d in value['diagnoses']]
    changes = []
    for index, diagnosis in enumerate(diagnoses):
        if diagnosis['histologic_subtype'] is None and diagnosis['diagnostic_qualification'] is not None:
            changes.append({'field': f'diagnoses[{index}].diagnostic_qualification',
                            'from': diagnosis['diagnostic_qualification'], 'to': None})
            diagnosis['diagnostic_qualification'] = None
    return dict(value, diagnoses=diagnoses), changes


def validate_result(case, payload, prompt_limit, rates, result):
    status, prediction = decode_response(result['raw_response'])
    usage = result['usage']
    cost = estimate_cost(usage, rates)
    result['estimated_usd'] = cost
    if cost is None:
        return 'invalid_usage'
    if (usage['prompt_tokens'] > prompt_limit
            or usage['completion_tokens'] > payload['max_completion_tokens']):
        return 'usage_over_reserve'
    if status != 'parsed':
        return status
    prediction, changes = normalize_prediction(prediction)
    errors = validate_output(prediction)
    if changes:
        result['normalizations'] = changes
        result['original_validation_errors'] = ['Absent subtype must have null qualification'] * len(changes)
    result['validation_errors'] = errors
    if errors:
        return 'invalid_schema'
    metadata = case['input']['metadata']
    result['output'] = dict(prediction, mrn=metadata['pat_mrn_id'],
                            note_id=metadata['note_id'],
                            note_signed_time=metadata['note_signed_time'])
    return 'ok'


def run(planned, directory, budget, send):
    requests, rates, reserve = planned
    if not math.isfinite(budget) or budget < reserve:
        raise CanaryError('Reference cost reserve exceeds the requested budget.')
    fingerprint = hashlib.sha256(json.dumps(
        [GATEWAY, requests, rates, budget], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise CanaryError('Run directory must be private and owned by the current user.')
    # The run lock is held while calling the gateway. A process exit releases the
    # lock but leaves its committed in-flight record for explicit reconciliation.
    lock_fd = os.open(directory / '.lock', os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'wb') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CanaryError('This canary directory already has an active runner.') from None
        with closing(sqlite3.connect(directory / 'ledger.sqlite3')) as db:
            db.execute('PRAGMA synchronous=FULL')
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS manifest (fingerprint TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS requests (position INTEGER PRIMARY KEY, status TEXT, result TEXT)')
                previous = db.execute('SELECT fingerprint FROM manifest').fetchone()
                if previous is None:
                    db.execute('INSERT INTO manifest VALUES (?)', (fingerprint,))
                elif previous[0] != fingerprint:
                    raise CanaryError('Run configuration or inputs changed. Resume with the original parameters.')
            pending = db.execute("SELECT position, status, result FROM requests WHERE status != 'ok'").fetchall()
            for position, status, saved in pending:
                if status != 'invalid_schema':
                    raise CanaryError('A prior request needs reconciliation before resuming.')
                result = json.loads(saved)
                status = validate_result(*requests[position - 1], rates, result)
                if status != 'ok':
                    raise CanaryError('A prior request needs reconciliation before resuming.')
                with db:
                    db.execute('UPDATE requests SET status = ?, result = ? WHERE position = ?',
                               (status, json.dumps(result, ensure_ascii=False), position))
            for position, (case, payload, prompt_limit) in enumerate(requests, 1):
                if db.execute('SELECT 1 FROM requests WHERE position = ?', (position,)).fetchone():
                    continue
                locator = {key: case[key] for key in ('case_id', 'source_file', 'source_row')}
                with db:
                    db.execute("INSERT INTO requests VALUES (?, 'inflight', ?)",
                               (position, json.dumps(locator)))
                try:
                    response, request_id = send(payload)
                except urllib.error.HTTPError as error:
                    code = error.code
                    error.close()
                    with db:
                        db.execute("UPDATE requests SET status = 'http_error', result = ? WHERE position = ?",
                                   (json.dumps(dict(locator, http_status=code)), position))
                    raise CanaryError('Gateway HTTP error. Request saved for reconciliation.') from None
                except (OSError, ValueError):
                    raise CanaryError('Gateway request outcome unknown. Automatic replay disabled.') from None

                usage = response.get('usage') if isinstance(response, dict) else None
                result = dict(locator, request_id=request_id, usage=usage, raw_response=response)
                status = validate_result(case, payload, prompt_limit, rates, result)
                with db:
                    db.execute('UPDATE requests SET status = ?, result = ? WHERE position = ?',
                               (status, json.dumps(result, ensure_ascii=False), position))
                if status != 'ok':
                    raise CanaryError('Canary stopped on ' + status + '. Inspect the private ledger.')

            results = [json.loads(row[0]) for row in db.execute('SELECT result FROM requests ORDER BY position')]
            temp = directory / 'predictions.jsonl.tmp'
            with temp.open('w', encoding='utf-8') as stream:
                for result in results:
                    export = {key: result[key] for key in ('case_id', 'source_file', 'source_row', 'output')}
                    stream.write(json.dumps(export, ensure_ascii=False) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            temp.replace(directory / 'predictions.jsonl')
            return {'validated_records': len(results),
                    'estimated_usd': math.fsum(r['estimated_usd'] for r in results)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--budget-usd', type=float)
    args = parser.parse_args()
    preflight = ROOT / 'runs/note-production-preflight-20261110'
    config_path = ROOT / ('config.local.json' if args.execute else 'config.example.json')
    config = json.loads(config_path.read_text())
    model = next(m for m in config['models'] if m['id'] == 'gpt-6-luna')
    source_path = preflight / 'longitudinal_pilot_inputs.jsonl'
    cases = [json.loads(line) for line in source_path.read_text().splitlines()]
    prompt = (ROOT / 'prompts/note_v1.3.txt').read_text()
    planned = plan(cases, prompt, model, config.get('max_completion_tokens', 3000))
    print(json.dumps({'mode': 'execute' if args.execute else 'dry_run', 'planned_requests': len(cases),
                      'reference_cost_reserve_usd': planned[2], 'model': MODEL}), flush=True)
    if not args.execute:
        return
    if args.budget_usd is None:
        raise CanaryError('Live canary requires --budget-usd.')
    if (config.get('provider') != 'nyu_gateway' or config.get('base_url') != GATEWAY
            or os.environ.get('LLM_API_BASE_URL', GATEWAY).rstrip('/') != GATEWAY):
        raise CanaryError('Canary only accepts the approved NYU gateway URL.')
    key = resolve_key(config)
    check_live_config(config, key)
    os.umask(0o077)
    result = run(planned, preflight / 'canary_luna', args.budget_usd,
                 lambda payload: request_completion(GATEWAY + '/' + MODEL, payload, key, gateway=True))
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except (CanaryError, SetupError) as error:
        raise SystemExit(str(error)) from None
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, StopIteration) as error:
        raise SystemExit('Canary setup or local state error: ' + type(error).__name__) from None
