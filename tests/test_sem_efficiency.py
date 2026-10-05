import io
import tarfile
import tempfile
import unittest
from pathlib import Path

import numpy as np

from sem_efficiency.core import (ScopedReader, make_partition, safe_member, source_freeze,
                                 verify_source)


class FakeManifest:
    def __init__(self):
        self.data = {'items': [dict(id=f'{s}{i}', split=s, defect_class=str(i % 2),
                                   duplicate_group=f'{s}{i}', image_sha256=f'{s}{i}', source_group=None)
                               for s in ('train', 'calibration', 'test') for i in range(12)]}
        self.access = []

    def items(self, split):
        return [r for r in self.data['items'] if r['split'] == split]

    def reader(self, split, **kwargs):
        parent = self
        class Reader:
            def load_image(self, iid):
                if iid not in {r['id'] for r in parent.items(split)}:
                    raise ValueError('not in split')
                parent.access.append((split, iid))
                return np.zeros((8, 8), np.float32)
            load_mask = load_image
        return Reader()


class ProtocolTests(unittest.TestCase):
    def test_frozen_encoder_training_is_in_new_training_pool(self):
        m = FakeManifest()
        f = {'training': {'item_ids': ['train0', 'train1']}}
        p = make_partition(m, f, 6, 3, 4)
        self.assertTrue({'train0', 'train1'} <= set(p['train']))
        self.assertFalse(set(p['tune']) & set(p['development_evaluation']))
        self.assertFalse(any(i.startswith('test') for ids in p.values() for i in ids))
        self.assertEqual(m.access, [])

    def test_encoder_contaminated_with_calibration_is_rejected(self):
        with self.assertRaises(ValueError):
            make_partition(FakeManifest(), {'training': {'item_ids': ['calibration0']}}, 6, 3, 4)

    def test_shared_source_family_between_roles_rejected(self):
        m = FakeManifest()
        for row in m.data['items']:
            row['source_group'] = 'one-acquisition'
        with self.assertRaises(ValueError):
            make_partition(m, {'training': {'item_ids': ['train0']}}, 6, 3, 4)

    def test_original_test_and_unregistered_masks_inaccessible(self):
        m = FakeManifest()
        pools = make_partition(m, {'training': {'item_ids': ['train0']}}, 6, 3, 4)
        reader = ScopedReader(m, pools, [])
        for role, iid in [('test', 'test0'), ('train', 'test0'), ('tune', pools['development_evaluation'][0])]:
            with self.assertRaises(ValueError):
                reader.load(role, iid, 'mask')
        self.assertEqual(m.access, [])

    def test_source_changes_and_new_files_break_freeze(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d); (p/'a.py').write_text('pass')
            frozen = source_freeze(p)
            verify_source(p, frozen)
            (p/'b.py').write_text('pass')
            with self.assertRaises(ValueError):
                verify_source(p, frozen)

    def test_archive_symlinks_and_traversal_never_extracted(self):
        for name, kind in [('ok.jpg', tarfile.REGTYPE), ('../bad.jpg', tarfile.REGTYPE),
                           ('C:/bad.jpg', tarfile.REGTYPE), ('link.jpg', tarfile.SYMTYPE)]:
            m = tarfile.TarInfo(name); m.size = 1024; m.type = kind
            self.assertEqual(safe_member(m), name == 'ok.jpg')


class LearningTests(unittest.TestCase):
    def test_padding_has_zero_loss_gradient(self):
        import torch
        from sem_efficiency.models import masked_loss
        x = torch.zeros((1,1,4,4), requires_grad=True)
        y = torch.ones_like(x); valid = torch.zeros_like(x); valid[:,:,:2,:2] = 1
        loss = masked_loss(x, y, valid); loss.backward()
        self.assertEqual(int(torch.count_nonzero(x.grad[:,:,2:,:])), 0)
        self.assertGreater(int(torch.count_nonzero(x.grad[:,:,:2,:2])), 0)

    def test_residual_adapter_starts_as_exact_base_head(self):
        import torch
        from sem_efficiency.models import SmallHead
        base = torch.nn.Conv2d(8,1,3,padding=1)
        candidate = SmallHead(base, adapter=True)
        x = torch.randn(2,8,8,8)
        torch.testing.assert_close(base(x), candidate(x))

    def test_copied_head_can_train_when_original_network_is_frozen(self):
        import torch
        from sem_efficiency.models import SmallHead
        base = torch.nn.Conv2d(8,1,3,padding=1)
        for parameter in base.parameters():
            parameter.requires_grad_(False)
        head = SmallHead(base)
        self.assertTrue(all(p.requires_grad for p in head.parameters()))
        self.assertFalse(any(p.requires_grad for p in base.parameters()))

    def test_persisted_predictions_modified_before_masks_rejected(self):
        from sem_efficiency.experiment import load_predictions
        from sem_efficiency.core import digest
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'p.npy'; np.save(p, np.zeros((4,4)), allow_pickle=False)
            rows = [dict(id='x', file=str(p), sha256=digest(p))]
            np.save(p, np.ones((4,4)), allow_pickle=False)
            with self.assertRaises(ValueError):
                load_predictions(rows)


if __name__ == '__main__':
    unittest.main()
