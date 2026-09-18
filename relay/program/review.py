#!/usr/bin/env python3
"""Create a self-contained HTML quality report with image and joint inspection."""
import argparse
import base64
import html
import io
import json
from pathlib import Path
import tarfile
from export_lerobot import read_episode, inspect_episode


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('episode')
    p.add_argument('--output', required=True)
    args=p.parse_args()
    meta,samples=read_episode(args.episode)
    report=inspect_episode(args.episode)
    pictures=[]
    if samples:
        for index in sorted(set([0,len(samples)//2,len(samples)-1])):
            sample=samples[index]
            with tarfile.open(sample['_chunk']) as tar:
                b=tar.extractfile(sample['color_file']).read()
            pictures.append('<figure><img src="data:image/png;base64,'+base64.b64encode(b).decode()+'"><figcaption>Frame '+str(sample['source_frame_index'])+'</figcaption></figure>')
    plot=[]
    colors=['#2563eb','#f97316','#16a34a','#db2777','#7c3aed','#0891b2']
    for j in range(6):
        values=[s[k][j] for s in samples for k in ['state','action'] if s.get(k)]
        lo,hi=(min(values),max(values)) if values else (-1,1)
        span=max(hi-lo,0.02)
        lines=[]
        for key,dash in [('state',''),('action',' stroke-dasharray="5 4"')]:
            points=[]
            for i,s in enumerate(samples):
                if s.get(key) is not None:
                    x=30+i*900/max(len(samples)-1,1); y=140-(s[key][j]-lo)*110/span
                    points.append('%.1f,%.1f'%(x,y))
            lines.append('<polyline fill="none" stroke="'+colors[j]+'" stroke-width="1.5"'+dash+' points="'+' '.join(points)+'"/>')
        plot.append('<h3>Joint '+str(j+1)+' (rad)</h3><svg viewBox="0 0 960 160">'+''.join(lines)+'</svg>')
    body='''<!doctype html><html lang="zh"><meta charset="utf-8"><title>Piper 数据质量检查</title>
<style>body{font:16px system-ui;max-width:1100px;margin:32px auto;color:#172033;padding:16px}pre{white-space:pre-wrap;background:#f1f5f9;padding:16px}section{display:flex;gap:12px}figure{margin:0;flex:1}img,svg{width:100%}svg{background:#f8fafc}h3{margin-bottom:4px}</style>
<h1>Piper 数据质量检查</h1><p>实线：从臂实际反馈；虚线：主臂目标。过期值可能仍可见，训练有效性以质量统计与原始标记为准。</p>'''
    body+='<p>'+html.escape(meta['task'])+'</p><pre>'+html.escape(json.dumps(report,ensure_ascii=False,indent=2))+'</pre>'
    body+='<section>'+''.join(pictures)+'</section>'+''.join(plot)+'</html>'
    Path(args.output).write_text(body,encoding='utf-8')
    print(args.output)


if __name__=='__main__':main()
