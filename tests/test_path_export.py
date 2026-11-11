import unittest
from export_path_batch import aggregate,restore_metadata,normalize_metadata

def output(date,category='Malignant',rid='1'):
 return dict(mrn='001',report_id=rid,diagnosis_date=date,derived_classification=category)
class ExportTests(unittest.TestCase):
 def test_missing_source_mrn_is_not_a_patient(self):
  meta=normalize_metadata(dict(mrn='null',report_id='A',collected_datetime=None))
  self.assertIsNone(meta['mrn'])
  self.assertEqual(aggregate([dict(output('2001'),mrn=None)],{}),[])
 def test_source_metadata_is_authoritative(self):
  original=dict(mrn=None,report_id='wrong',collected_datetime=None,diagnosis_date='2001')
  value,fields=restore_metadata(original,dict(mrn='001',report_id='A',collected_datetime='2020-01-01'))
  self.assertEqual(value['mrn'],'001');self.assertEqual(value['diagnosis_date'],'2001');self.assertIsNone(original['mrn']);self.assertEqual(len(fields),3)
 def test_history_date_wins_and_ids_trace(self):
  r=aggregate([output('2001'),output('2021',rid='2'),output('1990','Other','3')],{})[0]
  self.assertEqual(r['pathology_positive_date'],'2001');self.assertEqual(r['pathology_source_id'],['1'])
 def test_uncertain_event_not_target_date(self):
  r=aggregate([output('2014-09-15','Uncertain')],{})[0]
  self.assertIsNone(r['pathology_positive_date']);self.assertIn('unresolved_target_classification',r['pathology_reason'])
 def test_undated_report_does_not_veto_supported_date(self):
  r=aggregate([output(None),output('2020',rid='2')],{})[0]
  self.assertEqual(r['pathology_positive_date'],'2020')
  self.assertEqual(r['pathology_source_id'],['2'])
  self.assertNotIn('target_date_unknown',r['pathology_reason'])
 def test_all_undated_still_unknown(self):
  r=aggregate([output(None)],{})[0]
  self.assertIsNone(r['pathology_positive_date'])
  self.assertIn('target_date_unknown',r['pathology_reason'])
 def test_overlap_still_needs_review(self):
  self.assertIsNone(aggregate([output('2020'),output('2020-02',rid='2')],{})[0]['pathology_positive_date'])
 def test_reviewed_precision_can_select_supported_full_date(self):
  r=aggregate([output('2020'),output('2020-02-03',rid='2')],{}, {'001':{'diagnosis_date':'2020-02-03','source_ids':['2']}})[0]
  self.assertEqual(r['pathology_positive_date'],'2020-02-03')
  self.assertEqual(r['pathology_source_id'],['2'])
  self.assertNotIn('overlapping_date_precision',r['pathology_reason'])
 def test_adjudication_cannot_invent_or_contradict_date(self):
  with self.assertRaises(ValueError):aggregate([output('2020')],{}, {'001':{'diagnosis_date':'2020-02-03','source_ids':['1']}})
  with self.assertRaises(ValueError):aggregate([output('2019'),output('2020',rid='2')],{}, {'001':{'diagnosis_date':'2020','source_ids':['2']}})
 def test_invalid_patient_preserved(self):
  r=aggregate([],{'001':['bad']})[0];self.assertEqual(r['pathology_source_id'],['bad']);self.assertIn('invalid_output',r['pathology_reason'])
if __name__=='__main__':unittest.main()
