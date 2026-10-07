"""MCP registration and session routing for bound AX references."""

import pytest
from fastmcp import Client

import macos_mcp.__main__ as server


@pytest.mark.asyncio
async def test_ax_tool_names_and_session_owner_are_stable_across_calls(monkeypatch):
    class FakeRefs:
        def __init__(self):
            self.calls = []

        def observe(self, owner, pid, window_index):
            self.calls.append(("observe", owner, pid, window_index))
            return {"pid": pid, "nodes": [{"ref": "opaque"}]}

        def press(self, owner, ref, pid):
            self.calls.append(("press", owner, ref, pid))
            return {"ok": True}

        def set_value(self, owner, ref, value, pid):
            self.calls.append(("value", owner, ref, value, pid))
            return {"ok": True, "value": value}

    fake = FakeRefs()
    monkeypatch.setattr(server, "ax_refs", fake)
    async with Client(server.mcp) as first, Client(server.mcp) as second:
        names = {tool.name for tool in await first.list_tools()}
        assert {"ObserveAX", "PressAX", "SetAXValue"} <= names
        assert (await first.call_tool("ObserveAX", {"pid": 101})).data["nodes"][0][
            "ref"
        ]
        assert (await first.call_tool("PressAX", {"ref": "opaque", "pid": 101})).data[
            "ok"
        ]
        assert (
            await first.call_tool("SetAXValue", {"ref": "opaque", "value": "Eden"})
        ).data["value"] == "Eden"
        await second.call_tool("ObserveAX", {"pid": 202})

    first_owner = fake.calls[0][1]
    assert first_owner == fake.calls[1][1] == fake.calls[2][1]
    assert fake.calls[3][1] != first_owner
    assert fake.calls[0][2:] == (101, 0)
