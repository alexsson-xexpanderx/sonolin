import pytest

from sonolin import cli


def test_every_subcommand_parses():
    parser = cli.build_parser()
    samples = [
        ["discover"], ["add", "10.0.0.5"], ["status"], ["set", "night_mode", "on"],
        ["balance", "-20"], ["seek", "1:30"], ["mode", "shuffle"], ["volume", "+5"],
        ["volume", "30", "--group"], ["ramp", "20"], ["queue-play", "3"],
        ["favourite", "radio"], ["alarm-add", "07:30", "--repeat", "WEEKDAYS"],
        ["alarm-enable", "3", "off"], ["sleep"], ["sleep", "off"], ["sleep", "30"],
        ["pair", "Right"], ["say", "hello", "--volume", "30"], ["announce", "--chime"],
        ["stream", "--format", "mp3"], ["play-file", "a.flac", "b.mp3"],
        ["library", "/music", "--search", "x"], ["serve"], ["diag", "zp"],
        ["raw", "playerVolume", "getVolume", "{}"],
    ]
    for argv in samples:
        assert parser.parse_args(["-r", "Room", *argv]).cmd == argv[0]


def test_bad_values_are_rejected():
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["set", "not_a_setting", "1"])
    with pytest.raises(SystemExit):
        parser.parse_args(["mode", "backwards"])


@pytest.mark.parametrize("given,expected", [
    ("90", "0:01:30"), ("1:02", "0:01:02"), ("1:02:03", "1:02:03"), ("3725", "1:02:05"),
])
def test_seek_positions(given, expected):
    assert cli._hms(given) == expected


def test_on_off_parsing():
    assert cli._bool("on") is True and cli._bool("OFF") is False
    with pytest.raises(ValueError):
        cli._bool("maybe")


def test_picking_by_name():
    items = [type("I", (), {"title": t})() for t in ("Radio One", "Radio Two", "Jazz")]
    assert cli._pick(items, "jazz").title == "Jazz"
    assert cli._pick(items, "radio one").title == "Radio One"
    with pytest.raises(SystemExit):
        cli._pick(items, "radio")      # ambiguous
    with pytest.raises(SystemExit):
        cli._pick(items, "classical")  # absent


def test_cli_does_not_need_qt():
    """The CLI must work on a machine without PyQt6 installed."""
    import subprocess
    import sys

    code = ("import sys, sonolin.cli, sonolin.controller, sonolin.services; "
            "print(sorted(m for m in sys.modules if m.startswith('PyQt')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True).stdout.strip()
    assert out == "[]"
