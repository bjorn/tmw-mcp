"""Tests for the env+file credentials loader.

Run with: .venv/bin/python -m unittest tests.test_credentials
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tmw_mcp.credentials import load_credentials


_TMW_VARS = (
    'TMW_USERNAME', 'TMW_PASSWORD', 'TMW_SERVER', 'TMW_PORT',
    'TMW_CHAR_NAME', 'TMW_WORLD',
    'TMW_CREDENTIALS_FILE',
)


def _clear_env():
    """Remove every TMW_* var so each test starts from a clean slate."""
    for v in _TMW_VARS:
        os.environ.pop(v, None)


class EnvOnlyTest(unittest.TestCase):

    def setUp(self) -> None:
        _clear_env()
        self.addCleanup(_clear_env)

    def test_env_vars_populate_creds(self):
        os.environ['TMW_USERNAME'] = 'alice'
        os.environ['TMW_PASSWORD'] = 'secret'
        os.environ['TMW_CHAR_NAME'] = 'Alicia'
        creds = load_credentials()
        self.assertEqual(creds['username'], 'alice')
        self.assertEqual(creds['password'], 'secret')
        self.assertEqual(creds['char_name'], 'Alicia')
        # Defaults still in place.
        self.assertEqual(creds['server'], 'server.themanaworld.org')
        self.assertEqual(creds['port'], 6901)

    def test_tmw_server_with_port_suffix(self):
        os.environ['TMW_SERVER'] = 'test.example.com:5555'
        creds = load_credentials()
        self.assertEqual(creds['server'], 'test.example.com')
        self.assertEqual(creds['port'], 5555)

    def test_tmw_server_without_port_keeps_default_port(self):
        os.environ['TMW_SERVER'] = 'test.example.com'
        creds = load_credentials()
        self.assertEqual(creds['server'], 'test.example.com')
        self.assertEqual(creds['port'], 6901)

    def test_bad_port_is_ignored(self):
        os.environ['TMW_PORT'] = 'not-a-number'
        creds = load_credentials()
        self.assertEqual(creds['port'], 6901)


class FilePlusEnvTest(unittest.TestCase):

    def setUp(self) -> None:
        _clear_env()
        self.addCleanup(_clear_env)

    def test_file_provides_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'creds.json')
            with open(path, 'w') as f:
                json.dump({
                    'username': 'bob', 'password': 'hunter2',
                    'server': 'old.example.com', 'port': 1234,
                }, f)
            creds = load_credentials(path)
            self.assertEqual(creds['username'], 'bob')
            self.assertEqual(creds['server'], 'old.example.com')
            self.assertEqual(creds['port'], 1234)

    def test_env_overrides_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'creds.json')
            with open(path, 'w') as f:
                json.dump({'username': 'file_user',
                           'password': 'file_pw'}, f)
            os.environ['TMW_USERNAME'] = 'env_user'
            creds = load_credentials(path)
            self.assertEqual(creds['username'], 'env_user')
            # Password unset in env so file value wins.
            self.assertEqual(creds['password'], 'file_pw')

    def test_missing_file_is_silent(self):
        creds = load_credentials('/nonexistent/path.json')
        # Default server still resolves; no exception.
        self.assertEqual(creds['server'], 'server.themanaworld.org')


class CredentialsFileEnvTest(unittest.TestCase):
    """``TMW_CREDENTIALS_FILE`` overrides the path argument."""

    def setUp(self) -> None:
        _clear_env()
        self.addCleanup(_clear_env)

    def test_env_file_wins_over_path_arg(self):
        with tempfile.TemporaryDirectory() as d:
            path_a = os.path.join(d, 'a.json')
            path_b = os.path.join(d, 'b.json')
            with open(path_a, 'w') as f:
                json.dump({'username': 'from_a'}, f)
            with open(path_b, 'w') as f:
                json.dump({'username': 'from_b'}, f)
            os.environ['TMW_CREDENTIALS_FILE'] = path_b
            creds = load_credentials(path_a)
            self.assertEqual(creds['username'], 'from_b')


class OverridesWinTest(unittest.TestCase):

    def setUp(self) -> None:
        _clear_env()
        self.addCleanup(_clear_env)

    def test_overrides_beat_env_and_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'creds.json')
            with open(path, 'w') as f:
                json.dump({'username': 'file_user',
                           'password': 'file_pw'}, f)
            os.environ['TMW_USERNAME'] = 'env_user'
            creds = load_credentials(
                path,
                user='cli_user',
                password='cli_pw',
            )
            self.assertEqual(creds['username'], 'cli_user')
            self.assertEqual(creds['password'], 'cli_pw')

    def test_empty_override_does_not_clobber(self):
        os.environ['TMW_USERNAME'] = 'env_user'
        creds = load_credentials(user='', password=None)
        # Empty string and None mean "no override".
        self.assertEqual(creds['username'], 'env_user')


if __name__ == '__main__':
    unittest.main()
