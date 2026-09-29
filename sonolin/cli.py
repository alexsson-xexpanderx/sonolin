"""Command line controller — the replacement for noson-cli.

    sonolin discover
    sonolin -r "Living Room" status
    sonolin -r "Living Room" volume +5
    sonolin -r "Living Room" say "Dinner is ready" --volume 35
    sonolin -r "Living Room" stream                 # this machine's audio, until Ctrl-C
    sonolin -r "Living Room" play-file song.flac    # serves it until Ctrl-C
    sonolin -r "Living Room" alarms
    sonolin -r "Living Room" diag batterystatus
    sonolin -r "Living Room" raw playerVolume getVolume

Set SONOLIN_ROOM to skip ``-r`` every time.

Anything the speaker has to fetch from this machine — a local file, a spoken
clip, the desktop stream — is served by this process, so those commands stay
running for as long as the speaker needs them.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import signal
import sys
import time
import urllib.request
from pathlib import Path

from . import Speaker, discover
from .config import Config
from .controller import Controller
from .speaker import by_name
from .ws import SonosWebSocketError

SOUND_KEYS = {
    "bass": int, "treble": int, "loudness": "bool", "night_mode": "bool",
    "dialog_mode": "bool", "cross_fade": "bool", "sub_enabled": "bool",
    "sub_gain": int, "surround_enabled": "bool", "surround_level": int,
    "music_surround_level": int, "audio_delay": int, "status_light": "bool",
    "buttons_enabled": "bool", "mute": "bool",
}


def _bool(text: str) -> bool:
    if text.lower() in ("1", "on", "yes", "true", "enable", "enabled"):
        return True
    if text.lower() in ("0", "off", "no", "false", "disable", "disabled"):
        return False
    raise ValueError(f"expected on/off, got {text!r}")


def _resolve(args, controller: Controller | None = None) -> Speaker:
    if args.ip:
        return Speaker(args.ip)
    room = args.room or os.environ.get("SONOLIN_ROOM")
    if room:
        try:
            return by_name(room, timeout=args.timeout)
        except LookupError:
            # Asleep speakers are invisible to discovery; try the remembered ones.
            for known in Config.load().speakers:
                if known.name.lower() == room.lower():
                    return Speaker(known.ip, name=known.name, model=known.model, uid=known.uid)
            raise
    found = discover(args.timeout)
    if not found:
        sys.exit("no Sonos players found — check the firewall allows SSDP (UDP 1900), "
                 "or use --ip")
    if len(found) > 1:
        names = ", ".join(s.name for s in found)
        sys.exit(f"several players found ({names}); pick one with --room or --ip")
    return found[0]


def _print(obj) -> None:
    if isinstance(obj, (dict, list)):
        print(json.dumps(obj, indent=2, default=str))
    else:
        print(obj)


def _serve_until_interrupted(message: str, on_stop=None) -> None:
    print(message)
    print("Serving from this process. Press Ctrl-C to stop.")
    stop = False

    def handler(_sig, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    while not stop:
        time.sleep(0.3)
    if on_stop:
        on_stop()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="sonolin", description="Control Sonos players.")
    ap.add_argument("-r", "--room", help="room name (or set SONOLIN_ROOM)")
    ap.add_argument("-i", "--ip", help="player address, skipping discovery")
    ap.add_argument("-t", "--timeout", type=int, default=5, help="discovery timeout in seconds")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="command")

    def cmd(name, help_text, **kw):
        return sub.add_parser(name, help=help_text, **kw)

    cmd("discover", "list every player on the network")
    p = cmd("add", "remember a speaker by address, for when discovery cannot see it")
    p.add_argument("address")

    cmd("status", "model, firmware, battery, LED, mic")
    cmd("caps", "what the player says it can do")
    cmd("track", "what is playing now")
    cmd("sound", "every tone and spatial setting this model has")
    p = cmd("set", "change a sound or device setting, e.g. `set night_mode on`")
    p.add_argument("key", choices=sorted(SOUND_KEYS))
    p.add_argument("value")
    p = cmd("balance", "left/right balance, -100 (left) to +100 (right)")
    p.add_argument("offset", type=int)

    for name, help_text in (("play", "resume"), ("pause", "pause"), ("stop", "stop"),
                            ("next", "skip forward"), ("prev", "skip back")):
        cmd(name, help_text)
    p = cmd("seek", "jump to a position, H:MM:SS or seconds")
    p.add_argument("position")
    p = cmd("mode", "play mode")
    p.add_argument("mode", choices=["normal", "repeat", "repeat-one", "shuffle",
                                    "shuffle-repeat", "shuffle-repeat-one"])

    p = cmd("volume", "show or set volume (0-100)")
    p.add_argument("level", nargs="?", help="absolute 0-100, or +5 / -5 to adjust")
    p.add_argument("--group", action="store_true", help="act on the whole group")
    p = cmd("ramp", "slide the volume to a level, the way an alarm does")
    p.add_argument("level", type=int)

    cmd("queue", "list the queue")
    p = cmd("queue-play", "play the queue from a position (1-based)")
    p.add_argument("position", type=int)
    p = cmd("queue-remove", "remove a queue entry (1-based)")
    p.add_argument("position", type=int)
    cmd("queue-clear", "empty the queue")

    cmd("favourites", "list Sonos favourites")
    p = cmd("favourite", "play a Sonos favourite by (partial) name")
    p.add_argument("name")
    cmd("playlists", "list Sonos playlists")
    p = cmd("playlist", "play a Sonos playlist by (partial) name")
    p.add_argument("name")

    cmd("alarms", "list the household's alarms")
    p = cmd("alarm-add", "create an alarm on this speaker")
    p.add_argument("time", help="HH:MM")
    p.add_argument("--repeat", default="DAILY",
                   help="DAILY, WEEKDAYS, WEEKENDS, ONCE, or ON_0123456 (0 = Sunday)")
    p.add_argument("--volume", type=int, default=20)
    p.add_argument("--minutes", type=int, default=60, help="how long it plays (0 = until stopped)")
    p = cmd("alarm-enable", "switch an alarm on or off")
    p.add_argument("id")
    p.add_argument("state", choices=["on", "off"])
    p = cmd("alarm-delete", "delete an alarm")
    p.add_argument("id")

    p = cmd("sleep", "show the sleep timer, set it in minutes, or `off`")
    p.add_argument("minutes", nargs="?", help="minutes, or `off`; omit to show")

    p = cmd("join", "add this player to another player's group")
    p.add_argument("target", help="room name of the group to join")
    cmd("unjoin", "leave its group")
    cmd("party", "group every player behind this one")
    p = cmd("pair", "bond with an identical speaker as a stereo pair (this = left)")
    p.add_argument("right", help="room name of the right-hand speaker")
    cmd("unpair", "separate a stereo pair")
    cmd("line-in", "switch to line-in")
    cmd("tv", "switch a soundbar to its TV input")

    p = cmd("say", "speak text over the music, without stopping it")
    p.add_argument("text")
    p.add_argument("--voice", default="en")
    p.add_argument("--wpm", type=int, default=165)
    p.add_argument("--volume", type=int)
    p = cmd("announce", "play a clip over the music without stopping it")
    p.add_argument("url", nargs="?", help="a URL the speaker can reach")
    p.add_argument("--chime", action="store_true", help="use the player's built-in chime")
    p.add_argument("--volume", type=int, help="clip volume, independent of the music")

    p = cmd("stream", "send this machine's audio output to the speaker, until Ctrl-C")
    p.add_argument("--format", default="flac", choices=["flac", "mp3", "wav"])
    p.add_argument("--source", help="capture source; see `sources`")
    cmd("sources", "list audio sources that can be streamed")
    p = cmd("play-file", "play local audio files, served from here until Ctrl-C")
    p.add_argument("files", nargs="+")
    p = cmd("library", "scan a folder and list or search it")
    p.add_argument("folder", nargs="?", help="default: your XDG music folder")
    p.add_argument("--search", help="only tracks matching this")
    p = cmd("serve", "keep the media server up so queued local tracks stay playable")
    p.add_argument("folder", nargs="?")

    cmd("services", "list the music services available to this household")
    p = cmd("search", "search a music service, e.g. `search TuneIn stations jazz`")
    p.add_argument("service"); p.add_argument("category"); p.add_argument("term")
    p.add_argument("--play", type=int, metavar="N", help="play result N")
    p = cmd("radio", "find a TuneIn station and play it")
    p.add_argument("term")
    p.add_argument("--pick", type=int, default=1, metavar="N", help="which match (default 1)")
    p = cmd("link", "link this app to a service that needs an account (Spotify, …)")
    p.add_argument("service")

    p = cmd("uri", "play any URI the speaker can fetch")
    p.add_argument("uri")
    p.add_argument("--title", default="")
    p = cmd("led", "show or set the status light")
    p.add_argument("state", nargs="?", choices=["on", "off"])
    p = cmd("rename", "rename the room")
    p.add_argument("name")
    p = cmd("diag", "read an undocumented diagnostic page, e.g. zp, batterystatus")
    p.add_argument("page", nargs="?", default="", help="omit to list what exists")
    p = cmd("raw", "send any websocket command (exploration escape hatch)")
    p.add_argument("namespace")
    p.add_argument("command")
    p.add_argument("body", nargs="?", default="{}", help="JSON object of arguments")
    return ap


PLAY_MODES = {
    "normal": "NORMAL", "repeat": "REPEAT_ALL", "repeat-one": "REPEAT_ONE",
    "shuffle": "SHUFFLE_NOREPEAT", "shuffle-repeat": "SHUFFLE",
    "shuffle-repeat-one": "SHUFFLE_REPEAT_ONE",
}


def _hms(text: str) -> str:
    if ":" in text:
        parts = [int(p) for p in text.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        h, m, s = parts
    else:
        total = int(float(text))
        h, m, s = total // 3600, total % 3600 // 60, total % 60
    return f"{h}:{m:02d}:{s:02d}"


def _pick(items, name: str):
    wanted = name.lower()
    exact = [i for i in items if i.title.lower() == wanted]
    partial = [i for i in items if wanted in i.title.lower()]
    hits = exact or partial
    if not hits:
        sys.exit(f"nothing called {name!r}; have: {', '.join(i.title for i in items) or 'none'}")
    if len(hits) > 1 and not exact:
        sys.exit(f"{name!r} is ambiguous: {', '.join(i.title for i in hits)}")
    return hits[0]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.verbose:
        import logging
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.cmd == "discover":
        found = discover(args.timeout)
        cfg = Config.load()
        for s in found:
            cfg.remember(s.ip, s.name, s.model, s.uid)
        cfg.save()
        asleep = [k for k in cfg.speakers if k.ip not in {s.ip for s in found}]
        for s in found:
            print(f"{s.name:<22} {s.ip:<15} {s.model:<24} fw {s.firmware}")
        for k in asleep:
            print(f"{k.name:<22} {k.ip:<15} {k.model:<24} (remembered, not answering)")
        return 0 if found or asleep else 1

    if args.cmd == "sources":
        from .speaker import _Loop
        from .mediaserver import default_monitor, list_monitors

        default = _Loop.submit(default_monitor())
        for name in _Loop.submit(list_monitors()):
            print(f"{'*' if name == default else ' '} {name}")
        return 0

    if args.cmd == "library":
        c = Controller([Path(args.folder)] if args.folder else None)
        if not c.library.roots:
            sys.exit("no music folder found; pass one")
        count = c.library.scan()
        tracks = c.library.search(args.search) if args.search else c.library.tracks
        for t in sorted(tracks, key=lambda t: (t.display_artist.lower(), t.album.lower(),
                                               t.disc, t.track)):
            print(f"{t.display_artist[:24]:<24} {t.album[:24]:<24} {t.track:>3} "
                  f"{t.display_title[:40]}")
        s = c.library.stats()
        print(f"\n{count} tracks, {s['albums']} albums, {s['artists']} artists")
        return 0

    if args.cmd == "add":
        c = Controller([])
        try:
            sp = c.add_by_ip(args.address)
        except (ConnectionError, OSError) as exc:
            sys.exit(str(exc))
        print(f"remembered {sp.name} ({sp.model}) at {sp.ip}")
        return 0

    sp = _resolve(args)
    c = Controller([])
    c.speakers = [sp]
    try:
        return _run(args, c, sp)
    except SonosWebSocketError as exc:
        sys.exit(f"player refused the command: {exc}")
    except (LookupError, ValueError, ConnectionError, RuntimeError) as exc:
        sys.exit(str(exc))
    finally:
        c.close()


def _run(args, c: Controller, sp: Speaker) -> int:
    cmd = args.cmd
    if cmd == "status":
        _print(sp.status())
    elif cmd == "caps":
        _print(sp.capabilities)
    elif cmd == "track":
        _print(sp.track)
    elif cmd == "sound":
        _print({k: v for k, v in sp.sound().items() if v is not None})
    elif cmd == "set":
        kind = SOUND_KEYS[args.key]
        value = _bool(args.value) if kind == "bool" else kind(args.value)
        setattr(sp.soco, args.key, value)
        print(f"{args.key} = {getattr(sp.soco, args.key)}")
    elif cmd == "balance":
        off = max(-100, min(100, args.offset))
        sp.soco.balance = (100 - max(0, off), 100 - max(0, -off))
        print(f"balance (L, R) = {sp.soco.balance}")
    elif cmd in ("play", "pause", "stop", "next"):
        getattr(sp, cmd)()
    elif cmd == "prev":
        sp.previous()
    elif cmd == "seek":
        sp.seek(_hms(args.position))
    elif cmd == "mode":
        sp.soco.play_mode = PLAY_MODES[args.mode]
    elif cmd == "volume":
        if args.level is None:
            print(sp.group_volume if args.group else sp.volume)
        elif args.level[0] in "+-":
            delta = int(args.level)
            print(sp.relative_group_volume(delta) if args.group
                  else (sp.relative_volume(delta) or sp.volume))
        elif args.group:
            sp.group_volume = int(args.level)
        else:
            sp.volume = int(args.level)
    elif cmd == "ramp":
        print(f"ramping; takes about {sp.ramp_to_volume(args.level)} s")
    elif cmd == "queue":
        for n, item in enumerate(sp.soco.get_queue(max_items=1000), 1):
            print(f"{n:>4}  {item.title}  —  {getattr(item, 'creator', '') or ''}")
    elif cmd == "queue-play":
        sp.soco.play_from_queue(args.position - 1)
    elif cmd == "queue-remove":
        sp.soco.remove_from_queue(args.position - 1)
    elif cmd == "queue-clear":
        sp.soco.clear_queue()
    elif cmd == "favourites":
        for f in c.favourites(sp):
            print(f.title)
    elif cmd == "favourite":
        fav = _pick(c.favourites(sp), args.name)
        c.play_favourite(sp, fav)
        print(f"playing {fav.title}")
    elif cmd == "playlists":
        for pl in c.playlists(sp):
            print(pl.title)
    elif cmd == "playlist":
        pl = _pick(c.playlists(sp), args.name)
        c.play_playlist(sp, pl)
        print(f"playing {pl.title}")
    elif cmd == "alarms":
        from .alarms import describe_recurrence

        for a in c.alarms(sp):
            print(f"{a.alarm_id:>4}  {'on ' if a.enabled else 'off'}  "
                  f"{a.start_time.strftime('%H:%M')}  {describe_recurrence(a.recurrence):<14} "
                  f"{a.zone.player_name:<18} vol {a.volume}")
    elif cmd == "alarm-add":
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", args.time)
        if not m:
            raise ValueError("time must be HH:MM")
        start = datetime.time(int(m.group(1)), int(m.group(2)))
        duration = (datetime.time(args.minutes // 60, args.minutes % 60)
                    if args.minutes else None)
        a = c.create_alarm(sp, start, recurrence=args.repeat.upper(),
                           volume=args.volume, duration=duration)
        print(f"alarm {a.alarm_id} set for {args.time} in {sp.name}")
    elif cmd in ("alarm-enable", "alarm-delete"):
        alarm = next((a for a in c.alarms(sp) if str(a.alarm_id) == args.id), None)
        if alarm is None:
            raise LookupError(f"no alarm with id {args.id}")
        if cmd == "alarm-delete":
            c.delete_alarm(alarm)
        else:
            c.set_alarm_enabled(alarm, args.state == "on")
    elif cmd == "sleep":
        if args.minutes == "off":
            sp.sleep_timer(None)
        elif args.minutes is not None:
            sp.sleep_timer(int(args.minutes) * 60)
        left = sp.soco.get_sleep_timer()
        print(f"sleep timer: {left // 60} min left" if left else "sleep timer off")
    elif cmd == "join":
        sp.join(by_name(args.target, timeout=args.timeout))
    elif cmd == "unjoin":
        sp.unjoin()
    elif cmd == "party":
        sp.party_mode()
    elif cmd == "pair":
        sp.create_stereo_pair(by_name(args.right, timeout=args.timeout))
    elif cmd == "unpair":
        sp.separate_stereo_pair()
    elif cmd == "line-in":
        sp.switch_to_line_in()
    elif cmd == "tv":
        sp.switch_to_tv()
    elif cmd == "say":
        c.say(sp, args.text, voice=args.voice, wpm=args.wpm, volume=args.volume)
        if not c.wait_clip_fetched(20):
            print("the speaker did not fetch the clip — is a firewall blocking "
                  f"{c.server_url}?", file=sys.stderr)
            return 1
        time.sleep(2)  # let it finish reading before the server goes away
    elif cmd == "announce":
        if not args.url and not args.chime:
            raise ValueError("give a URL or --chime")
        _print(sp.announce(args.url, clip_type="CHIME" if args.chime else None,
                           volume=args.volume))
    elif cmd == "stream":
        if args.source:
            c.set_capture_source(args.source)
        url = c.stream_desktop(sp, args.format)
        _serve_until_interrupted(f"{sp.name} is playing this machine's audio from {url}",
                                 on_stop=lambda: c.stop_desktop(sp))
    elif cmd == "play-file":
        files = [Path(f) for f in args.files]
        missing = [str(f) for f in files if not f.is_file()]
        if missing:
            raise ValueError(f"not found: {', '.join(missing)}")
        tracks = c.library.add_files(files)
        if not tracks:
            raise ValueError("none of those is a supported audio file")
        c.play_now(sp, tracks[0])
        if len(tracks) > 1:
            c.enqueue(sp, tracks[1:])
        _serve_until_interrupted(f"playing {tracks[0].display_title} on {sp.name}")
    elif cmd == "serve":
        if args.folder:
            c.library.roots = [Path(args.folder)]
        else:
            from .library import default_music_dirs

            c.library.roots = default_music_dirs()
        count = c.library.scan()
        url = c.start_server()
        _serve_until_interrupted(f"{count} tracks available at {url}")
    elif cmd in ("services", "search", "radio", "link"):
        from .services import Services

        svc = Services()
        if cmd == "services":
            for s in svc.catalogue():
                state = "ready" if not s.needs_link else (
                    "linked" if svc.is_linked(s.name, sp) else "needs `link`")
                print(f"{s.name:<34} {s.auth:<11} {state}")
        elif cmd == "link":
            url = svc.begin_link(args.service, sp)
            print(f"Open this address, log in to {args.service} there, then press Enter:\n\n  {url}\n")
            input()
            svc.complete_link(args.service)
            print(f"{args.service} linked.")
        else:
            name, category, term = ("TuneIn", "stations", args.term) if cmd == "radio" \
                else (args.service, args.category, args.term)
            results = svc.search(name, sp, category, term)
            pick = args.pick if cmd == "radio" else args.play
            if pick:
                if not 1 <= pick <= len(results):
                    raise LookupError(f"only {len(results)} result(s)")
                item = results[pick - 1]
                svc.play(name, sp, item)
                print(f"playing {item.title}")
            else:
                for n, item in enumerate(results, 1):
                    kind = "folder" if svc.is_container(item) else \
                        "radio" if svc.is_stream(item) else "track"
                    print(f"{n:>3}  {item.title}  ({kind})")
    elif cmd == "uri":
        sp.play_uri(args.uri, title=args.title)
    elif cmd == "led":
        if args.state is None:
            print("on" if sp.led else "off")
        else:
            sp.led = args.state == "on"
    elif cmd == "rename":
        sp.soco.player_name = args.name
    elif cmd == "diag":
        page = f"/status/{args.page}" if args.page else "/status"
        with urllib.request.urlopen(f"http://{sp.ip}:1400{page}", timeout=8) as r:
            body = r.read().decode("utf-8", "replace")
        if not args.page:
            # The firmware writes its links unquoted: <a href=/status/zp>.
            print("\n".join(re.findall(r'href="?/status/([^"\s>]+)', body)))
        else:
            print(re.sub(r"<\?xml[^>]*\?>|<\?xml-stylesheet[^>]*\?>", "", body).strip())
    elif cmd == "raw":
        _print(sp.ws_command(args.namespace, args.command, **json.loads(args.body)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
