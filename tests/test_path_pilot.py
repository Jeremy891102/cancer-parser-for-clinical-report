import copy,json,unittest
from unittest.mock import patch
import run_path_pilot as app

class PathTests(unittest.TestCase):
    def value(self):
        return dict(mrn='001',report_id='A',collected_datetime='2022-01-01',diagnosis_date=None,
                    history_of_ovarian_cancer_documented=True,has_target_site_specimen=False,
                    specimens=[],diagnoses=[],clinical_history='History of ovarian cancer',derived_classification='Malignant')
    def test_history_positive_can_have_no_current_diagnoses_or_date(self):
        self.assertEqual(app.validate_output(self.value()),[])
    def test_fixed_schema_and_invalid_date(self):
        v=self.value();v['diagnosis_date']='2023-02-29';self.assertTrue(app.validate_output(v))
        v=self.value();del v['clinical_history'];self.assertTrue(app.validate_output(v))
    def test_links_and_numeric_types(self):
        v=self.value();s={k:None for k in app.TEMPLATE['specimens'][0]};s.update(specimen_id='1',sampled_site='ovary')
        d={k:([] if isinstance(t,list) else None) for k,t in app.TEMPLATE['diagnoses'][0].items()};d.update(diagnosis_id='1',specimen_ids=['2'])
        v.update(specimens=[s],diagnoses=[d]);self.assertTrue(app.validate_output(v))
        d['specimen_ids']=['1'];self.assertEqual(app.validate_output(v),[])
        d['tumor_size_mm']=[True];self.assertTrue(app.validate_output(v))
    def test_organ_grouping_rejects_named_ids_and_per_bullet_diagnoses(self):
        v=self.value()
        s={k:None for k in app.TEMPLATE['specimens'][0]};s.update(specimen_id='B.1',source_specimen_label='B',sampled_site='uterus')
        d={k:([] if isinstance(t,list) else None) for k,t in app.TEMPLATE['diagnoses'][0].items()};d.update(diagnosis_id='B.1',specimen_ids=['B.1'])
        v.update(specimens=[s],diagnoses=[d]);self.assertEqual(app.validate_organ_grouping(v),[])
        v['diagnoses'].append(dict(d,diagnosis_id='B.2'));self.assertTrue(app.validate_organ_grouping(v))
        v['diagnoses']=[dict(d,diagnosis_id='D1')];self.assertTrue(app.validate_organ_grouping(v))
        v['diagnoses']=[];s['specimen_id']='B.uterus';self.assertTrue(app.validate_organ_grouping(v))

    def test_network_failure_stops_after_three(self):
        self.assertTrue(app.should_stop('request_or_response_error',None,3))
        self.assertFalse(app.should_stop('request_or_response_error',None,2))
        self.assertTrue(app.should_stop('http_error',401,0))
        self.assertTrue(app.should_stop('http_error',404,0))

if __name__=='__main__':unittest.main()
