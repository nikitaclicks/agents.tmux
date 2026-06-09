#!/usr/bin/env python3
"""Shared indicator snapshot helpers for presentation frontends."""

from dataclasses import dataclass
import json
from typing import Sequence

from tmux_agents import Agent

STATUS_ICON = {"busy": "⚡", "waiting": "💬", "idle": "○"}


@dataclass(frozen=True)
class IndicatorItem:
    name: str
    icon: str
    window: str
    target: str
    status: str
    label: str
    snippet: str
    focusable: bool
    source: str = "local"


@dataclass(frozen=True)
class IndicatorSnapshot:
    badge: str
    state: str
    busy: int
    waiting: int
    idle: int
    items: tuple[IndicatorItem, ...]
    tooltip: str

    @property
    def classes(self) -> list[str]:
        return ["agents-tmux", self.state]


def can_focus_target(target: str) -> bool:
    return bool(target) and not target.startswith("[pid:")


def build_indicator_snapshot(agents: Sequence[Agent]) -> IndicatorSnapshot:
    items = tuple(_build_item(agent) for agent in agents)

    if not agents:
        return IndicatorSnapshot(
            badge="◌",
            state="empty",
            busy=0,
            waiting=0,
            idle=0,
            items=items,
            tooltip="agents.tmux\nNo agents found.",
        )

    busy = sum(1 for agent in agents if agent.status == "busy")
    waiting = sum(1 for agent in agents if agent.status == "waiting")
    idle = len(agents) - busy - waiting

    if busy == 0 and waiting == 0:
        badge = "○"
        state = "idle"
    elif waiting == 0:
        badge = f"🟢 {busy}"
        state = "busy"
    elif busy == 0:
        badge = f"🔴 {waiting}"
        state = "waiting"
    else:
        badge = f"🟡 {waiting}"
        state = "mixed"

    return IndicatorSnapshot(
        badge=badge,
        state=state,
        busy=busy,
        waiting=waiting,
        idle=idle,
        items=items,
        tooltip=_build_tooltip(items, busy, waiting, idle),
    )


def render_waybar_payload(snapshot: IndicatorSnapshot) -> dict:
    text = snapshot.badge
    primary = _primary_item(snapshot.items)
    if snapshot.state in {"busy", "waiting", "mixed"} and primary and primary.snippet:
        text = f"{snapshot.badge} {_truncate(primary.snippet, 28)}"

    return {
        "text": text,
        "alt": snapshot.state,
        "tooltip": snapshot.tooltip,
        "class": snapshot.classes,
    }


def render_waybar_json(snapshot: IndicatorSnapshot) -> str:
    return json.dumps(render_waybar_payload(snapshot), ensure_ascii=False)


def _build_item(agent: Agent) -> IndicatorItem:
    source = getattr(agent, "source", "local")
    location = agent.window if source == "local" else f"{source}:{agent.window}"
    return IndicatorItem(
        name=agent.name,
        icon=agent.icon,
        window=agent.window,
        target=agent.target,
        status=agent.status,
        label=f"{agent.icon} {agent.name} @ {location}  {STATUS_ICON[agent.status]} {agent.status}",
        snippet=agent.snippet,
        focusable=can_focus_target(agent.target),
        source=source,
    )


def _build_tooltip(
    items: Sequence[IndicatorItem],
    busy: int,
    waiting: int,
    idle: int,
) -> str:
    lines = [
        "agents.tmux",
        f"busy {busy} · waiting {waiting} · idle {idle}",
    ]

    for item in items:
        lines.append("")
        lines.append(item.label)
        if item.snippet:
            lines.append(f"  ↳ {item.snippet}")

    return "\n".join(lines)


def _primary_item(items: Sequence[IndicatorItem]) -> IndicatorItem | None:
    if not items:
        return None
    return sorted(items, key=_item_priority)[0]


def _item_priority(item: IndicatorItem) -> tuple[int, str]:
    priority = {"waiting": 0, "busy": 1, "idle": 2}.get(item.status, 3)
    return priority, item.name


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
