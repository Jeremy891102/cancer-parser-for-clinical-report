#!/usr/bin/env python3
"""Run local NOTE inference in bounded groups with a durable source-row ledger."""
import argparse
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing
import csv
import fcntl
import hashlib
from itertools import groupby
import json
import math
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import threading
import time
import urllib.error
import zlib

from postprocess_notes import COLUMNS, aggregate
from run_note_canary import GATEWAY, MODEL, plan, validate_result
from run_note_pilot import ROOT, check_live_config, make_payload, request_completion, resolve_key
from cancer_pipeline.storage import save_csv, save_gz, save_json

from cancer_pipeline.gateway import perform_request
from cancer_pipeline.engine import add, finish, setup
from cancer_pipeline.engine import infer as infer_records


def infer(db, cases, prompt, rates, budget, workers, worker, checkpoint, reused=None):
    return infer_records(db, cases, prompt, rates, budget, workers, worker, checkpoint, reused,
                         model_version=MODEL, completion_cap=CAP, reasoning_effort='low', stop_event=STOP)


CAP = 3000
STOP = threading.Event()
EXPECTED = 707291
SHARDS = {'all': (slice(None), EXPECTED), 'local': (slice(0, 6), 353653),
          'server': (slice(6, 12), 353638)}
RECORD_COLUMNS = ['MRN', 'note_id', 'note_signed_time', 'derived_classification', 'diagnosis_date',
                  'source_file', 'source_row', 'status']


def fingerprint_file(path):
    digest = hashlib.sha256()
    before = path.stat()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(2**20), b''):
            digest.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('Source changed during hashing')
    return {'name': path.name, 'sha256': digest.hexdigest(), 'bytes': after.st_size,
            'mtime_ns': after.st_mtime_ns}


def assigned_sources(files, shard):
    selection, count = SHARDS[shard]
    return files[selection], count


def portable_contract(contract):
    return dict(contract, files=[{k: v for k, v in source.items() if k != 'mtime_ns'}
                                 for source in contract['files']])


def read_cases(files):
    csv.field_size_limit(100_000_000)
    for path, contract in files:
        stat = path.stat()
        if (stat.st_size, stat.st_mtime_ns) != (contract['bytes'], contract['mtime_ns']):
            raise ValueError('Source changed after preflight')
        with path.open(newline='', encoding='utf-8-sig') as stream:
            reader = csv.DictReader(stream)
            for row_number, row in enumerate(reader, 1):
                metadata = {k: row[k] for k in ('pat_mrn_id', 'note_id', 'note_signed_time')}
                if not metadata['pat_mrn_id'] or not metadata['note_id'] or row['full_text'] is None:
                    raise ValueError('Missing source identifier or malformed CSV row')
                yield {'case_id': f'{path.stem}:{row_number}', 'source_file': path.name,
                       'source_row': row_number,
                       'input': {'metadata': metadata, 'full_text': row['full_text']}}
        stat = path.stat()
        if (stat.st_size, stat.st_mtime_ns) != (contract['bytes'], contract['mtime_ns']):
            raise ValueError('Source changed during processing')








def process(case, payload, limit, rates, send):
    return perform_request(payload, limit, rates, send,
                           lambda result: validate_result(case, payload, limit, rates, result))




