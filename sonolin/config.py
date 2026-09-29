"""Persisted settings: known speakers, music folders, preferences.

Stored as JSON under ``$XDG_CONFIG_HOME/sonolin/config.json``. The important
entry is the speaker list. SSDP discovery only finds players that are awake, and
a battery Move or Roam spends much of its life asleep; without a memory of it,
the app would forget the speaker exists every time it dozes off. noson-app gets
the same effect from its persisted ``--deviceurl``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


def config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "sonolin" / "config.json"


@dataclass
class KnownSpeaker:
    ip: str
    name: str = ""
    model: str = ""
    uid: str = ""


@dataclass
class Config:
    speakers: list[KnownSpeaker] = field(default_factory=list)
    music_dirs: list[str] = field(default_factory=list)
    last_speaker: str = ""
    stream_format: str = "flac"
    capture_source: str = ""
    tts_voice: str = "en"
    announce_volume: int = 40
    #: Explicitly trusted SHA-256 certificate fingerprints, keyed by speaker IP.
    websocket_fingerprints: dict[str, str] = field(default_factory=dict)
    #: Where the media server listens. Inside 1400-1410, the TCP range noson's
    #: README tells users to open, so an existing firewall rule already covers
    #: it. SoCo's event listener starts at 1400 and counts up, hence not 1400.
    server_port: int = 1405
    last_service: str = ""
    theme: str = "Dark"
    #: What the light/dark toggle switches to, per side: the last theme picked
    #: for that side, so a custom dark theme survives a trip to light mode.
    theme_dark: str = "Dark"
    theme_light: str = "Light"

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        path = path or config_path()
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError):
            log.warning("ignoring unreadable config %s", path)
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        data = {k: v for k, v in raw.items() if k in known}
        data["speakers"] = [
            KnownSpeaker(**{k: v for k, v in s.items() if k in KnownSpeaker.__dataclass_fields__})
            for s in raw.get("speakers", []) if isinstance(s, dict) and s.get("ip")
        ]
        return cls(**data)

    def save(self, path: Path | None = None) -> None:
        """Write atomically, so a crash mid-write cannot leave a torn file."""
        path = path or config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(asdict(self), fh, indent=2)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def remember(self, ip: str, name: str = "", model: str = "", uid: str = "") -> None:
        """Record a speaker, updating it in place if its uid or ip is known.

        Matching on uid first means a speaker that picked up a new DHCP lease is
        updated rather than duplicated.
        """
        for s in self.speakers:
            if (uid and s.uid == uid) or s.ip == ip:
                s.ip = ip
                s.name = name or s.name
                s.model = model or s.model
                s.uid = uid or s.uid
                return
        self.speakers.append(KnownSpeaker(ip, name, model, uid))

    def forget(self, ip: str) -> None:
        self.speakers = [s for s in self.speakers if s.ip != ip]
