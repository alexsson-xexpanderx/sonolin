import stat

import pytest

from tools import make_icon


@pytest.fixture
def targets(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    paths = (root / "sonolin/gui/sonolin.svg", root / "data/icons/sonolin.svg")
    for path in paths:
        path.parent.mkdir(parents=True)
    monkeypatch.setattr(make_icon, "ROOT", root)
    monkeypatch.setattr(make_icon, "TARGETS", paths)
    return paths


@pytest.mark.parametrize("existing", [False, True])
def test_generates_both_icons(targets, existing, capsys):
    if existing:
        for target in targets:
            target.write_text("old icon")

    make_icon.main()

    for target in targets:
        assert target.read_text(encoding="utf-8") == make_icon.icon()
        assert stat.S_IMODE(target.stat().st_mode) == 0o644
        assert list(target.parent.iterdir()) == [target]
    assert capsys.readouterr().out.splitlines() == [
        "wrote sonolin/gui/sonolin.svg", "wrote data/icons/sonolin.svg",
    ]


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("existing", [False, True])
def test_replaces_symlink_without_writing_outside_checkout(targets, tmp_path, index, existing):
    outside = tmp_path / "outside.svg"
    if existing:
        outside.write_bytes(b"preserve me")
    targets[index].symlink_to(outside)

    make_icon.main()

    if existing:
        assert outside.read_bytes() == b"preserve me"
    else:
        assert not outside.exists()
    for target in targets:
        assert not target.is_symlink()
        assert target.read_text(encoding="utf-8") == make_icon.icon()
        assert list(target.parent.iterdir()) == [target]


@pytest.mark.parametrize("index", [0, 1])
def test_failed_replace_preserves_target_and_cleans_temp(targets, monkeypatch, index):
    for target in targets:
        target.write_bytes(b"old icon")
    replace = make_icon.os.replace

    def fail_replace(src, dst):
        if dst == targets[index]:
            raise OSError("replacement failed")
        replace(src, dst)

    monkeypatch.setattr(make_icon.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replacement failed"):
        make_icon.main()

    assert targets[index].read_bytes() == b"old icon"
    for target in targets:
        assert list(target.parent.iterdir()) == [target]
