"""The official DCE browser collector accepts Chrome and WebView debugger sockets."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.parametrize("socket", ["chrome_devtools_remote", "webview_devtools_remote_1234"])
def test_dce_page_discovery_uses_verified_browser_target(
    monkeypatch: pytest.MonkeyPatch, socket: str,
) -> None:
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    from capture_dce_android_batch_ranks import PAGE, _discover_page
    import capture_dce_android_batch_ranks as capture

    calls: list[tuple[str, ...]] = []

    def adb_command(_adb: Path, _port: int, *args: str) -> str:
        calls.append(args)
        if args[0] == "connect":
            return "connected to 127.0.0.1:5555"
        if args == ("devices",):
            return "List of devices attached\n127.0.0.1:5555\tdevice\n"
        if args[-2:] == ("cat", "/proc/net/unix"):
            return f"00000000 @unrelated_socket\n00000000 @{socket}\n"
        if args[-2:] == ("tcp:0", f"localabstract:{socket}"):
            return "17777"
        raise AssertionError(args)

    monkeypatch.setattr(capture, "_adb_command", adb_command)
    monkeypatch.setattr(capture, "_target_for_port", lambda port: {
        "type": "page", "url": PAGE, "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/page/1",
    })
    port, target = _discover_page(Path("synthetic-adb"), 5038, "127.0.0.1:5555")
    assert port == 17777
    assert target["url"] == PAGE
    assert any(f"localabstract:{socket}" in args for args in calls)
