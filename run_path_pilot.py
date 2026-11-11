#!/usr/bin/env python3
"""PATH pilot without reference labels: preserve predictions for later review."""
import argparse,calendar,datetime as dt,hashlib,json,os,re,time,urllib.error
from pathlib import Path
from collections import Counter
from run_note_pilot import (ROOT,SUBTYPES,CATEGORIES,SetupError,resolve_key,model_base_url,
                            check_live_config,request_completion,make_payload,decode_response,estimate_cost,write_private,load_jsonl)

from cancer_pipeline.formats import validate_path, validate_organ_grouping

def validate_output(value):
    return validate_path(value, TEMPLATE, SUBTYPES)

TEMPLATE=json.loads((ROOT/'schemas/path_output_format.json').read_text())




def should_stop(status,http_status,consecutive_transport_errors):
    return http_status in [401,403,404,429] or consecutive_transport_errors>=3

def summary(records):
    result={}
    for model in sorted({r['model'] for r in records}):
        rs=[r for r in records if r['model']==model]
        result[model]={'attempted':len(rs),'status_counts':dict(Counter(r['status'] for r in rs)),
            'prompt_tokens':sum(r.get('usage',{}).get('prompt_tokens',0) for r in rs),
            'completion_tokens':sum(r.get('usage',{}).get('completion_tokens',0) for r in rs),
            'estimated_usd_known_usage':sum(r.get('estimated_usd') or 0 for r in rs),
            'billing_note':'Configured rates only, not gateway invoice; separate cache-write fees excluded',
            'accuracy_evaluated':False}
    return result

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--config',type=Path,default=ROOT/'config.local.json');ap.add_argument('--inputs',type=Path,default=ROOT/'data/path_inputs_20.jsonl')
    ap.add_argument('--prompt',type=Path,default=ROOT/'prompts/path_v1.5.txt');ap.add_argument('--limit',type=int,default=1)
    ap.add_argument('--models',nargs='+');ap.add_argument('--max-completion-tokens',type=int,default=12000);ap.add_argument('--execute',action='store_true')
    a=ap.parse_args();config=json.loads(a.config.read_text());models=config['models']
    if a.models:
        if not set(a.models)<={m['id'] for m in models}:raise SetupError('Requested model not configured')
        models=[m for m in models if m['id'] in a.models]
    if not 1<=a.limit<=50 or not 256<=a.max_completion_tokens<=16000:raise SetupError('Invalid pilot limit/token cap')
    cases=list(load_jsonl(a.inputs).values())[:a.limit]
    if not cases or not models or len(cases)*len(models)>60:raise SetupError('Pilot requires 1 to 60 requests')
    for c in cases:
        if set(c['input'])!={'metadata','report_text'} or not c['input']['report_text'].strip():raise SetupError('Invalid PATH input')
        if set(c['input']['metadata'])!={'mrn','report_id','collected_datetime'}:raise SetupError('Invalid PATH metadata')
    prompt=a.prompt.read_text()
    print(json.dumps({'mode':'execute' if a.execute else 'dry_run','module':'PATH','models':[m['id'] for m in models],'cases_per_model':len(cases),'planned_requests':len(cases)*len(models),'max_completion_tokens':a.max_completion_tokens,'reference_labels_loaded':False}),flush=True)
    if not a.execute:return
    key=resolve_key(config)
    for m in models:check_live_config(dict(config,base_url=model_base_url(config,m)),key)
    os.umask(0o077);out=ROOT/'runs'/('path-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ'));out.mkdir(parents=True,mode=0o700)
    write_private(out/'manifest.json',{'module':'PATH','prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest(),'inputs_sha256':hashlib.sha256(a.inputs.read_bytes()).hexdigest(),'case_ids':[c['case_id'] for c in cases],'models':[{k:m.get(k) for k in ['id','model_version','reasoning_effort','rates_per_million']} for m in models],'max_completion_tokens':a.max_completion_tokens,'accuracy_evaluated':False,'reference_labels_loaded':False,'input_scope':'Report text and original CSV metadata from supplied input snapshot','automatic_retries':0})
    records=[];consecutive=0
    print('Run directory:',out,flush=True)
    for m in models:
        for c in cases:
            start=time.monotonic();r={'case_id':c['case_id'],'model':m['id']};http=None
            try:
                response,rid=request_completion(model_base_url(config,m),make_payload(m.get('model_version',m['id']),prompt,c['input'],a.max_completion_tokens,m.get('reasoning_effort')),key,gateway=config.get('provider')=='nyu_gateway')
                status,pred=decode_response(response);r.update(status=status,request_id=rid,returned_model=response.get('model'),raw_response=response,usage=response.get('usage') or {})
                consecutive=0
                if status=='parsed':
                    errors=validate_output(pred)
                    if not errors and any(v in a.prompt.name for v in ['v1.3','v1.4','v1.5']):errors+=validate_organ_grouping(pred)
                    r.update(status='invalid_schema' if errors else 'ok',validation_errors=errors,output=pred)
                    r['output_with_source_metadata']=dict(pred,**c['input']['metadata'])
                r['estimated_usd']=estimate_cost(r['usage'],m.get('rates_per_million'))
            except urllib.error.HTTPError as error:
                http=error.code;r.update(status='http_error',http_status=http,estimated_usd=None);error.close();consecutive=0
            except (urllib.error.URLError,TimeoutError,OSError,ValueError,TypeError,KeyError) as error:
                consecutive+=1;r.update(status='request_or_response_error',error_type=type(error).__name__,estimated_usd=None)
            r['elapsed_seconds']=round(time.monotonic()-start,3);records.append(r)
            with (out/'results.jsonl').open('a') as f:f.write(json.dumps(r,ensure_ascii=False)+'\n')
            print(c['case_id'],m['id'],r['status'],flush=True)
            if should_stop(r['status'],http,consecutive):
                write_private(out/'summary.json',summary(records));print('Stopped after authorization/routing/rate-limit or repeated transport failure.',flush=True);return
    write_private(out/'summary.json',summary(records));print('Results:',out,flush=True)

if __name__=='__main__':
    try:main()
    except SetupError as e:raise SystemExit('Setup failed: '+str(e))
    except (ValueError,OSError):raise SystemExit('Setup failed; check local paths/configuration (details withheld).')
