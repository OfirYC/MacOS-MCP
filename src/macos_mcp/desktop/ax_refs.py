"""Bounded macOS accessibility observations and process-bound element actions."""

import secrets
import time
from collections import OrderedDict, deque
from dataclasses import dataclass

import psutil
from Cocoa import NSRunningApplication

import macos_mcp.ax as ax

MAX_NODES = 80
MAX_CONTEXT = 16
MAX_VISITED = 300
MAX_DEPTH = 10
MAX_REFS = 400
REF_TTL_SECONDS = 300
ACTION_ROLES = frozenset(
    {
        "AXButton",
        "AXCheckBox",
        "AXRadioButton",
        "AXPopUpButton",
        "AXMenuItem",
        "AXTextField",
        "AXTextArea",
        "AXComboBox",
        "AXSlider",
        "AXSwitch",
    }
)
VALUE_ROLES = frozenset({"AXTextField", "AXTextArea", "AXComboBox"})


def _identity(pid: int) -> float | None:
    """Bind a PID to its kernel process start time, even before AppKit registers it."""
    try:
        return float(psutil.Process(pid).create_time())
    except (psutil.Error, OSError, ValueError):
        return None


def _bundle_id(pid: int) -> str | None:
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    bundle = app.bundleIdentifier() if app is not None else None
    return str(bundle) if bundle else None


def _same_element(left, right) -> bool:
    return left == right


@dataclass
class _Ref:
    owner: str
    pid: int
    identity: float
    window: object
    element: object
    role: str
    observed_at: float


