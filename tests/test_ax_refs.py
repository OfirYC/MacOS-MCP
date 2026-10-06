"""Reference-bound desktop actions, including failure boundaries."""

from dataclasses import dataclass, field

import pytest

from macos_mcp.desktop import ax_refs


@dataclass(eq=False)
class Element:
    pid: int
    role: str
    title: str = ""
    value: str = ""
    children: list["Element"] = field(default_factory=list)
    windows: list["Element"] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    settable: bool = False
    enabled: bool = True
    presses: int = 0


class RunningApp:
    def __init__(self, pid: int, generation: float, bundle: str):
        self.pid = pid
        self.generation = generation
        self.bundle = bundle

    def bundleIdentifier(self):
        return self.bundle

    def processIdentifier(self):
        return self.pid

    def launchDate(self):
        return self

    def timeIntervalSinceReferenceDate(self):
        return self.generation


@pytest.fixture
def world(monkeypatch):
    apps = {}
    running = {}
    for pid, bundle in [(101, "test.app.a"), (202, "test.app.b")]:
        button = Element(pid, "AXButton", "Save", actions=["AXPress"])
        field = Element(pid, "AXTextField", "Name", settable=True)
        window = Element(pid, "AXWindow", f"Window {pid}", children=[button, field])
        apps[pid] = Element(pid, "AXApplication", windows=[window])
        running[pid] = RunningApp(pid, float(pid), bundle)

    class Control:
        def __init__(self, pid):
            self.Element = apps[pid]

    def attribute(element, key):
        return {
            "AXRole": element.role,
            "AXTitle": element.title,
            "AXValue": element.value,
            "AXWindows": element.windows,
            "AXEnabled": element.enabled,
        }.get(key)

    monkeypatch.setattr(ax_refs.ax, "Control", Control)
    monkeypatch.setattr(ax_refs.ax, "GetAttribute", attribute)
    monkeypatch.setattr(ax_refs.ax, "GetChildren", lambda element: element.children)
    monkeypatch.setattr(ax_refs.ax, "GetElementPid", lambda element: element.pid)
    monkeypatch.setattr(ax_refs.ax, "GetActionNames", lambda element: element.actions)
    monkeypatch.setattr(
        ax_refs.ax, "IsAttributeSettable", lambda element, key: element.settable
    )
    monkeypatch.setattr(
        ax_refs.ax, "PerformAction", lambda element, key: _press(element, key)
    )
    monkeypatch.setattr(
        ax_refs.ax,
        "SetAttribute",
        lambda element, key, value: _set(element, key, value),
    )
    monkeypatch.setattr(ax_refs, "_running_app", lambda pid: running.get(pid))
    return ax_refs.AXRefService(), apps, running


def _press(element, key):
    if key != "AXPress" or key not in element.actions:
        return False
    element.presses += 1
    return True


def _set(element, key, value):
    if key != "AXValue" or not element.settable:
        return False
    element.value = value
    return True


def test_observe_press_reobserve_and_session_process_binding(world):
    service, apps, _ = world
    first = service.observe("chat-a", 101)
    ref = next(node["ref"] for node in first["nodes"] if node["name"] == "Save")
    assert first["pid"] == 101
    assert first["window_title"] == "Window 101"
    assert first["windows"] == [{"index": 0, "title": "Window 101"}]
    with pytest.raises(ValueError, match="session"):
        service.press("chat-b", ref)
    with pytest.raises(ValueError, match="process"):
        service.press("chat-a", ref, pid=202)
    assert apps[101].windows[0].children[0].presses == 0
    assert apps[202].windows[0].children[0].presses == 0
    assert service.press("chat-a", ref)["ok"] is True
    assert apps[101].windows[0].children[0].presses == 1
    assert apps[202].windows[0].children[0].presses == 0
    with pytest.raises(ValueError, match="(?i)stale"):
        service.press("chat-a", ref)
    assert service.observe("chat-a", 101)["nodes"][0]["name"] == "Save"


def test_stale_element_window_process_and_pid_reuse_fail_closed(world):
    service, apps, running = world
    ref = service.observe("chat-a", 101)["nodes"][0]["ref"]
    apps[101].windows[0].children[0] = Element(
        101, "AXButton", "Save", actions=["AXPress"]
    )
    with pytest.raises(ValueError, match="(?i)stale"):
        service.press("chat-a", ref)

    ref = service.observe("chat-a", 101)["nodes"][0]["ref"]
    apps[101].windows = [Element(101, "AXWindow", "Window 101")]
    with pytest.raises(ValueError, match="(?i)stale"):
        service.press("chat-a", ref)

    apps[101].windows[0].children = [
        Element(101, "AXButton", "Save", actions=["AXPress"])
    ]
    ref = service.observe("chat-a", 101)["nodes"][0]["ref"]
    running[101] = RunningApp(101, 999.0, "test.app.a")
    with pytest.raises(ValueError, match="(?i)stale"):
        service.press("chat-a", ref)

    running.pop(101)
    with pytest.raises(ValueError, match="(?i)stale"):
        service.press("chat-a", ref)


def test_value_control_verifies_exact_result_and_reports_unsupported_fallback(world):
    service, apps, _ = world
    nodes = service.observe("chat-a", 101)["nodes"]
    field_ref = next(node["ref"] for node in nodes if node["name"] == "Name")
    assert service.set_value("chat-a", field_ref, "Eden")["value"] == "Eden"
    assert apps[101].windows[0].children[1].value == "Eden"
    assert any(
        node.get("value") == "Eden" for node in service.observe("chat-a", 101)["nodes"]
    )
    apps[101].windows[0].children[1].settable = False
    field_ref = next(
        node["ref"]
        for node in service.observe("chat-a", 101)["nodes"]
        if node["name"] == "Name"
    )
    with pytest.raises(ValueError, match="fallback"):
        service.set_value("chat-a", field_ref, "Other")


def test_actionable_nodes_survive_long_static_context(world):
    service, apps, _ = world
    window = apps[101].windows[0]
    window.children = [
        Element(101, "AXStaticText", f"Paragraph {i}") for i in range(85)
    ] + [Element(101, "AXButton", "Continue", actions=["AXPress"])]
    result = service.observe("chat-a", 101)
    assert result["truncated"] is True
    assert len(result["nodes"]) <= 80
    assert result["nodes"][0]["name"] == "Paragraph 0"
    assert result["nodes"][-1]["name"] == "Continue"
    assert result["nodes"][-1]["ref"]


def test_failed_native_mutation_cannot_replay_a_ref(world, monkeypatch):
    service, apps, _ = world
    ref = service.observe("chat-a", 101)["nodes"][0]["ref"]

    def ambiguous_press(element, _action):
        element.presses += 1
        return False

    monkeypatch.setattr(ax_refs.ax, "PerformAction", ambiguous_press)
    with pytest.raises(ValueError, match="AXPress failed"):
        service.press("chat-a", ref)
    assert apps[101].windows[0].children[0].presses == 1
    with pytest.raises(ValueError, match="(?i)stale"):
        service.press("chat-a", ref)
    assert apps[101].windows[0].children[0].presses == 1
