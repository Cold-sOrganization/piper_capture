#!/usr/bin/env python3
"""GOS receive-only service. HTTP control is reachable only over local/SSH access."""
import argparse
import collections
import concurrent.futures
import fcntl
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import tarfile
import threading
import time
import uuid

from core import CanHistory, DeviceClock, atomic_json, sha256


class Episode:
    def __init__(self, root, task, camera_meta, cfg):
        self.id = time.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8]
        self.path = root / self.id
        self.path.mkdir()
        self.queue = queue.Queue(maxsize=20000)
        self.error = None
        self.done = threading.Event()
        self.pending_frames = 0
        self.counter_lock = threading.Lock()
        self.meta = {'schema': 'piper-raw-v1', 'id': self.id, 'task': task,
                     'status': 'recording', 'start_monotonic_ns': time.monotonic_ns(),
                     'wall_time_untrusted_ns': time.time_ns(), 'camera': camera_meta,
                     'config': cfg, 'chunks': [], 'frames': 0, 'valid_frames': 0,
                     'raw_can_frames': 0, 'success': None,
                     'action_semantics': 'leader_CAN_joint_targets_rad_and_gripper_width_m',
                     'state_semantics': 'follower_CAN_joint_feedback_rad_and_gripper_width_m',
                     'sync': 'camera lower-envelope host mapping, frozen at episode start; causal CAN sample-and-hold'}
        atomic_json(self.path / 'manifest.json', self.meta)
        self.thread = threading.Thread(target=self._write, daemon=True)
        self.thread.start()

    def submit(self, event):
        if self.error:
            return
        if event[0] == 'frame':
            with self.counter_lock:
                if self.pending_frames >= 120:
                    self.error = 'image_queue_overflow; recording invalidated'
                    return
                self.pending_frames += 1
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            self.error = 'writer_queue_overflow; recording invalidated'

    def finish(self, success):
        self.meta['success'] = success
        self.queue.put(('stop', None), timeout=10)
        if not self.done.wait(30):
            raise RuntimeError('Writer did not finish within 30 seconds')
        return self.meta

    def _write(self):
        import cv2
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        files, samples, raw, pending = {}, [], [], []
        last_flush = time.monotonic()

        def encode_frame(index, rgb, depth):
            encoded_files = {}
            for name, arr in [('color', rgb[:, :, ::-1]), ('depth', depth)]:
                ok, encoded = cv2.imencode('.png', arr, [cv2.IMWRITE_PNG_COMPRESSION, 1])
                if not ok:
                    raise RuntimeError(name + ' PNG encoding failed')
                encoded_files['%s/%08d.png' % (name, index)] = encoded.tobytes()
            return encoded_files

        def seal():
            nonlocal files, samples, raw, pending, last_flush
            if not samples and not raw:
                return
            chunk_index = len(self.meta['chunks'])
            name = 'chunk_%06d.tar' % chunk_index
            path = self.path / name
            for future in pending:
                files.update(future.result(timeout=20))
            files['samples.jsonl'] = ''.join(json.dumps(x, allow_nan=False) + '\n' for x in samples).encode()
            files['can.jsonl.gz'] = gzip.compress(''.join(json.dumps(x) + '\n' for x in raw).encode(), compresslevel=1)
            with open(str(path) + '.part', 'wb') as out:
                with tarfile.open(fileobj=out, mode='w') as tar:
                    for key, value in files.items():
                        info = tarfile.TarInfo(key)
                        info.size = len(value)
                        tar.addfile(info, io.BytesIO(value))
                out.flush()
                os.fsync(out.fileno())
            os.replace(str(path) + '.part', path)
            self.meta['chunks'].append({'file': name, 'sha256': sha256(path), 'bytes': path.stat().st_size,
                                        'frames': len(samples), 'can_frames': len(raw)})
            atomic_json(self.path / 'manifest.json', self.meta)
            files, samples, raw, pending = {}, [], [], []
            last_flush = time.monotonic()

        try:
            while True:
                try:
                    kind, event = self.queue.get(timeout=0.5)
                except queue.Empty:
                    if time.monotonic() - last_flush >= 2:
                        seal()
                    continue
                if kind == 'stop':
                    break
                if kind == 'can':
                    raw.append(event)
                    self.meta['raw_can_frames'] += 1
                elif kind == 'frame':
                    with self.counter_lock:
                        self.pending_frames -= 1
                    sample, rgb, depth = event
                    index = self.meta['frames']
                    sample['source_frame_index'] = index
                    pending.append(pool.submit(encode_frame, index, rgb, depth))
                    for name in ['color', 'depth']:
                        sample[name + '_file'] = '%s/%08d.png' % (name, index)
                    samples.append(sample)
                    self.meta['frames'] += 1
                    self.meta['valid_frames'] += int(sample['valid'])
                if len(samples) >= 30 or time.monotonic() - last_flush >= 2:
                    seal()
            seal()
            self.meta['status'] = 'failed' if self.error else 'complete'
        except Exception as exc:
            self.error = repr(exc)
            self.meta['status'] = 'failed'
        finally:
            pool.shutdown(wait=True)
            self.meta['error'] = self.error
            self.meta['end_monotonic_ns'] = time.monotonic_ns()
            try:
                atomic_json(self.path / 'manifest.json', self.meta)
            finally:
                self.done.set()


