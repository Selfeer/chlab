"""Companion services: spec validation, ports, compose, ClickHouse config and mc commands.

Nothing here starts a container; mc and Docker calls are captured, not run.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from chlab.core import compose, configgen, services
from chlab.core.registry import Registry
from chlab.core.resolve import Resolved
from chlab.core.runtime import Runtime
from chlab.core.services import s3
from chlab.core.sources import parse_source
from chlab.core.spec import (
    KNOWN_SERVICES,
    MAX_KEEPERS,
    ClusterSpec,
    S3UserSpec,
    SpecError,
    parse_s3_user,
)
from chlab.core.topology import SERVICE_PORT_BASE_OFFSET, build_topology, port_span


def spec(**overrides) -> ClusterSpec:
    fields = {"name": "lab", "source": "/bin/clickhouse", "port_base": 21000, "services": ("s3",)}
    fields.update(overrides)
    return ClusterSpec(**fields)


# --- spec ----------------------------------------------------------------------------------


def test_registry_matches_the_names_the_spec_accepts():
    assert set(services.REGISTRY) == set(KNOWN_SERVICES)


def test_unknown_services_are_rejected():
    with pytest.raises(SpecError, match="unknown service 'kafka'"):
        spec(services=("kafka",))


def test_a_service_can_be_requested_once():
    with pytest.raises(SpecError, match="only once"):
        spec(services=("s3", "s3"))


def test_s3_defaults_to_the_warehouse_bucket():
    assert spec().effective_buckets == ("warehouse",)
    assert spec(services=()).effective_buckets == ()


@pytest.mark.parametrize(
    "overrides",
    [{"buckets": ("data",)}, {"s3_users": (S3UserSpec("reader", "readerpass"),)}],
)
def test_buckets_and_s3_users_need_the_s3_service(overrides):
    with pytest.raises(SpecError, match="--with s3"):
        spec(services=(), **overrides)


@pytest.mark.parametrize("bucket", ["A", "ab", "-data", "data_lake", "x" * 64])
def test_invalid_bucket_names_are_rejected(bucket):
    with pytest.raises(SpecError, match="invalid bucket name"):
        spec(buckets=(bucket,))


def test_short_s3_secrets_are_rejected_before_the_server_sees_them():
    with pytest.raises(SpecError, match="at least 8"):
        S3UserSpec("reader", "short")


def test_the_root_s3_identity_cannot_be_redeclared():
    with pytest.raises(SpecError, match="root identity"):
        S3UserSpec("admin", "password123")


def test_s3_users_may_only_name_buckets_that_exist():
    with pytest.raises(SpecError, match="missing"):
        spec(s3_users=(S3UserSpec("reader", "readerpass", buckets=("missing",)),))


def test_keeper_count_is_bounded_so_service_ports_stay_clear():
    with pytest.raises(SpecError, match=f"at most {MAX_KEEPERS}"):
        spec(keeper_nodes=MAX_KEEPERS + 1)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("writer:writerpass", ("writer", "writerpass", "readwrite", ())),
        ("reader:readerpass:readonly", ("reader", "readerpass", "readonly", ())),
        ("reader:readerpass:readonly:a,b", ("reader", "readerpass", "readonly", ("a", "b"))),
        ("reader:readerpass::a", ("reader", "readerpass", "readwrite", ("a",))),
    ],
)
def test_parse_s3_user(text, expected):
    user = parse_s3_user(text)
    assert (user.name, user.secret, user.access, user.buckets) == expected


@pytest.mark.parametrize(
    "text, message",
    [
        ("reader", "name:secret"),
        ("r:readerpass", "3-64"),
        ("reader:readerpass:root", "unknown S3 access"),
    ],
)
def test_parse_s3_user_rejects_bad_input(text, message):
    with pytest.raises(SpecError, match=message):
        parse_s3_user(text)


def test_s3_users_round_trip_through_a_dict():
    original = spec(
        buckets=("a1x", "b2y"), s3_users=(S3UserSpec("reader", "readerpass", "readonly", ("a1x",)),)
    )
    restored = ClusterSpec.from_dict(json.loads(json.dumps(original.to_dict())))
    assert restored == original


# --- ports ---------------------------------------------------------------------------------


def test_service_ports_sit_above_every_node_and_keeper_port():
    topology = build_topology(spec(shards=30, replicas=1, keeper_nodes=MAX_KEEPERS))
    highest_other = max(
        [node.tcp_port for node in topology.nodes]
        + [keeper.client_port for keeper in topology.keepers]
    )
    assert topology.services[0].port_base == 21000 + SERVICE_PORT_BASE_OFFSET
    assert topology.services[0].port_base > highest_other


def test_port_span_covers_the_services():
    with_s3 = spec()
    assert port_span(with_s3) > build_topology(with_s3).services[0].port(9) - 21000


def test_port_span_is_unchanged_for_instances_without_services():
    """Earlier instances' blocks must keep describing what they reserved."""
    assert port_span(spec(services=())) == 310


