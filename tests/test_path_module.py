import tempfile,unittest,json,gzip
from pathlib import Path
from unittest.mock import patch
import run_path_module as app
from run_path_module import read_cases,digest,save_json,save_csv,save_gz,reusable_pilot
class ModuleTests(unittest.TestCase):
 def test_source_rows_are_not_deduplicated(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'in.csv';p.write_text('mrn,accession,collected_datetime,report_text\n001,A,2020,report\n001,A,2020,report\nnull,B,,report\n')
   c=read_cases(p);self.assertEqual(len(c),3);self.assertNotEqual(c[0]['case_id'],c[1]['case_id']);self.assertEqual(c[0]['input']['metadata']['mrn'],'001')
 def test_atomic_outputs_roundtrip(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t);save_json(p/'a.json',{'a':1});save_json(p/'a.json',{'a':2});self.assertEqual(json.loads((p/'a.json').read_text()),{'a':2})
   save_gz(p/'b.gz',[{'mrn':None}]);self.assertEqual(json.loads(gzip.open(p/'b.gz','rt').read()),{'mrn':None})
   save_csv(p/'c.csv',['MRN','ids'],[dict(MRN='001',ids=['A'])]);self.assertIn('001',(p/'c.csv').read_text());self.assertFalse(list(p.glob('*.tmp')))
 def test_execute_lifecycle_with_fake_transport(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);(root/'prompts').mkdir();(root/'prompts/path_v1.5.txt').write_text('prompt')
   source=root/'source.csv';source.write_text('mrn,accession,collected_datetime,report_text\n001,A,2020,report\n002,B,2021,report\n')
   (root/'config.local.json').write_text(json.dumps({'models':[{'id':'gpt-6.1-sol'}]}))
   def fake(c,*args):return dict(case_id=c['case_id'],source_row=c['source_row'],input_sha256=app.digest(c['input']),status='ok',output=dict(c['input']['metadata'],derived_classification='Benign',diagnosis_date=None),estimated_usd=0)
   with patch.object(app,'ROOT',root),patch.object(app,'reusable_pilot',return_value={}),patch.object(app,'check_live_config'),patch.object(app,'resolve_key',return_value='test'),patch.object(app,'model_base_url',return_value='https://example.com'),patch.object(app,'process',side_effect=fake),patch('sys.argv',['module','--source',str(source),'--workers','1','--execute']):app.main()
   manifest=json.loads((root/'results/manifest.json').read_text());self.assertEqual(manifest['state'],'complete');self.assertEqual(manifest['valid_outputs'],2)
   self.assertTrue((root/'results/path/batch_0001.jsonl.gz').exists())
   fix=root/'results/audit/id_fix.json';fix.write_text(json.dumps({'corrections':[{'MRN':'001','old':'A','new':'CLEAN-A'}]}))
   manifest['derived_output_corrections']=['audit/id_fix.json'];(root/'results/manifest.json').write_text(json.dumps(manifest))
   records={r['case_id']:r for r in map(json.loads,(root/'results/audit/path_all_attempts.jsonl').read_text().splitlines())}
   app.export(root/'results',app.read_cases(source),records,manifest,'complete')
   with gzip.open(root/'results/path/batch_0001.jsonl.gz','rt') as f:outputs=[json.loads(l) for l in f]
   self.assertEqual(outputs[0]['report_id'],'CLEAN-A');self.assertEqual(records['PATH-00001']['output']['report_id'],'A')
 def test_digest_ignores_key_order_not_report_content(self):
  self.assertEqual(digest(dict(a=1,b=2)),digest(dict(b=2,a=1)));self.assertNotEqual(digest({'text':'x'}),digest({'text':'y'}))
if __name__=='__main__':unittest.main()

class RetrySelectionTests(unittest.TestCase):
 def test_effective_journal_keeps_original_attempts(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);(root/'audit').mkdir();original=root/'audit/path_all_attempts.jsonl';original.write_text('original\n')
   self.assertEqual(app.active_journal(root),original)
   (root/'manifest.json').write_text(json.dumps({'active_attempts_file':'audit/path_selected_attempts.jsonl'}))
   selected=root/'audit/path_selected_attempts.jsonl';selected.write_text('replacement\n')
   self.assertEqual(app.active_journal(root),selected)
   self.assertEqual(original.read_text(),'original\n')
