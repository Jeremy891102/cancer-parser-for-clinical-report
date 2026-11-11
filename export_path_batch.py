"""Export saved PATH predictions to compressed batches and traceable CSVs; no API."""
import argparse,csv,gzip,hashlib,json,os
from collections import Counter,defaultdict
from pathlib import Path
from cancer_pipeline.dates import date_bounds
from run_path_pilot import validate_output,validate_organ_grouping

from cancer_pipeline.metadata import normalize_metadata
from cancer_pipeline.aggregation import aggregate_path as aggregate

ROOT=Path(__file__).resolve().parent
FINAL=['MRN','onset_date','source','pathology_positive_date','progress_note_positive_date','icd10_positive_date','in_pathology','in_progress_note','in_icd10','pathology_source_id','progress_note_source_id','icd10_source_id']
EXTRA=['pathology_categories','progress_note_categories','pathology_reason','progress_note_reason','icd10_reason','exclusion_reason']


def restore_metadata(pred,meta):
 """Source identifiers/timestamp are program-owned, never model-generated."""
 return dict(pred,**meta),[k for k,v in meta.items() if pred.get(k)!=v]


def write_csv(path,columns,rows):
 with path.open('x',newline='',encoding='utf-8') as f:
  w=csv.DictWriter(f,fieldnames=columns);w.writeheader()
  for r in rows:w.writerow({k:json.dumps(r.get(k),ensure_ascii=False) if isinstance(r.get(k),list) else r.get(k) for k in columns})

