# chlab

Run any ClickHouse build, immediately. See [SPEC.md](SPEC.md) for the design.

```bash
chlab up --clickhouse docker://altinityinfra/clickhouse-server:1984-26.3.12.20001.altinityantalya \
         --shards 2 --replicas 2 --seed

chlab up --clickhouse https://altinity-build-artifacts.s3.amazonaws.com/PRs/2074/…_amd64.deb \
         --name pr2074 --profile 2x2-seeded

chlab ls     # both running, side by side, on their own ports
chlab ui     # setup wizard and SQL editor at localhost:7777
```

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Add `.[zstd]` if you work with packages compressed by newer dpkg; otherwise the `zstd`
command-line tool is used when present. Docker and Docker Compose v2 are required to run
clusters (not to resolve sources).

## Commands

```bash
# sources
chlab resolve <source> [--clickhouse-version X] [--force] [--json]
chlab cache ls | path | clear

# clusters
chlab up --clickhouse <source> [--name N] [--shards S] [--replicas R] [--keeper K]
         [--user name[:password][:none|readonly|readwrite|admin]] [--grant "user:PRIVS ON target"]
         [--database name[:Atomic|Replicated]] [--setting key=value] [--seed] [--profile P]
chlab ls
chlab status <name>
chlab stop <name> | start <name> | down <name> | rm <name>
chlab logs <name> [service] [--follow] [--tail N]

# querying
chlab query <name> "SELECT 1" [--node clickhouse2 | --all] [--user U --password P] [--json]
chlab client <name> [node]

# topologies
chlab profiles ls | save <profile> <instance> | rm <profile>

# web
chlab ui [--port 7777] [--no-open]
```

`--all` runs the same query on every node and reports whether they agree — useful for
checking replication or comparing behaviour across a cluster.

## What runs where

| Source | Runtime |
|---|---|
| `docker://image:tag` | that image, unmodified, with only generated configs mounted in |
| `.deb` / `.tgz` / raw binary / local path | the binary bind-mounted into a shared base image |

Data lives in per-node Docker volumes, so `stop` / `start` preserves it and `rm` discards it.
Ports are bound to loopback only and stay stable for the life of a named instance.

## Tests

```bash
.venv/bin/python -m pytest
```

No network, no Docker: the suite builds real `.deb` and `.tgz` archives in a tmpdir, serves
them from a local HTTP server, and drives the web app against a stubbed runtime.