def test_service_port_offsets_stay_inside_their_stride():
    with pytest.raises(ValueError):
        build_topology(spec()).services[0].port(10)


def test_companions_are_addressable_services():
    topology = build_topology(spec())
    assert topology.container("s3") == "chlab-lab-s3"
    assert "s3" in topology.service_names


# --- compose and config --------------------------------------------------------------------


def _resolved() -> Resolved:
    return Resolved(
        source=parse_source("/bin/clickhouse"), version="1", binary_path=Path("/bin/clickhouse")
    )


def test_s3_is_rendered_with_loopback_ports_and_its_own_volume(tmp_path: Path):
    document = compose.render(build_topology(spec()), _resolved(), tmp_path, "img")
    service = document["services"]["s3"]
    assert service["image"] == s3.IMAGE
    assert service["hostname"] == "s3"
    assert service["ports"] == ["127.0.0.1:21390:9000", "127.0.0.1:21391:9001"]
    assert "s3-data" in document["volumes"]


def test_no_s3_without_the_service(tmp_path: Path):
    document = compose.render(build_topology(spec(services=())), _resolved(), tmp_path, "img")
    assert "s3" not in document["services"]


def test_every_node_gets_named_collections_per_bucket(tmp_path: Path):
    configgen.write_configs(
        build_topology(spec(buckets=("warehouse", "other-data"))), tmp_path, base_image_mode=True
    )
    root = ET.parse(tmp_path / "configs/clickhouse1/config.d/60-chlab-s3.xml").getroot()
    collections = root.find("named_collections")
    assert [child.tag for child in collections] == ["s3", "s3_warehouse", "s3_other_data"]
    assert collections.findtext("s3/url") == "http://s3:9000/warehouse/"
    assert collections.findtext("s3_other_data/url") == "http://s3:9000/other-data/"
    assert collections.findtext("s3/access_key_id") == "admin"


def test_overlays_for_services_are_mounted(tmp_path: Path):
    topology = build_topology(spec())
    configgen.write_configs(topology, tmp_path, base_image_mode=False)
    node = compose.render(topology, _resolved(), tmp_path, "img")["services"]["clickhouse1"]
    assert any(mount.endswith("config.d/60-chlab-s3.xml:ro") for mount in node["volumes"])


def test_bare_binaries_get_a_query_log():
    """ProfileEvents come from system.query_log, which a bare binary lacks unless configured."""
    assert configgen.base_config()["query_log"]["table"] == "query_log"


# --- mc --------------------------------------------------------------------------------------


def test_mc_runs_on_the_instance_network_as_root_by_default():
    command = s3.mc_command(spec(), ["ls", "s3/warehouse"])
    assert command[command.index("--network") + 1] == "chlab-lab_default"
    assert "MC_HOST_s3=http://admin:password@s3:9000" in command
    assert command[-3:] == [s3.MC_IMAGE, "ls", "s3/warehouse"]


def test_mc_can_act_as_an_extra_user_with_awkward_secrets():
    lab = spec(s3_users=(S3UserSpec("reader", "p@ss/w0rd!"),))
    command = s3.mc_command(lab, ["ls"], as_user="reader")
    assert "MC_HOST_s3=http://reader:p%40ss%2Fw0rd%21@s3:9000" in command


def test_mc_refuses_an_unknown_user():
    with pytest.raises(s3.S3Error, match="no S3 user 'ghost'"):
        s3.mc_command(spec(), ["ls"], as_user="ghost")


def test_mounts_are_passed_with_their_mode():
    command = s3.mc_command(spec(), ["cp"], mounts=[("/a", "/in/a", True), ("/b", "/out", False)])
    assert "/a:/in/a:ro" in command and "/b:/out" in command


@pytest.mark.parametrize(
    "path, expected",
    [
        ("warehouse/a", "s3/warehouse/a"),
        ("/warehouse", "s3/warehouse"),
        ("s3/x", "s3/x"),
        ("", "s3/"),
    ],
)
def test_object_paths_are_prefixed_with_the_alias(path, expected):
    assert s3.object_path(path) == expected


def test_scoped_policies_cover_only_their_buckets():
    lab = spec(buckets=("aaa", "bbb"))
    user = S3UserSpec("reader", "readerpass", "readonly", ("aaa",))
    statement = s3.policy_document(lab, user)["Statement"][0]
    assert statement["Resource"] == ["arn:aws:s3:::aaa", "arn:aws:s3:::aaa/*"]
    assert "s3:PutObject" not in statement["Action"]


