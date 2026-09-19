#!/usr/bin/env python3
"""Verify raw chunks and export valid continuous segments via official LeRobot v3."""
import argparse
import collections
import io
import json
from pathlib import Path
import shutil
import tarfile

from core import JOINT_NAMES, atomic_json, sha256


def read_episode(path, verify=True):
    path = Path(path)
    meta = json.loads((path / 'manifest.json').read_text())
    if meta['status'] == 'recording':
        raise ValueError('Episode is still recording')
    samples = []
    for chunk in meta['chunks']:
        source = path / chunk['file']
        if verify and (source.stat().st_size != chunk['bytes'] or sha256(source) != chunk['sha256']):
            raise ValueError('Corrupt/missing chunk: ' + str(source))
        with tarfile.open(source) as tar:
            for line in tar.extractfile('samples.jsonl'):
                sample = json.loads(line)
                sample['_chunk'] = str(source)
                samples.append(sample)
    if len(samples) != meta['frames']:
        raise ValueError('Manifest frame count mismatch')
    return meta, samples


def continuous_segments(samples, fps, min_frames=15):
    """Split invalid samples/gaps; never close a time gap by concatenating data.

    One source frame per grid point. Only small jitter (< 0.45 frame) is allowed.
    Raw timestamps and source indices remain available in the result dataset.
    """
    segment = []
    period = 1e9 / fps
    for sample in samples:
        valid = sample['valid'] and sample.get('state') is not None and sample.get('action') is not None
        if valid and segment:
            expected = segment[0]['t_ns'] + len(segment) * period
            valid_continuation = (sample['source_frame_index'] == segment[-1]['source_frame_index'] + 1
                                  and sample['t_ns'] > segment[-1]['t_ns']
                                  and abs(sample['t_ns'] - expected) <= period * 0.45)
        else:
            valid_continuation = True
        if not valid or not valid_continuation:
            if len(segment) >= min_frames:
                yield segment
            segment = []
        if valid:
            segment.append(sample)
    if len(segment) >= min_frames:
        yield segment


def inspect_episode(path):
    meta, samples = read_episode(path)
    deltas = [(b['t_ns'] - a['t_ns']) / 1e6 for a, b in zip(samples, samples[1:])]
    reasons = collections.Counter(r for s in samples for r in s['reasons'])
    ages = {key: [(s['t_ns'] - min(s[key + '_component_t_ns'])) / 1e6 for s in samples if len(s[key + '_component_t_ns']) == 4] for key in ['state','action']}
    return {'id': meta['id'], 'status': meta['status'], 'frames': len(samples),
            'valid_frames': sum(s['valid'] for s in samples), 'invalid_reasons': dict(reasons),
            'raw_can_frames': meta['raw_can_frames'],
            'duration_s': (samples[-1]['t_ns']-samples[0]['t_ns'])/1e9 if len(samples)>1 else 0,
            'max_frame_interval_ms': max(deltas, default=None),
            'max_color_depth_delta_ms': max((s['color_depth_delta_ms'] for s in samples), default=None),
            'max_component_age_ms': {k: max(v, default=None) for k,v in ages.items()},
            'training_segments': [len(s) for s in continuous_segments(samples, meta['config']['fps'])]}


