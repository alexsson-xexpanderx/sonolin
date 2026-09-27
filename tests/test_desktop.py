import shutil
import subprocess

import pytest

from sonolin import desktop


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(desktop, "_refresh", lambda notes: notes.append("refresh skipped"))
    return tmp_path


def test_install_and_uninstall(home, monkeypatch):
    monkeypatch.setattr(desktop.shutil, "which",
                        lambda name: "/opt/bin/sonolin-gui" if name == "sonolin-gui" else None)
    desktop.install()
    entry = (home / "applications" / "sonolin.desktop").read_text()
    assert "Exec=/opt/bin/sonolin-gui" in entry and "Icon=sonolin" in entry
    assert "StartupWMClass=sonolin" in entry, "must match the app's desktop file name"
    icon = home / "icons/hicolor/scalable/apps/sonolin.svg"
    assert icon.read_bytes() == desktop.ICON.read_bytes()
    desktop.uninstall()
    assert not icon.exists() and not (home / "applications" / "sonolin.desktop").exists()


def test_falls_back_to_the_module_when_not_on_path(home, monkeypatch):
    monkeypatch.setattr(desktop.shutil, "which", lambda name: None)
    desktop.install()
    assert "-m sonolin.gui" in (home / "applications" / "sonolin.desktop").read_text()


@pytest.mark.skipif(shutil.which("desktop-file-validate") is None, reason="validator not installed")
def test_entry_is_valid(home):
    desktop.install()
    out = subprocess.run(["desktop-file-validate", str(home / "applications/sonolin.desktop")],
                         capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip() == "", out.stdout


def test_window_and_menu_use_the_same_name():
    from pathlib import Path

    app_src = (Path(desktop.__file__).parent / "gui" / "app.py").read_text()
    assert f'setDesktopFileName("{desktop.APP_ID}")' in app_src
