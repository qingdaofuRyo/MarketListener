from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

pytest.importorskip("tkinter")

LAUNCHER_PATH = Path(__file__).resolve().parents[2] / "scripts" / "market_listener_launcher.py"
sys.path.insert(0, str(LAUNCHER_PATH.parent))
SPEC = importlib.util.spec_from_file_location("market_listener_launcher", LAUNCHER_PATH)
assert SPEC is not None and SPEC.loader is not None
launcher = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = launcher
SPEC.loader.exec_module(launcher)
icon_module = importlib.import_module("market_listener_icon")


def _project_tree(tmp_path: Path) -> Path:
    root = tmp_path / "MarketListener"
    root.mkdir()
    (root / "Plan_R4.md").write_text("plan", encoding="utf-8")
    python = root / "desktop" / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"synthetic executable marker")
    return root


def test_find_project_root_from_frozen_launcher_location(tmp_path: Path) -> None:
    root = _project_tree(tmp_path)
    executable = root / "artifacts" / "launcher" / "MarketListenerLauncher.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()

    assert launcher.find_project_root(frozen=True, executable=executable) == root


def test_find_project_root_from_source_script_location(tmp_path: Path) -> None:
    root = _project_tree(tmp_path)
    script = root / "scripts" / "market_listener_launcher.py"
    script.parent.mkdir(parents=True)
    script.touch()

    assert launcher.find_project_root(frozen=False, script_file=script) == root


def test_build_backend_command_uses_project_virtualenv_and_data_root(tmp_path: Path) -> None:
    root = _project_tree(tmp_path)

    command = launcher.build_backend_command(root)

    assert command == [
        str(root / "desktop" / ".venv" / "Scripts" / "python.exe"),
        "-m", "market_monitor", "serve",
        "--data-root", str(root / "data_control"),
        "--host", "127.0.0.1", "--port", "8765",
    ]


def test_build_backend_command_rejects_invalid_port(tmp_path: Path) -> None:
    root = _project_tree(tmp_path)

    with pytest.raises(ValueError, match="1至65535"):
        launcher.build_backend_command(root, port=0)


def test_tdx_commands_keep_launch_and_download_explicit(tmp_path: Path) -> None:
    root = _project_tree(tmp_path)
    base = [str(root / "desktop" / ".venv" / "Scripts" / "python.exe"),
            "-m", "market_monitor.tdx_download_ui"]

    assert launcher.build_tdx_command(root, "financial", download=False) == [
        *base, "--launch-only", "--terminal", "financial",
    ]
    assert launcher.build_tdx_command(root, "futures", download=True) == [
        *base, "--execute", "--terminal", "futures", "--state-dir",
        str(root / "data_control" / "state" / "tdx_download_ui"),
    ]
    with pytest.raises(ValueError, match="未知通达信终端"):
        launcher.build_tdx_command(root, "unknown", download=True)


def test_launcher_log_summary_filters_access_and_progress_but_keeps_errors() -> None:
    formatter = launcher.LauncherLogFormatter()
    text = (
        'INFO:     127.0.0.1:1234 - "GET /api/ready HTTP/1.1" 200 OK\n'
        ' 50%|█████     | 5/10 [00:01<00:01, 5.00it/s]\r'
        'INFO:     Application startup complete.\n'
        'ERROR:     provider unavailable\n'
        '{"status":"ATTENTION_REQUIRED","terminals":{"financial":"UI_DONE_UNVERIFIED",'
        '"futures":"OPEN_DOWNLOAD_DIALOG_MANUALLY"}}\n'
    )

    events = formatter.feed(text[:90]) + formatter.feed(text[90:])

    assert ("service", "Application startup complete.") in events
    assert ("error", "provider unavailable") in events
    assert any("通达信期货通" in message and "盘后下载" in message for _, message in events)
    assert not any("GET /api/ready" in message or "50%" in message for _, message in events)


def test_launcher_log_summary_recognizes_indented_json_report() -> None:
    formatter = launcher.LauncherLogFormatter()
    events = formatter.feed('''{
  "status": "ATTENTION_REQUIRED",
  "terminals": {
    "financial": "WAITING_FOR_LOGIN_OR_MAIN_WINDOW",
    "futures": "OPEN_DOWNLOAD_DIALOG_MANUALLY"
  }
}
''')
    assert len(events) == 1
    assert events[0][0] == "warning"
    assert "尚未出现已登录主窗口" in events[0][1]


def test_launcher_settings_are_persisted_and_invalid_files_use_defaults(tmp_path: Path) -> None:
    path = tmp_path / "state" / "launcher_settings.json"
    assert launcher.load_settings(path) == launcher.LauncherSettings()

    selected = launcher.LauncherSettings(silent_start=True, close_to_tray=True)
    launcher.save_settings(path, selected)
    assert launcher.load_settings(path) == selected

    path.write_text(
        '{"schema":"market-listener/launcher-settings/v1","silentStart":true,"minimizeOnClose":true}',
        encoding="utf-8",
    )
    assert launcher.load_settings(path) == selected

    path.write_text("invalid JSON", encoding="utf-8")
    assert launcher.load_settings(path) == launcher.LauncherSettings()


