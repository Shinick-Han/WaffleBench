"""Posthoc warm CPU profile on frozen normal calibration images only. No test scoring or refit."""
import argparse,hashlib,json,time,statistics
from pathlib import Path
import numpy as np
from inspection_images import core
from inspection_images_v2 import scoring
from inspection_images_v3 import pipeline,cli

def main():
 p=argparse.ArgumentParser();p.add_argument('--build-dir',required=True);p.add_argument('--root',required=True);p.add_argument('--out',required=True);a=p.parse_args()
 paths=cli._paths(a.build_dir,cli.DEFAULT_V1_BUILD);protocol=pipeline.load_protocol()
 start=time.perf_counter();ex=cli.extractors(paths,protocol);setup=time.perf_counter()-start
 freeze=pipeline.verify_freeze(a.root,paths['visa'],paths['split_csv'],ex)
 ids=freeze['splits']['calibration'][:16]
 if len(ids)!=16 or set(ids)&set(freeze['splits']['test_unlabeled']):raise RuntimeError('profile identity boundary')
 result={}
 for name,fs in protocol['feature_sets'].items():
  with np.load(Path(a.root)/freeze['memory'][name]['artifact']) as z:
   mean,std,bank=z['mean'],z['std'],z['bank']
  method='baseline224' if name=='r224' else 'primary448';cfg=protocol['methods'][method]
  timings=[];feature=[];distance=[];diff=[]
  for trial in range(7):
   t=time.perf_counter();x=ex[name].extract([paths['visa']/i for i in ids]);t1=time.perf_counter()
   x=core.normalize(x,mean,std);d=scoring.patch_distances(x,bank,protocol['distance_chunk']);scores=scoring.aggregate(d,cfg['aggregation'],cfg['top_k']);t2=time.perf_counter()
   mismatch=max(abs(float(s)-freeze['calibration']['scores'][method][i]) for i,s in zip(ids,scores));diff.append(mismatch)
   if mismatch>1e-6:raise RuntimeError(f'frozen calibration parity failed {method}: {mismatch}')
   if trial:timings.append(t2-t);feature.append(t1-t);distance.append(t2-t1)
  result[method]={'images_per_batch':len(ids),'measured_batches':len(timings),'total_batch_seconds':timings,'warm_batch_median_ms':statistics.median(timings)*1000,'warm_batch_p95_ms':float(np.percentile(timings,95))*1000,'amortized_ms_per_image':statistics.median(timings)*1000/len(ids),'feature_batch_median_ms':statistics.median(feature)*1000,'distance_and_aggregation_batch_median_ms':statistics.median(distance)*1000,'bank_array_bytes':bank.nbytes,'memory_artifact_bytes':(Path(a.root)/freeze['memory'][name]['artifact']).stat().st_size,'frozen_calibration_max_abs_score_difference':max(diff)}
 receipt={'schema_version':1,'scope':'posthoc_calibration_only_cpu_software_profile','freeze_digest':freeze['freeze_digest'],'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'model_setup_seconds':setup,'calibration_ids':ids,'test_images_scored':0,'thresholds_changed':False,'methods':result,'primary_over_baseline_warm_batch_ratio':result['primary448']['warm_batch_median_ms']/result['baseline224']['warm_batch_median_ms'],'limitations':['One workstation, CPU ResNet18 with frozen 4-thread extractor; 16 fixed normal calibration images, one warmup plus six measured batches.','Amortized per-image time is batch total divided by 16, not single-image latency or p95 per-image. Batch p95 has six samples only.','Includes image loading, preprocessing, feature extraction, normalization, exact nearest distances and aggregation; excludes model setup, freeze verification and equipment time.','Posthoc descriptive timing. No test image is scored, no parameter/threshold/model is refit or selected, no factory throughput claim.']}
 Path(a.out).write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8',newline='\n');print(json.dumps({'ok':True,'ratio':receipt['primary_over_baseline_warm_batch_ratio'],'methods':result}))
if __name__=='__main__':main()
