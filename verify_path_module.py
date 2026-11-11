"""Offline verification; optional wait for the module PID, no API requests."""
import argparse,csv,gzip,json,os,time
from pathlib import Path
from run_path_pilot import validate_output,validate_organ_grouping
from run_path_module import active_journal
ROOT=Path(__file__).resolve().parent

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--wait-pid',type=int);a=ap.parse_args()
 if a.wait_pid:
  while True:
   try:os.kill(a.wait_pid,0)
   except ProcessLookupError:break
   time.sleep(20)
 os.umask(0o077);root=ROOT/'results';manifest=json.loads((root/'manifest.json').read_text());rows=[]
 for file in sorted((root/'path').glob('batch_*.jsonl.gz')):
  with gzip.open(file,'rt',encoding='utf-8') as f:rows.extend(json.loads(x) for x in f)
 assert len(rows)==manifest['valid_outputs']
 assert all(not validate_output(x) and not validate_organ_grouping(x) for x in rows)
 with (root/'csv/path_record_level.csv').open(newline='') as f:csvrows=list(csv.DictReader(f))
 assert len(csvrows)==len(rows)
 with (root/'csv/path_patient_level.csv').open(newline='') as f:patients=list(csv.DictReader(f))
 assert len(patients)==len({p['MRN'] for p in patients})
 assert all(p['MRN'] and p['MRN'].lower() not in ['null','nan','none'] for p in patients)
 with active_journal(root).open() as f:attempts=[json.loads(x) for x in f]
 assert len(attempts)==len({r['case_id'] for r in attempts})==manifest['finished_records']
 if manifest['state'] in ['complete','complete_with_review']:assert manifest['finished_records']==manifest['total_source_records']
 report=dict(state=manifest['state'],source_records=manifest['total_source_records'],finished_records=len(attempts),valid_schema_outputs=len(rows),patient_rows=len(patients),unlinked_reports=sum(o['mrn'] is None for o in rows),batch_files=len(list((root/'path').glob('batch_*.jsonl.gz'))),main_batch_bytes=sum(p.stat().st_size for p in (root/'path').glob('batch_*.jsonl.gz')),checks_passed=True,accuracy_evaluated=False)
 (root/'verification.json').write_text(json.dumps(report,indent=2));print(json.dumps(report),flush=True)
if __name__=='__main__':main()
