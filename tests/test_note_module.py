import csv
import json
from pathlib import Path
import tempfile
import sqlite3
import unittest
from unittest.mock import Mock

from run_note_module import export, infer, process, setup
from run_note_pilot import TEMPLATE


class NoteModuleTests(unittest.TestCase):
    def test_resume_preserves_completed_and_blank_rows(self):
        db = sqlite3.connect(':memory:')
        setup(db)
        model = {'rates_per_million': {'input': .1, 'cached_input': .01, 'output': .5}}
        m = {'pat_mrn_id': '001', 'note_id': '002', 'note_signed_time': '2020-01-01'}
        cases = [{'case_id': 'one', 'source_file': 'fake.csv', 'source_row': 1,
                  'input': {'metadata': m, 'full_text': 'synthetic'}},
                 {'case_id': 'two', 'source_file': 'fake.csv', 'source_row': 2,
                  'input': {'metadata': dict(m, note_id='003'), 'full_text': ''}}]
        response = {'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps(dict(TEMPLATE, diagnoses=[]))}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 12}}
        send = Mock(return_value=(response, 'req'))
        worker = lambda case, payload, limit: process(case, payload, limit, model['rates_per_million'], send)
        self.assertEqual(infer(db, iter(cases), 'prompt', model['rates_per_million'], 2, 1,
                               worker, lambda *a: None), 'complete')
        self.assertEqual(infer(db, iter(cases), 'prompt', model['rates_per_million'], 2, 1,
                               worker, lambda *a: None), 'complete')
        send.assert_called_once()
        self.assertEqual(dict(db.execute('SELECT status, COUNT(*) FROM notes GROUP BY status')),
                         {'ok': 1, 'empty_text': 1})

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'note').mkdir()
            (root / 'csv').mkdir()
            self.assertEqual(export(db, root), {'Uncertain': 1})
            with (root / 'csv/note_record_level.csv').open() as stream:
                records = list(csv.DictReader(stream))
            self.assertEqual([r['status'] for r in records], ['ok', 'empty_text'])
            with (root / 'csv/note_patient_level.csv').open() as stream:
                patient = next(csv.DictReader(stream))
            self.assertEqual(patient['MRN'], '001')
            self.assertEqual(patient['status'], 'needs_review')
            self.assertIn('invalid_output', patient['progress_note_reason'])

    def test_existing_inflight_prevents_replay(self):
        db = sqlite3.connect(':memory:')
        setup(db)
        db.execute("INSERT INTO notes (position, status) VALUES (1, 'inflight')")
        worker = Mock()
        self.assertEqual(infer(db, iter(()), '', {}, 1, 1, worker, Mock()),
                         'paused_reconciliation')
        worker.assert_not_called()

    def test_shards_have_disjoint_sources_and_cover_all_expected_rows(self):
        from run_note_module import EXPECTED, assigned_sources
        files = [Path(f'progress_notes_batch_{n:02d}_of_12.csv') for n in range(12)]
        local, local_count = assigned_sources(files, 'local')
        remote, remote_count = assigned_sources(files, 'server')
        self.assertFalse(set(local) & set(remote))
        self.assertEqual(local + remote, files)
        self.assertEqual(local_count + remote_count, EXPECTED)

    def test_timeout_is_held_and_next_note_runs_without_replay(self):
        db = sqlite3.connect(':memory:')
        setup(db)
        rates = {'input': .1, 'cached_input': .01, 'output': .5}
        cases = [{'case_id': str(n), 'source_file': 'fake.csv', 'source_row': n,
                  'input': {'metadata': {'pat_mrn_id': '001', 'note_id': str(n),
                                         'note_signed_time': '2020-01-01'}, 'full_text': 'synthetic'}}
                 for n in (1, 2)]
        response = {'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps(dict(TEMPLATE, diagnoses=[]))}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 12}}
        send = Mock(side_effect=[TimeoutError(), (response, 'req-2')])
        worker = lambda case, payload, limit: process(case, payload, limit, rates, send)
        self.assertEqual(infer(db, iter(cases), 'prompt', rates, 1, 1, worker, Mock()), 'complete')
        self.assertEqual(infer(db, iter(cases), 'prompt', rates, 1, 1, worker, Mock()), 'complete')
        self.assertEqual(send.call_count, 2)
        self.assertGreater(db.execute("SELECT reserved_usd FROM notes WHERE position=1").fetchone()[0], 0)

    def test_http_500_is_held_but_auth_failure_stops(self):
        import io
        import urllib.error
        rates = {'input': .1, 'cached_input': .01, 'output': .5}
        cases = [{'case_id': str(n), 'source_file': 'fake.csv', 'source_row': n,
                  'input': {'metadata': {'pat_mrn_id': '001', 'note_id': str(n),
                                         'note_signed_time': '2020-01-01'}, 'full_text': 'synthetic'}}
                 for n in (1, 2)]
        response = {'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps(dict(TEMPLATE, diagnoses=[]))}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 12}}
        for code, expected, calls in ((500, 'complete', 2), (401, 'paused_reconciliation', 1)):
            with self.subTest(code=code):
                db = sqlite3.connect(':memory:')
                setup(db)
                send = Mock(side_effect=[urllib.error.HTTPError('https://example.invalid', code,
                                                               'test', {}, io.BytesIO()),
                                         (response, 'req-2')])
                worker = lambda case, payload, limit: process(case, payload, limit, rates, send)
                self.assertEqual(infer(db, iter(cases), 'prompt', rates, 1, 1, worker, Mock()), expected)
                self.assertEqual(send.call_count, calls)
                if code == 500:
                    self.assertEqual(db.execute('SELECT status FROM notes WHERE position=1').fetchone()[0],
                                     'gateway_server_error')
                    self.assertGreater(db.execute('SELECT reserved_usd FROM notes WHERE position=1').fetchone()[0], 0)

    def test_fast_request_refills_slot_before_slow_request_finishes(self):
        import threading
        db = sqlite3.connect(':memory:')
        setup(db)
        rates = {'input': .1, 'cached_input': .01, 'output': .5}
        cases = [{'case_id': str(n), 'source_file': 'fake.csv', 'source_row': n,
                  'input': {'metadata': {'pat_mrn_id': '001', 'note_id': str(n),
                                         'note_signed_time': '2020-01-01'}, 'full_text': 'synthetic'}}
                 for n in (1, 2, 3)]
        first_started = threading.Event()
        third_started = threading.Event()
        refill_before_first_finished = []

        def worker(case, payload, limit):
            if case['source_row'] == 1:
                first_started.set()
                refill_before_first_finished.append(third_started.wait(2))
            elif case['source_row'] == 2:
                first_started.wait(2)
            else:
                third_started.set()
            return 'ok', {'estimated_usd': 0}

        self.assertEqual(infer(db, iter(cases), 'prompt', rates, 1, 2, worker, Mock()), 'complete')
        self.assertEqual(refill_before_first_finished, [True])
        self.assertEqual(db.execute("SELECT COUNT(*) FROM notes WHERE status='ok'").fetchone()[0], 3)

    def test_budget_counts_completed_and_outstanding_requests(self):
        db = sqlite3.connect(':memory:')
        setup(db)
        rates = {'input': .1, 'cached_input': .01, 'output': .5}
        cases = [{'case_id': str(n), 'source_file': 'fake.csv', 'source_row': n,
                  'input': {'metadata': {'pat_mrn_id': '001', 'note_id': str(n),
                                         'note_signed_time': '2020-01-01'}, 'full_text': 'synthetic'}}
                 for n in (1, 2, 3)]
        from run_note_module import CAP, MODEL, make_payload
        payload = make_payload(MODEL, 'prompt', cases[0]['input'], CAP, 'low')
        limit = len(json.dumps(payload, ensure_ascii=False).encode()) + 1024
        quote = (limit * rates['input'] + CAP * rates['output']) / 1_000_000
        worker = Mock(return_value=('ok', {'estimated_usd': quote}))
        self.assertEqual(infer(db, iter(cases), 'prompt', rates, 2.1 * quote, 2, worker, Mock()),
                         'paused_budget')
        self.assertEqual(worker.call_count, 2)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM notes').fetchone()[0], 2)

    def test_rate_limit_waits_then_continues_without_replaying_note(self):
        import io
        import urllib.error
        from unittest.mock import patch
        db = sqlite3.connect(':memory:')
        setup(db)
        rates = {'input': .1, 'cached_input': .01, 'output': .5}
        cases = [{'case_id': str(n), 'source_file': 'fake.csv', 'source_row': n,
                  'input': {'metadata': {'pat_mrn_id': '001', 'note_id': str(n),
                                         'note_signed_time': '2020-01-01'}, 'full_text': 'synthetic'}}
                 for n in (1, 2)]
        response = {'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps(dict(TEMPLATE, diagnoses=[]))}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 12}}
        send = Mock(side_effect=[urllib.error.HTTPError('https://example.invalid', 429,
                                                      'test', {'Retry-After': '90'}, io.BytesIO()),
                                 (response, 'req-2')])
        worker = lambda case, payload, limit: process(case, payload, limit, rates, send)
        with patch('run_note_module.STOP.wait', return_value=False) as pause:
            self.assertEqual(infer(db, iter(cases), 'prompt', rates, 1, 1, worker, Mock()), 'complete')
            pause.assert_called_once()
            self.assertGreater(pause.call_args[0][0], 89)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(db.execute('SELECT status FROM notes WHERE position=1').fetchone()[0],
                         'rate_limited')
