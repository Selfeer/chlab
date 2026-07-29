from __future__ import annotations

from pathlib import Path

import pytest

from chlab.core.profiles import ProfileNotFound, Profiles, apply, summarise
from chlab.core.registry import InstanceNotFound, Registry
from chlab.core.spec import ClusterSpec, DatabaseSpec, UserSpec


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    return Registry(tmp_path / "state")


def spec(name="lab", **overrides) -> ClusterSpec:
    return ClusterSpec(
        **{"name": name, "source": "/bin/clickhouse", "port_base": 21000, **overrides}
    )


def test_saved_specs_round_trip(registry: Registry):
    original = spec(users=(UserSpec("dev", "pw", role="admin"),), databases=(DatabaseSpec("d"),))
    registry.save(original)
    assert registry.load("lab") == original


def test_missing_instance_reports_its_name(registry: Registry):
    with pytest.raises(InstanceNotFound, match="ghost"):
        registry.load("ghost")


def test_exists_reflects_what_is_on_disk(registry: Registry):
    assert not registry.exists("lab")
    registry.save(spec())
    assert registry.exists("lab")


def test_list_returns_every_saved_instance(registry: Registry):
    registry.save(spec("a", port_base=21000))
    registry.save(spec("b", port_base=22000))
    assert [instance.name for instance in registry.list()] == ["a", "b"]


def test_list_skips_unreadable_entries(registry: Registry):
    """A corrupt directory must not make the whole listing fail."""
    registry.save(spec("good"))
    broken = registry.instance_dir("broken")
    broken.mkdir(parents=True)
    (broken / "spec.json").write_text("{not json")
    assert [instance.name for instance in registry.list()] == ["good"]


def test_delete_removes_the_directory(registry: Registry):
    registry.save(spec())
    registry.delete("lab")
    assert not registry.exists("lab")
    assert not registry.instance_dir("lab").exists()


def test_delete_reports_unknown_instances(registry: Registry):
    with pytest.raises(InstanceNotFound):
        registry.delete("ghost")


def test_taken_ports_reserves_stopped_instances(registry: Registry):
    """A stopped instance's ports are free at the OS level but still belong to it."""
    registry.save(spec("a", port_base=21000, shards=2, replicas=2))
    taken = registry.taken_ports()
    assert 21000 in taken and 21030 in taken


def test_taken_ports_can_exclude_one_instance(registry: Registry):
    registry.save(spec("a", port_base=21000))
    assert registry.taken_ports(excluding="a") == set()


# --- profiles ----------------------------------------------------------------------------


@pytest.fixture
def profiles(tmp_path: Path) -> Profiles:
    return Profiles(tmp_path / "config")


def test_profile_drops_instance_specific_fields(profiles: Profiles):
    profiles.save("3x2", spec(shards=3, replicas=2))
    stored = profiles.get("3x2")
    assert stored["shards"] == 3
    for field in ("name", "source", "version", "port_base"):
        assert field not in stored


def test_profiles_round_trip_users(profiles: Profiles):
    profiles.save("with-users", spec(users=(UserSpec("dev", "pw", role="admin"),)))
    restored = apply(profiles.get("with-users"), {})
    assert restored["users"][0] == UserSpec("dev", "pw", role="admin")


def test_explicit_overrides_beat_the_profile(profiles: Profiles):
    profiles.save("3x2", spec(shards=3, replicas=2))
    merged = apply(profiles.get("3x2"), {"shards": 5, "replicas": None})
    assert merged["shards"] == 5
    assert merged["replicas"] == 2  # None means "not specified", so the profile wins


def test_missing_profile_reports_its_name(profiles: Profiles):
    with pytest.raises(ProfileNotFound, match="ghost"):
        profiles.get("ghost")


def test_delete_removes_the_profile(profiles: Profiles):
    profiles.save("temp", spec())
    profiles.delete("temp")
    assert profiles.names() == []


def test_summary_describes_the_topology(profiles: Profiles):
    profiles.save("full", spec(shards=2, replicas=2, keeper_nodes=3, seed=True))
    summary = summarise(profiles.get("full"))
    assert "2 shard(s) x 2 replica(s)" in summary
    assert "3 keeper" in summary
    assert "seeded" in summary