class Collector:
    def __init__(self, args):
        self.args = args
        self.root = Path(args.spool).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_file = open(self.root / 'collector.lock', 'a')
        fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # A killed recorder must not leave an episode falsely marked complete.
        for p in self.root.glob('*/manifest.json'):
            m = json.loads(p.read_text())
            if m['status'] == 'recording':
                m.update(status='interrupted', error='collector restarted before clean stop')
                atomic_json(p, m)
        self.history = CanHistory(args.max_age_ms, args.max_skew_ms)
        self.stop_event = threading.Event()
        self.guard = threading.RLock()
        self.recording = None
        self.last_episode = None
        self.camera_meta = {}
        self.camera_frames = 0
        self.last_camera_ns = 0
        self.last_sample = None
        self.errors = {}
        self.clocks = {}
        self.last_numbers = {}
        self.camera_drops = collections.Counter()
        self.threads = []

    def _event(self, event):
        with self.guard:
            if self.recording:
                self.recording.submit(event)

    def can_loop(self):
        # No arm SDK constructors/control calls, and no CAN send method is used.
        from gs_usb.gs_usb import GsUsb
        from gs_usb.gs_usb_frame import GsUsbFrame
        from gs_usb.constants import GS_CAN_MODE_LISTEN_ONLY, GS_CAN_MODE_HW_TIMESTAMP
        def identity():
            found = []
            for p in Path('/sys/bus/usb/devices').glob('*'):
                try:
                    if (p/'idVendor').read_text().strip() == '1d50' and (p/'idProduct').read_text().strip() == '606f':
                        found.append(((p/'busnum').read_text().strip(),(p/'devnum').read_text().strip()))
                except OSError:
                    pass
            return tuple(found)

        attempted = None
        while not self.stop_event.is_set():
            current = identity()
            if not current or current == attempted:
                self.stop_event.wait(1)
                continue
            attempted, dev = current, None
            try:
                devices = GsUsb.scan()
                if len(devices) != 1:
                    raise RuntimeError('Expected exactly one USB-CAN adapter; found %d' % len(devices))
                dev = devices[0]
                if not dev.device_capability.feature & GS_CAN_MODE_LISTEN_ONLY:
                    raise RuntimeError('Adapter does not support hardware listen-only mode')
                if not dev.set_bitrate(1000000):
                    raise RuntimeError('Cannot configure CAN bitrate')
                dev.start(GS_CAN_MODE_LISTEN_ONLY | GS_CAN_MODE_HW_TIMESTAMP)
                if not dev.device_flags & GS_CAN_MODE_LISTEN_ONLY:
                    raise RuntimeError('Listen-only mode was not activated')
                last_read = time.monotonic()
                while not self.stop_event.is_set():
                    frame = GsUsbFrame()
                    if not dev.read(frame, timeout_ms=20):
                        if time.monotonic() - last_read > 5:
                            raise RuntimeError('No CAN data for 5s. Check arm power; physically replug USB-CAN to recover this adapter.')
                        continue
                    last_read = time.monotonic()
                    self.errors.pop('can', None)
                    t = time.monotonic_ns()
                    data = bytes(frame.data[:frame.can_dlc])
                    raw = {'receive_ns': t, 'usb_timestamp_us': frame.timestamp_us,
                           'id': frame.arbitration_id, 'flags': frame.can_id, 'data': data.hex()}
                    self._event(('can', raw))
                    if not frame.is_error_frame and not frame.is_remote_frame and not frame.is_extended_id:
                        self.history.add(frame.arbitration_id, data, t)
            except Exception as exc:
                self.errors['can'] = repr(exc)
            finally:
                if dev:
                    dev.stop()

    def camera_loop(self):
        from realsense_c import RealSense
        camera = None
        try:
            camera = RealSense(self.args.width, self.args.height, self.args.fps)
            while not self.stop_event.is_set():
                frames = camera.read()
                if 'color' not in frames or 'depth' not in frames:
                    continue
                for name, item in frames.items():
                    key = (name, item['timestamp_domain'])
                    clock = self.clocks.setdefault(key, DeviceClock())
                    item['mapped_ns'] = clock.map(item['timestamp_ms'], item['receive_ns'])
                color, depth = frames['color'], frames['depth']
                # librealsense may reuse one stream frame in a frameset.
                if color['frame_number'] == self.last_numbers.get('color'):
                    continue
                reasons = []
                for name in ('color', 'depth'):
                    number = frames[name]['frame_number']
                    prev = self.last_numbers.get(name)
                    if prev is not None:
                        if number <= prev:
                            reasons.append(name + '_reused_or_reset_frame')
                        self.camera_drops[name] += max(0, number - prev - 1)
                    self.last_numbers[name] = number
                t = color['mapped_ns']
                sample = self.history.snapshot(t)
                sample.update(t_ns=t, receive_ns=color['receive_ns'],
                              color_timestamp_ms=color['timestamp_ms'], depth_timestamp_ms=depth['timestamp_ms'],
                              color_timestamp_domain=color['timestamp_domain'], depth_timestamp_domain=depth['timestamp_domain'],
                              color_frame_number=color['frame_number'], depth_frame_number=depth['frame_number'],
                              depth_mapped_ns=depth['mapped_ns'])
                if color['timestamp_domain'] == depth['timestamp_domain']:
                    delta_ms = abs(color['timestamp_ms'] - depth['timestamp_ms'])
                else:
                    delta_ms = abs(color['mapped_ns'] - depth['mapped_ns']) / 1e6
                    reasons.append('camera_timestamp_domains_differ')
                sample['color_depth_delta_ms'] = delta_ms
                if delta_ms > self.args.max_camera_delta_ms:
                    reasons.append('color_depth_time_mismatch')
                sample['reasons'].extend(reasons)
                sample['valid'] = not sample['reasons']
                self.camera_frames += 1
                self.last_camera_ns = time.monotonic_ns()
                self.camera_meta = camera.metadata
                self.last_sample = sample.copy()
                self._event(('frame', (sample, color['data'], depth['data'])))
        except Exception as exc:
            self.errors['camera'] = repr(exc)
        finally:
            if camera:
                camera.close()

    def status(self):
        now = time.monotonic_ns()
        with self.guard:
            return {'recording': self.recording.id if self.recording else None,
                    'last_episode': self.last_episode, 'camera_frames': self.camera_frames,
                    'camera_age_ms': (now - self.last_camera_ns) / 1e6 if self.last_camera_ns else None,
                    'can_age_ms': (now - self.history.last_receive_ns) / 1e6 if self.history.last_receive_ns else None,
                    'can_counts': dict(self.history.counts), 'camera_drops': dict(self.camera_drops),
                    'errors': dict(self.errors), 'last_sample': self.last_sample,
                    'camera': self.camera_meta, 'spool': str(self.root),
                    'writer_error': self.recording.error if self.recording else None,
                    'writer_queue': self.recording.queue.qsize() if self.recording else 0}

    def start_recording(self, task, allow_invalid=False):
        if not isinstance(task, str) or not task.strip():
            raise ValueError('A nonempty task description is required')
        with self.guard:
            if self.recording:
                raise RuntimeError('An episode is already recording')
            if self.errors or self.camera_frames < 30 or time.monotonic_ns() - self.last_camera_ns > 500_000_000:
                raise RuntimeError('Sensors are not ready; check status')
            if not allow_invalid and time.monotonic_ns() - self.history.last_receive_ns > 500_000_000:
                raise RuntimeError('CAN feedback is stale')
            if not allow_invalid and (not self.last_sample or self.last_sample['state'] is None or self.last_sample['action'] is None):
                raise RuntimeError('Both leader targets and follower feedback must have been observed; check status')
            for clock in self.clocks.values():
                clock.frozen = True
            cfg = vars(self.args).copy()
            cfg['allow_invalid_diagnostic'] = allow_invalid
            cfg['clock_offsets_ns'] = {str(k): v.offset_ns for k, v in self.clocks.items()}
            self.recording = Episode(self.root, task.strip(), json.loads(json.dumps(self.camera_meta)), cfg)
            return {'episode': self.recording.id}

    def stop_recording(self, success=None):
        with self.guard:
            ep, self.recording = self.recording, None
        if ep is None:
            raise RuntimeError('No episode is recording')
        if self.errors:
            ep.error = json.dumps(self.errors)
        result = ep.finish(success)
        self.last_episode = ep.id
        for clock in self.clocks.values():
            clock.frozen = False
        return result

    def dispatch(self, request):
        op = request.get('op', 'status')
        if op == 'status':
            return self.status()
        if op == 'start':
            return self.start_recording(request['task'], request.get('allow_invalid', False))
        if op == 'stop':
            return self.stop_recording(request.get('success'))
        if op == 'list':
            return [json.loads(p.read_text()) for p in sorted(self.root.glob('*/manifest.json'))]
        if op == 'label':
            eid = request['episode']
            if not re.fullmatch(r'[0-9]{8}_[0-9]{6}_[a-f0-9]{8}', eid):
                raise ValueError('Invalid episode identifier')
            path = self.root / eid / 'manifest.json'
            m = json.loads(path.read_text())
            if m['status'] == 'recording':
                raise ValueError('Stop recording before labelling')
            m['success'] = bool(request.get('success', False))
            m['discarded'] = bool(request.get('discarded', False))
            atomic_json(path, m)
            return {'labelled': eid}
        if op == 'ack':
            episode, name, digest = request['episode'], request['file'], request['sha256']
            if not re.fullmatch(r'[0-9]{8}_[0-9]{6}_[a-f0-9]{8}', episode) or not re.fullmatch(r'chunk_[0-9]{6}\.tar', name):
                raise ValueError('Invalid episode/chunk identifier')
            path = self.root / episode / name
            m = json.loads((path.parent / 'manifest.json').read_text())
            entry = next((c for c in m['chunks'] if c['file'] == name), None)
            if entry is None or entry['sha256'] != digest:
                raise ValueError('Acknowledgement digest does not match manifest')
            atomic_json(path.with_suffix('.ack.json'), {'sha256': digest, 'received_on_relay': True})
            if request.get('release', False) and path.exists():
                path.unlink()
            return {'acknowledged': name}
        raise ValueError('Unknown operation')

    def run(self):
        for target in (self.can_loop, self.camera_loop):
            thread = threading.Thread(target=target, daemon=True)
            self.threads.append(thread)
            thread.start()
        app = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    size = int(self.headers.get('Content-Length', 0))
                    if not 0 < size < 65536:
                        raise ValueError('Invalid request size')
                    result = {'ok': True, 'result': app.dispatch(json.loads(self.rfile.read(size)))}
                    code = 200
                except Exception as exc:
                    result, code = {'ok': False, 'error': str(exc)}, 400
                body = json.dumps(result, allow_nan=False).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', self.args.port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(json.dumps({'ready': True, 'pid': os.getpid(), 'port': self.args.port}), flush=True)
        try:
            while not self.stop_event.wait(1):
                ep = self.recording
                if ep:
                    used = sum(p.stat().st_size for p in self.root.glob('*/*.tar'))
                    if used > self.args.max_spool_gb * 1024 ** 3 or shutil.disk_usage(self.root).free < 512 * 1024 ** 2:
                        ep.error = 'spool_limit_or_low_disk'
                    if time.monotonic_ns() - self.last_camera_ns > 3_000_000_000:
                        ep.error = 'camera_stream_timeout'
                    if ep.error or self.errors:
                        self.stop_recording(False)
        finally:
            if self.recording:
                self.stop_recording(None)
            self.stop_event.set()
            for thread in self.threads:
                thread.join(5)
            server.shutdown()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--spool', default='/home/user/piper_capture/spool')
    p.add_argument('--port', type=int, default=8765)
    p.add_argument('--width', type=int, default=640)
    p.add_argument('--height', type=int, default=480)
    p.add_argument('--fps', type=int, default=15)
    p.add_argument('--max-age-ms', type=float, default=100)
    p.add_argument('--max-skew-ms', type=float, default=20)
    p.add_argument('--max-camera-delta-ms', type=float, default=35)
    p.add_argument('--max-spool-gb', type=float, default=2)
    app = Collector(p.parse_args())
    for sig in [signal.SIGINT, signal.SIGTERM]:
        signal.signal(sig, lambda *_: app.stop_event.set())
    app.run()


if __name__ == '__main__':
    main()
