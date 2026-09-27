<img src="data/icons/sonolin.svg" width="96" align="right" alt="">

# Sonolin

A Sonos controller for the Linux desktop, written entirely in Python. It replaces
[noson-app](https://github.com/janbar/noson-app) — about 82,000 lines of C++,
QML and libnoson — with roughly 6,000 lines of Python, and reaches parts of
Sonos that noson-app never did.

![Sonolin](docs/queue.png)

## Install

```bash
pip install -e '.[gui]'
```

Python 3.11 or newer. Two optional system tools unlock two features:

| Tool | For |
|---|---|
| `ffmpeg` | streaming this computer's audio to a speaker |
| `espeak-ng` | spoken announcements |

`pactl` (part of PulseAudio, and of PipeWire's Pulse layer) is used to list
audio sources.

To put it in your application menu, with its icon:

```bash
python3 -m sonolin.desktop install
```

This installs for your user only, into `~/.local/share`, and refreshes the menu
(KDE, GNOME and others). `python3 -m sonolin.desktop uninstall` takes it out again.

## Run it

```bash
sonolin-gui
```

Or from a terminal. Set `SONOLIN_ROOM` once and skip `-r` from then on:

```bash
sonolin discover
sonolin -r "Living Room" status
sonolin -r "Living Room" volume +5
sonolin -r "Living Room" radio "jazz24"
sonolin -r "Living Room" say "Dinner is ready" --volume 35
sonolin -r "Living Room" stream                  # this computer's audio, until Ctrl-C
sonolin -r "Living Room" play-file song.flac     # served from here until Ctrl-C
sonolin -r "Living Room" alarm-add 07:00 --repeat WEEKDAYS
sonolin --help                                   # 53 commands
```

## What it does

**Playback.** Play, pause, skip, seek, shuffle, repeat, crossfade, sleep timer.
Volume per speaker or for the whole group, mute, and a slow ramp like an alarm uses.

**The queue.** View, play from, remove, reorder, clear, save as a Sonos playlist.
Playing a song carries on down the album, playlist or list it was picked from:
like Spotify, *Play* replaces the queue with that list and starts at the song.
*Play next* and *Add to queue*, in the right-click menu, keep the queue as it is.

**Your music files.** Scans your music folder (FLAC, MP3, M4A, Ogg, Opus) with
tag and cover-art readers written from scratch, so there is no tag library to
install. Browse by artist and album or search, then play or queue.

**Music services.** Browse and search any service your household uses, the way
a music app does rather than as a list of folders. A service opens on your own
music: personal mixes such as Discover Weekly and Daily Mixes, then your
playlists, songs and albums, then the service's own categories. Search results come back in
sections: songs with their covers, then artists, albums and playlists as
artwork cards. An artist gets a page with their photo, popular songs, radio and
discography; an album or playlist gets a page with its cover, length and
numbered tracks. The song playing now is highlighted wherever it appears.
Right-click anything to play it, play it next, add it to the queue, add it to a
Sonos playlist or start a new playlist with it, or jump to its artist or album.
TuneIn works immediately. Spotify, Apple Music and other services that need an
account are linked once, through the service's own login page in your browser.

**Favourites, playlists and alarms.** The same ones the Sonos app shows. Create
alarms with any repeat pattern, switch them on and off, delete them.

**Announcements over the music.** Speak text or play a sound, and the music ducks
under it and comes back up afterwards, with nothing lost from the queue. See
[below](#2-the-local-websocket-api-port-1443) for why this matters.

**Streaming this computer's audio.** Whatever your desktop is playing, sent to a
speaker as a live FLAC, MP3 or WAV stream. This was noson-app's headline feature,
and nothing else in the Python Sonos world does it.

**Sound.** Bass, treble, loudness, balance. On soundbars also night mode, speech
enhancement, surround level and mode, subwoofer level and crossover, and audio
delay. The Sound page only shows the controls your model actually has.

**Rooms.** Group and ungroup, group everything, create and separate stereo pairs,
switch to line-in or TV, rename the room.

**The unit itself.** Battery level and health on Move and Roam, status light,
touch-control lock, firmware, and the undocumented diagnostic pages.

**Your desktop.** Media keys, Plasma's and GNOME's media widgets, and the lock
screen all control the selected speaker, through MPRIS.

**Themes.** The sun/moon button at the foot of the side bar switches between
light and dark. *Settings › Themes…* opens the theme editor: pick a theme and the whole app recolours at once; change
any colour with a picker or a hex code; save it as your own; import a theme file
someone shared; export yours. It comes with Dark, Light, Nord, Solarized Light and
Synthwave. The toggle remembers your favourite on each side, so a custom dark
theme survives a trip to light mode.

**Sleeping speakers.** A Move or Roam drops off the network when idle, so
discovery cannot see it. Sonolin remembers every speaker it has found, shows a
sleeping one as asleep instead of forgetting it, and picks it up again when it
wakes. Speakers discovery cannot reach at all, for instance across a VLAN, can
be added by address in *Settings*.

![Browsing Spotify](docs/browse-artist.png)

## Making a theme

![The theme editor](docs/themes.png)

A theme is a small JSON file. Only `name` is required; any colour you leave out
comes from the `base` palette (`dark` or `light`), so this is a complete theme:

```json
{
  "name": "Coral",
  "base": "light",
  "colors": { "accent": "#ff6b6b" }
}
```

Import it with *Settings › Themes… › Import…*, or drop it into `~/.config/sonolin/themes/`.
To see every colour you can set, export any theme: exported files list all
sixteen, each described by name in the editor (`bg`, `surface`, `accent`,
`selection_text` and so on). A file with a mistake is refused with the reason,
such as the line of a JSON error or which value is not a colour.

## How local playback works

Sonos speakers fetch media themselves over HTTP. They cannot read your disk, so
anything local — a music file, its cover art, a spoken announcement, the desktop
stream — is published by a small web server inside Sonolin.

- It listens on **TCP 1405**, inside the 1400–1410 range noson-app's README asks
  you to open, so an existing firewall rule already covers it. If 1405 is taken
  it falls back to a random port.
- It only serves files in your scanned library, each under an opaque id. There
  is no URL that takes a file path, so nothing else on the disk is reachable.
- It has no password, because speakers have no way to send one. While Sonolin
  runs, anything on your local network can fetch your indexed music and cover
  art.
- A queued local track only plays while Sonolin is running. `sonolin serve`
  keeps the server up without the GUI.

## The four ways to control a Sonos

Sonos can be driven four different ways and they barely overlap. noson-app used
only the first.

### 1. UPnP/SOAP, port 1400

The original protocol, reached through [SoCo](https://github.com/SoCo/SoCo):
transport, queue, library, playlists, alarms, grouping, EQ, device settings.
Several things noson-app never wired up live here too: group volume, stereo
pairing, battery, balance, the home-theatre settings, status light, button lock.

### 2. The local WebSocket API, port 1443

The modern Sonos Control API, spoken directly to the speaker with no cloud
account and no developer registration. Neither libnoson nor SoCo implements it;
`sonolin/ws.py` does. It is the only route to:

- **`audioClip`** — play a clip *over* the music. The music ducks, the clip
  plays, the music returns. The usual trick for doorbells and text-to-speech is
  to snapshot the speaker, take over its queue, play the clip, and restore
  everything, which is slow and audibly interrupts. This does it properly.
  `clipType: CHIME` plays a sound built into the speaker, with no URL at all.
- **`playerVolume` duck and unduck**, and **`homeTheater`** options.

`sonolin raw <namespace> <command> '<json>'` sends any command, for exploring.
One quirk: a group has a different id here than over UPnP, so it has to be read
from the `groups` namespace rather than built.

### 3. Diagnostic pages, port 1400

Undocumented, and listed by `sonolin diag`. On a Move running firmware 97.1:
`batterystatus`, `enetports`, `leds` (the recent LED pattern history),
`wireless`, `zp`, `VERSION`, `proc/ath_rincon/status`, `ifconfig` and `showstp`.
`sonolin diag <page>` prints one.

### 4. What had to be built from scratch

The HTTP server that serves local files, cover art, announcement clips and the
live desktop stream. libnoson has one; nothing in Python did.

## Compared with noson-app

Everything noson-app does on the Linux desktop is here, except:

- **Other platforms.** This targets desktop Linux. The GUI would run elsewhere,
  but desktop streaming relies on PulseAudio or PipeWire.
- **Translations.** English only.
- **Artist pictures for local music.** noson-app fetches them from Deezer and
  Last.fm. Sonolin uses the art embedded in files and `cover.jpg`-style files
  beside them.
- **Saving a stream as a radio station, or anything as a new favourite.** Any
  stream URL can be played with `sonolin uri`, but not saved.
- **Separate volume sliders for each room in a group.** There is one volume
  control that acts on either the speaker or the whole group.

## What has been checked on real hardware

Tested against a Sonos Move on firmware 97.1-80312: discovery, and recovering a
speaker that was asleep; status and battery; reading sound settings; the queue
(reading, adding local tracks in all four formats with their titles, removing,
reordering); favourites, playlists and alarms; TuneIn browsing, search and
loading a station; linking Spotify, then searching it and playing a result;
browsing Spotify artists, albums and playlists; adding songs, whole albums and
artist radio to a Sonos playlist; the
diagnostic pages; the WebSocket namespaces; MPRIS as seen from outside the
process. Every test that changed the speaker put it back
exactly as it was, and nothing was played aloud.

Written and unit-tested but **not yet heard on a speaker**, because the testing
happened in the middle of the night: playing local files, the desktop stream,
spoken and chime announcements, creating alarms. Grouping and stereo pairs need
a second speaker.

## Development

```bash
python3 -m pytest              # 117 tests, about a second, no speaker needed
python3 tools/lint.py sonolin/*.py sonolin/gui/*.py tests/*.py
```

`ffmpeg` is needed to generate the test audio; tests that use it are skipped
without it.

| Module | Role |
|---|---|
| `speaker.py` | one object per speaker, over both control protocols |
| `ws.py` | the port-1443 WebSocket client |
| `controller.py` | the application core the GUI and CLI share; no UI in it |
| `mediaserver.py` | the HTTP server speakers fetch local media from |
| `library.py`, `tags.py` | scanning and reading local audio files |
| `services.py` | music-service browse, search, linking and playing |
| `tts.py`, `events.py`, `config.py`, `alarms.py` | speech, pushed state, settings, alarm codes |
| `cli.py` | the `sonolin` command |
| `desktop.py` | adds and removes the application-menu entry and icon |
| `gui/` | the PyQt6 window, its panels, the service browser, themes and their editor, MPRIS, and the thread-pool helper |

Settings live in `~/.config/sonolin/config.json`; music-service tokens beside it
in `service_tokens.json`. Artwork is cached in `~/.cache/sonolin/art` (up to
250 MB). Covers of queued songs from a linked service come from the service's
image servers rather than the speaker, which is many times faster; which
address belongs to which song is kept in `~/.cache/sonolin/art-sources.json`.

## Licence

GPL-3.0-only, the same as noson-app.