def test_log_reader_only_returns_new_content_and_handles_truncation(tmp_path: Path) -> None:
    path = tmp_path / "backend.log"
    path.write_bytes("初次启动\n".encode())

    first, offset, truncated = launcher.read_log_chunk(path, 0)
    assert first.decode() == "初次启动\n"
    assert truncated is False

    with path.open("ab") as stream:
        stream.write("服务就绪\n".encode())
    second, offset, truncated = launcher.read_log_chunk(path, offset)
    assert second.decode() == "服务就绪\n"
    assert truncated is False

    path.write_bytes(b"new\n")
    third, offset, truncated = launcher.read_log_chunk(path, offset)
    assert third == b"new\n"
    assert truncated is True
    assert offset == 4


def test_windows_startup_registration_can_be_enabled_and_removed(tmp_path: Path) -> None:
    class FakeKey:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeRegistry:
        HKEY_CURRENT_USER = "HKCU"
        KEY_READ = 1
        KEY_SET_VALUE = 2
        REG_SZ = 1

        def __init__(self) -> None:
            self.values: dict[str, str] = {}

        def OpenKey(self, *_args):
            return FakeKey()

        def CreateKeyEx(self, *_args):
            return FakeKey()

        def QueryValueEx(self, _key, name):
            if name not in self.values:
                raise FileNotFoundError(name)
            return self.values[name], self.REG_SZ

        def SetValueEx(self, _key, name, _reserved, _kind, value):
            self.values[name] = value

        def DeleteValue(self, _key, name):
            if name not in self.values:
                raise FileNotFoundError(name)
            del self.values[name]

    registry = FakeRegistry()
    executable = tmp_path / "MarketListenerLauncher.exe"
    assert not launcher.startup_is_enabled(executable, registry=registry)

    launcher.set_startup_enabled(executable, True, registry=registry)
    assert registry.values[launcher.RUN_VALUE_NAME] == launcher.windows_startup_command(executable)
    assert launcher.startup_is_enabled(executable, registry=registry)

    launcher.set_startup_enabled(executable, False, registry=registry)
    assert not launcher.startup_is_enabled(executable, registry=registry)


def test_close_option_hides_to_tray_and_unchecked_option_exits() -> None:
    class FakeVariable:
        def __init__(self, selected: bool) -> None:
            self.selected = selected

        def get(self) -> bool:
            return self.selected

    class FakeRoot:
        def __init__(self) -> None:
            self.hidden = False
            self.destroyed = False

        def withdraw(self) -> None:
            self.hidden = True

        def destroy(self) -> None:
            self.destroyed = True

    window = launcher.LauncherWindow.__new__(launcher.LauncherWindow)
    window.root = FakeRoot()
    window.close_to_tray_var = FakeVariable(True)
    window.tray_ready = launcher.threading.Event()
    window.tray_ready.set()
    window._append_log = lambda _value: None
    window._stop_tray = lambda: None
    window.process = None
    window.owns_process = False
    window.closing = False

    window.close()
    assert window.root.hidden
    assert not window.root.destroyed

    window.close(force_exit=True)
    assert window.root.destroyed

    direct = launcher.LauncherWindow.__new__(launcher.LauncherWindow)
    direct.root = FakeRoot()
    direct.close_to_tray_var = FakeVariable(False)
    direct._append_log = lambda _value: None
    direct._stop_tray = lambda: None
    direct.process = None
    direct.owns_process = False
    direct.closing = False
    direct.close()
    assert direct.root.destroyed


def test_silent_start_keeps_browser_closed_until_open_button_is_used() -> None:
    class FakeWidget:
        def __init__(self) -> None:
            self.value = None

        def set(self, value):
            self.value = value

        def configure(self, **kwargs):
            self.value = kwargs

    opened: list[str] = []
    window = launcher.LauncherWindow.__new__(launcher.LauncherWindow)
    window.busy = True
    window.ready = False
    window.owns_process = False
    window.silent_for_this_run = True
    window.status = FakeWidget()
    window.start_button = FakeWidget()
    window.open_button = FakeWidget()
    window.stop_button = FakeWidget()
    window._append_log = lambda _value: None
    window.open_web = lambda: opened.append("browser")

    window._on_ready(owned=True)

    assert window.ready
    assert window.owns_process
    assert "系统托盘运行" in window.status.value
    assert window.open_button.value == {"state": "normal"}
    assert opened == []