def test_unscoped_policies_cover_every_bucket():
    lab = spec(buckets=("aaa", "bbb"))
    statement = s3.policy_document(lab, S3UserSpec("writer", "writerpass", "writeonly"))[
        "Statement"
    ][0]
    assert len(statement["Resource"]) == 4
    assert "s3:GetObject" not in statement["Action"]


def test_initialise_creates_buckets_then_users(monkeypatch):
    calls = []
    monkeypatch.setattr(
        s3, "run_mc", lambda spec, args, stdin=None: calls.append((args, stdin)) or ""
    )
    lab = spec(buckets=("aaa",), s3_users=(S3UserSpec("reader", "readerpass", "readonly"),))
    topology = build_topology(lab)
    s3.S3().initialise(topology, topology.services[0], lambda _m: None)
    assert calls[0][0] == ["mb", "--ignore-existing", "s3/aaa"]
    assert calls[1][0] == ["admin", "user", "add", "s3", "reader", "readerpass"]
    assert json.loads(calls[2][1])["Statement"][0]["Resource"][0] == "arn:aws:s3:::aaa"
    assert calls[3][0][:3] == ["admin", "policy", "attach"]


def test_reattaching_an_attached_policy_is_not_an_error(monkeypatch):
    def run(spec, args, stdin=None):
        if args[:3] == ["admin", "policy", "attach"]:
            raise s3.S3Error("policy is already attached")
        return ""

    monkeypatch.setattr(s3, "run_mc", run)
    topology = build_topology(spec(s3_users=(S3UserSpec("reader", "readerpass"),)))
    s3.S3().initialise(topology, topology.services[0], lambda _m: None)


# --- runtime ---------------------------------------------------------------------------------


def test_profile_events_flush_every_node_and_ask_the_whole_cluster(tmp_path: Path, monkeypatch):
    from chlab.core.query import QueryResult

    runtime = Runtime(registry=Registry(tmp_path / "state"))
    runtime.registry.save(spec(shards=2))
    seen = []

    def fake_execute(url, sql, **kwargs):
        seen.append((url, sql))
        if sql.startswith("SELECT"):
            return QueryResult(columns=["event", "sum"], rows=[["S3GetObject", "3"]])
        return QueryResult()

    monkeypatch.setattr("chlab.core.runtime.execute", fake_execute)
    events = runtime.profile_events("lab", "q-1", "S3")
    flushed = sorted(url for url, sql in seen if sql == "SYSTEM FLUSH LOGS")
    assert flushed == ["http://127.0.0.1:21000", "http://127.0.0.1:21010"]
    lookup = next(sql for _, sql in seen if sql.startswith("SELECT"))
    assert "clusterAllReplicas('default_cluster', system.query_log)" in lookup
    assert "initial_query_id = 'q-1'" in lookup
    assert events == {"S3GetObject": 3}


def test_up_initialises_services_before_users(tmp_path: Path, monkeypatch):
    order = []
    monkeypatch.setattr(s3.S3, "initialise", lambda self, *a: order.append("s3"))
    monkeypatch.setattr(
        Runtime, "_run_statements", lambda self, node, statements: order.append("sql")
    )
    runtime = Runtime(registry=Registry(tmp_path / "state"))
    from chlab.core.spec import UserSpec

    runtime._initialise(build_topology(spec(users=(UserSpec("dev"),))), notify=lambda _m: None)
    assert order[0] == "s3" and "sql" in order


def test_a_failed_service_setup_is_an_init_error(tmp_path: Path, monkeypatch):
    from chlab.core.runtime import InitError

    def broken(self, *args):
        raise s3.S3Error("mc mb failed: boom")

    monkeypatch.setattr(s3.S3, "initialise", broken)
    runtime = Runtime(registry=Registry(tmp_path / "state"))
    with pytest.raises(InitError, match="setting up s3 failed"):
        runtime._initialise(build_topology(spec()), notify=lambda _m: None)


def test_environment_describes_the_s3_service(tmp_path: Path, monkeypatch):
    from chlab.core.environment import describe

    monkeypatch.setattr("chlab.core.runtime._compose_containers", lambda: {})
    runtime = Runtime(registry=Registry(tmp_path / "state"))
    runtime.registry.save(spec(s3_users=(S3UserSpec("reader", "readerpass", "readonly"),)))
    info = describe(runtime.status("lab"), runtime.registry)["services"]["s3"]
    assert info["host"]["endpoint"] == "http://127.0.0.1:21390"
    assert info["network"]["endpoint"] == "http://s3:9000"
    assert info["users"][0]["buckets"] == ["warehouse"]
    assert "s3" in info["named_collections"]