def export(paths, output, repo_id, include_failed=False, min_frames=15):
    import numpy as np
    from PIL import Image
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    output = Path(output).expanduser().resolve()
    if output.exists():
        raise ValueError('Output already exists; choose a new output directory')
    prepared = []
    skipped = []
    for path in paths:
        meta, samples = read_episode(path)
        if meta['config'].get('allow_invalid_diagnostic'):
            skipped.append({'id':meta['id'], 'reason':'diagnostic_episode'})
            continue
        if meta.get('discarded') or meta['status'] != 'complete' or (meta['success'] is not True and not include_failed):
            skipped.append({'id':meta['id'], 'reason':'not_successfully_completed'})
            continue
        segments = list(continuous_segments(samples, meta['config']['fps'], min_frames))
        prepared.extend((meta, segment) for segment in segments)
    if not prepared:
        raise ValueError('No valid continuous training segments; diagnostics/missing action are never fabricated')
    meta0 = prepared[0][0]
    w, h, fps = (meta0['camera'][k] for k in ['width', 'height', 'fps'])
    for meta, _ in prepared:
        if [meta['camera'][k] for k in ['width','height','fps']] != [w,h,fps]:
            raise ValueError('All episodes must have the same resolution and FPS')
    features = {
        'observation.state': {'dtype':'float32','shape':(7,),'names':JOINT_NAMES},
        'action': {'dtype':'float32','shape':(7,),'names':JOINT_NAMES},
        'observation.images.front': {'dtype':'video','shape':(h,w,3),'names':['height','width','channels']},
        'source.frame_index': {'dtype':'int64','shape':(1,),'names':None},
        'source.monotonic_ns': {'dtype':'int64','shape':(1,),'names':None},
        'sync.color_depth_ms': {'dtype':'float32','shape':(1,),'names':None},
    }
    ds = LeRobotDataset.create(repo_id=repo_id, root=output, fps=fps, robot_type='piper_shared_can',
                              features=features, vcodec='h264', video_backend='pyav', image_writer_threads=2)
    attachments = output / 'attachments'
    attachments.mkdir()
    episode_map = []
    try:
        for output_ep, (meta, segment) in enumerate(prepared):
            depth_dir = attachments / 'depth' / ('episode_%06d' % output_ep)
            depth_dir.mkdir(parents=True)
            atomic_json(depth_dir / 'camera.json', meta['camera'])
            mapping = []
            current_path, tar = None, None
            try:
                for frame_index, sample in enumerate(segment):
                    if current_path != sample['_chunk']:
                        if tar:
                            tar.close()
                        current_path = sample['_chunk']
                        tar = tarfile.open(current_path)
                    rgb = np.asarray(Image.open(io.BytesIO(tar.extractfile(sample['color_file']).read())).convert('RGB'))
                    depth = tar.extractfile(sample['depth_file']).read()
                    (depth_dir / ('%08d.png' % frame_index)).write_bytes(depth)
                    ds.add_frame({'observation.state': np.asarray(sample['state'], dtype=np.float32),
                                  'action': np.asarray(sample['action'], dtype=np.float32),
                                  'observation.images.front': rgb,
                                  'source.frame_index': np.asarray([sample['source_frame_index']],dtype=np.int64),
                                  'source.monotonic_ns': np.asarray([sample['t_ns']],dtype=np.int64),
                                  'sync.color_depth_ms': np.asarray([sample['color_depth_delta_ms']],dtype=np.float32),
                                  'task': meta['task']})
                    mapping.append({k:v for k,v in sample.items() if k != '_chunk'})
                ds.save_episode()
            finally:
                if tar:
                    tar.close()
            atomic_json(depth_dir / 'source_samples.json', mapping)
            episode_map.append({'dataset_episode': output_ep, 'raw_episode':meta['id'],
                                'source_start_frame':segment[0]['source_frame_index'], 'frames':len(segment)})
    finally:
        ds.finalize()
    report = {'repo_id':repo_id, 'format':'LeRobot v3.0', 'episodes':episode_map, 'skipped':skipped,
              'units':{'joints':'radians','gripper':'metres'},
              'depth': 'unaligned native Z16 PNG; apply per-episode depth_scale_m and extrinsics',
              'timing': '15 Hz nominal grid; original GOS monotonic timestamps in source.monotonic_ns',
              'action': 'causal last received leader CAN target at the camera sample time'}
    atomic_json(attachments / 'capture_manifest.json', report)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('episodes', nargs='+', help='Local received raw episode directories')
    p.add_argument('--inspect', action='store_true')
    p.add_argument('--output')
    p.add_argument('--repo-id', default='local/piper_d435i')
    p.add_argument('--include-failed', action='store_true')
    p.add_argument('--min-frames', type=int, default=15)
    args = p.parse_args()
    if args.inspect:
        for path in args.episodes:
            print(json.dumps(inspect_episode(path), ensure_ascii=False, indent=2))
        return
    if not args.output:
        p.error('--output is required for export')
    print(json.dumps(export(args.episodes,args.output,args.repo_id,args.include_failed,args.min_frames),ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()

