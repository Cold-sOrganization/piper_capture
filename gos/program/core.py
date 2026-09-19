"""Timestamped, receive-only Piper decoding and transactional chunk utilities."""
import collections
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import threading

JOINT_NAMES = ['joint%d' % i for i in range(1, 7)] + ['gripper_width']
ACTION_IDS = [0x155, 0x156, 0x157, 0x159]
STATE_IDS = [0x2A5, 0x2A6, 0x2A7, 0x2A8]


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + '.tmp')
    with temp.open('w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, allow_nan=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(temp), str(path))


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def decode(can_id, data):
    """Matches installed pyAgxArm Piper/AGX gripper default codecs.

    Classic CAN has no source address: the leader/follower role attribution
    assumes the configured unique command/feedback IDs on this shared bus.
    """
    if len(data) != 8:
        return None
    if can_id in ACTION_IDS[:3] + STATE_IDS[:3]:
        return [x * math.pi / 180000.0 for x in struct.unpack('>ii', data)]
    if can_id in [0x159, 0x2A8]:
        if can_id == 0x2A8 and data[7] == 1:
            return None  # Angle-mode gripper cannot be silently labelled metres.
        return [struct.unpack('>i', data[:4])[0] * 1e-6]
    return None


class CanHistory:
    def __init__(self, max_age_ms=100, max_joint_skew_ms=20):
        self.lock = threading.Lock()
        self.messages = collections.defaultdict(lambda: collections.deque(maxlen=1500))
        self.counts = collections.Counter()
        self.max_age_ns = int(max_age_ms * 1e6)
        self.max_skew_ns = int(max_joint_skew_ms * 1e6)
        self.last_receive_ns = 0

    def add(self, can_id, data, t_ns):
        values = decode(can_id, data)
        with self.lock:
            self.counts['0x%03X' % can_id] += 1
            self.last_receive_ns = t_ns
            if can_id in ACTION_IDS + STATE_IDS:
                self.messages[can_id].append((t_ns, values))

    def snapshot(self, t_ns):
        result = {'valid': True, 'reasons': []}
        with self.lock:
            for key, ids in [('action', ACTION_IDS), ('state', STATE_IDS)]:
                parts, stamps = [], []
                for can_id in ids:
                    item = next((x for x in reversed(self.messages[can_id]) if x[0] <= t_ns), None)
                    if item is None or item[1] is None:
                        result['reasons'].append('%s_missing_or_unsupported_0x%X' % (key, can_id))
                        parts = []
                        break
                    stamps.append(item[0])
                    parts.extend(item[1])
                result[key] = parts if len(parts) == 7 else None
                result[key + '_component_t_ns'] = stamps
                if len(stamps) == 4:
                    if t_ns - min(stamps) > self.max_age_ns:
                        result['reasons'].append(key + '_stale')
                    if max(stamps[:3]) - min(stamps[:3]) > self.max_skew_ns:
                        result['reasons'].append(key + '_joint_packet_skew')
            result['valid'] = not result['reasons']
        return result


class DeviceClock:
    """Lower-envelope arrival mapping; raw clocks are always retained.

    This estimates a host-monotonic timestamp; it is not hardware sync.
    Mapping must be warmed up, then frozen for each recording session.
    """
    def __init__(self):
        self.offset_ns = None
        self.frozen = False

    def map(self, device_ms, receive_ns):
        value = int(device_ms * 1e6)
        candidate = receive_ns - value
        if self.offset_ns is None or (not self.frozen and candidate < self.offset_ns):
            self.offset_ns = candidate
        return value + self.offset_ns