def main():
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--run',type=Path,required=True);ap.add_argument('--inputs',type=Path,required=True);ap.add_argument('--results',type=Path,default=ROOT/'results');a=ap.parse_args()
 os.umask(0o077)
 records=[json.loads(l) for l in (a.run/'results.jsonl').read_text().splitlines()];inputs={r['case_id']:r['input'] for r in map(json.loads,a.inputs.read_text().splitlines())}
 if len(records)!=50 or len({r['case_id'] for r in records})!=50 or set(r['case_id'] for r in records)!=set(inputs):raise ValueError('Expected 50 unique completed request records')
 for d in ['note','path','icd','csv','audit']:(a.results/d).mkdir(parents=True,exist_ok=True,mode=0o700)
 if (a.results/'path/batch_0001.jsonl.gz').exists():raise ValueError('Batch exists; refusing overwrite')
 valid=[];issues=[];invalid=defaultdict(list);record_rows=[];metadata_restorations=[];unlinked=0;invalid_count=0
 for r in records:
  meta=normalize_metadata(inputs[r['case_id']]['metadata']);pred=r.get('output');errors=validate_output(pred) if isinstance(pred,dict) else ['no_parsed_output']
  if not errors:errors+=validate_organ_grouping(pred)
  if not errors:
   pred,restored=restore_metadata(pred,meta)
   if restored:metadata_restorations.append({'case_id':r['case_id'],'fields':restored})
  if not meta['report_id']:errors.append('missing_report_id')
  if errors or r['status']!='ok':
   invalid_count+=1
   issues.append({'case_id':r['case_id'],'report_id':meta['report_id'],'MRN':meta['mrn'],'status':r['status'],'errors':errors});invalid[meta['mrn']].append(meta['report_id']);continue
  if not meta['mrn']:
   unlinked+=1;issues.append({'case_id':r['case_id'],'report_id':meta['report_id'],'MRN':None,'status':'source_metadata_issue','errors':['missing_patient_id']})
  valid.append(pred);record_rows.append(dict(MRN=pred['mrn'],report_id=pred['report_id'],collected_datetime=pred['collected_datetime'],derived_classification=pred['derived_classification'],diagnosis_date=pred['diagnosis_date']))
 with gzip.open(a.results/'path/batch_0001.jsonl.gz','xt',encoding='utf-8') as f:
  for o in valid:f.write(json.dumps(o,ensure_ascii=False,separators=(',',':'))+'\n')
 # Preserve all raw attempts, including invalid output, under the audit directory.
 with gzip.open(a.results/'audit/path_attempts_0001.jsonl.gz','xt',encoding='utf-8') as f:
  for r in records:f.write(json.dumps(r,ensure_ascii=False,separators=(',',':'))+'\n')
 patients=aggregate(valid,invalid)
 write_csv(a.results/'csv/path_record_level.csv',['MRN','report_id','collected_datetime','derived_classification','diagnosis_date'],record_rows)
 write_csv(a.results/'csv/path_patient_level.csv',['MRN','pathology_positive_date','pathology_source_id','categories','pathology_reason'],patients)
 write_csv(a.results/'csv/path_review.csv',['MRN','report_id','case_id','status','errors'],issues)
 for name,fields in [('note_record_level.csv',['MRN','note_id','note_signed_time','derived_classification','diagnosis_date']),('note_patient_level.csv',['MRN','progress_note_positive_date','progress_note_source_id','categories','progress_note_reason']),('icd_record_level.csv',['MRN','icd10_source_id','icd10_code','icd10_date']),('icd_patient_level.csv',['MRN','icd10_positive_date','icd10_source_id','icd10_reason']),('target_cohort.csv',FINAL),('exclusion_cohort.csv',FINAL+EXTRA)]:write_csv(a.results/'csv'/name,fields,[])
 manifest=dict(stage='PATH-only pilot; final cohort merge NOT performed',model='gpt-6.1-sol',accuracy_evaluated=False,clinical_review_status='predictions_awaiting_review',rates_verified_with_gateway=False,prompt='path_v1.5',prompt_sha256=hashlib.sha256((ROOT/'prompts/path_v1.5.txt').read_bytes()).hexdigest(),source_run=str(a.run.resolve()),input_snapshot=str(a.inputs.resolve()),inputs_sha256=hashlib.sha256(a.inputs.read_bytes()).hexdigest(),attempted=len(records),valid_outputs=len(valid),invalid_outputs=invalid_count,unlinked_reports=unlinked,record_review_issues=len(issues),patients=len(patients),metadata_restorations=metadata_restorations,classification_counts=dict(Counter(o['derived_classification'] for o in valid)),patient_review_counts=dict(Counter(reason for p in patients for reason in p['pathology_reason'])),estimated_usd=sum(r.get('estimated_usd') or 0 for r in records),note_status='not_run',icd_status='not_run',final_cohorts_status='header_only_not_merged',merge_date_policy='Earliest eligible NOTE/PATH date; ICD date only when neither NOTE nor PATH has an eligible date; unresolved evidence requires review')
 (a.results/'audit/path_prompt_v1.5.txt').write_text((ROOT/'prompts/path_v1.5.txt').read_text())
 selection=a.inputs.with_name(a.inputs.stem+'_manifest.json')
 if selection.exists():(a.results/'audit/selection_manifest.json').write_text(selection.read_text())
 (a.results/'manifest.json').write_text(json.dumps(manifest,indent=2))
 (a.results/'README.txt').write_text('PATH-only pilot. path/batch_0001.jsonl.gz contains valid fixed-schema report outputs, one per line. Source missing-value sentinels become null. Reports without MRN remain in record-level outputs and path_review.csv but are excluded from patient aggregation.\nNOTE and ICD folders are reserved, not processed. Their CSVs and final cohort CSVs contain headers only; empty files do NOT mean no cases.\npath_patient_level.csv is a provisional source-level aggregation, not a final clinically reviewed cohort. Review flags and invalid attempts are retained.\nIdentifiers and collection timestamps are copied by program from source metadata; model echoes remain in audit and restoration fields are logged in manifest.\nThe original source reports remain in the input snapshot/source CSV; they are not duplicated in the main batch. All raw model attempts remain in audit/.\nFinal merge policy: NOTE/PATH dates take priority; ICD is fallback only.\n')
 print(json.dumps(manifest,indent=2))
if __name__=='__main__':main()
