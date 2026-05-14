"""Tests for the update-host downloader and ZIP overlay reader.

These tests do not hit the live TMW update host. Instead each case
constructs a tiny in-memory resources.xml manifest plus a couple of
real zip files on disk, then points the ResourceManager at a local
``file://`` URL. That exercises the whole download + verify + overlay
pipeline against stdlib urllib and zipfile without any network.

Run with: .venv/bin/python -m unittest tests.test_resources
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import urllib.parse
import zipfile
import zlib
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tmw_mcp.resources import ResourceManager


def _make_zip(path: str, files: dict[str, bytes]) -> int:
    """Write a zip at ``path`` with the given member->bytes map.

    Returns the file's adler32, which is what resources.xml advertises.
    """
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    with open(path, 'rb') as f:
        return zlib.adler32(f.read()) & 0xFFFFFFFF


def _write_manifest(path: str, entries: list[dict]) -> None:
    """Write a resources.xml at ``path`` from a list of attribute dicts."""
    lines = ['<?xml version="1.0"?>', '<updates>']
    for e in entries:
        attrs = ' '.join(f'{k}="{v}"' for k, v in e.items())
        lines.append(f'<update {attrs}/>')
    lines.append('</updates>')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


def _host_url(host_dir: str) -> str:
    """Return a file:// URL with a trailing slash, suitable as a base."""
    return 'file://' + urllib.parse.quote(host_dir.rstrip('/')) + '/'


class _CachingTest(unittest.TestCase):
    """Base: each test method gets its own XDG_CACHE_HOME tempdir.

    ``mock.patch.dict(os.environ, ...)`` restores the *original* value
    rather than popping the key, so we don't leak into ~/.cache when
    tests/__init__.py has pre-set XDG_CACHE_HOME to a tempdir.
    """

    def setUp(self) -> None:
        self._cache_dir = tempfile.mkdtemp(prefix='tmw-resource-test-')
        self.addCleanup(self._wipe_cache)
        patcher = mock.patch.dict(os.environ,
                                  {'XDG_CACHE_HOME': self._cache_dir})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _wipe_cache(self) -> None:
        import shutil
        shutil.rmtree(self._cache_dir, ignore_errors=True)


class ManifestParseTest(_CachingTest):
    """The manifest parser must accept the live TMW format verbatim."""

    def test_parses_live_format(self):
        with tempfile.TemporaryDirectory() as host:
            zip_path = os.path.join(host, 'TMW.zip')
            adler = _make_zip(zip_path, {'items.xml': b'<items/>'})
            _write_manifest(os.path.join(host, 'resources.xml'), [
                {'type': 'data', 'file': 'TMW.zip',
                 'hash': f'{adler:08x}'},
                {'type': 'music', 'required': 'no',
                 'file': 'TMW-music.zip', 'hash': '13ffc394'},
            ])

            rm = ResourceManager()
            rm.update_from(_host_url(host), timeout=5.0)

            self.assertTrue(rm.ready())
            # Music must NOT be present in the overlay even though the
            # manifest listed it; we never want audio assets.
            self.assertEqual(rm.list_files(''), ['items.xml'])


class DownloadAndVerifyTest(_CachingTest):
    """Download path: missing zips are fetched and verified."""

    def test_downloads_missing_zip_and_verifies_adler(self):
        with tempfile.TemporaryDirectory() as host:
            zip_path = os.path.join(host, 'TMW.zip')
            adler = _make_zip(zip_path, {
                'items.xml': b'<items><item id="1" name="Bow"/></items>',
                'maps/test.tmx': b'<map width="3" height="3"/>',
            })
            _write_manifest(os.path.join(host, 'resources.xml'), [
                {'type': 'data', 'file': 'TMW.zip',
                 'hash': f'{adler:08x}'},
            ])

            rm = ResourceManager()
            rm.update_from(_host_url(host), timeout=5.0)

            self.assertTrue(rm.exists('items.xml'))
            self.assertTrue(rm.exists('maps/test.tmx'))
            self.assertIn(b'Bow', rm.open('items.xml'))

    def test_adler_mismatch_raises_and_leaves_no_partial_file(self):
        with tempfile.TemporaryDirectory() as host:
            zip_path = os.path.join(host, 'BAD.zip')
            _make_zip(zip_path, {'a.txt': b'hi'})
            # Lie about the hash.
            _write_manifest(os.path.join(host, 'resources.xml'), [
                {'type': 'data', 'file': 'BAD.zip', 'hash': 'deadbeef'},
            ])

            rm = ResourceManager()
            with self.assertRaisesRegex(Exception, 'adler32 mismatch'):
                rm.update_from(_host_url(host), timeout=5.0)

            # The .part should have been cleaned up, and the final
            # cache file should never have been created.
            for fname in os.listdir(self._cache_dir):
                full = os.path.join(self._cache_dir, fname)
                if os.path.isdir(full):
                    for inner in os.listdir(full):
                        self.assertFalse(
                            inner.endswith('.part'),
                            f'Partial file lingered: {inner}',
                        )
                        self.assertFalse(
                            inner == 'BAD.zip',
                            'Failed download should not leave the final file',
                        )


