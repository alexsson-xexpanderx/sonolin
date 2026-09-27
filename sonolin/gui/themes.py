"""Themes: named colour sets, stored as small JSON files anyone can edit.

A theme file looks like this, and every key under "colors" is optional:

    {
      "name": "My Theme",
      "base": "dark",
      "colors": { "accent": "#ff6b6b", "bg": "#101418" }
    }

Anything left out comes from the base palette, so a theme that only changes the
accent is three lines. The base also decides which side of the light/dark
toggle the theme belongs to. The app itself always writes every colour, so a
saved or exported theme shows every key there is to change.

Built-in themes live beside this module; imported and saved ones in
``$XDG_CONFIG_HOME/sonolin/themes``. Nothing here needs Qt.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..config import config_path

log = logging.getLogger(__name__)

#: Every colour a theme sets: (key, label, what it colours). The order is the
#: order the editor shows them in.
ROLES: tuple[tuple[str, str, str], ...] = (
    ("bg", "Background", "Behind everything"),
    ("surface", "Panels", "Side bar, pages, cards"),
    ("raised", "Controls", "Buttons, fields, hovered rows"),
    ("hover", "Controls, hovered", "Buttons under the pointer"),
    ("border", "Lines", "Outlines and dividers"),
    ("text", "Text", "Titles and body text"),
    ("muted", "Secondary text", "Artists, albums, descriptions"),
    ("faint", "Hints", "Track numbers, placeholders"),
    ("accent", "Accent", "Play button, now playing, links"),
    ("accent_hi", "Accent, hovered", "Accent under the pointer"),
    ("accent_lo", "Selection", "Selected rows, header glow"),
    ("on_accent", "Text on accent", "Symbols on the play button"),
    ("selection_text", "Text on selection", "Selected rows' text"),
    ("good", "Playing", "Speaker playing indicator"),
    ("warn", "Paused", "Speaker paused indicator"),
    ("danger", "Warning", "Errors and delete buttons"),
)
KEYS = tuple(k for k, _l, _d in ROLES)

DARK = {
    "bg": "#0f1115", "surface": "#161920", "raised": "#1e222b", "hover": "#262b36",
    "border": "#2a303c", "text": "#e7e9ee", "muted": "#8b93a3", "faint": "#5d6574",
    "accent": "#7aa2ff", "accent_hi": "#9bb9ff", "accent_lo": "#3b4f86",
    "on_accent": "#0f1115", "selection_text": "#ffffff",
    "good": "#5eead4", "warn": "#fbbf24", "danger": "#f87171",
}

LIGHT = {
    "bg": "#f4f5f7", "surface": "#ffffff", "raised": "#eceef2", "hover": "#e2e5eb",
    "border": "#d5d9e0", "text": "#1b1e24", "muted": "#5a6272", "faint": "#8b93a1",
    "accent": "#3563e9", "accent_hi": "#4f7af0", "accent_lo": "#d9e3ff",
    "on_accent": "#ffffff", "selection_text": "#12306e",
    "good": "#0f9f84", "warn": "#c98a00", "danger": "#d43d3d",
}

BASES = {"dark": DARK, "light": LIGHT}
HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
BUNDLED = Path(__file__).with_name("theme_files")


class ThemeError(ValueError):
    """A theme file that cannot be used, with a reason a person can act on."""


@dataclass
class Theme:
    name: str
    base: str
    colors: dict[str, str]
    builtin: bool = True
    path: Path | None = field(default=None, compare=False)

    def to_json(self) -> dict:
        return {"name": self.name, "base": self.base,
                "colors": {k: self.colors[k] for k in KEYS}}


def parse(data: object, *, builtin: bool = False, path: Path | None = None) \
        -> tuple[Theme, list[str]]:
    """Check a decoded theme file. Returns the theme and any warnings.

    Mistakes that would make the theme unusable raise `ThemeError`; ones that
    can be ignored safely (an unknown colour name, for instance a typo) are
    returned as warnings so the importer can mention them.
    """
    if not isinstance(data, dict):
        raise ThemeError("A theme file must hold one JSON object: {\"name\": …, \"colors\": {…}}.")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ThemeError("The theme has no name. Add  \"name\": \"Something\".")
    name = name.strip()[:60]
    base = data.get("base", "dark")
    if base not in BASES:
        raise ThemeError(f"\"base\" must be \"dark\" or \"light\", not {base!r}.")
    colors = data.get("colors", {})
    if not isinstance(colors, dict):
        raise ThemeError("\"colors\" must be an object of name: \"#rrggbb\" pairs.")
    bad = [k for k, v in colors.items()
           if k in KEYS and not (isinstance(v, str) and HEX.match(v.strip()))]
    if bad:
        raise ThemeError("These are not colours like \"#1e222b\": " + ", ".join(
            f"{k} = {colors[k]!r}" for k in bad))
    warnings = [f"Ignored {k!r}: not a colour this app uses." for k in colors if k not in KEYS]
    resolved = dict(BASES[base])
    resolved.update({k: v.strip().lower() for k, v in colors.items() if k in KEYS})
    return Theme(name, base, resolved, builtin=builtin, path=path), warnings


def load_file(path: Path, *, builtin: bool = False) -> tuple[Theme, list[str]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ThemeError(f"Not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno}).")
    except OSError as exc:
        raise ThemeError(f"Cannot read the file: {exc.strerror or exc}.")
    return parse(data, builtin=builtin, path=Path(path))


def user_dir() -> Path:
    return config_path().parent / "themes"


def builtin_themes() -> list[Theme]:
    themes = [Theme("Dark", "dark", dict(DARK)), Theme("Light", "light", dict(LIGHT))]
    for path in sorted(BUNDLED.glob("*.json")):
        try:
            themes.append(load_file(path, builtin=True)[0])
        except ThemeError as exc:
            log.warning("bundled theme %s is broken: %s", path.name, exc)
    return themes


def user_themes() -> list[Theme]:
    found = []
    for path in sorted(user_dir().glob("*.json")):
        try:
            found.append(load_file(path)[0])
        except ThemeError as exc:
            log.warning("skipping theme %s: %s", path, exc)
    return found


def all_themes() -> list[Theme]:
    return builtin_themes() + user_themes()


def find(name: str) -> Theme | None:
    return next((t for t in all_themes() if t.name == name), None)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "theme"


def _write(path: Path, theme: Theme) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".theme-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(theme.to_json(), fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_user(theme: Theme) -> Theme:
    """Save a theme of the user's own. Built-in names are not reused."""
    if any(t.name == theme.name for t in builtin_themes()):
        raise ThemeError(f"“{theme.name}” is a built-in theme; save yours under another name.")
    path = theme.path if theme.path and theme.path.parent == user_dir() \
        else user_dir() / f"{_slug(theme.name)}.json"
    saved = Theme(theme.name, theme.base, dict(theme.colors), builtin=False, path=path)
    _write(path, saved)
    return saved


def import_file(source: Path) -> tuple[Theme, list[str]]:
    """Validate a theme file and add it to the user's themes.

    A file named like a built-in theme is imported as "<name> (imported)" rather
    than shadowing the built-in one.
    """
    theme, warnings = load_file(source)
    if any(t.name == theme.name for t in builtin_themes()):
        theme.name = f"{theme.name} (imported)"
    theme.path = None
    for existing in user_themes():
        if existing.name == theme.name:
            theme.path = existing.path  # same name: replace it
    return save_user(theme), warnings


def export(theme: Theme, dest: Path) -> None:
    """Write a theme with every colour spelled out, ready to edit or share."""
    _write(Path(dest), theme)


def delete_user(theme: Theme) -> None:
    if theme.builtin or theme.path is None:
        raise ThemeError("Built-in themes cannot be deleted.")
    theme.path.unlink(missing_ok=True)
