"""Folder exclude / ignore matching."""

from sync.ignore import IgnoreRules


def test_plain_folder_name_matches_nested_paths():
    ignore = IgnoreRules(["TV", "iTubeGo"], [])
    assert ignore.is_ignored("TV")
    assert ignore.is_ignored("TV/Show/ep1.mp4")
    assert ignore.is_ignored("iTubeGo/foo.mp4")
    assert not ignore.is_ignored("Other/TV/x.mp4")
    assert not ignore.is_ignored("Movies/foo.mp4")


def test_aws_exclude_expands_folder_names():
    ignore = IgnoreRules(["TV"], [])
    args = ignore.aws_exclude_args()
    assert args == ["--exclude", "TV", "--exclude", "TV/*", "--exclude", "TV/**"]


def test_with_extra_globs_from_config():
    base = IgnoreRules(["V Vam My Render/202?????-??????*"], [])
    merged = base.with_extra_globs(["TV", "iTubeGo"])
    assert merged.is_ignored("TV/a.mp4")
    assert merged.is_ignored("iTubeGo/b.mp4")
    assert merged.is_ignored("V Vam My Render/20260610-024155_x.mkv")
