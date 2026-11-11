"""Resumable local PATH-only module. Never loads reference labels."""
import argparse,concurrent.futures as cf,csv,datetime as dt,gzip,hashlib,json,os,signal,threading,time,urllib.error
from collections import Counter,defaultdict
from pathlib import Path
from run_note_pilot import resolve_key,model_base_url,check_live_config,request_completion,make_payload,decode_response,estimate_cost
from run_path_pilot import validate_output,validate_organ_grouping
from export_path_batch import normalize_metadata,restore_metadata,aggregate,FINAL,EXTRA

from cancer_pipeline.storage import atomic, save_csv, save_gz, save_json

ROOT=Path(__file__).resolve().parent
STOP=threading.Event()
def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
def read_cases(source):
 csv.field_size_limit(100000000)
 with source.open(encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
 cases=[]
 for i,r in enumerate(rows,1):
  cases.append({'case_id':f'PATH-{i:05d}','source_row':i,'input':{'metadata':{'mrn':r['mrn'],'report_id':r['accession'],'collected_datetime':r['collected_datetime'] or None},'report_text':r['report_text']}})
 return cases

def active_journal(root):
 manifest=json.loads((root/'manifest.json').read_text()) if (root/'manifest.json').exists() else {}
 return root/manifest.get('active_attempts_file','audit/path_all_attempts.jsonl')


def reusable_pilot(cases,prompt_hash,model):
 run=ROOT/'runs/path-20261108T201458.086987Z';manifest=json.loads((run/'manifest.json').read_text())
 if manifest['prompt_sha256']!=prompt_hash or manifest['max_completion_tokens']!=12000 or manifest['models']!=[{k:model.get(k) for k in ['id','model_version','reasoning_effort','rates_per_million']}]:raise ValueError('Pilot settings differ; cannot reuse')
 inputs={r['case_id']:r['input'] for r in map(json.loads,(ROOT/'data/path_inputs_50.jsonl').read_text().splitlines())}
 result={}
 for old in map(json.loads,(run/'results.jsonl').read_text().splitlines()):
  index=int(old['case_id'].split('-')[-1])-1;case=cases[index]
  if digest(inputs[old['case_id']])!=digest(case['input']):raise ValueError('Pilot input does not match source row')
  if old['status']!='ok' or validate_output(old['output']) or validate_organ_grouping(old['output']):continue
  r=dict(old,case_id=case['case_id'],source_row=case['source_row'],input_sha256=digest(case['input']),reused_from=str(run),original_case_id=old['case_id'])
  result[case['case_id']]=r
 return result

def process(case,config,model,prompt,key):
 start=time.monotonic();r={'case_id':case['case_id'],'source_row':case['source_row'],'input_sha256':digest(case['input']),'model':model['id']}
 if not case['input']['report_text'].strip():return dict(r,status='empty_report',elapsed_seconds=0)
 try:
  response,rid=request_completion(model_base_url(config,model),make_payload(model.get('model_version',model['id']),prompt,case['input'],12000,model.get('reasoning_effort')),key,gateway=config.get('provider')=='nyu_gateway')
  status,pred=decode_response(response);r.update(status=status,request_id=rid,returned_model=response.get('model'),raw_response=response,usage=response.get('usage') or {})
  if status=='parsed':
   errors=validate_output(pred)
   if not errors:errors+=validate_organ_grouping(pred)
   r.update(status='invalid_schema' if errors else 'ok',validation_errors=errors,output=pred)
  r['estimated_usd']=estimate_cost(r['usage'],model.get('rates_per_million'))
 except urllib.error.HTTPError as error:
  r.update(status='http_error',http_status=error.code);error.close()
  if error.code in [401,403,404,429]:STOP.set()
 except (urllib.error.URLError,TimeoutError,OSError,ValueError,TypeError,KeyError) as error:r.update(status='request_or_response_error',error_type=type(error).__name__)
 r['elapsed_seconds']=round(time.monotonic()-start,3)
 return r

def export(root,cases,records,manifest,state):
 # Preserve user-approved derived ID fixes on any offline re-export/resume.
 previous=json.loads((root/'manifest.json').read_text()) if (root/'manifest.json').exists() else {}
 correction_files=previous.get('derived_output_corrections',[]);id_fixes={}
 for name in correction_files:
  for fix in json.loads((root/name).read_text())['corrections']:id_fixes[fix['MRN'],fix['old']]=fix['new']
 manifest=dict(manifest,derived_output_corrections=correction_files)
 for key in ['active_attempts_file','retry_runs','superseded_attempt_cost_usd']:
  if key in previous:manifest[key]=previous[key]
 outputs=[];invalid=defaultdict(list);issues=[];recordrows=[];source_map=[];by_batch=defaultdict(list);unlinked=0
 for c in cases:
  r=records.get(c['case_id']);meta=normalize_metadata(c['input']['metadata'])
  meta['report_id']=id_fixes.get((meta['mrn'],meta['report_id']),meta['report_id'])
  if r is None:continue
  if r['status']!='ok':
   invalid[meta['mrn']].append(meta['report_id']);issues.append(dict(MRN=meta['mrn'],report_id=meta['report_id'],case_id=c['case_id'],status=r['status'],errors=r.get('validation_errors',[])));continue
  o,_=restore_metadata(r['output'],meta);outputs.append(o);by_batch[(c['source_row']-1)//500+1].append(o)
  source_map.append(dict(case_id=c['case_id'],source_row=c['source_row'],report_id=meta['report_id'],MRN=meta['mrn'],batch=f"batch_{(c['source_row']-1)//500+1:04d}.jsonl.gz"))
  recordrows.append(dict(MRN=o['mrn'],report_id=o['report_id'],collected_datetime=o['collected_datetime'],derived_classification=o['derived_classification'],diagnosis_date=o['diagnosis_date']))
  if not o['mrn']:unlinked+=1;issues.append(dict(MRN=None,report_id=o['report_id'],case_id=c['case_id'],status='source_metadata_issue',errors=['missing_patient_id']))
 for b,values in by_batch.items():save_gz(root/'path'/f'batch_{b:04d}.jsonl.gz',values)
 # Include pending source records as blocked evidence in provisional patient aggregation.
 for c in cases:
  if c['case_id'] not in records:
   m=normalize_metadata(c['input']['metadata']);invalid[m['mrn']].append(m['report_id'])
 date_adjudications={}
 for name in previous.get('patient_date_adjudications',[]):
  date_adjudications.update(json.loads((root/name).read_text())['adjudications'])
 manifest=dict(manifest,patient_date_adjudications=previous.get('patient_date_adjudications',[]))
 patients=aggregate(outputs,invalid,date_adjudications)
 save_csv(root/'csv/path_record_level.csv',['MRN','report_id','collected_datetime','derived_classification','diagnosis_date'],recordrows)
 save_csv(root/'csv/path_patient_level.csv',['MRN','pathology_positive_date','pathology_source_id','categories','pathology_reason'],patients)
 save_csv(root/'csv/path_review.csv',['MRN','report_id','case_id','status','errors'],issues)
 save_csv(root/'audit/path_source_index.csv',['case_id','source_row','report_id','MRN','batch'],source_map)
 summary=dict(manifest,state=state,total_source_records=len(cases),finished_records=len(records),valid_outputs=len(outputs),unlinked_reports=unlinked,patients=len(patients),status_counts=dict(Counter(r['status'] for r in records.values())),classification_counts=dict(Counter(o['derived_classification'] for o in outputs)),patient_review_counts=dict(Counter(reason for p in patients for reason in p['pathology_reason'])),estimated_total_usd=sum(r.get('estimated_usd') or 0 for r in records.values())+manifest.get('superseded_attempt_cost_usd',0),estimated_new_usd=sum(r.get('estimated_usd') or 0 for r in records.values() if not r.get('reused_from'))+manifest.get('superseded_attempt_cost_usd',0),updated_utc=dt.datetime.now(dt.timezone.utc).isoformat(),accuracy_evaluated=False,final_cohorts_status='not_merged',note_status='not_run',icd_status='not_run')
 save_json(root/'manifest.json',summary)
 print(json.dumps({k:summary[k] for k in ['state','finished_records','valid_outputs','estimated_new_usd','status_counts']}),flush=True)
 return summary

def main():
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--source',type=Path,default=Path('/Users/jeremy/Desktop/Ovariant_Cancer_Pathology_Pull.csv'));ap.add_argument('--workers',type=int,default=8);ap.add_argument('--execute',action='store_true');a=ap.parse_args()
 if not 1<=a.workers<=16:raise ValueError('workers must be 1..16')
 os.umask(0o077);source_hash=hashlib.sha256(a.source.read_bytes()).hexdigest();cases=read_cases(a.source);prompt_path=ROOT/'prompts/path_v1.5.txt';prompt=prompt_path.read_text();ph=hashlib.sha256(prompt.encode()).hexdigest()
 config=json.loads((ROOT/'config.local.json').read_text());model=next(m for m in config['models'] if m['id']=='gpt-6.1-sol');key=resolve_key(config)
 reused=reusable_pilot(cases,ph,model)
 print(json.dumps({'module':'PATH','source_records':len(cases),'reusable_pilot_records':len(reused),'new_requests':len(cases)-len(reused),'workers':a.workers,'mode':'execute' if a.execute else 'dry_run'}),flush=True)
 if not a.execute:return
 check_live_config(dict(config,base_url=model_base_url(config,model)),key)
 lock_path=ROOT/'runs/path_module.lock';lock_path.parent.mkdir(exist_ok=True)
 import fcntl
 with lock_path.open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  root=ROOT/'results';journal=active_journal(root)
  if root.exists() and not journal.exists():
   archive=ROOT/'runs'/('path50-export-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ'));root.rename(archive);print('Pilot export preserved:',archive.name,flush=True)
  for d in ['note','path','icd','csv','audit']:(root/d).mkdir(parents=True,exist_ok=True,mode=0o700)
  fingerprint=dict(source_sha256=source_hash,prompt_sha256=ph,model={k:model.get(k) for k in ['id','model_version','reasoning_effort','rates_per_million']},max_completion_tokens=12000)
  contract=root/'audit/run_contract.json'
  if contract.exists():
   if json.loads(contract.read_text())!=fingerprint:raise ValueError('Source/prompt/model changed; refusing to mix runs')
  else:save_json(contract,fingerprint)
  records={}
  if journal.exists():
   for line in journal.read_text().splitlines():
    r=json.loads(line);records[r['case_id']]=r
  else:
   records.update(reused)
   with journal.open('a') as f:
    for r in reused.values():f.write(json.dumps(r,ensure_ascii=False)+'\n')
    f.flush();os.fsync(f.fileno())
  for c in cases:
   if c['case_id'] in records and records[c['case_id']]['input_sha256']!=digest(c['input']):raise ValueError('Resume input mismatch')
  manifest=dict(module='PATH',prompt='v1.5',model_id='gpt-6.1-sol',source_file=str(a.source),**fingerprint,workers=a.workers,batch_source_rows=500,reused_pilot_count=len(reused),automatic_retries=0,rates_verified_with_gateway=False,merge_date_policy='Use earliest eligible NOTE/PATH date; ICD date only when both absent; unresolved evidence needs review')
  (root/'audit/path_prompt_v1.5.txt').write_text(prompt)
  (root/'README.txt').write_text('Full PATH module. Batch files contain valid report-level JSON objects. CSVs are provisional during execution; check manifest state and completion count. Each batch covers up to 500 source rows. audit/path_source_index.csv maps outputs to source rows. Raw attempts persist in audit/path_all_attempts.jsonl; restart with the same command to resume without repeating recorded requests.\nNOTE/ICD have NOT run. Their CSVs and final target/exclusion CSVs are header-only, NOT completed empty cohorts. Missing-MRN reports remain report-level and in path_review.csv but do not become a synthetic patient. Predictions require review; format validity is not accuracy.\n')
  for name,fields in [('note_record_level.csv',['MRN','note_id','note_signed_time','derived_classification','diagnosis_date']),('note_patient_level.csv',['MRN','progress_note_positive_date','progress_note_source_id','categories','progress_note_reason']),('icd_record_level.csv',['MRN','icd10_source_id','icd10_code','icd10_date']),('icd_patient_level.csv',['MRN','icd10_positive_date','icd10_source_id','icd10_reason']),('target_cohort.csv',FINAL),('exclusion_cohort.csv',FINAL+EXTRA)]:
   if not (root/'csv'/name).exists():save_csv(root/'csv'/name,fields,[])
  signal.signal(signal.SIGTERM,lambda *_:STOP.set());signal.signal(signal.SIGINT,lambda *_:STOP.set())
  export(root,cases,records,manifest,'running');pending=iter(c for c in cases if c['case_id'] not in records);since_checkpoint=0;errors=0;last_checkpoint=time.monotonic()
  with cf.ThreadPoolExecutor(max_workers=a.workers) as pool, journal.open('a') as f:
   active={}
   def submit():
    if STOP.is_set():return
    c=next(pending,None)
    if c:active[pool.submit(process,c,config,model,prompt,key)]=c['case_id']
   for _ in range(a.workers):submit()
   while active:
    done,_=cf.wait(active,timeout=10,return_when=cf.FIRST_COMPLETED)
    for future in done:
     active.pop(future);r=future.result();records[r['case_id']]=r;f.write(json.dumps(r,ensure_ascii=False)+'\n');f.flush();os.fsync(f.fileno());since_checkpoint+=1
     errors=errors+1 if r['status']=='request_or_response_error' else 0
     if errors>=3:STOP.set()
     print(r['case_id'],r['status'],f'{len(records)}/{len(cases)}',flush=True);submit()
    if since_checkpoint>=100 or time.monotonic()-last_checkpoint>=60:
     export(root,cases,records,manifest,'stopping' if STOP.is_set() else 'running');since_checkpoint=0;last_checkpoint=time.monotonic()
  complete=len(records)==len(cases);failed=any(r['status']!='ok' for r in records.values())
  export(root,cases,records,manifest,'complete_with_review' if complete and failed else 'complete' if complete else 'paused')
if __name__=='__main__':main()
