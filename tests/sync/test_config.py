"""Tests for sync.config bootstrap and TOML loading."""

from pathlib import Path

from sync import config as sync_config


def test_load_sources_expands_tilde(tmp_path, monkeypatch):
    home = tmp_path / "home"
    movies = home / "Movies"
    movies.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    cfg = tmp_path / "sync.macos.toml"
    cfg.write_text(
        '[[sources]]\npath = "~/Movies"\ns3 = "s3://park.movies.archive/"\n',
        encoding="utf-8",
    )
    sources = sync_config.load_sources(cfg)
    assert len(sources) == 1
    assert sources[0][0] == movies.resolve()
    assert sources[0][1] == "s3://park.movies.archive/"


def test_ensure_user_config_uses_repo_file(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    expected = cfg_dir / "sync.macos.toml"
    expected.write_text(
        '[[sources]]\npath = "~/Movies"\ns3 = "s3://park.movies.archive/"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(sync_config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(sync_config, "platform_tag", lambda: "macos")

    path = sync_config.ensure_user_config()
    assert path == expected
    # does not overwrite
    expected.write_text("# kept\n", encoding="utf-8")
    assert sync_config.ensure_user_config().read_text(encoding="utf-8") == "# kept\n"


def test_ensure_user_config_migrates_from_home(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    home_dir = tmp_path / "home_cfg"
    home_dir.mkdir()
    legacy = home_dir / "sync.macos.toml"
    legacy.write_text("# from home\n", encoding="utf-8")
    monkeypatch.setattr(sync_config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(sync_config, "HOME_CONFIG_DIR", home_dir)
    monkeypatch.setattr(sync_config, "platform_tag", lambda: "macos")

    path = sync_config.ensure_user_config()
    assert path == cfg_dir / "sync.macos.toml"
    assert path.read_text(encoding="utf-8") == "# from home\n"


def test_resolve_ignore_uses_repo_file(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    expected = cfg_dir / "sync.macos.ignore"
    expected.write_text("TV\n", encoding="utf-8")
    monkeypatch.setattr(sync_config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(sync_config, "platform_tag", lambda: "macos")

    path = sync_config.resolve_ignore_path()
    assert path == expected
    assert path.read_text(encoding="utf-8") == "TV\n"


def test_repo_platform_configs_exist():
    assert sync_config.user_sync_toml().is_file()
    assert sync_config.user_ignore().is_file()


def test_load_excludes_from_source_and_top_level(tmp_path):
    cfg = tmp_path / "sync.macos.toml"
    cfg.write_text(
        "\n".join(
            [
                'exclude = ["GlobalSkip"]',
                "[[sources]]",
                'path = "~/Movies"',
                's3 = "s3://park.movies.archive/"',
                'exclude = ["TV", "iTubeGo"]',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert sync_config.load_excludes(cfg) == ["GlobalSkip", "TV", "iTubeGo"]
