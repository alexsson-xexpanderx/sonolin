"""Fetch now-playing artwork without giving track metadata access to the LAN."""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
from urllib.parse import urljoin, urlsplit


def _destination(url: str, speaker_ip: str, media_url: str):
    # Reject characters that URL parsers or servers might silently normalise.
    if any(ord(c) <= 32 or ord(c) == 127 for c in url) or "\\" in url:
        raise ValueError("Invalid artwork URL")
    parts = urlsplit(url)
    if (parts.scheme not in ("http", "https") or not parts.hostname
            or parts.username is not None or parts.password is not None
            or "%" in parts.hostname):
        raise ValueError("Artwork requires an HTTP(S) URL without credentials")
    port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
    if not 0 < port < 65536:
        raise ValueError("Invalid artwork port")

    # These addresses come from application state, never from track metadata.
    # Limit LAN exceptions to the actual cover endpoints, not whole devices.
    local = (parts.scheme == "http" and parts.hostname == speaker_ip
             and port == 1400 and parts.path == "/getaa")
    if media_url:
        media = urlsplit(media_url)
        local |= (parts.scheme == media.scheme and parts.hostname == media.hostname
                  and port == media.port
                  and re.fullmatch(r"/art/[0-9a-f]{20}", parts.path) is not None)

    try:
        addresses = [ipaddress.ip_address(parts.hostname)]
    except ValueError:
        # LAN exceptions require literal addresses, so DNS cannot redirect them.
        local = False
        addresses = [ipaddress.ip_address(result[4][0]) for result in
                     socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)]
    if not addresses or (not local and any(not _public(ip) for ip in addresses)):
        raise ValueError("Artwork destination is not permitted")
    return parts, port, addresses


def _public(ip) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        # Transition addresses can encapsulate destinations outside the policy.
        if ip.ipv4_mapped or ip.sixtofour or ip.teredo:
            return False
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def fetch_art(url: str, speaker_ip: str, media_url: str = "") -> bytes | None:
    """Allow public HTTP(S) images and narrowly scoped local artwork endpoints.

    Connect to a validated numeric address while retaining the original Host
    header and TLS identity. Using http.client also avoids environment proxies
    and automatic redirects that would bypass destination validation.
    """
    for _ in range(6):
        parts, port, addresses = _destination(url, speaker_ip, media_url)
        conn = http.client.HTTPConnection(parts.hostname, port, timeout=8)
        try:
            for ip in addresses:
                try:
                    conn.sock = socket.create_connection((str(ip), port), timeout=8)
                    break
                except OSError:
                    if ip == addresses[-1]:
                        raise
            if parts.scheme == "https":
                conn.sock = ssl.create_default_context().wrap_socket(
                    conn.sock, server_hostname=parts.hostname)
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            conn.request("GET", path, headers={"Connection": "close"})
            with conn.getresponse() as response:
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location:
                        return None
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    return None
                return response.read(4_000_000)
        finally:
            conn.close()
    return None
