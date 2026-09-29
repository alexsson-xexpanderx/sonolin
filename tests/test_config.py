import json

from sonolin.config import Config, config_path


def test_missing_file_gives_defaults(xdg):
    cfg = Config.load()
    assert cfg.speakers == [] and cfg.stream_format == "flac"


def test_round_trip(xdg):
    cfg = Config.load()
    cfg.remember("10.0.0.5", "Kitchen", "Sonos One", "RINCON_A")
    cfg.stream_format = "mp3"
    cfg.save()
    again = Config.load()
    assert again.speakers[0].name == "Kitchen"
    assert again.stream_format == "mp3"
    assert config_path().parent == xdg / "sonolin"


def test_new_dhcp_lease_updates_rather_than_duplicates(xdg):
    cfg = Config()
    cfg.remember("10.0.0.5", "Kitchen", "Sonos One", "RINCON_A")
    cfg.remember("10.0.0.9", "Kitchen", "", "RINCON_A")
    assert len(cfg.speakers) == 1
    assert cfg.speakers[0].ip == "10.0.0.9"
    assert cfg.speakers[0].model == "Sonos One", "a blank value must not erase a known one"


def test_forget(xdg):
    cfg = Config()
    cfg.remember("10.0.0.5", "Kitchen")
    cfg.forget("10.0.0.5")
    assert cfg.speakers == []


def test_corrupt_or_foreign_file_is_tolerated(xdg):
    path = config_path()
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert Config.load().speakers == []
    path.write_text(json.dumps({"speakers": [{"ip": "1.2.3.4", "junk": 1}, {"name": "no ip"}],
                                "unknown_key": True}))
    cfg = Config.load()
    assert [s.ip for s in cfg.speakers] == ["1.2.3.4"]


def test_save_leaves_no_temp_files(xdg):
    Config().save()
    assert [p.name for p in config_path().parent.iterdir()] == ["config.json"]


def test_websocket_pins_persist_without_being_learned_from_discovery(xdg):
    cfg = Config(websocket_fingerprints={"10.0.0.5": "ab" * 32})
    cfg.remember("10.0.0.5", "Kitchen", uid="RINCON_A")
    cfg.remember("10.0.0.9", "Kitchen", uid="RINCON_A")
    cfg.save()
    assert Config.load().websocket_fingerprints == {"10.0.0.5": "ab" * 32}
