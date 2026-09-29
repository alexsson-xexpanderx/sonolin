from email.message import Message
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.response import addinfourl

import pytest

from sonolin import cli


@pytest.fixture
def diagnostic_response(monkeypatch):
    """Fake only the transport, keeping urllib's real redirect/error handling."""
    def respond(code=200, body=b"", location=None):
        requests = []

        class HTTPHandler(cli.urllib.request.HTTPHandler):
            def http_open(self, req):
                requests.append((req.full_url, req.get_method(), req.timeout))
                headers = Message()
                if location:
                    headers["Location"] = location
                response = addinfourl(BytesIO(body), headers, req.full_url,
                                      code if len(requests) == 1 else 200)
                response.msg = "test response"
                return response

        monkeypatch.setattr(cli.urllib.request, "HTTPHandler", HTTPHandler)
        monkeypatch.setattr(cli.urllib.request, "_opener", None)
        monkeypatch.setattr(cli.urllib.request, "getproxies", lambda: {})
        return requests

    return respond


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("location", [
    "http://127.0.0.1:8080/private",
    "http://192.0.2.1:8080/private",
    "//192.0.2.2/private",
    "/status/zp",
])
def test_diag_rejects_redirects(diagnostic_response, code, location):
    requests = diagnostic_response(code=code, location=location)
    args = cli.build_parser().parse_args(["diag"])
    with pytest.raises(HTTPError) as exc:
        cli._run(args, None, SimpleNamespace(ip="192.0.2.1"))
    assert exc.value.code == code
    exc.value.close()
    assert requests == [("http://192.0.2.1:1400/status", "GET", 8)]


@pytest.mark.parametrize("page,body,expected", [
    ("", b'<a href=/status/zp>ZP</a><a href="/status/batterystatus">Battery</a>',
     "zp\nbatterystatus\n"),
    ("zp", b'<?xml version="1.0"?><?xml-stylesheet href="style"?><ZP>\xff</ZP>',
     "<ZP>\ufffd</ZP>\n"),
])
def test_diag_output(diagnostic_response, capsys, page, body, expected):
    requests = diagnostic_response(body=body)
    args = cli.build_parser().parse_args(["diag", page])
    assert cli._run(args, None, SimpleNamespace(ip="192.0.2.1")) == 0
    path = f"/status/{page}" if page else "/status"
    assert requests == [(f"http://192.0.2.1:1400{path}", "GET", 8)]
    assert capsys.readouterr().out == expected


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
