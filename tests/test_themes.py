import json

import pytest

from sonolin.gui import themes
from sonolin.gui.themes import DARK, KEYS, LIGHT, ThemeError


def test_built_in_themes_are_complete_and_valid():
    names = [t.name for t in themes.builtin_themes()]
    assert names[:2] == ["Dark", "Light"]
    assert {"Nord", "Synthwave", "Solarized Light"} <= set(names)
    for t in themes.builtin_themes():
        assert set(t.colors) == set(KEYS) and t.base in ("dark", "light")


def test_a_partial_theme_takes_the_rest_from_its_base():
    t, warnings = themes.parse({"name": "Coral", "base": "light", "colors": {"accent": "#FF6B6B"}})
    assert t.colors["accent"] == "#ff6b6b"
    assert t.colors["bg"] == LIGHT["bg"]
    assert warnings == []


def test_base_defaults_to_dark():
    t, _ = themes.parse({"name": "x"})
    assert t.base == "dark" and t.colors == DARK


@pytest.mark.parametrize("data, says", [
    ([], "one JSON object"),
    ({"colors": {}}, "no name"),
    ({"name": "x", "base": "sepia"}, "\"dark\" or \"light\""),
    ({"name": "x", "colors": ["#fff"]}, "must be an object"),
    ({"name": "x", "colors": {"accent": "blue"}}, "accent = 'blue'"),
    ({"name": "x", "colors": {"bg": "#12"}}, "bg = '#12'"),
])
def test_unusable_themes_are_refused_with_a_reason(data, says):
    with pytest.raises(ThemeError) as err:
        themes.parse(data)
    assert says in str(err.value)


def test_typos_are_warned_about_not_fatal():
    t, warnings = themes.parse({"name": "x", "colors": {"acent": "#ff0000", "accent": "#00ff00"}})
    assert t.colors["accent"] == "#00ff00"
    assert warnings and "acent" in warnings[0]


def test_bad_json_names_the_line(tmp_path):
    f = tmp_path / "t.json"
    f.write_text('{\n  "name": "x",\n  "colors": {,}\n}')
    with pytest.raises(ThemeError) as err:
        themes.load_file(f)
    assert "line 3" in str(err.value)


def test_import_save_export_delete(tmp_path):
    src = tmp_path / "coral.json"
    src.write_text(json.dumps({"name": "Coral", "base": "light", "colors": {"accent": "#ff6b6b"}}))
    theme, _ = themes.import_file(src)
    assert not theme.builtin and theme.path.parent == themes.user_dir()
    assert [t.name for t in themes.user_themes()] == ["Coral"]
    written = json.loads(theme.path.read_text())
    assert set(written["colors"]) == set(KEYS), "saved files spell out every colour"

    again, _ = themes.import_file(src)  # same name again replaces, not duplicates
    assert [t.name for t in themes.user_themes()] == ["Coral"]

    out = tmp_path / "shared.json"
    themes.export(again, out)
    assert themes.load_file(out)[0].colors == again.colors

    themes.delete_user(again)
    assert themes.user_themes() == []


def test_importing_a_built_in_name_does_not_shadow_it(tmp_path):
    src = tmp_path / "dark.json"
    src.write_text(json.dumps({"name": "Dark", "colors": {"accent": "#ff0000"}}))
    theme, _ = themes.import_file(src)
    assert theme.name == "Dark (imported)"
    assert themes.find("Dark").builtin and themes.find("Dark").colors["accent"] == DARK["accent"]


def test_built_ins_cannot_be_saved_over_or_deleted():
    dark = themes.find("Dark")
    with pytest.raises(ThemeError):
        themes.save_user(dark)
    with pytest.raises(ThemeError):
        themes.delete_user(dark)


def test_every_theme_renders_a_stylesheet():
    pytest.importorskip("PyQt6")
    from sonolin.gui import style

    saved = dict(style.C)
    try:
        for t in themes.builtin_themes():
            style.C.clear(); style.C.update(t.colors)
            css = style.sheet()
            assert t.colors["accent"] in css and "white" not in css
    finally:
        style.C.clear(); style.C.update(saved)
