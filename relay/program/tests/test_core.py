import json
import math
from pathlib import Path
import struct
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import ACTION_IDS, STATE_IDS, CanHistory, DeviceClock, decode
from export_lerobot import continuous_segments


class ProtocolTests(unittest.TestCase):
    def test_signed_angles_and_units(self):
        q = decode(0x155, struct.pack('>ii',90000,-180000))
        self.assertAlmostEqual(q[0], math.pi/2)
        self.assertAlmostEqual(q[1], -math.pi)
        self.assertAlmostEqual(decode(0x159, struct.pack('>i',50000)+bytes(4))[0], 0.05)

    def test_angle_gripper_and_incomplete_packet_are_not_metres(self):
        self.assertIsNone(decode(0x2A8, bytes(7)+b'\x01'))
        self.assertIsNone(decode(0x155, bytes(7)))

    def fill(self, h, t=1_000_000_000):
        for cid in ACTION_IDS + STATE_IDS:
            h.add(cid, bytes(8), t)

    def test_missing_action_never_becomes_state(self):
        h = CanHistory()
        for cid in STATE_IDS:
            h.add(cid, bytes(8), 1_000_000_000)
        s = h.snapshot(1_001_000_000)
        self.assertIsNone(s['action'])
        self.assertEqual(s['state'], [0.0]*7)
        self.assertFalse(s['valid'])

    def test_stale_future_and_joint_skew(self):
        h = CanHistory()
        self.fill(h)
        self.assertTrue(h.snapshot(1_005_000_000)['valid'])
        self.assertFalse(h.snapshot(1_101_000_000)['valid'])
        self.assertFalse(h.snapshot(999_000_000)['valid'])
        h.add(0x155, bytes(8), 1_030_000_000)
        self.assertIn('action_joint_packet_skew',h.snapshot(1_031_000_000)['reasons'])

    def test_clock_freeze(self):
        c = DeviceClock()
        c.map(1000,2_000_000_000)
        c.map(1010,2_009_000_000)
        c.frozen = True
        self.assertEqual(c.map(1020,2_018_000_000),2_019_000_000)

    def test_invalid_and_time_gaps_split_episodes(self):
        rows = [{'t_ns':i*100_000_000,'source_frame_index':i,'valid':True,'state':[0]*7,'action':[0]*7} for i in range(10)]
        rows[4]['valid'] = False
        rows[8]['t_ns'] += 300_000_000
        segs=list(continuous_segments(rows,10,min_frames=2))
        self.assertEqual([len(x) for x in segs],[4,3])


if __name__ == '__main__':
    unittest.main()

