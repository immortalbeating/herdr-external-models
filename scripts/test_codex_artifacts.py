import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import codex_artifacts

SID = '12345678-1234-1234-1234-123456789abc'
OTHER = '12345678-1234-1234-1234-123456789abd'


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / 'home'
        self.src = self.home / 'generated_images' / SID
        self.src.mkdir(parents=True)
        self.a = self.src / 'a.png'
        self.b = self.src / 'b.png'
        self.a.write_bytes(b'first-image')
        self.b.write_bytes(b'second-image')
        (self.src / 'unselected.png').write_bytes(b'extra')
        self.job = self.root / 'job'
        self.job.mkdir()
        self.state = {'spec': {'mode': 'exec', 'env': {'CODEX_HOME': str(self.home), 'SECRET': 'fake-credential'}},
                      'native_session': {'kind': 'id', 'agent': 'codex', 'value': SID}}

    def collect(self, sources=None, destination=None):
        return codex_artifacts.collect_images(self.job, self.state, [self.a] if sources is None else sources, destination)

    def test_exact_subset_and_receipts(self):
        result = self.collect([self.b, self.a])
        out = self.job / 'artifacts' / 'codex-images' / SID
        self.assertEqual({p.name for p in out.iterdir()}, {'a.png', 'b.png'})
        self.assertEqual(result[0], {'kind': 'codex-generated-image', 'source': str(self.b), 'path': str(out / 'b.png'),
                                     'sha256': hashlib.sha256(b'second-image').hexdigest(), 'bytes': 12,
                                     'native_session_id': SID, 'already_present': False})
        self.assertEqual(self.a.read_bytes(), b'first-image')
        self.assertNotIn('fake-credential', repr(result))

    def test_invalid_sources_and_identity(self):
        other = self.home / 'generated_images' / OTHER / 'x.png'
        other.parent.mkdir()
        other.write_bytes(b'x')
        for sources in ([], [other], [self.root / 'outside.png'], [self.src / 'missing.png'],
                        [self.src / '..' / SID / 'a.png'], [self.src], [self.a, self.a]):
            with self.subTest(sources=sources), self.assertRaises(ValueError):
                self.collect(sources)
        for native in (None, {'kind': 'path', 'agent': 'codex', 'value': SID},
                       {'kind': 'id', 'agent': 'agy', 'value': SID},
                       {'kind': 'id', 'agent': 'codex', 'value': '../secret'}):
            self.state['native_session'] = native
            with self.assertRaises(ValueError) as err:
                self.collect()
            self.assertNotIn('fake-credential', str(err.exception))

    def test_mode_and_explicit_scope(self):
        dest = self.root / 'allowed' / 'nested'
        with self.assertRaises(ValueError):
            self.collect(destination=dest)
        self.state['spec']['artifact_output_dirs'] = [str(self.root / 'allowed')]
        self.assertTrue(Path(self.collect(destination=dest)[0]['path']).is_file())
        with self.assertRaises(ValueError):
            self.collect(destination=self.root / 'elsewhere')
        self.state['spec']['artifact_output_dirs'] = ['relative']
        with self.assertRaises(ValueError):
            self.collect(destination=dest)
        for mode, kind in [('direct', 'codex'), ('exec', 'agy'), ('herdr', 'agy')]:
            self.state['spec'].update(mode=mode, kind=kind)
            with self.assertRaises(ValueError):
                self.collect()
        self.state['spec'].update(mode='herdr', kind='codex')
        self.collect()

    def test_idempotence_and_conflict(self):
        first = self.collect()[0]
        second = self.collect()[0]
        self.assertFalse(first['already_present'])
        self.assertTrue(second['already_present'])
        Path(first['path']).write_bytes(b'changed')
        with self.assertRaises(ValueError):
            self.collect()
        self.assertEqual(Path(first['path']).read_bytes(), b'changed')

    def test_all_existing_conflicts_preflight_before_publication(self):
        out = self.job / 'artifacts' / 'codex-images' / SID
        out.mkdir(parents=True)
        (out / 'b.png').write_bytes(b'conflicting')
        with self.assertRaises(ValueError):
            self.collect([self.a, self.b])
        self.assertFalse((out / 'a.png').exists())
        self.assertEqual((out / 'b.png').read_bytes(), b'conflicting')

    def test_second_copy_failure_returns_completed_receipts(self):
        out = self.job / 'artifacts' / 'codex-images' / SID
        original = codex_artifacts.shutil.copyfileobj
        count = 0
        def fail_second(src, dst):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('copy failed fake-credential')
            return original(src, dst)
        with mock.patch.object(codex_artifacts.shutil, 'copyfileobj', side_effect=fail_second):
            with self.assertRaises(codex_artifacts.ImageCollectionError) as raised:
                self.collect([self.a, self.b])
        error = raised.exception
        self.assertIsInstance(error.__cause__, OSError)
        self.assertIn('copy failed', str(error))
        self.assertNotIn('fake-credential', str(error))
        self.assertEqual(len(error.completed_receipts), 1)
        receipt = error.completed_receipts[0]
        self.assertEqual(receipt['sha256'], hashlib.sha256(self.a.read_bytes()).hexdigest())
        self.assertEqual(Path(receipt['path']).read_bytes(), self.a.read_bytes())
        self.assertFalse((out / 'b.png').exists())
        self.assertEqual({p.name for p in out.iterdir()}, {'a.png'})
        self.assertEqual(self.b.read_bytes(), b'second-image')

    def test_copy_failure_and_mismatched_copy_do_not_publish(self):
        out = self.job / 'artifacts' / 'codex-images' / SID
        with mock.patch.object(codex_artifacts.shutil, 'copyfileobj', side_effect=OSError('copy failed')):
            with self.assertRaises(OSError):
                self.collect()
        self.assertEqual(list(out.iterdir()), [])
        def corrupt(src, dst):
            dst.write(b'corrupt')
        with mock.patch.object(codex_artifacts.shutil, 'copyfileobj', side_effect=corrupt):
            with self.assertRaises(OSError):
                self.collect()
        self.assertEqual(list(out.iterdir()), [])
        self.assertEqual(self.a.read_bytes(), b'first-image')

    def test_windows_reparse_attribute_rejected(self):
        original = Path.lstat
        def reparse(path):
            if path == self.a:
                return mock.Mock(st_mode=0, st_file_attributes=0x400)
            return original(path)
        with mock.patch.object(Path, 'lstat', reparse):
            with self.assertRaises(ValueError):
                self.collect()

    def test_symlinks_rejected(self):
        link = self.src / 'link.png'
        try:
            link.symlink_to(self.a)
        except OSError:
            self.skipTest('Symlink creation unavailable')
        with self.assertRaises(ValueError):
            self.collect([link])
        allowed = self.root / 'allowed'
        allowed.mkdir()
        (allowed / 'linked').symlink_to(self.src, target_is_directory=True)
        self.state['spec']['artifact_output_dirs'] = [str(allowed)]
        with self.assertRaises(ValueError):
            self.collect(destination=allowed / 'linked')


if __name__ == '__main__':
    unittest.main()
