import json
import unittest
from unittest import mock

import tmux_agents
from indicator import build_indicator_snapshot
from tmux_agents import (
    Agent,
    _config_b64,
    _parse_ssh_host,
    _remote_hosts,
    discover_remote_agents,
)


class ParseSshHostTests(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(_parse_ssh_host("ssh box"), "box")

    def test_strips_user(self):
        self.assertEqual(_parse_ssh_host("ssh -o BatchMode=yes user@host cmd"), "host")

    def test_skips_option_args(self):
        self.assertEqual(_parse_ssh_host("/usr/bin/ssh -p 22 -i key host"), "host")
        self.assertEqual(_parse_ssh_host("ssh -L 8080:localhost:80 gw"), "gw")

    def test_rejects_non_ssh(self):
        self.assertIsNone(_parse_ssh_host("sshd: nikita"))
        self.assertIsNone(_parse_ssh_host("rsync foo bar"))

    def test_git_target_extracted(self):
        # Filtering of non-shell targets is the caller's job (ignore_hosts).
        self.assertEqual(_parse_ssh_host("ssh git@github.com git-upload-pack"), "github.com")


class RemoteHostsTests(unittest.TestCase):
    def _hosts(self, discovered, config):
        with mock.patch.object(tmux_agents, "_ssh_hosts_from_ps", return_value=set(discovered)):
            return _remote_hosts(config)

    def test_union_of_pinned_and_discovered(self):
        hosts = self._hosts({"a", "b"}, {"remote_hosts": ["c"], "auto_discover_remote": True})
        self.assertEqual(hosts, ["a", "b", "c"])

    def test_ignore_filter(self):
        hosts = self._hosts({"a", "github.com"}, {"ignore_hosts": ["github.com"]})
        self.assertEqual(hosts, ["a"])

    def test_allow_filter(self):
        hosts = self._hosts({"a", "b"}, {"allow_hosts": ["b"]})
        self.assertEqual(hosts, ["b"])

    def test_auto_discovery_disabled(self):
        hosts = self._hosts({"a"}, {"auto_discover_remote": False, "remote_hosts": ["c"]})
        self.assertEqual(hosts, ["c"])


class ConfigShippingTests(unittest.TestCase):
    def test_b64_carries_only_portable_keys(self):
        config = {
            "session": "auto",
            "detection": {"tail_lines": 5},
            "agents": [{"name": "x"}],
            "remote_hosts": ["secret-host"],
            "ssh_options": ["-o", "X"],
        }
        decoded = json.loads(
            __import__("base64").b64decode(_config_b64(config))
        )
        # Detection rules travel; remote-orchestration keys never do (no recursion).
        self.assertEqual(set(decoded), {"session", "detection", "agents"})
        self.assertNotIn("remote_hosts", decoded)


class DiscoverRemoteAgentsTests(unittest.TestCase):
    def _fake_run(self, stdout="", returncode=0, stderr=""):
        return mock.Mock(stdout=stdout, returncode=returncode, stderr=stderr)

    def test_parses_json_and_tags_source(self):
        payload = json.dumps([
            {"name": "pi", "icon": "π", "window": "main", "target": "main:1.1",
             "status": "busy", "snippet": "...", "source": "local"},
        ])
        with mock.patch.object(tmux_agents.subprocess, "run", return_value=self._fake_run(payload)):
            agents = discover_remote_agents("box", {}, script="#", config_b64="x")
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0].source, "box")  # source overridden to the host
        self.assertEqual(agents[0].name, "pi")

    def test_raises_on_failure(self):
        with mock.patch.object(tmux_agents.subprocess, "run",
                               return_value=self._fake_run(returncode=255, stderr="denied")):
            with self.assertRaises(RuntimeError):
                discover_remote_agents("box", {}, script="#", config_b64="x")


class RemoteLabelTests(unittest.TestCase):
    def test_remote_agent_labeled_with_host(self):
        snapshot = build_indicator_snapshot([
            Agent(name="pi", icon="π", window="main", target="main:1.1",
                  status="idle", snippet="", source="box"),
        ])
        item = snapshot.items[0]
        self.assertEqual(item.source, "box")
        self.assertIn("box:main", item.label)
        # Remote tmux panes remain focusable (driven over ssh).
        self.assertTrue(item.focusable)


if __name__ == "__main__":
    unittest.main()
