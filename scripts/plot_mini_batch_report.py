"""Render exportable charts from derived experiment metrics; requires matplotlib."""
import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report_dir', type=Path)
    folder = parser.parse_args().report_dir.resolve()
    s = json.loads((folder / 'summary.json').read_text())
    with (folder / 'per_task.csv').open() as stream:
        tasks = list(csv.DictReader(stream))
    names = {'django/django': 'Django', 'matplotlib/matplotlib': 'Matplotlib', 'sphinx-doc/sphinx': 'Sphinx', 'sympy/sympy': 'SymPy'}
    colors = {'resolved': '#278477', 'unresolved': '#b97043', 'empty_patch': '#a0a8b0'}
    plt.rcParams.update({'font.size': 10, 'axes.spines.top': False, 'axes.spines.right': False, 'svg.fonttype': 'none', 'pdf.fonttype': 42})
    fig, axes = plt.subplots(2, 2, figsize=(14, 9), layout='constrained')
    a = axes[0, 0]
    repos = list(s['domains'])
    for i, repo in enumerate(repos):
        d = s['domains'][repo]
        left = 0
        for outcome in colors:
            value = d['outcomes'].get(outcome, 0) / d['n']
            a.barh(i, value, left=left, color=colors[outcome], label=outcome if i == 0 else None)
            left += value
        a.text(1.02, i, f"{d['outcomes'].get('resolved', 0)}/{d['n']}", va='center')
    a.set(yticks=range(len(repos)), yticklabels=[names[r] for r in repos], xlim=(0, 1), title='A. Official outcomes by repository', xlabel='Share of all tasks (empty patches included)')
    a.xaxis.set_major_formatter(PercentFormatter(1))
    a.invert_yaxis()
    a.legend(loc='lower center', bbox_to_anchor=(.5, -.32), ncol=3, frameon=False)
    a = axes[0, 1]
    start = datetime.fromisoformat(s['started'])
    for i, repo in enumerate(repos):
        for t in tasks:
            if t['repo'] == repo:
                begin = (datetime.fromisoformat(t['started']) - start).total_seconds() / 3600
                a.barh(i, float(t['elapsed_seconds']) / 3600, left=begin, height=.65, color=colors[t['outcome']], edgecolor='white', linewidth=.3)
    a.set(yticks=range(len(repos)), yticklabels=[names[r] for r in repos], title='B. Domain queues (one task at a time per domain)', xlabel='Hours since batch start (UTC)')
    a.invert_yaxis()
    a.grid(axis='x', alpha=.2)
    a = axes[1, 0]
    phases = list(s['phases'])
    x = list(range(len(phases)))
    prompt = [s['phases'][p]['prompt_tokens']/1e6 for p in phases]
    completion = [s['phases'][p]['completion_tokens']/1e6 for p in phases]
    a.bar(x, prompt, label='Input (includes repeated context)', color='#487ca8')
    a.bar(x, completion, bottom=prompt, label='Output (includes reasoning)', color='#e5b052')
    a.set(xticks=x, xticklabels=['Analysis', 'Implementation', 'Review', 'Revision', 'Final review'], title='C. Returned API token usage by phase', ylabel='Million tokens')
    a.tick_params(axis='x', labelrotation=15)
    a.legend(frameon=False, fontsize=9)
    a.grid(axis='y', alpha=.2)
    a = axes[1, 1]
    for outcome in colors:
        selected = [t for t in tasks if t['outcome'] == outcome]
        a.scatter([float(t['total_tokens'])/1e6 for t in selected], [float(t['elapsed_seconds'])/60 for t in selected], color=colors[outcome], label=outcome, alpha=.65, s=25)
    a.set(title='D. Per-task tokens and end-to-end duration', xlabel='Returned total tokens (million)', ylabel='Minutes (generation + evaluation + overhead)')
    a.grid(alpha=.2)
    a.legend(frameon=False)
    fig.suptitle('GPTSwarm + mini-swe-agent | Fixed team | Verified 154-task subset\n' + f"102/154 resolved (66.23%) | {s['overall']['batch_wall_seconds']/3600:.2f} h wall time | {s['overall']['total_tokens']/1e6:.2f}M observed tokens", fontsize=15)
    for ext in ['svg', 'pdf', 'png']:
        fig.savefig(folder / ('experiment_overview.' + ext), dpi=180)
    plt.close(fig)
    (folder / 'plot_mini_batch_report.py').write_bytes(Path(__file__).read_bytes())


if __name__ == '__main__':
    main()
