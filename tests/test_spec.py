from __future__ import annotations

import pytest

from chlab.core.spec import (
    ClusterSpec,
    DatabaseSpec,
    SpecError,
    UserSpec,
    parse_database,
    parse_grant,
    parse_user,
    suggest_name,
)


def spec(**overrides) -> ClusterSpec:
    return ClusterSpec(**{"name": "test", "source": "/bin/clickhouse", **overrides})


def test_node_count_is_shards_times_replicas():
    assert spec(shards=3, replicas=2).node_count == 6


@pytest.mark.parametrize(
    "replicas, keeper_nodes, expected",
    [
        (1, None, 0),  # no replication, no keeper needed
        (2, None, 1),  # replication implies keeper
        (2, 0, 0),  # explicitly refused
        (1, 3, 3),  # explicitly requested
    ],
)
def test_keeper_is_implied_by_replication(replicas, keeper_nodes, expected):
    assert spec(replicas=replicas, keeper_nodes=keeper_nodes).effective_keeper_nodes == expected


@pytest.mark.parametrize("name", ["Upper", "-leading", "has space", "", "a" * 32])
def test_rejects_bad_instance_names(name):
    with pytest.raises(SpecError):
        spec(name=name)


@pytest.mark.parametrize("shards, replicas", [(0, 1), (1, 0), (-1, 1)])
def test_rejects_empty_topologies(shards, replicas):
    with pytest.raises(SpecError, match="at least 1"):
        spec(shards=shards, replicas=replicas)


def test_rejects_replicated_database_without_keeper():
    with pytest.raises(SpecError, match="needs Keeper"):
        spec(replicas=1, keeper_nodes=0, databases=(DatabaseSpec("d", "Replicated"),))


def test_project_name_namespaces_docker_objects():
    assert spec(name="antalya").project_name == "chlab-antalya"


def test_round_trips_through_a_dict():
    original = spec(
        users=(UserSpec("dev", "pw", role="admin", grants=("SELECT ON x.*",)),),
        databases=(DatabaseSpec("d"),),
        shards=2,
        replicas=2,
    )
    restored = ClusterSpec.from_dict(original.to_dict())
    assert restored == original


def test_from_dict_ignores_fields_it_does_not_know():
    """A spec written by a different chlab version must stay loadable."""
    payload = spec().to_dict()
    payload["some_future_field"] = 1
    payload["users"] = [{"name": "dev", "access_management": True}]
    restored = ClusterSpec.from_dict(payload)
    assert restored.users[0].name == "dev"


# --- argument parsing --------------------------------------------------------------------


def test_parse_user_defaults_to_readwrite():
    user = parse_user("dev")
    assert (user.name, user.password, user.role) == ("dev", "", "readwrite")


def test_parse_user_reads_password_and_role():
    user = parse_user("dev:secret:admin")
    assert (user.name, user.password, user.role) == ("dev", "secret", "admin")
    assert user.is_admin


def test_parse_user_keeps_colons_in_passwords():
    """Only a known role name in the last field is treated as a role."""
    assert parse_user("dev:p:ss").password == "p:ss"
    assert parse_user("dev:p:ss").role == "readwrite"
    assert parse_user("dev:p:ss:admin").password == "p:ss"
    assert parse_user("dev:p:ss:admin").role == "admin"


def test_parse_user_rejects_an_unknown_role_given_alone():
    """A single trailing field that is not a role is a password, not a typo'd role."""
    assert parse_user("dev:wizard").password == "wizard"


def test_user_spec_rejects_an_unknown_role():
    with pytest.raises(SpecError, match="unknown role"):
        UserSpec("dev", role="wizard")


def test_parse_grant_splits_on_the_first_colon():
    assert parse_grant("ro:SELECT ON demo.*") == ("ro", "SELECT ON demo.*")


@pytest.mark.parametrize("text", ["ro", "ro:", ":SELECT ON x"])
def test_parse_grant_rejects_incomplete_input(text):
    with pytest.raises(SpecError):
        parse_grant(text)


def test_parse_database_defaults_to_atomic():
    assert parse_database("analytics") == DatabaseSpec("analytics", "Atomic")


def test_parse_database_rejects_unknown_engine():
    with pytest.raises(SpecError, match="unsupported database engine"):
        parse_database("analytics:Log")


@pytest.mark.parametrize("catalog", ["iceberg-rest", "ice-rest"])
def test_parse_database_accepts_a_catalog_service_as_engine(catalog):
    assert parse_database(f"rest:{catalog}") == DatabaseSpec("rest", catalog)


def test_rejects_catalog_database_without_its_catalog():
    with pytest.raises(SpecError, match="add --with iceberg-rest"):
        spec(services=("s3",), databases=(DatabaseSpec("rest", "iceberg-rest"),))


def test_accepts_catalog_database_with_its_catalog():
    result = spec(
        services=("s3", "iceberg-rest"), databases=(DatabaseSpec("rest", "iceberg-rest"),)
    )
    assert result.databases[0].engine == "iceberg-rest"


# --- naming ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source, expected",
    [
        ("docker://altinityinfra/clickhouse-server:1984-26.3.12.20001.x", "ch-26-3-12-20001"),
        ("https://host/clickhouse-common-static_26.3.17.20001.alt_amd64.deb", "ch-26-3-17-20001"),
        ("/usr/bin/clickhouse", "clickhouse"),
    ],
)
def test_suggest_name_prefers_versions(source, expected):
    assert suggest_name(source) == expected


def test_suggest_name_avoids_collisions():
    source = "docker://x/y:24.8.1.1"
    assert suggest_name(source, taken={"ch-24-8-1-1"}) == "ch-24-8-1-1-2"


def test_every_chlab_exception_inherits_the_base_class():
    """The CLI catches ChlabError and nothing else.

    An exception that escapes the hierarchy reaches the user as a traceback — which is exactly
    what happened to QueryError when the hierarchy was introduced. Asserting the invariant here
    means the next one cannot slip through.
    """
    import importlib
    import inspect
    import pkgutil

    import chlab.core
    from chlab.core.errors import ChlabError

    stray = []
    for module_info in pkgutil.iter_modules(chlab.core.__path__):
        module = importlib.import_module(f"chlab.core.{module_info.name}")
        for name, obj in vars(module).items():
            if not inspect.isclass(obj) or obj.__module__ != module.__name__:
                continue
            if issubclass(obj, BaseException) and not issubclass(obj, ChlabError):
                stray.append(f"{module.__name__}.{name}")
    assert stray == [], f"these exceptions do not inherit ChlabError: {stray}"


def test_profile_settings_round_trip_and_are_copied():
    source = {"allow_insert_into_iceberg": "1"}
    spec = ClusterSpec(name="t", source="/x", profile_settings=source)
    source["max_threads"] = "1"
    assert spec.profile_settings == {"allow_insert_into_iceberg": "1"}
    assert ClusterSpec.from_dict(spec.to_dict()).profile_settings == spec.profile_settings


def test_profile_setting_keys_are_validated():
    with pytest.raises(SpecError, match="invalid setting name"):
        ClusterSpec(name="t", source="/x", profile_settings={"max threads": "1"})
