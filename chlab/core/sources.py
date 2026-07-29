"""Parsing of ``--clickhouse`` source strings.

A source string is one of:

    docker://altinityinfra/clickhouse-server:1984-26.3.12.20001.altinityantalya
    https://example.com/clickhouse-common-static_26.3.17.20001_amd64.deb
    https://example.com/clickhouse
    /usr/bin/clickhouse
    ./builds/clickhouse-common-static.tgz

``url://`` is accepted as an alias for ``https://`` and ``file://`` for a local path, matching
the conventions used by Altinity's clickhouse-regression suite.
"""

from __future__ import annotations

import enum
import os
import re
from dataclasses import dataclass
from pathlib import Path

DOCKER_PREFIX = "docker://"
URL_ALIAS_PREFIX = "url://"
FILE_PREFIX = "file://"

#: Suffixes we know how to unpack, longest first so ``.tar.gz`` wins over ``.gz``.
PACKAGE_SUFFIXES = (".tar.gz", ".deb", ".tgz", ".rpm")


class SourceKind(str, enum.Enum):
    DOCKER = "docker"
    URL = "url"
    PATH = "path"


class ArtifactType(str, enum.Enum):
    """What the referenced file actually is, once we have it locally."""

    DEB = "deb"
    TGZ = "tgz"
    RPM = "rpm"
    BINARY = "binary"
    #: Not a file at all — the artifact is a container image.
    IMAGE = "image"


class UnsupportedSource(ValueError):
    """Raised for sources chlab cannot handle."""


@dataclass(frozen=True)
class Source:
    """A parsed, not-yet-fetched ClickHouse source."""

    raw: str
    kind: SourceKind
    #: Image reference, URL, or filesystem path, with any chlab-specific prefix stripped.
    value: str
    artifact: ArtifactType

    @property
    def is_docker(self) -> bool:
        return self.kind is SourceKind.DOCKER

    @property
    def filename(self) -> str:
        """Basename of the referenced file. Meaningless for docker sources."""
        return os.path.basename(self.value.split("?", 1)[0].rstrip("/"))


def _artifact_type(name: str) -> ArtifactType:
    lowered = name.lower()
    for suffix in PACKAGE_SUFFIXES:
        if lowered.endswith(suffix):
            if suffix == ".deb":
                return ArtifactType.DEB
            if suffix == ".rpm":
                return ArtifactType.RPM
            return ArtifactType.TGZ
    return ArtifactType.BINARY


def parse_source(raw: str) -> Source:
    """Parse a source string. Does not touch the network or the filesystem."""
    if not raw or not raw.strip():
        raise UnsupportedSource("source is empty")

    text = raw.strip()

    if text.startswith(DOCKER_PREFIX):
        image = text[len(DOCKER_PREFIX) :]
        if not image:
            raise UnsupportedSource(f"{raw!r} has no image reference after docker://")
        return Source(raw, SourceKind.DOCKER, image, ArtifactType.IMAGE)

    if text.startswith(URL_ALIAS_PREFIX):
        text = "https://" + text[len(URL_ALIAS_PREFIX) :]

    if text.startswith(("http://", "https://")):
        source = Source(raw, SourceKind.URL, text, _artifact_type(text.split("?", 1)[0]))
        if source.artifact is ArtifactType.RPM:
            raise UnsupportedSource("rpm packages are not supported yet")
        return source

    if text.startswith(FILE_PREFIX):
        text = text[len(FILE_PREFIX) :]

    path = Path(text).expanduser()
    source = Source(raw, SourceKind.PATH, str(path), _artifact_type(path.name))
    if source.artifact is ArtifactType.RPM:
        raise UnsupportedSource("rpm packages are not supported yet")
    return source


_TAG_VERSION = re.compile(r"\d+\.\d+\.\d+(?:\.\d+)?[0-9A-Za-z.\-+]*")


def version_from_image_tag(image: str) -> str | None:
    """Best-effort version guess from a docker image tag.

    Only a fallback for when the image cannot be run; ``clickhouse server --version`` is
    authoritative. Tags like ``1984-26.3.12.20001.altinityantalya`` carry a leading build
    number, so we take the longest match rather than the first.
    """
    _, _, tag = image.rpartition(":")
    if not tag or "/" in tag:
        return None
    matches = _TAG_VERSION.findall(tag)
    if not matches:
        return None
    return max(matches, key=len).strip(".-+")
