#!/usr/bin/env python3
"""Offline NOTE JSON -> traceable patient-level dates and review lists."""
import argparse
import calendar
from collections import Counter, defaultdict
import csv
import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path

from run_note_pilot import validate_output, ROOT

from cancer_pipeline.aggregation import aggregate_notes, unresolved_target as profile_unresolved_target
from cancer_pipeline.profiles import Study
from cancer_pipeline.dates import date_bounds


STUDY = Study.load(ROOT / 'profiles/ovarian.json')
TARGETS = set(STUDY.settings['target_sites'])
REFERENCE = {r['name']: r['reference_group'] for r in json.loads((ROOT / 'schemas/subtypes_56.json').read_text())}
COLUMNS = ['MRN', 'progress_note_positive_date', 'in_progress_note', 'progress_note_source_id', 'categories']




def unresolved_target(value):
    return profile_unresolved_target(value, STUDY)


def aggregate(records):
    return aggregate_notes(records, target_sites=TARGETS, subtype_groups=REFERENCE,
                           validate=validate_output, unresolved=unresolved_target)


def write_csv(path, columns, rows):
    with path.open('x', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            result = {}
            for key in columns:
                value = row.get(key)
                result[key] = json.dumps(value, ensure_ascii=False) if isinstance(value, list) else str(value).lower() if isinstance(value, bool) else value
            writer.writerow(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=ROOT / 'data/note_labels_20.jsonl', help='JSONL records with case_id and output; never calls a model')
    parser.add_argument('--output-dir', type=Path, help='New output directory; existing directories are never overwritten')
    args = parser.parse_args()
    records = []
    for number, line in enumerate(args.input.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            records.append({'case_id': 'line-' + str(number), 'output': None})
    if not records:
        raise SystemExit('Input has no records.')
    result = aggregate(records)
    directory = args.output_dir or ROOT / 'runs' / ('note-postprocess-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ'))
    os.umask(0o077)
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    patients = result['patients']
    write_csv(directory / 'note_patient_level.csv', COLUMNS, patients)
    positives = [p for p in patients if p['status'] == 'target_positive']
    write_csv(directory / 'note_target_positive.csv', COLUMNS, positives)
    categories_by_patient = {p['MRN']: p['categories'] for p in patients}
    review_rows = [{'MRN': r['MRN'], 'reason': r['reason'], 'progress_note_source_id': r['note_ids'],
                    'categories': categories_by_patient.get(r['MRN'], [])} for r in result['issues']]
    write_csv(directory / 'note_review.csv', ['MRN', 'reason', 'progress_note_source_id', 'categories'], review_rows)
    non_target = [p for p in patients if p['status'] == 'no_established_target_malignancy']
    write_csv(directory / 'note_non_target.csv', ['MRN', 'categories', 'all_note_ids'], non_target)
    (directory / 'validation_and_patient_audit.json').write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
    summary = {'source': str(args.input.resolve()), 'source_sha256': hashlib.sha256(args.input.read_bytes()).hexdigest(),
        'input_records': len(records), 'patients': len(patients), 'status_counts': dict(Counter(p['status'] for p in patients)),
        'positive_patients': len(positives), 'review_patients': len({r['MRN'] for r in result['issues'] if r['MRN']}),
        'unlinked_issues': sum(not r['MRN'] for r in result['issues']), 'non_target_patients': len(non_target),
        'api_calls': 0, 'input_kind': 'preparsed JSON; no model predictions generated',
        'date_rule': 'Only supported target Malignant dates contribute; retain partial precision and source IDs',
        'scope': 'NOTE module only; no PATH/ICD merge, no definitive negative/control assignment'}
    (directory / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps({k: v for k, v in summary.items() if k != 'source'}, indent=2))
    print('Output directory:', directory)


if __name__ == '__main__':
    main()
