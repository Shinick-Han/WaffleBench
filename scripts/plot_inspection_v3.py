"""Standalone scientific figure from the audited evidence, no new experiment."""
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1] / 'evidence/inspection-improvements-v3'
s = json.loads((ROOT / 'summary.json').read_text())
c = json.loads((ROOT / 'classification-aggregate.json').read_text())
a = json.loads((ROOT / 'audit.json').read_text())
p = json.loads((ROOT / 'inference-profile.json').read_text())
if a['status'] != 'passed':
    raise ValueError('Figure requires audited evidence')
green, gray, ink = '#276449', '#87938c', '#202b28'
plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'axes.spines.top':False,'axes.spines.right':False,'axes.labelcolor':ink,'text.color':ink,'axes.edgecolor':'#c7d0c8','figure.facecolor':'#f3f5f0','axes.facecolor':'#f3f5f0'})
fig, axes = plt.subplots(1, 3, figsize=(15.5, 5), gridspec_kw={'width_ratios':[1.15, 1, 1]})
q = s['combined_vs_logistic']
axes[0].bar([0,1], [q['mean_comparator'],q['mean_policy']], color=[gray,green], width=.55)
axes[0].set(xticks=[0,1],xticklabels=['Logistic\nprobability / cost','CB400\ntwo-step route'],ylim=(0,34),ylabel='Mean confirmed DOI per lot',title='Equal inspection budget: 360 CU')
for x,v in enumerate([q['mean_comparator'],q['mean_policy']]):
    axes[0].text(x,v+.4,f'{v:.2f}',ha='center',weight='bold')
axes[0].text(.5,31.8,f'+{100*q["relative_gain"]:.2f}%',ha='center',weight='bold',color=green)
axes[0].text(.5,-.29,'Paired difference +1.95\n95% CI [1.46, 2.43]; 100 lots',transform=axes[0].transAxes,ha='center',va='top',fontsize=9)
for j,(key,label,color) in enumerate([('logistic/identity','Logistic',gray),('cb400/identity','CB400',green)]):
    v=c[key]['aggregate']
    axes[1].bar([-.18+j*.36,.82+j*.36],[v['precision'],v['recall_among_candidates']],color=color,width=.34,label=label)
axes[1].set(xticks=[0,1],xticklabels=['Precision','Recall'],ylim=(0,1),ylabel='Fraction',title='Original optical candidates only')
axes[1].legend(frameon=False,loc='upper right')
axes[1].text(.5,-.29,'78,323 candidate sites; 19,477 DOI\nNot all-site physical recall',transform=axes[1].transAxes,ha='center',va='top',fontsize=9)
values=[p['baseline']['median_ms'],p['compiled']['median_ms']]
axes[2].bar([0,1],values,color=[gray,green],width=.55)
axes[2].set(xticks=[0,1],xticklabels=['Original\nmodel API','Compiled\npredictor'],ylim=(0,12),ylabel='Median wall time (ms)',title='Same CB400 model: warm inference')
for x,v in enumerate(values):
    axes[2].text(x,v+.2,f'{v:.2f} ms',ha='center',weight='bold')
axes[2].text(.5,-.29,f'3,915 sites; 100 repetitions per block\nCold compilation excluded ({p["compile_ms"]:.0f} ms)',transform=axes[2].transAxes,ha='center',va='top',fontsize=9)
fig.suptitle('Falsify Lab · Inspection v3',x=.04,ha='left',fontsize=18,weight='bold')
fig.text(.04,.885,'Authored numerical synthetic benchmark · equipment CU, classifier quality and CPU time are separate',fontsize=10)
fig.text(.04,.025,'Preselected primary candidate; independent paired-lot bootstrap. Cost-saving target of 30% was not met. No factory/SEM performance claim.',fontsize=9)
fig.subplots_adjust(left=.06,right=.975,top=.77,bottom=.3,wspace=.42)
for suffix in ['png','svg']:
    fig.savefig(ROOT / ('summary.'+suffix),dpi=200)
print(str(ROOT / 'summary.png'))
