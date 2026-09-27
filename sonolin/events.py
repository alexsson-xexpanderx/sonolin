"""Live state, pushed by the speakers instead of polled.

Sonos supports UPnP GENA: subscribe to a service and the player POSTs a NOTIFY
whenever anything changes. SoCo implements the listener; this wraps it in a
plain callback interface so a UI can stay current without polling, which is what
noson-app's `handleDataUpdate` path does over in C++.

Callbacks arrive on SoCo's listener thread, never on the caller's. A GUI must
hand the value to its own thread — the Qt front end does this with a signal.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: Service attribute on a SoCo instance -> what changes in it are about.
SERVICES = {
    "avTransport": "transport",
    "renderingControl": "rendering",
    "zoneGroupTopology": "topology",
    "alarmClock": "alarms",
    "contentDirectory": "content",
}


@dataclass
class Watcher:
    """Subscriptions to one player, delivered as callbacks."""

    speaker: Any
    on_change: Callable[[str, dict], None]
    auto_renew: bool = True
    _subs: list = field(default_factory=list, repr=False)

    def start(self, which: tuple[str, ...] = ("avTransport", "renderingControl", "zoneGroupTopology")) -> None:
        from soco import events

        for attr in which:
            service = getattr(self.speaker.soco, attr, None)
            if service is None:
                log.debug("%s has no %s service", self.speaker.name, attr)
                continue
            try:
                sub = service.subscribe(auto_renew=self.auto_renew)
            except Exception:
                log.exception("could not subscribe to %s", attr)
                continue
            sub.callback = self._make_callback(SERVICES.get(attr, attr))
            self._subs.append(sub)
        log.info("watching %s (%d subscriptions)", self.speaker.name, len(self._subs))

    def _make_callback(self, kind: str) -> Callable:
        def handle(event) -> None:
            try:
                self.on_change(kind, dict(getattr(event, "variables", {}) or {}))
            except Exception:
                log.exception("event callback failed for %s", kind)

        return handle

    def stop(self) -> None:
        for sub in self._subs:
            try:
                sub.unsubscribe()
            except Exception:
                log.debug("unsubscribe failed", exc_info=True)
        self._subs.clear()
        try:
            from soco import events

            events.event_listener.stop()
        except Exception:
            log.debug("listener stop failed", exc_info=True)
