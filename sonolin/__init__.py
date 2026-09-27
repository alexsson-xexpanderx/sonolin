"""Sonolin — a Python Sonos controller, replacing noson-app's C++ core."""

from .speaker import Speaker, by_name, discover
from .ws import SonosWebSocket, SonosWebSocketError

__all__ = ["Speaker", "SonosWebSocket", "SonosWebSocketError", "by_name", "discover"]
__version__ = "0.1.0"
