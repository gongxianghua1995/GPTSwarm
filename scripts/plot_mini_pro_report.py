"""Plot the derived Pro report; no raw trajectories required. Requires matplotlib."""
import argparse
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.ticker import PercentFormatter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report_dir', type=Path)
    folder = parser.parse_args().report_dir.resolve()
    s = json.loads((folder / 'summary.json').read_text())
    with (folder / 'per_attempt.csv').open() as stream:
        attempts = list(csv.DictReader(stream))
    tasks = [r for r in attempts if r['selected'] == 'True']
    names = {'ansible/ansible':'Ansible', 'flipt-io/flipt':'Flipt',
             'internetarchive/openlibrary':'OpenLibrary', 'protonmail/webclients':'Webclients'}
    colors = {'resolved':'#278477', 'unresolved':'#c17b4e', 'discarded':'#aab2bd', 'interrupted':'#a8658e'}
    plt.rcParams.update({'font.size':10, 'axes.spines.top':False, 'axes.spines.right':False,
                         'svg.fonttype':'none', 'pdf.fonttype':42})
    fig, axes = plt.subplots(3, 2, figsize=(15, 13), layout='constrained')
    repos = list(s['domains'])
    a = axes[0,0]
    for i,repo in enumerate(repos):
        d = s['domains'][repo]
        left = 0
        for outcome in ['resolved','unresolved']:
            value = d['outcomes'].get(outcome,0) / d['n']
            a.barh(i,value,left=left,color=colors[outcome],label=outcome if i==0 else None)
            left += value
        a.text(1.01,i,f"{d['outcomes'].get('resolved',0)}/{d['n']}",va='center')
    a.set(yticks=range(4),yticklabels=[names[r] for r in repos], xlim=(0,1.13),
          title='A. Final outcomes after infrastructure retry replacement',xlabel='Share of selected tasks')
    a.xaxis.set_major_formatter(PercentFormatter(1))
    a.set_xticks([0,.25,.5,.75,1])
    a.invert_yaxis()
    a.legend(frameon=False,loc='lower center',bbox_to_anchor=(.5,-.31),ncol=2)
    a = axes[0,1]
    t=s['timing']; start=datetime.fromisoformat(t['started'])
    hour=lambda date:(datetime.fromisoformat(date)-start).total_seconds()/3600
    for i,repo in enumerate(repos):
        for row in attempts:
            if row['repo'] != repo:continue
            color=colors[row['outcome']] if row['selected']=='True' else colors['interrupted' if row['interrupted']=='True' else 'discarded']
            a.barh(i,float(row['elapsed_seconds'])/3600,left=hour(row['started']),height=.65,color=color,edgecolor='white',linewidth=.15)
    a.axvspan(hour(t['pause_started']),hour(t['retry_resumed_started']),color='#e8dcbf',alpha=.6,zorder=0)
    a.set(yticks=range(4),yticklabels=[names[r] for r in repos], title='B. All 308 attempts; shaded interval = budget pause',xlabel='Hours since 2026-09-20 04:21 UTC')
    a.invert_yaxis();a.grid(axis='x',alpha=.2)
    a.legend(handles=[Patch(color=colors['discarded'],label='Superseded original'),Patch(color=colors['interrupted'],label='Interrupted retry')],frameon=False,loc='lower center',bbox_to_anchor=(.5,-.31),ncol=2,fontsize=9)
    a=axes[1,0]; phases=list(s['phases']); x=range(5)
    prompt=[s['phases'][p]['prompt_tokens']/1e6 for p in phases]
    completion=[s['phases'][p]['completion_tokens']/1e6 for p in phases]
    a.bar(x,prompt,color='#487ca8',label='Input (includes repeated context)')
    a.bar(x,completion,bottom=prompt,color='#e5b052',label='Output (includes reasoning)')
    a.set(xticks=x,xticklabels=['Analysis','Implement','Review','Revision','Final review'],ylabel='Million tokens',title='C. Selected attempts: observed tokens by phase')
    a.legend(frameon=False,fontsize=9);a.grid(axis='y',alpha=.2)
    a=axes[1,1]
    for outcome in ['resolved','unresolved']:
        rows=[r for r in tasks if r['outcome']==outcome]
        a.scatter([float(r['total_tokens'])/1e6 for r in rows],[float(r['elapsed_seconds'])/60 for r in rows],s=22,alpha=.6,color=colors[outcome],label=outcome)
    a.set(title='D. Selected attempts: tokens vs. end-to-end duration',xlabel='Returned total tokens (million)',ylabel='Minutes (generation + eval + overhead)')
    a.grid(alpha=.2);a.legend(frameon=False)
    a=axes[2,0]
    groups=['selected','superseded_original','interrupted_retry']; labels=['Selected (216)','Superseded (89)','Interrupted (3)']
    for i,g in enumerate(groups):
        val=s['scopes'][g]['total_tokens']/1e6
        a.barh(i,val,color=['#487ca8',colors['discarded'],colors['interrupted']][i])
        a.text(val+2,i,f'{val:.3f}M',va='center')
    a.set(yticks=range(3),yticklabels=labels,xlim=(0,315),xlabel='Million returned tokens',title='E. Full cost ledger: 271.467M observed tokens')
    a.invert_yaxis();a.grid(axis='x',alpha=.2)
    a=axes[2,1];w=.35
    a.bar([v-w/2 for v in x],[s['phases'][p]['started'] for p in phases],w,label='Started',color='#487ca8')
    a.bar([v+w/2 for v in x],[s['phases'][p]['exits'].get('Submitted',0) for p in phases],w,label='Submitted',color='#278477')
    a.set(xticks=x,xticklabels=['Analysis','Implement','Review','Revision','Final review'],ylabel='Tasks (out of 216)',title='F. Selected attempts: role phase completion',ylim=(0,240))
    a.legend(frameon=False);a.grid(axis='y',alpha=.2)
    o=s['scopes']['selected']
    fig.suptitle('GPTSwarm + mini-swe-agent | Fixed team | Pro local test subset\n'+f"{o['outcomes']['resolved']}/{o['n']} resolved ({o['outcomes']['resolved']/o['n']:.2%}) | {t['campaign_wall_seconds']/3600:.2f} h elapsed | {o['total_tokens']/1e6:.2f}M selected tokens",fontsize=15)
    for ext in ['png','svg','pdf']:
        fig.savefig(folder/('experiment_overview.'+ext),dpi=180)
    plt.close(fig)
    (folder/Path(__file__).name).write_bytes(Path(__file__).read_bytes())
    files={p.name:{'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'bytes':p.stat().st_size}
           for p in sorted(folder.iterdir()) if p.is_file() and p.name!='artifact_manifest.json'}
    (folder/'artifact_manifest.json').write_text(json.dumps(dict(schema_version=1,
        description='Derived report artifacts only; excludes this manifest itself.',files=files),indent=2)+'\n')
    print(f'Wrote PNG/SVG/PDF and {len(files)}-file artifact manifest: {folder}')


if __name__=='__main__':
    main()
