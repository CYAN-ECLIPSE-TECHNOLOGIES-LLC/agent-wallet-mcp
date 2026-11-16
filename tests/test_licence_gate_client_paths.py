# Copyright (c) 2026 Cyan Eclipse Technologies LLC
# SPDX-License-Identifier: MIT
"""F-AW-TC1 regression battery (hermetic): the activation-client
loader takes its client location from deployment configuration
(CETLIC_CLIENT_DIR) only. Shipped sources carry no internal host
absolute path (Coding Standard CM-2), an unconfigured loader
returns None instead of falling back to an estate path, and a
configured directory still loads its client."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from licence_gate import _load_activation_client

ROOT = Path(__file__).resolve().parent.parent
CM2_SCAN_TARGETS = ("licence_gate.py", "server.py", "policy.py",
                    "smoke.py")
INTERNAL_PATH_MARKER = "/home/witness/"

STUB_ACTIVATION = '''
class ActivationClient:
    def __init__(self, base_url):
        self.base_url = base_url
'''


class TestNoInternalHostPath(unittest.TestCase):
    def test_cm2_scan_targets_carry_no_internal_host_path(self):
        for name in CM2_SCAN_TARGETS:
            source = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn(INTERNAL_PATH_MARKER, source,
                             f"{name} ships an internal host path")


class TestLoaderConfiguration(unittest.TestCase):
    def tearDown(self):
        for mod in ("client.activation", "client"):
            sys.modules.pop(mod, None)

    def test_loader_returns_none_when_client_dir_unset(self):
        env = {k: v for k, v in os.environ.items()
               if k != "CETLIC_CLIENT_DIR"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(
                _load_activation_client("http://127.0.0.1:8799"))

    def test_loader_returns_none_when_env_dir_has_no_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ,
                                 {"CETLIC_CLIENT_DIR": tmp}):
                self.assertIsNone(
                    _load_activation_client("http://127.0.0.1:8799"))

    def test_loader_loads_client_from_env_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "client"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("", encoding="utf-8")
            (pkg / "activation.py").write_text(STUB_ACTIVATION,
                                               encoding="utf-8")
            with mock.patch.dict(os.environ,
                                 {"CETLIC_CLIENT_DIR": tmp}):
                client = _load_activation_client("http://127.0.0.1:8799")
            self.assertIsNotNone(client)
            self.assertEqual(client.base_url, "http://127.0.0.1:8799")
            if tmp in sys.path:
                sys.path.remove(tmp)


if __name__ == "__main__":
    unittest.main()
