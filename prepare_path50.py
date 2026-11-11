"""Reproducible local PATH selection; no API or reference labels."""
import csv,hashlib,json,os,random
from pathlib import Path
ROOT=Path(__file__).resolve().parent
if __name__=='__main__':
 os.umask(0o077);csv.field_size_limit(100000000)
 src=Path('/Users/jeremy/Desktop/Ovariant_Cancer_Pathology_Pull.csv')
 with src.open(encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
 selected=random.Random(20261108).sample([(i,r) for i,r in enumerate(rows,1) if r['report_text'].strip() and r['mrn'].strip()],50)
 with (ROOT/'data/path_inputs_50.jsonl').open('x') as f:
  for i,r in selected:f.write(json.dumps({'case_id':'PATH50-'+str(i).zfill(5),'input':{'metadata':{'mrn':r['mrn'],'report_id':r['accession'],'collected_datetime':r['collected_datetime'] or None},'report_text':r['report_text']}},ensure_ascii=False)+'\n')
 (ROOT/'data/path_inputs_50_manifest.json').write_text(json.dumps({'seed':20261108,'source_sha256':hashlib.sha256(src.read_bytes()).hexdigest(),'source_rows':len(rows),'selected_rows':[i for i,r in selected],'selection':'Uniform source rows without replacement; raw reports, no reference labels'},indent=2))
 print('Prepared 50 reports privately.')
