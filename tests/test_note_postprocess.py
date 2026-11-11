import copy
import unittest
import postprocess_notes as app
from test_pilot import output


def record(case, mrn, note, date, category='Malignant', origin='ovary', temporal='history_only'):
    value=output(category,date)
    value.update(mrn=mrn,note_id=note,note_signed_time='2026-11-01')
    value['diagnoses'][0].update(primary_origin_site=origin,temporal_status=temporal)
    if category=='Benign':value['diagnoses'][0]['histologic_subtype']='Serous cystadenoma'
    if category=='Borderline':value['diagnoses'][0]['histologic_subtype']='Serous borderline tumor'
    return {'case_id':case,'output':value}


class PostprocessTests(unittest.TestCase):
    def test_earliest_date_and_source_not_input_order(self):
        result=app.aggregate([record('a','0001','N2','2021'),record('b','0001','N1','2001')])
        patient=result['patients'][0]
        self.assertEqual(patient['progress_note_positive_date'],'2001')
        self.assertEqual(patient['progress_note_source_id'],['N1'])
        self.assertEqual(patient['MRN'],'0001')

    def test_benign_or_ned_does_not_erase_target_history(self):
        result=app.aggregate([record('a','1','N1','2010',temporal='remission_or_ned'),
                              record('b','1','N2','2005','Benign')])
        self.assertEqual(result['patients'][0]['progress_note_positive_date'],'2010')

    def test_non_target_cancer_date_does_not_become_target_onset(self):
        result=app.aggregate([record('a','1','N1','1996','Other','breast'),record('b','1','N2','2001')])
        self.assertEqual(result['patients'][0]['progress_note_positive_date'],'2001')

    def test_unknown_date_is_not_negative(self):
        result=app.aggregate([record('a','1','N1',None)])
        self.assertEqual(result['patients'][0]['status'],'target_date_unknown')
        self.assertEqual(result['issues'][0]['reason'],'no_usable_target_date')

    def test_same_date_retains_multiple_note_ids(self):
        result=app.aggregate([record('a','1','N1','2001'),record('b','1','N2','2001')])
        self.assertEqual(result['patients'][0]['progress_note_source_id'],['N1','N2'])

    def test_overlapping_partial_dates_require_review(self):
        result=app.aggregate([record('a','1','N1','2001'),record('b','1','N2','2001-06-02')])
        self.assertIsNone(result['patients'][0]['progress_note_positive_date'])
        self.assertIn('overlapping_date_precision',[r['reason'] for r in result['issues']])

    def test_disjoint_partial_dates_can_be_ordered(self):
        result=app.aggregate([record('a','1','N1','2001-06'),record('b','1','N2','2002')])
        self.assertEqual(result['patients'][0]['progress_note_positive_date'],'2001-06')

    def test_conflicting_same_note_id_is_not_silently_chosen(self):
        result=app.aggregate([record('a','1','N1','2001'),record('b','1','N1','2002')])
        self.assertIsNone(result['patients'][0]['progress_note_positive_date'])
        self.assertIn('conflicting_note_versions',[r['reason'] for r in result['issues']])

    def test_malignant_without_target_origin_requires_review(self):
        result=app.aggregate([record('a','1','N1','2001',origin='breast')])
        self.assertIsNone(result['patients'][0]['progress_note_positive_date'])
        self.assertIn('target_origin_not_confirmed',[r['reason'] for r in result['issues']])

    def test_invalid_date_blocks_patient_not_silently_dropped(self):
        value=record('a','1','N1','2023-02-29')
        result=app.aggregate([value])
        self.assertEqual(result['patients'][0]['status'],'needs_review')
        self.assertIn('invalid_output',[r['reason'] for r in result['issues']])

    def test_confirmed_target_histology_with_benign_category_is_flagged(self):
        value=record('a','1','N1','2001')
        value['output']['derived_classification']='Benign'
        result=app.aggregate([value])
        self.assertEqual(result['patients'][0]['status'],'needs_review')
        self.assertIn('target_category_conflict',[r['reason'] for r in result['issues']])

    def test_future_diagnosis_is_not_used(self):
        result=app.aggregate([record('a','1','N1','2030-01-01')])
        self.assertIsNone(result['patients'][0]['progress_note_positive_date'])
        self.assertIn('diagnosis_after_note_date',[r['reason'] for r in result['issues']])

    def test_missing_patient_id_is_reported(self):
        result=app.aggregate([record('a',None,'N1','2001')])
        self.assertEqual(result['patients'],[])
        self.assertEqual(result['issues'][0]['reason'],'missing_patient_id')

    def test_uncertain_without_target_finding_is_not_review(self):
        note = record('a', '1', 'N1', None, 'Uncertain', 'breast')
        note['output']['family_history_of_ovarian_cancer_documented'] = True
        self.assertEqual(app.aggregate([note])['patients'][0]['status'],
                         'no_established_target_malignancy')

    def test_unresolved_target_finding_is_review(self):
        note = record('a', '1', 'N1', None, 'Uncertain')
        note['output']['diagnoses'][0].update(
            diagnosis_as_documented='Ovarian cyst with septations',
            histologic_subtype=None, diagnostic_qualification=None,
            temporal_status='current_disease')
        self.assertEqual(app.aggregate([note])['patients'][0]['status'], 'needs_review')

    def test_unresolved_mullerian_origin_is_review(self):
        note = record('a', '1', 'N1', None, 'Uncertain', 'mullerian_unspecified')
        self.assertEqual(app.aggregate([note])['patients'][0]['status'], 'needs_review')

if __name__=='__main__':unittest.main()