class OverlayShadowingTest(_CachingTest):
    """Later zips in the manifest shadow earlier ones."""

    def test_second_zip_shadows_first(self):
        with tempfile.TemporaryDirectory() as host:
            base_zip = os.path.join(host, 'base.zip')
            mods_zip = os.path.join(host, 'mods.zip')
            base_adler = _make_zip(base_zip, {
                'items.xml': b'<items><item id="1" name="Old"/></items>',
                'monsters.xml': b'<monsters><monster id="1" name="Slime"/></monsters>',
            })
            mods_adler = _make_zip(mods_zip, {
                'items.xml': b'<items><item id="1" name="New"/></items>',
            })
            _write_manifest(os.path.join(host, 'resources.xml'), [
                {'type': 'data', 'file': 'base.zip',
                 'hash': f'{base_adler:08x}'},
                {'type': 'data', 'file': 'mods.zip',
                 'hash': f'{mods_adler:08x}'},
            ])

            rm = ResourceManager()
            rm.update_from(_host_url(host), timeout=5.0)

            # mods.zip wins for items.xml...
            self.assertIn(b'New', rm.open('items.xml'))
            # ...but the file only in base.zip is still reachable.
            self.assertIn(b'Slime', rm.open('monsters.xml'))


class CacheReuseTest(_CachingTest):
    """Re-running update_from on an unchanged manifest is cheap."""

    def test_second_call_does_not_redownload(self):
        with tempfile.TemporaryDirectory() as host:
            zip_path = os.path.join(host, 'TMW.zip')
            adler = _make_zip(zip_path, {'a.txt': b'one'})
            _write_manifest(os.path.join(host, 'resources.xml'), [
                {'type': 'data', 'file': 'TMW.zip',
                 'hash': f'{adler:08x}'},
            ])

            rm = ResourceManager()
            rm.update_from(_host_url(host), timeout=5.0)
            self.assertEqual(rm.open('a.txt'), b'one')

            # Mutate the server's zip in a way that would corrupt a
            # second download IF the manager re-fetched. A correctly
            # caching manager should leave the local cache alone since
            # the manifest hash hasn't changed.
            os.remove(zip_path)
            with open(zip_path, 'wb') as f:
                f.write(b'not a zip')
            # Manifest still advertises the original (good) adler.
            rm2 = ResourceManager()
            rm2.update_from(_host_url(host), timeout=5.0)
            self.assertEqual(rm2.open('a.txt'), b'one',
                             'Cached good zip should not be replaced by'
                             ' broken upstream when adler still matches')


class OverrideDirTest(unittest.TestCase):
    """TMW_CLIENT_DATA bypasses the downloader entirely."""

    def test_override_reads_filesystem(self):
        with tempfile.TemporaryDirectory() as data:
            os.makedirs(os.path.join(data, 'maps'))
            with open(os.path.join(data, 'items.xml'), 'wb') as f:
                f.write(b'<items><item id="42" name="Override"/></items>')
            with open(os.path.join(data, 'maps', 'test.tmx'), 'wb') as f:
                f.write(b'<map/>')

            rm = ResourceManager(override_dir=data)
            self.assertTrue(rm.ready())
            self.assertIn(b'Override', rm.open('items.xml'))
            self.assertEqual(rm.list_files('maps'), ['test.tmx'])

    def test_override_via_env_var(self):
        with tempfile.TemporaryDirectory() as data:
            with open(os.path.join(data, 'monsters.xml'), 'wb') as f:
                f.write(b'<monsters/>')
            with mock.patch.dict(os.environ, {'TMW_CLIENT_DATA': data}):
                rm = ResourceManager()
                self.assertTrue(rm.ready())
                self.assertTrue(rm.exists('monsters.xml'))


class HostNormalizationTest(unittest.TestCase):
    """update_from accepts hosts with or without a trailing slash or scheme."""

    def test_normalization_adds_scheme_and_slash(self):
        # We only verify the normalizer; no network call. Using a host
        # that won't resolve guarantees we'd hit a clean error if the
        # normalizer fed the URL through.
        self.assertEqual(
            ResourceManager._normalize_host('updates.example.com'),
            'http://updates.example.com/',
        )
        self.assertEqual(
            ResourceManager._normalize_host('http://updates.example.com'),
            'http://updates.example.com/',
        )
        self.assertEqual(
            ResourceManager._normalize_host('https://updates.example.com/sub'),
            'https://updates.example.com/sub/',
        )


class ManifestRejectsBadRootTest(_CachingTest):
    """resources.xml with the wrong root tag should raise a clear error."""

    def test_wrong_root_tag(self):
        with tempfile.TemporaryDirectory() as host:
            with open(os.path.join(host, 'resources.xml'), 'w') as f:
                f.write('<not-updates/>')
            rm = ResourceManager()
            with self.assertRaisesRegex(RuntimeError, 'manifest root'):
                rm.update_from(_host_url(host), timeout=5.0)


if __name__ == '__main__':
    unittest.main()
