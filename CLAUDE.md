# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Sonolin is a Python rewrite of noson-app (`/home/alexsson/git/noson-app`, C++/QML). The README
covers features and the user-facing picture; this file covers what is needed to change the code.

## Commands

```bash
python3 -m pytest                                    # all tests, <1 s, no speaker needed
python3 -m pytest tests/test_mediaserver.py -k range # one test
python3 tools/lint.py sonolin/*.py sonolin/gui/*.py tests/*.py
python3 -m sonolin.cli -i 192.168.1.14 status        # CLI without installing
sonolin-gui                                          # after `pip install -e '.[gui]'`
```

A test that builds `MainWindow` and lets the event loop run must keep it offline: the window
starts speaker discovery and a music scan on timers shortly after it opens. The autouse
`_offline` fixture in `tests/test_app_theme.py` patches both out; put window tests there.

No linter is installed on this machine; `tools/lint.py` is a symtable-based stand-in for
pyflakes (undefined names, unused imports). Run it after moving code between modules.

GUI checks run headless: `QT_QPA_PLATFORM=offscreen`, pump the event loop, and `window.grab()`
to a PNG. Point `XDG_CONFIG_HOME` at a scratch directory first so the user's real
`~/.config/sonolin` is not touched.

## Architecture

Three layers, and the direction of dependency matters:

- **`speaker.py` / `ws.py`** — one `Speaker` per player, over two protocols. UPnP on port 1400
  goes through SoCo (`speaker.soco`); the local WebSocket API on port 1443 goes through
  `SonosWebSocket`. The websocket is asyncio and SoCo is blocking, so `_Loop` runs one
  background event loop per process and `_Loop.submit` bridges into it from any thread.
- **`controller.py`** — the application core, shared by the CLI and the GUI and containing no
  UI. It owns the library, the media server, event watchers and the persisted `Config`, and
  holds every operation that needs more than one of them.
- **`cli.py` and `gui/`** — thin fronts. The CLI must not import PyQt6, which
  `test_cli_does_not_need_qt` enforces; `alarms.py` exists only so the CLI can share
  recurrence labels without importing `gui/panels.py`.

In the GUI, panels (`gui/panels.py`) never call a speaker: they take data in and emit signals
out. The one exception is `gui/browser.py`, which runs its own `workers.run` jobs through
`Services`, because page navigation is request-and-response by nature. It still never touches
a speaker on the GUI thread, and it discards results for pages the user has already left
(`_serial`). Its artwork (`ArtLoader`, shared with the queue) comes from `QNetworkAccessManager`
with a disk cache. A speaker's own cover address (`:1400/getaa`) is slow — the speaker makes
covers one at a time, ~0.25 s each — so the window gives the loader a `resolver` that asks the
song's service for its image-server address instead (`Services.cover_for`, on the loader's own
pool), remembered across runs in `~/.cache/sonolin/art-sources.json`. What still comes from a
speaker is asked for two at a time, newest first. Because service calls now run in parallel,
the token store takes turns saving and writes atomically (`_token_store` in `services.py`);
keep it that way. `gui/app.py` decides what each signal means and runs it through `workers.run`, which puts
the blocking call on `QThreadPool` and delivers the result back on the GUI thread. Every call
to a speaker is a network round trip to wifi; none may run on the GUI thread.

State reaches the window by GENA events (`events.py`, which trigger an immediate refresh) and
by a full poll every `POLL_EVERY` ticks with the play position interpolated locally between
polls. `_polling` allows one poll in flight, so a sleeping speaker cannot stack up timeouts.

## What "Play" means

Everywhere in the app — browser, Library, Favourites — *Play* replaces the queue with the list
the item was picked from and starts at that item, so playback continues down the album or
playlist (`Services.play_context`, `Controller.play_local_list`). Appending a single song and
playing it was the original behaviour, and it stopped after that one song. *Play next* and
*Add to queue* are the ways to keep the queue. Radio has no queue position and plays directly.

## Things the speakers are strict about

Each of these was found by testing against real hardware; the code comments say where.

