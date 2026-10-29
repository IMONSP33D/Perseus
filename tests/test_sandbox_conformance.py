"""Runs the backend conformance suite (tests/sandbox_conformance.py) against each backend that can run here.

  local_unsafe  runs when `unshare -rn` works (Linux with user namespaces): exercises the full
                runner path with real compilers, no account needed. Never a production backend.
  e2b           runs only with PERSEUS_TEST_E2B=1 and an E2B key (E2B_API_KEY, or
                PERSEUS_TEST_E2B_KEY_FILE); uses PERSEUS_TEST_E2B_TEMPLATE if set. Costs a few cents.
"""

import os
import unittest

from perseus.sandbox.backends import local_unsafe
from tests.sandbox_conformance import Conformance


@unittest.skipUnless(local_unsafe.namespaces_work(), "needs `unshare -rn` (user + network namespaces)")
class LocalUnsafeConformance(Conformance, unittest.TestCase):
    def make_backend(self):
        return local_unsafe.LocalUnsafeBackend({}, environ={local_unsafe.FLAG: "1"})


@unittest.skipUnless(os.environ.get("PERSEUS_TEST_E2B") == "1", "set PERSEUS_TEST_E2B=1 and an E2B key to run")
class E2BConformance(Conformance, unittest.TestCase):
    def make_backend(self):
        from perseus.sandbox.backends.e2b import E2BBackend
        return E2BBackend({"key_file": os.environ.get("PERSEUS_TEST_E2B_KEY_FILE", ""),
                           "e2b_template": os.environ.get("PERSEUS_TEST_E2B_TEMPLATE", "")})


if __name__ == "__main__":
    unittest.main()
