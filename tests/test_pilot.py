import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import run_note_pilot as app


def output(category='Malignant', date='2008'):
    return dict(mrn='001', note_id='N1', note_signed_time='2024-01-01', diagnosis_date=date,
                family_history_of_ovarian_cancer_documented=False,
                diagnoses=[dict(diagnosis_as_documented='ovarian cancer',
                  histologic_subtype='Carcinoma / cancer, subtype unspecified',
                  diagnostic_qualification='confirmed', primary_origin_site='ovary',
                  primary_origin_certainty='confirmed', temporal_status='history_only')],
                derived_classification=category)


class PilotTests(unittest.TestCase):
    def test_dates_follow_category_but_calendar_stays_valid(self):
        value=output('Borderline','2024-10-21')
        self.assertEqual(app.validate_output(value), [])
        value['diagnosis_date']='2023-02-29'
        self.assertTrue(app.validate_output(value))
        value=output(); value['diagnoses'][0]['diagnosis_date']='2001'
        self.assertTrue(app.validate_output(value))

    def test_missing_field_and_unknown_subtype_rejected(self):
        value=output(); del value['diagnosis_date']
        self.assertTrue(app.validate_output(value))
        value=output(); value['diagnoses'][0]['histologic_subtype']='made up'
        self.assertTrue(app.validate_output(value))

    def test_diagnosis_order_does_not_affect_score(self):
        gold=output(); gold['diagnoses'].append(dict(gold['diagnoses'][0],primary_origin_site='breast'))
        pred=copy.deepcopy(gold); pred['diagnoses'].reverse()
        self.assertTrue(app.compare(pred,gold)['normalized_diagnoses_exact'])
        pred['diagnoses'][0]['primary_origin_site']='lung'
        self.assertFalse(app.compare(pred,gold)['normalized_diagnoses_exact'])

    def test_cost_does_not_double_count_reasoning(self):
        usage={'prompt_tokens':1000,'completion_tokens':500,
               'prompt_tokens_details':{'cached_tokens':100},
               'completion_tokens_details':{'reasoning_tokens':300}}
        rate={'input':1,'cached_input':0.1,'output':5}
        self.assertAlmostEqual(app.estimate_cost(usage,rate),0.00341)
        self.assertIsNone(app.estimate_cost({},rate))

    def test_request_does_not_contain_answers(self):
        case={'metadata':{'pat_mrn_id':'001','note_id':'N1','note_signed_time':'2024-01-01'},'full_text':'ovarian cancer'}
        payload=app.make_payload('gpt-5.4-mini','instructions JSON',case,3000,'low')
        self.assertEqual(json.loads(payload['messages'][1]['content']),case)
        self.assertFalse(payload['store'])
        self.assertNotIn('expected_output',json.dumps(payload))
        self.assertNotIn('temperature',payload)

    def test_live_execution_requires_explicit_data_configuration(self):
        with self.assertRaises(ValueError):
            app.check_live_config({'base_url':'https://api.openai.com/v1','data_approved_for_endpoint':False},'test-key')
        with self.assertRaises(ValueError):
            app.check_live_config({'base_url':'http://example.com/v1','data_approved_for_endpoint':True},'test-key')

    def test_empty_choices_and_truncation_are_not_success(self):
        self.assertEqual(app.decode_response({'choices':[]})[0],'invalid_response')
        value={'choices':[{'message':{'content':json.dumps(output())},'finish_reason':'length'}]}
        self.assertEqual(app.decode_response(value)[0],'truncated')

    def test_config_errors_do_not_disclose_secret(self):
        config={'base_url':'https://example.com/v1?token=secret-value','data_approved_for_endpoint':True}
        with self.assertRaises(ValueError) as caught: app.check_live_config(config,'secret-value')
        self.assertNotIn('secret-value',str(caught.exception))


class ExecutionTests(unittest.TestCase):
    def test_mocked_run_preserves_usage_and_never_writes_key(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            config={'base_url':'https://example.test/v1','api_key':'TEST_ONLY_NOT_A_REAL_KEY',
                    'data_approved_for_endpoint':True,'max_completion_tokens':3000,
                    'models':[{'id':'test-model','reasoning_effort':'low','rates_per_million':{'input':1,'cached_input':.1,'output':5}}]}
            (root/'config.json').write_text(json.dumps(config))
            case={'case_id':'SYNTHETIC001','input':{'metadata':{'pat_mrn_id':'001','note_id':'N1','note_signed_time':'2024-01-01'},'full_text':'ovarian cancer'}}
            (root/'input.jsonl').write_text(json.dumps(case)+'\n')
            (root/'labels.jsonl').write_text(json.dumps({'case_id':case['case_id'],'output':output()})+'\n')
            prompt=root/'prompt.txt';prompt.write_text('Extract JSON.')
            response={'model':'test-model-snapshot','choices':[{'message':{'content':json.dumps(output())},'finish_reason':'stop'}],
                      'usage':{'prompt_tokens':1000,'completion_tokens':500}}
            argv=['pilot','--config',str(root/'config.json'),'--inputs',str(root/'input.jsonl'),
                  '--labels',str(root/'labels.jsonl'),'--prompt',str(prompt),'--execute']
            with patch.object(app,'ROOT',root),patch('sys.argv',argv),patch.dict(app.os.environ,{'OPENAI_API_KEY':''}),\
                 patch.object(app,'request_completion',return_value=(response,'synthetic-request')) as request,\
                 contextlib.redirect_stdout(io.StringIO()):
                app.main()
            self.assertEqual(request.call_count,1)
            run=next((root/'runs').iterdir())
            result=json.loads((run/'results.jsonl').read_text())
            self.assertEqual(result['status'],'ok')
            self.assertEqual(result['usage']['completion_tokens'],500)
            summary=json.loads((run/'summary.json').read_text())
            self.assertEqual(summary['test-model']['target_recall'],1)
            for file in run.iterdir():self.assertNotIn(config['api_key'],file.read_text())

if __name__=='__main__': unittest.main()