class AXRefService:
    """Keeps opaque AX references inside one MCP server process.

    The refs are never serialized. A new observation or action invalidates old
    refs for its owner and process; server restart also invalidates everything.
    """

    def __init__(self):
        self._refs: OrderedDict[str, _Ref] = OrderedDict()

    def _clear(self, owner: str, pid: int) -> None:
        for key, entry in list(self._refs.items()):
            if entry.owner == owner and entry.pid == pid:
                self._refs.pop(key, None)

    def observe(self, owner: str, pid: int, window_index: int = 0) -> dict:
        """Read a bounded AX tree from an exact running process and window.

        Args:
            owner: MCP session identifier.
            pid: Target application process ID.
            window_index: Zero-based index into the app's current AXWindows.

        Returns:
            Bounded nodes with opaque refs for actionable roles.

        Raises:
            ValueError: The process/window cannot be bound to a live generation.
        """
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise ValueError("A positive application PID is required")
        identity = _identity(pid)
        if identity is None:
            raise ValueError(
                "Application process is unavailable or has no launch identity"
            )
        app_element = ax.Control(pid=pid).Element
        windows = ax.GetAttribute(app_element, "AXWindows") or []
        if (
            not isinstance(window_index, int)
            or window_index < 0
            or window_index >= len(windows)
        ):
            raise ValueError(
                "Application window index is unavailable; inspect the app again"
            )
        window = windows[window_index]
        if (
            ax.GetElementPid(window) != pid
            or ax.GetAttribute(window, "AXRole") != "AXWindow"
        ):
            raise ValueError("Application window cannot be bound to this process")

        candidates = []
        queue = deque([(child, 1) for child in ax.GetChildren(window)])
        visited = 0
        while queue and visited < MAX_VISITED:
            element, depth = queue.popleft()
            visited += 1
            if ax.GetElementPid(element) != pid:
                continue
            role = str(ax.GetAttribute(element, "AXRole") or "")
            title = ax.GetAttribute(element, "AXTitle") or ax.GetAttribute(
                element, "AXDescription"
            )
            value = ax.GetAttribute(element, "AXValue")
            name = str(title or (value if role not in VALUE_ROLES else "") or "")[:160]
            if role in ACTION_ROLES or name:
                candidates.append((element, role, name, value))
            if depth < MAX_DEPTH:
                queue.extend((child, depth + 1) for child in ax.GetChildren(element))

        context = [
            (i, item)
            for i, item in enumerate(candidates)
            if item[1] not in ACTION_ROLES
        ][:MAX_CONTEXT]
        actions = [
            (i, item) for i, item in enumerate(candidates) if item[1] in ACTION_ROLES
        ][: MAX_NODES - len(context)]
        selected = sorted(actions + context, key=lambda item: item[0])
        self._clear(owner, pid)
        nodes = []
        for _, (element, role, name, value) in selected:
            node = {"role": role, "name": name}
            if role in VALUE_ROLES:
                node["value"] = str(value or "")[:160]
            if role in ACTION_ROLES:
                ref = secrets.token_urlsafe(18)
                self._refs[ref] = _Ref(
                    owner, pid, identity, window, element, role, time.monotonic()
                )
                node["ref"] = ref
            nodes.append(node)
        while len(self._refs) > MAX_REFS:
            self._refs.popitem(last=False)
        return {
            "pid": pid,
            "bundle_id": _bundle_id(pid),
            "window_index": window_index,
            "window_title": str(ax.GetAttribute(window, "AXTitle") or "")[:160],
            "windows": [
                {
                    "index": index,
                    "title": str(ax.GetAttribute(item, "AXTitle") or "")[:160],
                }
                for index, item in enumerate(windows[:10])
            ],
            "nodes": nodes,
            "truncated": bool(queue) or len(candidates) > len(selected),
        }

    def _resolve(self, owner: str, ref: str, pid: int | None = None) -> _Ref:
        entry = self._refs.get(ref)
        if entry is None:
            raise ValueError("Stale AX reference; observe the application again")
        if entry.owner != owner:
            raise ValueError("AX reference belongs to another session")
        if pid is not None and pid != entry.pid:
            raise ValueError("AX reference belongs to another process")
        if time.monotonic() - entry.observed_at > REF_TTL_SECONDS:
            self._refs.pop(ref, None)
            raise ValueError("Stale AX reference; observe the application again")
        if _identity(entry.pid) != entry.identity:
            self._refs.pop(ref, None)
            raise ValueError("Stale AX reference; application process changed")
        app_element = ax.Control(pid=entry.pid).Element
        windows = ax.GetAttribute(app_element, "AXWindows") or []
        if not any(_same_element(window, entry.window) for window in windows):
            self._refs.pop(ref, None)
            raise ValueError("Stale AX reference; window changed")
        if (
            ax.GetElementPid(entry.window) != entry.pid
            or ax.GetAttribute(entry.window, "AXRole") != "AXWindow"
        ):
            self._refs.pop(ref, None)
            raise ValueError("Stale AX reference; window is unavailable")

        # Check exact membership, not merely role/name: a replacement control
        # with the same label must never inherit an old ref.
        queue = deque([(child, 1) for child in ax.GetChildren(entry.window)])
        visited = 0
        found = False
        while queue and visited < MAX_VISITED:
            element, depth = queue.popleft()
            visited += 1
            if _same_element(element, entry.element):
                found = True
                break
            if depth < MAX_DEPTH:
                queue.extend((child, depth + 1) for child in ax.GetChildren(element))
        if (
            not found
            or ax.GetElementPid(entry.element) != entry.pid
            or ax.GetAttribute(entry.element, "AXRole") != entry.role
        ):
            self._refs.pop(ref, None)
            raise ValueError("Stale AX reference; control was replaced or detached")
        return entry

    def press(self, owner: str, ref: str, pid: int | None = None) -> dict:
        """Invoke AXPress on a bound control without global mouse input."""
        entry = self._resolve(owner, ref, pid)
        if ax.GetAttribute(entry.element, "AXEnabled") is False:
            raise ValueError("AX control is disabled")
        if "AXPress" not in ax.GetActionNames(entry.element):
            raise ValueError(
                "AXPress is unsupported; use a fresh Snapshot and Click fallback"
            )
        try:
            pressed = ax.PerformAction(entry.element, "AXPress")
        finally:
            # An AX error does not prove the app ignored the action.
            self._clear(owner, entry.pid)
        if not pressed:
            raise ValueError("AXPress failed; observe the application before retrying")
        return {
            "ok": True,
            "pid": entry.pid,
            "action": "press",
            "verify": "Observe again to verify the result",
        }

    def set_value(
        self, owner: str, ref: str, value: str, pid: int | None = None
    ) -> dict:
        """Set a plain text AXValue and check the exact resulting value."""
        if not isinstance(value, str):
            raise ValueError("Value must be text")
        entry = self._resolve(owner, ref, pid)
        if entry.role not in VALUE_ROLES or not ax.IsAttributeSettable(
            entry.element, "AXValue"
        ):
            raise ValueError(
                "AXValue is unsupported; use a fresh Snapshot and Type fallback"
            )
        if ax.GetAttribute(entry.element, "AXEnabled") is False:
            raise ValueError("AX control is disabled")
        try:
            updated = ax.SetAttribute(entry.element, "AXValue", value)
        finally:
            self._clear(owner, entry.pid)
        if not updated:
            raise ValueError(
                "AXValue update failed; observe the application before retrying"
            )
        for _ in range(6):
            actual = ax.GetAttribute(entry.element, "AXValue")
            if actual == value:
                return {
                    "ok": True,
                    "pid": entry.pid,
                    "action": "set_value",
                    "value": value,
                    "verify": "Observe again to verify the result",
                }
            time.sleep(0.05)
        raise ValueError(
            "AXValue did not match the requested value; observe before retrying"
        )