def read_canary(prompt, model):
    base = ROOT / 'runs/note-production-preflight-20261110'
    cases = [json.loads(x) for x in (base / 'longitudinal_pilot_inputs.jsonl').read_text().splitlines()]
    requests, rates, _ = plan(cases, prompt, model, CAP)
    expected = hashlib.sha256(json.dumps(
        [GATEWAY, requests, rates, .30], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with closing(sqlite3.connect((base / 'canary_luna/ledger.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute('SELECT fingerprint FROM manifest').fetchone() != (expected,):
            raise ValueError('Canary settings differ from production')
        saved = db.execute('SELECT position,status,result FROM requests ORDER BY position').fetchall()
    if len(saved) != len(cases):
        raise ValueError('Canary incomplete')
    reused = {}
    for (position, status, raw), case in zip(saved, cases):
        result = json.loads(raw)
        if status != 'ok' or result['case_id'] != case['case_id'] or position != len(reused) + 1:
            raise ValueError('Canary ledger mismatch')
        reused[case['case_id']] = (case['input'], result)
    return reused


def predictions(db, ordering):
    query = """SELECT case_id,source_file,source_row,mrn,note_id,signed,status,result
               FROM notes ORDER BY """ + ordering
    for case, file, row, mrn, note, signed, status, blob in db.execute(query):
        result = json.loads(zlib.decompress(blob)) if blob else {}
        output = result.get('output')
        yield {'case_id': case, 'source_file': file, 'source_row': row, 'status': status,
               'MRN': mrn, 'note_id': note, 'note_signed_time': signed,
               'derived_classification': output['derived_classification'] if output else None,
               'diagnosis_date': output['diagnosis_date'] if output else None,
               'output': output or {'mrn': mrn, 'note_id': note}}


def export(db, root):
    batch = []
    batch_number = 0
    classifications = Counter()
    for row in predictions(db, 'position'):
        if row['derived_classification']:
            batch.append(row['output'])
            classifications[row['derived_classification']] += 1
        if len(batch) == 500:
            batch_number += 1
            save_gz(root / 'note' / f'batch_{batch_number:04d}.jsonl.gz', batch)
            batch.clear()
    if batch:
        save_gz(root / 'note' / f'batch_{batch_number + 1:04d}.jsonl.gz', batch)

    def patient_rows():
        for _, rows in groupby(predictions(db, 'mrn, position'), key=lambda row: row['MRN']):
            result = aggregate(rows)
            row = result['patients'][0]
            yield dict(row, progress_note_reason=sorted({r['reason'] for r in result['issues']}))

    save_csv(root / 'csv/note_record_level.csv', RECORD_COLUMNS, predictions(db, 'position'))
    save_csv(root / 'csv/note_patient_level.csv', COLUMNS + ['status', 'progress_note_reason'], patient_rows())
    return classifications


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--budget-usd', type=float)
    parser.add_argument('--shard', choices=SHARDS, default='all')
    parser.add_argument('--source-dir', type=Path, default=Path.home() / 'Desktop/progress-notes')
    parser.add_argument('--reference-contract', type=Path)
    args = parser.parse_args()
    if args.shard == 'server' and args.execute and not args.reference_contract:
        raise ValueError('Server shard requires the original source contract')
    if args.shard != 'all' and args.execute and args.budget_usd is not None and args.budget_usd > 250:
        raise ValueError('Each shard has a maximum reference budget of US$250')
    if not 1 <= args.workers <= 8 or (args.execute and
            (args.budget_usd is None or not math.isfinite(args.budget_usd) or args.budget_usd <= 0)):
        raise ValueError('Invalid worker count or reference budget')
    os.umask(0o077)
    source = args.source_dir
    paths = sorted(source.glob('progress_notes_batch_*_of_12.csv'))
    if [p.name for p in paths] != [f'progress_notes_batch_{i:02d}_of_12.csv' for i in range(12)]:
        raise ValueError('Expected all twelve NOTE source files')
    assigned, expected = assigned_sources(paths, args.shard)
    config = json.loads((ROOT / ('config.local.json' if args.execute else 'config.example.json')).read_text())
    model = next(m for m in config['models'] if m['id'] == 'gpt-6-luna')
    prompt = (ROOT / 'prompts/note_v1.3.txt').read_text()
    reused = read_canary(prompt, model)
    if config.get('provider') != 'nyu_gateway' or config.get('base_url') != GATEWAY or os.environ.get(
            'LLM_API_BASE_URL', GATEWAY).rstrip('/') != GATEWAY:
        raise ValueError('NOTE module only permits the approved NYU gateway')
    if not args.execute:
        print(json.dumps({'mode': 'dry_run', 'expected_records': expected,
                          'shard': args.shard, 'source_files': [path.name for path in assigned],
                          'reusable_canary': sum(Path(k.split(':')[0] + '.csv') in
                                                 {Path(p.name) for p in assigned} for k in reused)}))
        return
    key = resolve_key(config)
    check_live_config(config, key)
    root = ROOT / 'results'
    audit = root / 'audit'
    if any(p.is_symlink() or p.stat().st_uid != os.getuid() or p.stat().st_mode & 0o077
           for p in (root, audit, root / 'note', root / 'csv')):
        raise ValueError('Output directories must be private and owned by the current user')
    if shutil.disk_usage(root).free < 10 * 2**30:
        raise ValueError('Insufficient disk space for NOTE ledger and exports')
    with (audit / 'note_module.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        shard_path = audit / 'note_shard.json'
        shard_contract = {'shard': args.shard, 'source_files': [p.name for p in assigned],
                          'expected_records': expected}
        if shard_path.exists() and json.loads(shard_path.read_text()) != shard_contract:
            raise ValueError('Existing ledger has a different shard assignment')
        print('Hashing all twelve source files.', flush=True)
        files = [(p, fingerprint_file(p)) for p in paths]
        contract = {'files': [f for _, f in files], 'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                    'gateway': GATEWAY, 'model': model, 'completion_cap': CAP}
        contract_path = audit / 'note_run_contract.json'
        if contract_path.exists() and json.loads(contract_path.read_text()) != contract:
            raise ValueError('Run source or settings changed')
        if args.reference_contract and portable_contract(contract) != portable_contract(
                json.loads(args.reference_contract.read_text())):
            raise ValueError('Source hash or inference settings differ from the original run')
        save_json(contract_path, contract)
        rates = model['rates_per_million']
        signal.signal(signal.SIGTERM, lambda *_: STOP.set())
        signal.signal(signal.SIGINT, lambda *_: STOP.set())

        def checkpoint(state, count, spent, **details):
            summary = dict(state=state, finished_records=count, total_source_records=expected,
                           shard=args.shard,
                           estimated_new_usd=spent, reference_budget_usd=args.budget_usd, **details)
            save_json(audit / 'note_manifest.json', summary)
            print(json.dumps(summary), flush=True)

        with closing(sqlite3.connect(audit / 'note_ledger.sqlite3')) as db:
            setup(db)
            with db:
                maximum_reserve = (256_000 * rates['input'] + CAP * rates['output']) / 1_000_000
                db.execute(
                    "UPDATE notes SET reserved_usd=? WHERE status='request_outcome_unknown' AND reserved_usd=0",
                    (maximum_reserve,))
                for position, blob in db.execute("SELECT position,result FROM notes WHERE status='http_error'").fetchall():
                    result = json.loads(zlib.decompress(blob))
                    if 500 <= result['http_status'] < 600 or result['http_status'] == 429:
                        result.update(original_status='http_error', reserved_usd=maximum_reserve,
                                      reconciliation='held_without_replay')
                        if result['http_status'] == 429:
                            result['retry_after_seconds'] = 60
                        status = 'rate_limited' if result['http_status'] == 429 else 'gateway_server_error'
                        finish(db, position, status, result)
            if not {r[0] for r in db.execute('SELECT DISTINCT source_file FROM notes')} <= set(
                    shard_contract['source_files']):
                raise ValueError('Existing ledger contains records assigned to a different shard')
            if args.shard != 'all':
                save_json(shard_path, shard_contract)
            count, spent = db.execute('SELECT COUNT(*),COALESCE(SUM(usd),0) FROM notes').fetchone()
            checkpoint('running', count, spent)
            send = lambda payload: request_completion(GATEWAY + '/' + MODEL, payload, key, gateway=True)
            worker = lambda case, payload, limit: process(case, payload, limit, rates, send)
            state = infer(db, read_cases(assigned_sources(files, args.shard)[0]), prompt, rates, args.budget_usd,
                          args.workers, worker, checkpoint, reused)
            count, spent = db.execute('SELECT COUNT(*),COALESCE(SUM(usd),0) FROM notes').fetchone()
            statuses = dict(db.execute('SELECT status, COUNT(*) FROM notes GROUP BY status'))
            if state == 'complete':
                if count != expected:
                    raise ValueError('Source row count differs from approved preflight')
                if args.shard != 'all':
                    checkpoint('shard_complete', count, spent, status_counts=statuses)
                    return
                checkpoint('exporting', count, spent, status_counts=statuses)
                categories = export(db, root)
                checkpoint(state, count, spent, status_counts=statuses, classification_counts=categories)
            else:
                checkpoint(state, count, spent, status_counts=statuses,
                           unknown_request_reserve_usd=db.execute(
                               'SELECT COALESCE(SUM(reserved_usd),0) FROM notes').fetchone()[0])


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError, StopIteration, csv.Error) as error:
        raise SystemExit('NOTE module stopped: ' + type(error).__name__) from None