def test_silent_start_withdraws_root_before_window_is_created(tmp_path: Path, monkeypatch) -> None:
    class FakeRoot:
        hidden = False

        def withdraw(self) -> None:
            self.hidden = True

        def mainloop(self) -> None:
            pass

    fake_root = FakeRoot()
    observed: list[bool] = []
    monkeypatch.setattr(launcher, "find_project_root", lambda: _project_tree(tmp_path))
    monkeypatch.setattr(launcher, "load_settings", lambda _path: launcher.LauncherSettings(silent_start=True))
    monkeypatch.setattr(launcher.tk, "Tk", lambda: fake_root)
    monkeypatch.setattr(
        launcher, "LauncherWindow",
        lambda root, *_args, **_kwargs: observed.append(root.hidden),
    )

    assert launcher.main([]) == 0
    assert observed == [True]


def test_tray_icon_image_and_menu_actions() -> None:
    image = launcher.create_tray_image()
    assert image.size == (64, 64)
    assert image.getpixel((9, 40)) != image.getpixel((2, 2))
    assert image.tobytes() == icon_module.render_icon(64).tobytes()

    class FakeRoot:
        def after(self, *_args):
            pytest.fail("exit action should stop polling")

    actions: list[str] = []
    window = launcher.LauncherWindow.__new__(launcher.LauncherWindow)
    window.root = FakeRoot()
    window.tray_actions = launcher.SimpleQueue()
    window.show_window = lambda: actions.append("show")
    window.open_web = lambda: actions.append("open")
    window.close = lambda **_kwargs: actions.append("exit")
    for action in ("show", "open", "exit"):
        window.tray_actions.put((action, None))

    window._drain_tray_actions()

    assert actions == ["show", "open", "exit"]


def test_tray_menu_registers_restore_browser_and_exit_actions(monkeypatch) -> None:
    class FakeIcon:
        def __init__(self, _name, _image, _title, menu) -> None:
            self.menu = menu
            self.visible = False

        def run_detached(self, setup) -> None:
            setup(self)

        def stop(self) -> None:
            pass

    monkeypatch.setattr(launcher.pystray, "Icon", FakeIcon)
    window = launcher.LauncherWindow.__new__(launcher.LauncherWindow)
    window.tray_actions = launcher.SimpleQueue()
    window.tray_ready = launcher.threading.Event()
    window.tray_shutdown_pending = False

    window._start_tray()

    assert window.tray_ready.is_set()
    assert window.tray_icon.visible
    choices = {item.text: item for item in window.tray_icon.menu.items if item.text}
    assert choices["显示主窗口"].default
    for label in ("显示主窗口", "打开网页终端", "退出 MarketListener"):
        choices[label](window.tray_icon)
    assert [window.tray_actions.get_nowait()[0] for _ in range(3)] == ["show", "open", "exit"]


def test_windows_icon_uses_the_same_mark_at_taskbar_sizes(tmp_path: Path) -> None:
    from PIL import Image

    icon_path = tmp_path / "MarketListener.ico"
    icon_module.write_ico(icon_path)
    with Image.open(icon_path) as icon:
        assert icon.format == "ICO"
        assert set(icon.ico.sizes()) == set(icon_module.ICON_SIZES)
    mark = icon_module.render_icon(64)
    assert all(abs(actual - expected) <= 3 for actual, expected in zip(mark.getpixel((32, 50))[:3], (72, 224, 177)))


@pytest.mark.skipif(sys.platform != "win32", reason="the launcher is a Windows desktop app")
def test_tk_window_embeds_log_view_and_startup_options(tmp_path: Path, monkeypatch) -> None:
    import tkinter as tk

    monkeypatch.setattr(launcher.LauncherWindow, "_start_tray", lambda _self: None)
    root = tk.Tk()
    root.withdraw()
    try:
        project = _project_tree(tmp_path)
        history = project / "data_control" / "logs" / "launcher-backend.log"
        history.parent.mkdir(parents=True)
        history.write_text("ERROR: old provider failure\n", encoding="utf-8")
        window = launcher.LauncherWindow(root, project)
        assert len(window.window_icon_images) == 4
        assert window.log_text.winfo_exists()
        assert window.log_text.cget("state") == "disabled"
        assert window.raw_log_text.winfo_exists()
        assert "old provider failure" in window.raw_log_text.get("1.0", "end")
        assert "old provider failure" not in window.log_text.get("1.0", "end")
        assert len(window.tdx_buttons) == 4
        assert window.startup_checkbox.cget("text") == "开机自启"
        assert window.silent_var.get() is False
        assert window.close_to_tray_var.get() is False
    finally:
        root.destroy()


def test_binary_self_test_checks_project_without_opening_gui(tmp_path: Path, monkeypatch) -> None:
    root = _project_tree(tmp_path)
    monkeypatch.setattr(launcher, "find_project_root", lambda: root)
    monkeypatch.setattr(launcher.tk, "Tk", lambda: pytest.fail("self-test must not open the GUI"))

    assert launcher.main(["--self-test"]) == 0
