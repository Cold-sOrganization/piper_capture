"""Run with relay .venv Python. All fixtures are synthetic and temporary."""
import gzip
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from PIL import Image
from core import atomic_json, sha256
from export_lerobot import export, inspect_episode


class DatasetRoundtrip(unittest.TestCase):
    def fixture(self, root):
        root.mkdir()
        samples, files = [], {}
        for i in range(32):
            color = np.zeros((48,64,3),np.uint8)
            color[:,:,0] = 20 + i*4
            depth = np.full((48,64), 500+i, np.uint16)
            for key, arr in [('color',color),('depth',depth)]:
                b=io.BytesIO(); Image.fromarray(arr).save(b,format='PNG')
                files['%s/%08d.png'%(key,i)]=b.getvalue()
            t=10_000_000_000 + round(i*1e9/15)
            samples.append({'source_frame_index':i,'t_ns':t,'valid':True,'reasons':[],
                            'action':[0.1+i*0.001]*6+[0.04], 'state':[i*0.001]*6+[0.03],
                            'action_component_t_ns':[t-1_000_000]*4,'state_component_t_ns':[t-2_000_000]*4,
                            'color_depth_delta_ms':0.1,'color_file':'color/%08d.png'%i,'depth_file':'depth/%08d.png'%i})
        files['samples.jsonl']=''.join(json.dumps(s)+'\n' for s in samples).encode()
        files['can.jsonl.gz']=gzip.compress(b'')
        path=root/'chunk_000000.tar'
        with tarfile.open(path,'w') as tar:
            for name,data in files.items():
                info=tarfile.TarInfo(name); info.size=len(data); tar.addfile(info,io.BytesIO(data))
        meta={'id':'SYNTHETIC_TEST_ONLY','status':'complete','success':True,'task':'SYNTHETIC_TEST_ONLY',
              'config':{'fps':15,'synthetic_test':True},'camera':{'width':64,'height':48,'fps':15,'depth_scale_m':0.001},
              'frames':32,'raw_can_frames':0,'chunks':[{'file':path.name,'sha256':sha256(path),'bytes':path.stat().st_size}]}
        atomic_json(root/'manifest.json',meta)
        return meta

    def test_official_loader_and_temporal_windows(self):
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
        with tempfile.TemporaryDirectory(prefix='piper_synthetic_test_') as tmp:
            base=Path(tmp); raw=base/'raw'; self.fixture(raw)
            self.assertEqual(inspect_episode(raw)['valid_frames'],32)
            report=export([raw],base/'dataset','local/synthetic-test')
            self.assertEqual(report['episodes'][0]['frames'],32)
            ds=LeRobotDataset(repo_id='local/synthetic-test',root=base/'dataset',video_backend='pyav')
            self.assertEqual(len(ds),32)
            sample=ds[5]
            self.assertEqual(tuple(sample['observation.images.front'].shape),(3,48,64))
            self.assertAlmostEqual(float(sample['observation.state'][0]),0.005,places=6)
            self.assertAlmostEqual(float(sample['action'][0]),0.105,places=6)
            self.assertEqual(int(sample['source.frame_index'].item()),5)
            depth=np.asarray(Image.open(base/'dataset/attachments/depth/episode_000000/00000005.png'))
            self.assertTrue(np.all(depth==505))
            window=LeRobotDataset(repo_id='local/synthetic-test',root=base/'dataset',video_backend='pyav',delta_timestamps={'action':[0,1/15,2/15]})
            self.assertEqual(tuple(window[5]['action'].shape),(3,7))

    def test_corrupt_chunk_and_diagnostic_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix='piper_synthetic_test_') as tmp:
            base=Path(tmp); raw=base/'raw'; meta=self.fixture(raw)
            meta['config']['allow_invalid_diagnostic']=True
            atomic_json(raw/'manifest.json',meta)
            with self.assertRaisesRegex(ValueError,'No valid'):
                export([raw],base/'dataset','local/test')
            with (raw/'chunk_000000.tar').open('ab') as f:f.write(b'corruption')
            with self.assertRaisesRegex(ValueError,'Corrupt'):
                inspect_episode(raw)


if __name__=='__main__':
    unittest.main()

