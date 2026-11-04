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
from pathlib import Path

from run_note_pilot import validate_output, ROOT

TARGETS = {'ovary', 'fallopian_tube', 'peritoneum'}
REFERENCE = {r['name']: r['reference_group'] for r in json.loads((ROOT / 'schemas/subtypes_56.json').read_text())}
COLUMNS = ['MRN', 'progress_note_positive_date', 'in_progress_note', 'progress_note_source_id']


def date_bounds(value):
    parts = list(map(int, value.split('-')))
    year = parts[0]
    if len(parts) == 1:
        return dt.date(year, 1, 1), dt.date(year, 12, 31)
    month = parts[1]
    if len(parts) == 2:
        return dt.date(year, month, 1), dt.date(year, month, calendar.monthrange(year, month)[1])
    date = dt.date(*parts)
    return date, date


def aggregate(records):
    groups = defaultdict(list)
    issues = []
    for number, record in enumerate(records, 1):
        output = record.get('output') if isinstance(record, dict) else None
        case_id = record.get('case_id', 'line-' + str(number)) if isinstance(record, dict) else 'line-' + str(number)
        mrn = output.get('mrn') if isinstance(output, dict) else None
        note_id = output.get('note_id') if isinstance(output, dict) else None
        errors = validate_output(output)
        if not isinstance(mrn, str) or not mrn.strip():
            issues.append({'MRN': '', 'reason': 'missing_patient_id', 'case_id': case_id,
                           'note_ids': [note_id] if isinstance(note_id, str) else [], 'details': errors})
            continue
        groups[mrn].append((case_id, output, errors))
    patients = []
    for mrn, items in sorted(groups.items()):
        local_issues = []
        def issue(reason, case='', notes=None, details=None):
            local_issues.append({'MRN': mrn, 'reason': reason, 'case_id': case,
                                 'note_ids': notes or [], 'details': details or []})
        valid = []
        blocked = False
        for case, value, errors in items:
            if errors:
                issue('invalid_output', case, [value['note_id']] if isinstance(value.get('note_id'), str) else [], errors)
                blocked = True
            elif not value['note_id'] or not value['note_id'].strip():
                issue('missing_note_id', case)
                blocked = True
            else:
                valid.append((case, value))
        by_note = defaultdict(list)
        for case, value in valid:
            by_note[value['note_id']].append((case, value))
        unique = []
        for note_id, versions in by_note.items():
            if len({json.dumps(v, sort_keys=True) for _, v in versions}) > 1:
                issue('conflicting_note_versions', notes=[note_id])
                blocked = True
            else:
                unique.append(versions[0])
        target = []
        uncertain = []
        for case, value in unique:
            category = value['derived_classification']
            if category == 'Uncertain':
                uncertain.append(value['note_id'])
            if category != 'Malignant':
                if any(d['primary_origin_site'] in TARGETS and d['primary_origin_certainty']=='confirmed'
                       and d['diagnostic_qualification']=='confirmed'
                       and REFERENCE.get(d['histologic_subtype'])=='Malignant' for d in value['diagnoses']):
                    issue('target_category_conflict', case, [value['note_id']])
                    blocked = True
                continue
            supported = any(d['primary_origin_site'] in TARGETS and d['primary_origin_certainty'] == 'confirmed'
                and REFERENCE.get(d['histologic_subtype']) not in ['Borderline', 'Benign', 'Other', 'Separate precursor']
                for d in value['diagnoses'])
            if not supported:
                issue('target_origin_not_confirmed', case, [value['note_id']])
                blocked = True
                continue
            date = value['diagnosis_date']
            if date and value['note_signed_time']:
                try:
                    signed = dt.date.fromisoformat(value['note_signed_time'][:10])
                    if date_bounds(date)[0] > signed:
                        issue('diagnosis_after_note_date', case, [value['note_id']])
                        blocked = True
                        continue
                except ValueError:
                    issue('unusable_note_timestamp', case, [value['note_id']])
                    blocked = True
                    continue
            target.append(value)
        chosen = None
        source_ids = []
        dates = sorted({v['diagnosis_date'] for v in target if v['diagnosis_date']})
        if dates:
            earliest = min(dates, key=lambda d: date_bounds(d)[0])
            _, first_end = date_bounds(earliest)
            overlap = [d for d in dates if d != earliest and date_bounds(d)[0] <= first_end]
            if overlap:
                issue('overlapping_date_precision', notes=sorted(v['note_id'] for v in target if v['diagnosis_date'] in [earliest] + overlap))
                blocked = True
            elif not blocked:
                chosen = earliest
                source_ids = sorted({v['note_id'] for v in target if v['diagnosis_date'] == chosen})
        if target and not dates:
            issue('no_usable_target_date', notes=sorted({v['note_id'] for v in target}))
        if uncertain:
            issue('unresolved_target_classification', notes=sorted(set(uncertain)))
        if chosen:
            status = 'target_positive'
        elif blocked:
            status = 'needs_review'
        elif target:
            status = 'target_date_unknown'
        elif uncertain:
            status = 'needs_review'
        else:
            status = 'no_established_target_malignancy'
        patients.append({'MRN': mrn, 'progress_note_positive_date': chosen, 'in_progress_note': True,
                         'progress_note_source_id': source_ids, 'status': status,
                         'categories': sorted({v['derived_classification'] for _, v in unique}),
                         'all_note_ids': sorted(by_note), 'target_malignancy_documented': bool(target)})
        issues.extend(local_issues)
    return {'patients': patients, 'issues': issues}


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
    review_rows = [{'MRN': r['MRN'], 'reason': r['reason'], 'progress_note_source_id': r['note_ids']} for r in result['issues']]
    write_csv(directory / 'note_review.csv', ['MRN', 'reason', 'progress_note_source_id'], review_rows)
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
