"""One Sonolin at a time.

With the window closing to the tray, starting Sonolin again from the menu is
how people bring it back. A second copy would poll the same speakers and save
over the first one's settings, so it instead tells the running copy to show
itself, and exits.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from PyQt6.QtNetwork import QLocalServer, QLocalSocket

#: Per user, so two people logged in to one machine each get their own.
NAME = f"sonolin-{os.getuid()}"


def hand_over(name: str = NAME, timeout_ms: int = 500) -> bool:
    """Ask a running copy to show its window. True if one answered."""
    sock = QLocalSocket()
    sock.connectToServer(name)
    if not sock.waitForConnected(timeout_ms):
        return False
    sock.write(b"show\n")
    sock.waitForBytesWritten(timeout_ms)
    sock.disconnectFromServer()
    return True


def listen(on_show: Callable[[], None], name: str = NAME) -> QLocalServer:
    """Become the running copy: later starts reach `on_show` instead."""
    server = QLocalServer()
    if not server.listen(name):
        # A copy that crashed leaves its socket file behind; nothing is
        # listening on it (hand_over just found that), so it is safe to clear.
        QLocalServer.removeServer(name)
        server.listen(name)

    def accept() -> None:
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()
            conn.disconnected.connect(conn.deleteLater)
            on_show()

    server.newConnection.connect(accept)
    return server
