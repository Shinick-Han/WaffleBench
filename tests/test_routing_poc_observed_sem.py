import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from routing_poc.observed_sem import ObservedSEM


class ObservedSEMAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'model'
        self.root.mkdir()
        model_path = self.root / 'model.pt'
        model_path.write_bytes(b'non-executable test checkpoint')
        image_root = self.base / 'data'
        image_root.mkdir()
        self.calibration = image_root / 'cal.png'
        self.test = image_root / 'test.png'
        self.calibration.write_bytes(b'calibration fixture')
        self.test.write_bytes(b'test fixture')
        self.image_hash = hashlib.sha256(self.calibration.read_bytes()).hexdigest()
        manifest = {'data_mode': 'real', 'root': str(image_root), 'items': [
            {'split': 'calibration', 'image': 'cal.png', 'image_sha256': self.image_hash},
            {'split': 'test', 'image': 'test.png', 'image_sha256': hashlib.sha256(self.test.read_bytes()).hexdigest()},
        ]}
        self.manifest = self.base / 'manifest.json'
        self.manifest.write_text(json.dumps(manifest), encoding='utf-8')
        frozen = {'data_mode': 'real', 'model': {'file': 'model.pt', 'sha256': hashlib.sha256(model_path.read_bytes()).hexdigest()},
                  'manifest': {'path': str(self.manifest), 'file_sha256': hashlib.sha256(self.manifest.read_bytes()).hexdigest()}}
        (self.root / 'frozen.json').write_text(json.dumps(frozen), encoding='utf-8')

    def observer(self):
        return ObservedSEM(self.root, self.base / 'perception')

    def test_test_image_refused_before_optional_backend_import(self):
        with self.assertRaisesRegex(ValueError, 'not an allowed calibration'):
            self.observer()({'status': 'ok', 'image_path': str(self.test), 'image_sha256': 'unused'}, {'site_id': 'x'})

    def test_capture_failure_has_no_image_access(self):
        result = self.observer()({'status': 'failed', 'image_path': 'missing-file'}, {'site_id': 'x'})
        self.assertEqual(result['status'], 'not_run')

    def test_content_hash_mismatch_refused(self):
        observer = self.observer()
        self.calibration.write_bytes(b'changed after manifest freeze')
        with self.assertRaisesRegex(ValueError, 'image hash differs'):
            observer({'status': 'ok', 'image_path': str(self.calibration), 'image_sha256': self.image_hash}, {'site_id': 'x'})

    def test_checkpoint_tamper_refused(self):
        (self.root / 'model.pt').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'model hash differs'):
            self.observer()

    def test_manifest_tamper_refused(self):
        self.manifest.write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'manifest hash differs'):
            self.observer()

    def test_previous_evidence_not_overwritten(self):
        self.observer()
        with self.assertRaisesRegex(ValueError, 'output exists'):
            self.observer()


if __name__ == '__main__':
    unittest.main()
