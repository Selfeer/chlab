from __future__ import annotations

import pytest

from chlab.core.sources import (
    ArtifactType,
    SourceKind,
    UnsupportedSource,
    parse_source,
    version_from_image_tag,
)

DEB_URL = (
    "https://altinity-build-artifacts.s3.amazonaws.com/PRs/2074/b4cca456/build_amd_release/"
    "clickhouse-common-static_26.3.17.20001.altinityantalya_amd64.deb"
)


@pytest.mark.parametrize(
    "raw, kind, artifact",
    [
        ("docker://altinityinfra/clickhouse-server:1984-26.3.12", SourceKind.DOCKER, ArtifactType.IMAGE),
        (DEB_URL, SourceKind.URL, ArtifactType.DEB),
        ("https://example.com/clickhouse.tgz", SourceKind.URL, ArtifactType.TGZ),
        ("https://example.com/clickhouse.tar.gz", SourceKind.URL, ArtifactType.TGZ),
        ("https://example.com/clickhouse", SourceKind.URL, ArtifactType.BINARY),
        ("/usr/bin/clickhouse", SourceKind.PATH, ArtifactType.BINARY),
        ("./builds/pkg.deb", SourceKind.PATH, ArtifactType.DEB),
    ],
)
def test_parse_source_classifies(raw, kind, artifact):
    source = parse_source(raw)
    assert source.kind is kind
    assert source.artifact is artifact
    assert source.raw == raw


def test_url_alias_becomes_https():
    assert parse_source("url://example.com/clickhouse").value == "https://example.com/clickhouse"


def test_file_prefix_is_stripped():
    source = parse_source("file:///opt/clickhouse")
    assert source.kind is SourceKind.PATH
    assert source.value == "/opt/clickhouse"


def test_docker_prefix_is_stripped():
    source = parse_source("docker://altinity/clickhouse-server:24.8")
    assert source.value == "altinity/clickhouse-server:24.8"
    assert source.is_docker


def test_query_string_does_not_confuse_artifact_detection():
    source = parse_source("https://example.com/pkg.deb?X-Amz-Signature=abc")
    assert source.artifact is ArtifactType.DEB
    assert source.filename == "pkg.deb"


@pytest.mark.parametrize("raw", ["", "   ", "docker://"])
def test_rejects_empty_sources(raw):
    with pytest.raises(UnsupportedSource):
        parse_source(raw)


@pytest.mark.parametrize("raw", ["https://example.com/pkg.rpm", "/tmp/pkg.rpm"])
def test_rejects_rpm_for_now(raw):
    with pytest.raises(UnsupportedSource, match="rpm"):
        parse_source(raw)


@pytest.mark.parametrize(
    "image, expected",
    [
        ("altinityinfra/clickhouse-server:1984-26.3.12.20001.altinityantalya", "26.3.12.20001.altinityantalya"),
        ("clickhouse/clickhouse-server:24.8.14.10501", "24.8.14.10501"),
        ("clickhouse/clickhouse-server:latest", None),
        ("clickhouse/clickhouse-server", None),
        ("registry:5000/clickhouse-server", None),
    ],
)
def test_version_from_image_tag(image, expected):
    assert version_from_image_tag(image) == expected
