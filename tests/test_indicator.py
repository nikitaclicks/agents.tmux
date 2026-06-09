import unittest

from indicator import build_indicator_snapshot, render_waybar_payload
from tmux_agents import Agent


class IndicatorSnapshotTests(unittest.TestCase):
    def test_empty_snapshot(self):
        snapshot = build_indicator_snapshot([])

        self.assertEqual(snapshot.badge, "◌")
        self.assertEqual(snapshot.state, "empty")
        self.assertEqual(snapshot.tooltip, "agents.tmux\nNo agents found.")

    def test_all_idle_is_red(self):
        snapshot = build_indicator_snapshot([
            Agent(
                name="claude",
                icon="◆",
                window="main",
                target="main:1.1",
                status="idle",
                snippet="❯",
            ),
        ])

        self.assertTrue(snapshot.badge.startswith("🔴"))
        self.assertEqual(snapshot.state, "idle")

    def test_waybar_prefers_waiting_snippet(self):
        snapshot = build_indicator_snapshot([
            Agent(
                name="claude",
                icon="◆",
                window="main",
                target="main:1.1",
                status="busy",
                snippet="Editing…",
            ),
            Agent(
                name="copilot",
                icon="◇",
                window="feature",
                target="feature:2.1",
                status="waiting",
                snippet="Asked user",
            ),
        ])

        payload = render_waybar_payload(snapshot)

        self.assertEqual(snapshot.badge, "🟡 1")
        self.assertEqual(payload["alt"], "mixed")
        self.assertEqual(payload["class"], ["agents-tmux", "mixed"])
        self.assertIn("Asked user", payload["text"])
        self.assertIn("copilot", snapshot.tooltip)

    def test_external_targets_are_not_focusable(self):
        snapshot = build_indicator_snapshot([
            Agent(
                name="cursor",
                icon="⌶",
                window="[external]",
                target="[pid:1234]",
                status="idle",
                snippet="",
            ),
        ])

        self.assertFalse(snapshot.items[0].focusable)


if __name__ == "__main__":
    unittest.main()
