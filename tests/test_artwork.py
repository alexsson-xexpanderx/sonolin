"""Artwork destinations are checked before any socket is opened."""

import io
import socket
import ssl

import pytest

from sonolin.artwork import fetch_art


SPEAKER = "192.168.1.20"
MEDIA = "http://192.168.1.5:1405"
TOKEN = "a" * 20


class Socket:
    def __init__(self, response):
        self.response = response
        self.sent = b""
        self.closed = False

    def sendall(self, data):
        self.sent += data

    def makefile(self, *args):
        return io.BytesIO(self.response)

    def close(self):
        self.closed = True


@pytest.fixture
def network(monkeypatch):
    """Exercise the real HTTP parser, with all DNS and sockets kept offline."""
    calls, sockets, lookups = [], [], []
    replies = [b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nimage"]
    answers = ["93.184.216.34"]

    def resolve(host, port, **kwargs):
        lookups.append(host)
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET,
                 socket.SOCK_STREAM, 6, "", (ip, port)) for ip in answers]

    def connect(address, timeout):
        calls.append(address)
        sock = Socket(replies.pop(0))
        sockets.append(sock)
        return sock

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "create_connection", connect)
    return calls, sockets, lookups, replies, answers


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://10.0.0.1/", "http://172.16.0.1/",
    "http://192.168.1.2/", "http://169.254.169.254/latest/meta-data/",
    "http://0.0.0.0/", "http://100.64.0.1/", "http://224.0.0.1/",
    "http://[::1]/", "http://[::]/", "http://[fd00::1]/", "http://[fe80::1]/",
    "http://[::ffff:127.0.0.1]/", "http://[2002:7f00:1::]/",
    "http://[fe80::1%25eth0]/", "file:///etc/passwd", "ftp://example.com/a",
    "data:image/png;base64,AAAA", "//example.com/a", "http:///missing-host",
    "http://user:pass@example.com/a", "http://example.com:0/a",
    "http://example.com:99999/a", "http://example.com/a\r\nInjected:yes",
    "http://example.com\\@127.0.0.1/a",
    f"http://{SPEAKER}:80/getaa", f"https://{SPEAKER}:1400/getaa",
    f"http://{SPEAKER}:1400/status", f"http://{SPEAKER}:1400/getaa/../status",
    "http://192.168.1.21:1400/getaa", f"{MEDIA}/music/{TOKEN}",
    f"{MEDIA}/art/../status", f"{MEDIA}/art/%2e%2e", f"{MEDIA}/art/{TOKEN}/extra",
])
def test_rejected_before_connect(network, url):
    with pytest.raises(ValueError):
        fetch_art(url, SPEAKER, MEDIA)
    assert network[0] == []


@pytest.mark.parametrize("answers", [
    ["127.0.0.1"], ["10.0.0.1"], ["::1"], ["169.254.169.254"],
    ["93.184.216.34", "192.168.1.2"], ["93.184.216.34", "fd00::1"], [],
])
def test_dns_private_and_mixed_answers_are_blocked(network, answers):
    network[4][:] = answers
    with pytest.raises(ValueError):
        fetch_art("http://art.example/cover", SPEAKER)
    assert network[0] == []


@pytest.mark.parametrize("url, address, host", [
    ("http://art.example/cover?size=100", ("93.184.216.34", 80), "art.example"),
    (f"http://{SPEAKER}:1400/getaa?s=1", (SPEAKER, 1400), f"{SPEAKER}:1400"),
    (f"{MEDIA}/art/{TOKEN}", ("192.168.1.5", 1405), "192.168.1.5:1405"),
    ("http://[2606:4700:4700::1111]/art", ("2606:4700:4700::1111", 80),
     "[2606:4700:4700::1111]"),
])
def test_permitted_artwork(network, monkeypatch, url, address, host):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1234")
    assert fetch_art(url, SPEAKER, MEDIA) == b"image"
    assert network[0] == [address]
    assert f"Host: {host}\r\n".encode() in network[1][0].sent
    assert network[1][0].closed


def test_https_keeps_certificate_identity_and_pins_dns(network, monkeypatch):
    context = ssl.create_default_context()
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    wrapped = []

    def wrap(sock, *, server_hostname):
        wrapped.append(server_hostname)
        return sock

    monkeypatch.setattr(context, "wrap_socket", wrap)
    monkeypatch.setattr(ssl, "create_default_context", lambda: context)
    assert fetch_art("https://art.example/cover", SPEAKER) == b"image"
    assert wrapped == ["art.example"]
    assert network[0] == [("93.184.216.34", 443)]
    assert network[2] == ["art.example"]
    assert b"Host: art.example:443\r\n" in network[1][0].sent


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("target", [
    "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "file:///etc/passwd",
    f"http://{SPEAKER}:1400/status", "http://private.example/cover",
])
def test_redirects_cannot_reach_internal_destinations(network, status, target):
    network[3].insert(0, f"HTTP/1.1 {status} Redirect\r\nLocation: {target}\r\n\r\n".encode())
    network[4][:] = ["127.0.0.1"]
    with pytest.raises(ValueError):
        fetch_art("http://93.184.216.34/cover", SPEAKER, MEDIA)
    assert network[0] == [("93.184.216.34", 80)]
    assert network[1][0].closed


def test_relative_redirect_and_redirect_limit(network):
    redirect = b"HTTP/1.1 302 Found\r\nLocation: /next\r\n\r\n"
    network[3].insert(0, redirect)
    assert fetch_art("http://art.example/cover", SPEAKER) == b"image"
    assert b"GET /next HTTP/1.1\r\n" in network[1][-1].sent
    network[3][:] = [redirect] * 6
    assert fetch_art("http://art.example/loop", SPEAKER) is None
    assert len(network[0]) == 8
    assert all(sock.closed for sock in network[1])


def test_dns_is_rechecked_on_same_host_redirect(network, monkeypatch):
    network[3].insert(0, b"HTTP/1.1 302 Found\r\nLocation: /next\r\n\r\n")
    original = socket.getaddrinfo

    def rebind(*args, **kwargs):
        result = original(*args, **kwargs)
        network[4][:] = ["127.0.0.1"]
        return result

    monkeypatch.setattr(socket, "getaddrinfo", rebind)
    with pytest.raises(ValueError):
        fetch_art("http://art.example/cover", SPEAKER)
    assert network[0] == [("93.184.216.34", 80)]


def test_response_limit_and_http_failure(network):
    body = b"x" * 4_000_001
    network[3][:] = [b"HTTP/1.1 200 OK\r\nContent-Length: 4000001\r\n\r\n" + body]
    assert len(fetch_art("http://art.example/cover", SPEAKER)) == 4_000_000
    network[3][:] = [b"HTTP/1.1 404 Not Found\r\n\r\n"]
    assert fetch_art("http://art.example/missing", SPEAKER) is None
