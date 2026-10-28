"""The dashboard reachability check and the SSH tunnel help."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from perseus.web import reach
from perseus.web.server import Server
from tests.test_server import make


class SelfCheck(unittest.TestCase):
    def test_a_working_dashboard_passes_every_step(self):
        visits = []
        dash, _, _ = make()
        srv = Server(dash, "127.0.0.1", 0, on_visit=lambda k, d: visits.append(k))
        srv.start()
        try:
            res = reach.self_check(srv.port, srv.token)
            self.assertTrue(all(ok for ok, _ in res), res)
            self.assertEqual(len(res), 5)
            self.assertEqual(visits, [])                       # the self-check isn't reported as a browser
            import http.client
            c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
            c.request("GET", "/")
            c.getresponse().read()
            c.request("GET", f"/?token={srv.token}")
            c.getresponse().read()
            c.close()
            self.assertEqual(visits, ["no_token", "signed_in"])
        finally:
            srv.stop()

    def test_wrong_token_and_nothing_listening(self):
        dash, _, _ = make()
        srv = Server(dash, "127.0.0.1", 0)
        srv.start()
        try:
            res = reach.self_check(srv.port, "wrong")
            self.assertFalse(res[1][0])
        finally:
            srv.stop()
        res = reach.self_check(srv.port, "x")
        self.assertEqual(len(res), 1)
        self.assertIn("nothing answers", res[0][1])


class Sshd(unittest.TestCase):
    def conf(self, main, extra=None):
        d = Path(tempfile.mkdtemp())
        (d / "sshd_config").write_text(main)
        if extra is not None:
            (d / "sshd_config.d").mkdir()
            (d / "sshd_config.d" / "50-cloud.conf").write_text(extra)
        return d

    def test_default_ubuntu_allows(self):
        d = self.conf("Include /etc/ssh/sshd_config.d/*.conf\nPasswordAuthentication no\n")
        self.assertIsNone(reach.ssh_forwarding(d))

    def test_included_file_turns_it_off(self):
        d = self.conf("Include sshd_config.d/*.conf\nAllowTcpForwarding yes\n", "AllowTcpForwarding no\n")
        self.assertIn("AllowTcpForwarding no", reach.ssh_forwarding(d))      # first one wins, as in sshd

    def test_match_blocks_and_comments_are_ignored(self):
        d = self.conf("# AllowTcpForwarding no\nMatch User guest\n  AllowTcpForwarding no\n")
        self.assertIsNone(reach.ssh_forwarding(d))

    def test_disable_forwarding(self):
        self.assertIn("DisableForwarding", reach.ssh_forwarding(self.conf("DisableForwarding yes\n")))


class TunnelCommand(unittest.TestCase):
    def test_uses_the_public_address_and_port_of_this_session(self):
        with mock.patch.dict(os.environ, {"SSH_CONNECTION": "203.0.113.9 51000 151.185.34.35 2222", "USER": "ubuntu"}):
            self.assertEqual(reach.tunnel_command(8080),
                             "ssh -N -o ExitOnForwardFailure=yes -L 8080:127.0.0.1:8080 -p 2222 ubuntu@151.185.34.35")

    def test_private_address_behind_nat_is_not_guessed(self):
        with mock.patch.dict(os.environ, {"SSH_CONNECTION": "203.0.113.9 51000 10.0.0.5 22", "USER": "ubuntu"}):
            self.assertIn("ubuntu@<the address you ssh to>", reach.tunnel_command(8080))


class Visits(unittest.TestCase):
    def test_each_kind_once(self):
        said = []
        v = reach.VisitReporter(said.append)
        for k in ("no_token", "no_token", "page", "signed_in", "signed_in"):
            v(k)
        self.assertEqual(len(said), 2)
        self.assertIn("without the token", said[0])


if __name__ == "__main__":
    unittest.main()
