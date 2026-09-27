"""Put Sonolin in the desktop's application menu, or take it out again.

    python3 -m sonolin.desktop install
    python3 -m sonolin.desktop uninstall

Installs for the current user only, into ``$XDG_DATA_HOME`` (normally
``~/.local/share``): a menu entry in ``applications/`` and the icon in the
``hicolor`` theme, then asks the desktop to re-read its menus. Nothing outside
the home directory is touched, so no root is needed.

The entry runs ``sonolin-gui`` by its full path, because a program started from
the menu does not always get ``~/.local/bin`` on its search path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_ID = "sonolin"  # must equal QGuiApplication.desktopFileName, see gui/app.py
ICON = Path(__file__).parent / "gui" / "sonolin.svg"

ENTRY = """[Desktop Entry]
Type=Application
Name=Sonolin
GenericName=Sonos Controller
Comment=Control your Sonos speakers and stream this computer's audio to them
Exec={exe}
TryExec={exe}
Icon={app_id}
Terminal=false
Categories=AudioVideo;Audio;Player;
Keywords=sonos;speaker;music;radio;spotify;tunein;
StartupWMClass={app_id}
StartupNotify=true
"""


def data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def entry_path() -> Path:
    return data_home() / "applications" / f"{APP_ID}.desktop"


def icon_path() -> Path:
    return data_home() / "icons" / "hicolor" / "scalable" / "apps" / f"{APP_ID}.svg"


def find_launcher() -> str:
    exe = shutil.which("sonolin-gui")
    if exe:
        return exe
    # Not on PATH: fall back to running the module with this interpreter.
    return f"{sys.executable} -m sonolin.gui"


def _refresh(notes: list[str]) -> None:
    """Ask the desktop to notice the change. Each tool is optional."""
    for cmd in (["update-desktop-database", str(entry_path().parent)],
                ["kbuildsycoca6"], ["kbuildsycoca5"],
                ["gtk-update-icon-cache", "-f", "-t", str(data_home() / "icons" / "hicolor")]):
        if shutil.which(cmd[0]) is None:
            continue
        result = subprocess.run(cmd, capture_output=True, text=True)
        notes.append(f"{cmd[0]}: {'ok' if result.returncode == 0 else 'failed'}")
        if cmd[0] == "kbuildsycoca6" and result.returncode == 0:
            break  # Plasma 6 found; the Plasma 5 tool is not needed


def install() -> list[str]:
    notes = []
    exe = find_launcher()
    entry = entry_path()
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(ENTRY.format(exe=exe, app_id=APP_ID), encoding="utf-8")
    notes.append(f"menu entry: {entry}")
    icon = icon_path()
    icon.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ICON, icon)
    notes.append(f"icon: {icon}")
    if shutil.which("desktop-file-validate"):
        check = subprocess.run(["desktop-file-validate", str(entry)], capture_output=True, text=True)
        notes.append("entry valid" if check.returncode == 0 and not check.stdout.strip()
                     else f"validator says: {check.stdout.strip() or check.stderr.strip()}")
    _refresh(notes)
    return notes


def uninstall() -> list[str]:
    notes = []
    for path in (entry_path(), icon_path()):
        if path.exists():
            path.unlink()
            notes.append(f"removed {path}")
    _refresh(notes)
    return notes


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if args[:1] == ["install"]:
        notes = install()
    elif args[:1] == ["uninstall"]:
        notes = uninstall()
    else:
        print(__doc__.strip().split("\n\n")[1])
        return 2
    print("\n".join(notes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
