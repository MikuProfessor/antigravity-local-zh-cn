"""Offline checks for package rejection, replacement races and rollback."""
import base64
import hashlib
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

import update_install as install


class PackageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.archive = Path(temp.name) / 'Antigravity.zip'

    def test_digest_and_size_must_both_match(self):
        content = b'test data'
        self.archive.write_bytes(content)
        sha = base64.b64encode(hashlib.sha512(content).digest()).decode()
        release = {'size': len(content), 'sha512': sha}
        install._validate_package(self.archive, release)
        for fields in ({'size': 2}, {'sha512': 'incorrect'}):
            with self.subTest(fields=fields), self.assertRaises(install.InstallError):
                install._validate_package(self.archive, dict(release, **fields))

    def test_only_app_tree(self):
        for name in ('/outside', '../outside', 'Antigravity.app/a/../../other', 'Antigravity.app/file:fork'):
            with self.subTest(name=name):
                with zipfile.ZipFile(self.archive, 'w') as z:
                    z.writestr(name, 'text')
                with self.assertRaises(install.InstallError):
                    install._validate_zip(self.archive)

    def test_framework_links_allowed_but_writes_through_links_are_refused(self):
        link = zipfile.ZipInfo('Antigravity.app/Versions/Current')
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(self.archive, 'w') as z:
            z.writestr(link, 'A')
            z.writestr('Antigravity.app/Versions/A/lib', 'data')
        install._validate_zip(self.archive)
        with zipfile.ZipFile(self.archive, 'a') as z:
            z.writestr('Antigravity.app/Versions/current/unexpected', 'data')
        with self.assertRaises(install.InstallError):
            install._validate_zip(self.archive)

    def test_out_of_tree_link_rejected(self):
        link = zipfile.ZipInfo('Antigravity.app/Escape')
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(self.archive, 'w') as z:
            z.writestr(link, '../../outside')
        with self.assertRaises(install.InstallError):
            install._validate_zip(self.archive)


@unittest.skipUnless(sys.platform == 'darwin', 'requires same-volume macOS directory exchange')
class ActivationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.app, self.backup = [Path(temp.name) / v / 'Antigravity.app' for v in ('installed', 'staged')]
        for app, value in ((self.app, 'old'), (self.backup, 'new')):
            for name in (install.BINARY, install.ASAR, install.PLIST):
                file = app / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(value)
        self.stamp = install._stamp(self.app)
        self.quit = self.patch('_quit', return_value=True)
        self.pids = self.patch('_pids', return_value=[])
        self.launch = self.patch('_launch')

    def patch(self, name, **kwargs):
        tool = mock.patch.object(install, name, **kwargs)
        self.addCleanup(tool.stop)
        return tool.start()

    def test_committed_backup_is_complete_displaced_bundle(self):
        install._activate(self.app, self.backup, self.stamp)
        self.assertEqual(install._stamp(self.backup), self.stamp)
        self.assertEqual((self.app / install.BINARY).read_text(), 'new')
        self.launch.assert_called_once_with(self.app)

    def test_failed_launch_restores_and_reopens_original(self):
        self.launch.side_effect = [install.InstallError('launch failed'), None]
        with self.assertRaises(install.InstallError):
            install._activate(self.app, self.backup, self.stamp)
        self.assertEqual(install._stamp(self.app), self.stamp)
        self.assertEqual((self.backup / install.BINARY).read_text(), 'new')
        self.assertEqual(self.launch.call_count, 2)

    def test_concurrent_native_update_on_quit_is_preserved(self):
        def official_update(_app):
            path = self.app / install.BINARY
            new = path.with_name('incoming')
            new.write_text('official upgrade')
            os.replace(new, path)
            return True
        self.quit.side_effect = official_update
        with self.assertRaises(install.InstallError):
            install._activate(self.app, self.backup, self.stamp)
        self.assertEqual((self.app / install.BINARY).read_text(), 'official upgrade')
        self.assertEqual((self.backup / install.BINARY).read_text(), 'new')

    def test_concurrent_replacement_during_launch_is_preserved_with_backup(self):
        def official_updated(_app):
            binary = self.app / install.BINARY
            newer = binary.with_name('incoming')
            newer.write_text('newer official app')
            os.replace(newer, binary)
        self.launch.side_effect = official_updated
        with self.assertRaisesRegex(install.InstallError, '保留并行更新'):
            install._activate(self.app, self.backup, self.stamp)
        self.assertEqual((self.app / install.BINARY).read_text(), 'newer official app')
        self.assertEqual(install._stamp(self.backup), self.stamp)

    def test_refusal_to_quit_keeps_current_copy(self):
        self.quit.side_effect = install.InstallError('still editing')
        with self.assertRaises(install.InstallError):
            install._activate(self.app, self.backup, self.stamp)
        self.assertEqual(install._stamp(self.app), self.stamp)
        self.launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
