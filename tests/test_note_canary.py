import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from run_note_canary import CanaryError, plan, run
from run_note_pilot import TEMPLATE


class NoteCanaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'run'
        self.case = {
            'case_id': 'one', 'source_file': 'fake.csv', 'source_row': 2,
            'input': {'metadata': {'pat_mrn_id': '001', 'note_id': '002',
                                   'note_signed_time': '2020-01-01 12:00:00'},
                      'full_text': 'Synthetic test note'},
        }
        self.model = {'model_version': 'gpt-6-luna/v1.0.0', 'reasoning_effort': 'low',
                      'rates_per_million': {'input': 0.1, 'cached_input': 0.01, 'output': 0.5}}
        self.output = dict(TEMPLATE, mrn='wrong', note_id='wrong', diagnoses=[])
        self.response = {'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps(self.output)}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 15}}

    def make_plan(self, cases=None, prompt='synthetic prompt'):
        return plan(cases or [self.case], prompt, self.model, 3000)

    def test_duplicate_source_identity_is_rejected(self):
        with self.assertRaises(CanaryError):
            self.make_plan([self.case, dict(self.case, case_id='two')])

    def test_budget_gate_sends_zero_requests(self):
        request = Mock()
        with self.assertRaises(CanaryError):
            run(self.make_plan(), self.directory, 0.000001, request)
        request.assert_not_called()

    def test_success_restores_metadata_and_resumes_without_resending(self):
        request = Mock(return_value=(self.response, 'request-1'))
        planned = self.make_plan()
        run(planned, self.directory, 1, request)
        run(planned, self.directory, 1, request)
        request.assert_called_once()
        export = json.loads((self.directory / 'predictions.jsonl').read_text())
        self.assertEqual(export['output']['mrn'], '001')
        self.assertEqual(export['output']['note_id'], '002')
        self.assertEqual(export['source_row'], 2)
        with self.assertRaises(CanaryError):
            run(self.make_plan(prompt='changed'), self.directory, 1, request)
        request.assert_called_once()

    def test_timeout_and_process_interrupt_do_not_replay_or_export(self):
        for error in (TimeoutError, KeyboardInterrupt):
            with self.subTest(error=error):
                directory = self.directory / error.__name__
                self.directory.mkdir(exist_ok=True)
                request = Mock(side_effect=error)
                with self.assertRaises((CanaryError, KeyboardInterrupt)):
                    run(self.make_plan(), directory, 1, request)
                with self.assertRaises(CanaryError):
                    run(self.make_plan(), directory, 1, request)
                request.assert_called_once()
                self.assertFalse((directory / 'predictions.jsonl').exists())

    def test_stops_after_malformed_output_or_missing_usage(self):
        other = dict(self.case, case_id='two', source_row=3, input=dict(
            self.case['input'], metadata=dict(self.case['input']['metadata'], note_id='003')))
        planned = self.make_plan([self.case, other])
        for name, response in (
            ('schema', dict(self.response, choices=[{'finish_reason': 'stop', 'message': {'content': '{}'}}])),
            ('usage', dict(self.response, usage=None)),
            ('reserve', dict(self.response, usage={'prompt_tokens': 99999, 'completion_tokens': 15})),
        ):
            with self.subTest(name=name):
                self.directory.mkdir(exist_ok=True)
                directory = self.directory / name
                request = Mock(return_value=(response, 'request-1'))
                with self.assertRaises(CanaryError):
                    run(planned, directory, 1, request)
                request.assert_called_once()
                self.assertFalse((directory / 'predictions.jsonl').exists())

    def test_recovers_only_null_subtype_qualification_without_replay(self):
        diagnosis = dict(TEMPLATE['diagnoses'][0], histologic_subtype=None,
                         diagnostic_qualification='uncertain')
        original = dict(self.output, diagnoses=[diagnosis])
        response = dict(self.response, choices=[{'finish_reason': 'stop', 'message': {
            'content': json.dumps(original)}}])
        request = Mock(return_value=(response, 'request-1'))
        planned = self.make_plan()
        with patch('run_note_canary.normalize_prediction', side_effect=lambda value: (value, [])):
            with self.assertRaises(CanaryError):
                run(planned, self.directory, 1, request)
        run(planned, self.directory, 1, request)
        request.assert_called_once()
        record = json.loads((self.directory / 'predictions.jsonl').read_text())
        self.assertIsNone(record['output']['diagnoses'][0]['diagnostic_qualification'])
