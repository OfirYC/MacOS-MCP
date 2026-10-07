"""Opt-in packaged MCP and two real AppKit-process acceptance test.

Build/install the wheel in a separate venv, then run with:
    MACOS_MCP_AX_LIVE=1 MACOS_MCP_AX_SERVER=/path/to/venv/bin/macos-mcp \
      pytest -q tests/test_ax_refs_live.py
"""

import asyncio
import json
import os
import plistlib
import subprocess
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

LIVE = os.environ.get("MACOS_MCP_AX_LIVE") == "1"
SERVER = os.environ.get("MACOS_MCP_AX_SERVER")
FIXTURE = Path(__file__).parent / "fixtures" / "ax_ref_app.swift"
EVIDENCE_DIR = os.environ.get("MACOS_MCP_AX_EVIDENCE_DIR")


def payload(result):
    if result.isError:
        raise RuntimeError(result.content[0].text)
    return result.structuredContent or json.loads(result.content[0].text)


def capture_fixture_window(marker: Path, name: str) -> None:
    if not EVIDENCE_DIR:
        return
    destination = Path(EVIDENCE_DIR)
    destination.mkdir(parents=True, exist_ok=True)
    window_id = (Path(str(marker) + ".window")).read_text().strip()
    subprocess.run(
        ["screencapture", "-x", "-l", window_id, str(destination / name)],
        check=True,
    )


@pytest.mark.asyncio
@pytest.mark.skipif(not LIVE, reason="opt-in real macOS AX test")
async def test_packaged_server_observe_act_verify_two_owned_appkit_processes(tmp_path):
    assert SERVER and Path(SERVER).is_file(), (
        "MACOS_MCP_AX_SERVER must name installed wheel binary"
    )
    contents = tmp_path / "SartelAXFixture.app" / "Contents"
    binary = contents / "MacOS" / "SartelAXFixture"
    binary.parent.mkdir(parents=True)
    with (contents / "Info.plist").open("wb") as info:
        plistlib.dump({
            "CFBundleIdentifier": "ai.sartel.ax-fixture",
            "CFBundleName": "Sartel AX Fixture",
            "CFBundleExecutable": binary.name,
            "CFBundlePackageType": "APPL",
            "LSUIElement": True,
        }, info)
    subprocess.run(["swiftc", str(FIXTURE), "-o", str(binary)], check=True)
    markers = [tmp_path / "a.hit", tmp_path / "b.hit"]
    apps = [subprocess.Popen([str(binary), str(marker)]) for marker in markers]
    try:
        params = StdioServerParameters(
            command=SERVER,
            args=["serve", "--transport", "stdio"],
            env={**os.environ, "MACOS_MCP_SKIP_PERMISSION_CHECK": "1"},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert {"ObserveAX", "PressAX", "SetAXValue"} <= names

                async def observe(pid):
                    deadline = asyncio.get_running_loop().time() + 30
                    while True:
                        result = await session.call_tool("ObserveAX", {"pid": pid})
                        if not result.isError:
                            state = payload(result)
                            if any(
                                node["name"] == "Write marker"
                                for node in state["nodes"]
                            ):
                                return state
                        if asyncio.get_running_loop().time() > deadline:
                            raise AssertionError(
                                f"Owned AppKit process {pid} not AX discoverable: "
                                f"{result.content[0].text[:200]}"
                            )
                        await asyncio.sleep(0.1)

                first = await observe(apps[0].pid)
                second = await observe(apps[1].pid)
                assert first["pid"] != second["pid"]
                button = next(
                    node["ref"]
                    for node in first["nodes"]
                    if node["name"] == "Write marker"
                )
                field = next(
                    node["ref"]
                    for node in first["nodes"]
                    if node["role"] == "AXTextField"
                )
                capture_fixture_window(markers[0], "before.png")

                wrong = await session.call_tool(
                    "PressAX", {"ref": button, "pid": apps[1].pid}
                )
                assert wrong.isError and "process" in wrong.content[0].text.lower()
                unsupported = await session.call_tool(
                    "SetAXValue", {"ref": button, "value": "x"}
                )
                assert (
                    unsupported.isError
                    and "fallback" in unsupported.content[0].text.lower()
                )
                assert not any(marker.exists() for marker in markers)

                assert payload(
                    await session.call_tool(
                        "SetAXValue", {"ref": field, "value": "Eden"}
                    )
                )["ok"]
                after_value = await observe(apps[0].pid)
                assert any(node.get("value") == "Eden" for node in after_value["nodes"])
                fresh_button = next(
                    node["ref"]
                    for node in after_value["nodes"]
                    if node["name"] == "Write marker"
                )
                assert payload(
                    await session.call_tool("PressAX", {"ref": fresh_button})
                )["ok"]
                for _ in range(30):
                    if markers[0].exists():
                        break
                    await asyncio.sleep(0.1)
                assert markers[0].read_text() == "pressed"
                assert not markers[1].exists()
                assert any(
                    node["name"] == "Pressed"
                    for node in (await observe_pressed(session, apps[0].pid))["nodes"]
                )
                capture_fixture_window(markers[0], "after.png")
                stale = await session.call_tool("PressAX", {"ref": fresh_button})
                assert stale.isError and "stale" in stale.content[0].text.lower()

                new_ref = next(
                    node["ref"]
                    for node in (await observe_pressed(session, apps[0].pid))["nodes"]
                    if node["name"] == "Pressed"
                )
                apps[0].terminate()
                apps[0].wait(timeout=5)
                dead = await session.call_tool("PressAX", {"ref": new_ref})
                assert dead.isError and "stale" in dead.content[0].text.lower()
    finally:
        for app in apps:
            if app.poll() is None:
                app.terminate()
                app.wait(timeout=5)


async def observe_pressed(session, pid):
    result = await session.call_tool("ObserveAX", {"pid": pid})
    return payload(result)