- **Queued HTTP URLs need a file extension or a `<res protocolInfo>`**, or `AddURIToQueue`
  fails with UPnP 804. `music_url` carries the extension and `didl_for` carries the `<res>`.
- **Queueing without DIDL shows the bare URL as the title** in every Sonos app. That is why
  `Controller.enqueue` calls `AddURIToQueue` itself instead of SoCo's `add_uri_to_queue`.
- **The websocket household id and group id differ from the UPnP ones.** Read them from the
  websocket (`learn_household`, the `groups` namespace); never build them from SoCo values.
- **SoCo's DIDL for music-service items is a placeholder.** It works for queueing tracks and
  containers; radio needs the real `x-sonosapi-stream:` URI and broadcast item that
  `Services.stream_uri_and_meta` builds. The firmware may rewrite a legacy TuneIn id into its
  current TuneIn service when loading it; that is success, not failure.
- **SoCo crashes on a search or folder with exactly one result**: it only wraps a lone result in
  a list when it is an `OrderedDict`, and current xmltodict returns a plain `dict`.
  `Services._query` normalises the response first; use it rather than `MusicService.search` or
  `get_metadata` directly.
- **SoCo's placeholder format does work for Spotify tracks, albums, playlists and artist radio**,
  in both the queue and Sonos playlists; the speaker rewrites `soco://` into its own
  `x-sonos-spotify:` URIs. Only radio streams need `stream_uri_and_meta`.
- **A Sonos Move/Roam asleep still answers ARP**, so ping and the neighbour table lie. Only a
  TCP connect to port 1400 (`Speaker.reachable`) tells the truth.
- **MPRIS wire types**: `mpris:length` and `Position` must be int64, `mpris:trackid` an object
  path, `PropertiesChanged`'s third argument an `as` even when empty. PyQt picks D-Bus types
  from Python values, so `gui/mpris.py` builds these explicitly. Verify with
  `gdbus call --session --dest org.mpris.MediaPlayer2.sonolin ...`, which prints each type.
- **A short-lived process must not stop the media server before the speaker fetches from it.**
  The CLI's `say` waits on `MediaServer.wait_fetched`.

## Testing against the user's real speaker

The user has one Sonos Move ("Living Room", 192.168.1.14) in their home.

- **Never make it play audibly without asking first.** Verify by loading without starting
  (`play_uri(..., start=False)`), reading the result, and restoring a `soco.snapshot.Snapshot`.
- Refuse to touch the queue while its state is `PLAYING`. Anything added for a test is removed
  again, after checking that the entry is the one that was added, and the resulting queue is
  compared with the original.
- Playlist writes are tested on a temporary playlist created for the test and removed after
  it, checking its title first; the user's own playlists are never modified.
- Service-browser tests need the user's service link. **Symlink** (never copy)
  `~/.config/sonolin/service_tokens.json` into the scratch `XDG_CONFIG_HOME`: SoCo saves refreshed
  tokens back to the store, and a refresh written to a copy would leave the real token stale.
  Remove the link afterwards.
- Do not create or change alarms, group or pair speakers, rename rooms or link music-service
  accounts as part of testing. Linking a service contacts that service on the user's behalf;
  the login itself is the user's to do.

## Themes

`gui/themes.py` (no Qt) defines the sixteen colour roles, the Dark and Light palettes, and theme
files: built-ins in `gui/theme_files/`, the user's in `$XDG_CONFIG_HOME/sonolin/themes/`.
`style.apply(app, colors)` swaps `style.C` **in place** and rebuilds the stylesheet from
`style.sheet()`. Anything painted by hand must therefore read `style.C[...]` at paint time,
never capture a colour ahead of time: speaker rows store their *state* and look its colour up
when drawn, for example. The few widgets that bake colours into text (the browser's breadcrumbs,
the queue's playing row) have a `restyle()` that `MainWindow.apply_theme` calls. There is no
hard-coded colour in the stylesheet; use a role (`on_accent`, `selection_text`, …).

## Conventions

Comments explain why rather than what. User-facing text is plain language; the GUI uses no
jargon (the user has pushed back on it). Colours live only in `gui/style.py`'s `C` table.
