"""Update-host downloader and ZIP overlay reader.

The TMW server sends an update-host URL in SMSG_UPDATE_HOST (0x0063)
shortly after a successful login. The Mana reference client treats
that URL as a base for a small manifest, ``resources.xml``, listing
every ZIP that makes up the current client data (maps, items, monster
sprites, mods, optional music). This module mirrors that flow in
Python: download the manifest, fetch any zips not already cached,
verify their adler32 checksums, and present the union of their
contents as a read-only filesystem keyed by zip-internal path.

Manifest format (from updates.themanaworld.org/resources.xml):

    <updates>
        <update type="data" file="TMW.zip" hash="3ead88a3"/>
        <update type="music" required="no" file="TMW-music.zip" .../>
    </updates>

The ``hash`` attribute is an 8-hex adler32 of the zip file's bytes.

Stdlib only: urllib.request, zipfile, zlib, xml.etree.ElementTree.

The cache lives under ``${XDG_CACHE_HOME:-~/.cache}/tmw-mcp/<host>/``
where ``<host>`` is a sha1 fingerprint of the update-host URL, so two
servers (live + test, for instance) stay isolated.

Setting ``TMW_CLIENT_DATA`` to a directory short-circuits the
downloader entirely and reads files from that directory. Useful for
tests and for developers iterating on local TMX or item XML edits
without re-zipping.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
import zlib
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class UpdateEntry:
    """One ``<update>`` row from resources.xml."""

    name: str
    adler32: int
    type: str = 'data'
    required: bool = True
    description: str = ''


def _cache_root() -> str:
    base = os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache')
    return os.path.join(base, 'tmw-mcp')


def _host_dir(host_url: str) -> str:
    # sha1 fingerprint instead of url-encoding so we can keep paths short
    # and predictable regardless of host scheme/path quirks.
    digest = hashlib.sha1(host_url.encode('utf-8')).hexdigest()[:12]
    return os.path.join(_cache_root(), digest)


class ResourceManager:
    """Thread-safe layered read-only view of TMW client data.

    Reads are served from the union of any zips opened so far, with
    later entries shadowing earlier ones (matching Mana's update-on-top
    semantics). ``update_from(host_url)`` is the bridge between the
    update-host protocol and the overlay: it downloads what's missing,
    verifies hashes, and rebuilds the overlay atomically.

    If ``TMW_CLIENT_DATA`` is set in the environment, the overlay is
    bypassed and reads go straight to that filesystem directory.
    """

    def __init__(self, override_dir: str | None = None) -> None:
        env_override = os.environ.get('TMW_CLIENT_DATA') or ''
        self.override_dir: str | None = override_dir or (env_override or None)
        self._zips: list[zipfile.ZipFile] = []
        self._zip_paths: list[str] = []
        self._lock = threading.RLock()
        self._ready = threading.Event()
        if self.override_dir:
            self._ready.set()
            log.info('TMW_CLIENT_DATA override: %s', self.override_dir)

    # -- updater path ------------------------------------------------------

    def update_from(self, host_url: str, timeout: float = 60.0) -> None:
        """Fetch resources.xml at ``host_url`` and refresh the overlay.

        Idempotent: a second call with the same manifest only re-opens
        the cached zips; nothing is re-downloaded unless a hash changes.

        Network errors are logged and re-raised so callers (typically a
        background thread on the game loop) can surface them.
        """
        if self.override_dir:
            log.info('Skipping update_from: TMW_CLIENT_DATA override active')
            return

        base = self._normalize_host(host_url)
        cache_dir = _host_dir(base)
        os.makedirs(cache_dir, exist_ok=True)

        entries = self._fetch_manifest(base, timeout=timeout)
        wanted: list[tuple[UpdateEntry, str]] = []
        for entry in entries:
            if entry.type == 'music':
                continue  # We never need audio.
            local = os.path.join(cache_dir, entry.name)
            wanted.append((entry, local))

        for entry, local in wanted:
            if _file_matches(local, entry.adler32):
                log.debug('Cached: %s', entry.name)
                continue
            log.info('Downloading %s', entry.name)
            try:
                _download(base + entry.name, local, timeout=timeout,
                          expected_adler32=entry.adler32)
            except Exception as e:
                # A missing optional entry should not break the overlay.
                if entry.required:
                    raise
                log.warning('Optional %s failed to download: %s', entry.name, e)

        with self._lock:
            for z in self._zips:
                try:
                    z.close()
                except Exception:
                    pass
            self._zips = []
            self._zip_paths = []
            for entry, local in wanted:
                if not os.path.exists(local):
                    continue
                try:
                    self._zips.append(zipfile.ZipFile(local, 'r'))
                    self._zip_paths.append(local)
                except zipfile.BadZipFile as e:
                    log.warning('Bad zip %s, skipping: %s', entry.name, e)
            self._ready.set()
            log.info('Resource overlay ready (%d zips, host=%s)',
                     len(self._zips), base)

    @staticmethod
    def _normalize_host(host_url: str) -> str:
        url = host_url.strip()
        if not url:
            raise ValueError('empty update host URL')
        if '://' not in url:
            url = 'http://' + url
        if not url.endswith('/'):
            url += '/'
        return url

    @staticmethod
    def _fetch_manifest(base: str, timeout: float) -> list[UpdateEntry]:
        url = base + 'resources.xml'
        log.info('Fetching manifest: %s', url)
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = resp.read()
        try:
            root = ET.fromstring(data)
        except ET.ParseError as e:
            raise RuntimeError(f'Bad resources.xml from {url}: {e}') from e
        if root.tag != 'updates':
            raise RuntimeError(
                f'Unexpected manifest root {root.tag!r} from {url} '
                f'(expected <updates>)'
            )
        out: list[UpdateEntry] = []
        for node in root.findall('update'):
            name = (node.get('file') or '').strip()
            hash_hex = (node.get('hash') or '').strip()
            if not name or not hash_hex:
                continue
            try:
                adler = int(hash_hex, 16)
            except ValueError:
                log.warning('Bad hash for %s: %r (skipped)', name, hash_hex)
                continue
            out.append(UpdateEntry(
                name=name,
                adler32=adler,
                type=node.get('type', 'data'),
                required=node.get('required', 'yes') == 'yes',
                description=node.get('description', ''),
            ))
        return out

    # -- reader path -------------------------------------------------------

    def ready(self) -> bool:
        return self._ready.is_set()

    def wait_ready(self, timeout: float | None = None) -> bool:
        return self._ready.wait(timeout=timeout)

    def open(self, path: str) -> bytes:
        """Read a file by its zip-internal path. Last-write-wins overlay.

        Raises ``FileNotFoundError`` if the file is not in any source.
        """
        norm = path.replace('\\', '/').lstrip('/')
        if self.override_dir:
            fs_path = os.path.join(self.override_dir, norm)
            if os.path.isfile(fs_path):
                with open(fs_path, 'rb') as f:
                    return f.read()
            raise FileNotFoundError(norm)
        with self._lock:
            for z in reversed(self._zips):
                try:
                    return z.read(norm)
                except KeyError:
                    continue
        raise FileNotFoundError(norm)

    def open_text(self, path: str, encoding: str = 'utf-8') -> str:
        return self.open(path).decode(encoding)

    def exists(self, path: str) -> bool:
        try:
            self.open(path)
            return True
        except FileNotFoundError:
            return False

    def list_files(self, dir_path: str) -> list[str]:
        """Return filenames (not subdirectory entries) directly under
        ``dir_path``, deduplicated across the overlay.
        """
        prefix = dir_path.replace('\\', '/').strip('/')
        if prefix:
            prefix += '/'
        seen: set[str] = set()
        if self.override_dir:
            fs_path = os.path.join(self.override_dir, prefix.rstrip('/'))
            if os.path.isdir(fs_path):
                for name in os.listdir(fs_path):
                    if os.path.isfile(os.path.join(fs_path, name)):
                        seen.add(name)
            return sorted(seen)
        with self._lock:
            for z in self._zips:
                for entry in z.namelist():
                    if not entry.startswith(prefix):
                        continue
                    rest = entry[len(prefix):]
                    if not rest or '/' in rest:
                        continue
                    seen.add(rest)
        return sorted(seen)

    def close(self) -> None:
        with self._lock:
            for z in self._zips:
                try:
                    z.close()
                except Exception:
                    pass
            self._zips = []
            self._zip_paths = []
            self._ready.clear()
            if self.override_dir:
                self._ready.set()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _file_matches(path: str, adler: int) -> bool:
    if not os.path.exists(path):
        return False
    return _adler32_file(path) == adler


def _adler32_file(path: str) -> int:
    val = 1
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            val = zlib.adler32(chunk, val)
    return val


def _download(
    url: str,
    dest: str,
    *,
    timeout: float,
    expected_adler32: int | None,
) -> None:
    """Stream a zip to ``dest.part`` then atomically rename on success.

    Verifies adler32 inline so a hash mismatch never leaves a corrupt
    file at the final path. The cache directory is created by the
    caller; we only handle the file itself.
    """
    tmp = dest + '.part'
    val = 1
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        with open(tmp, 'wb') as f:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                val = zlib.adler32(chunk, val)
    if expected_adler32 is not None and val != expected_adler32:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError(
            f'adler32 mismatch for {os.path.basename(dest)}: '
            f'got {val:08x}, want {expected_adler32:08x}'
        )
    os.replace(tmp, dest)


# ---------------------------------------------------------------------------
# Process-wide default manager
# ---------------------------------------------------------------------------


_default: ResourceManager | None = None
_default_lock = threading.Lock()


def default_manager() -> ResourceManager:
    """Process-wide singleton consulted by items.py, maps.py, monsters.py."""
    global _default
    with _default_lock:
        if _default is None:
            _default = ResourceManager()
        return _default


def reset_default_manager() -> None:
    """Test hook: drop the singleton so the next call rebuilds it.

    Useful when a test wants to flip ``TMW_CLIENT_DATA`` between cases.
    """
    global _default
    with _default_lock:
        if _default is not None:
            _default.close()
        _default = None
