# chlab

Run any ClickHouse build, immediately.

`chlab` turns any ClickHouse artifact — a Docker image, a download URL, a `.deb`, a local
binary — into a running, configured cluster you can query. Built for manual testing and
SQL exploration across ClickHouse distributions (Altinity, upstream, Antalya, custom builds).

```bash
chlab up --clickhouse docker://altinityinfra/clickhouse-server:1984-26.3.12.20001.altinityantalya \
         --name antalya --shards 3 --replicas 2

chlab up --clickhouse https://altinity-build-artifacts.s3.amazonaws.com/PRs/2074/…/clickhouse-common-static_26.3.17.20001.altinityantalya_amd64.deb \
         --name pr2074

chlab ls        # both running, side by side, on their own ports
chlab ui        # setup wizard + SQL editor at localhost:7777
```

## Goals

- **Any source, one flag.** `--clickhouse <anything>` resolves and runs.
- **Named instances.** Multiple versions run concurrently on stable, distinct ports, so you
  can diff 24.8 against 26.3 in two browser tabs.
- **Interactive setup.** Shards, replicas, keeper, users, databases chosen through a web form
  before launch — not hand-edited XML.
- **Fast.** Re-running a previously resolved source is instant; launching a cluster is seconds,
  not minutes.

## Non-goals (v1)

TLS, Kafka, LDAP/Kerberos, RPM packages, ZooKeeper (Keeper only), sanitizer
detection, cross-architecture execution. All additive later; none change the core shape.

S3-compatible storage and Iceberg were non-goals in v1; they are now in scope — see
*Reproduction toolkit* below.

---

## Architecture

```
chlab/
  core/
    sources.py    parse a source string -> Source(kind, value, artifact type)
    cache.py      ~/.cache/chlab layout, cache keys
    download.py   streaming HTTP download, atomic, resumable-safe
    unpack.py     .deb (ar + tar) and .tgz extraction, no external tools required
    resolve.py    Source -> Resolved(docker_image | binary_path, version)
    topology.py   ClusterSpec -> nodes, shard/replica assignment, macros
    configgen.py  remote_servers.xml, macros.xml, keeper.xml, users.d/*.xml
    compose.py    ClusterSpec -> docker-compose.yml
    runtime.py    up/down/status/logs, health wait, port allocation
    registry.py   ~/.local/state/chlab/instances.json
    profiles.py   saved topologies
    seed.py       databases + synthetic tables
    query.py      HTTP query execution, single-node or fan-out
  cli.py          Typer entrypoint
  web/            FastAPI app, templates, static assets
```

The CLI and the web UI both construct a `ClusterSpec` and hand it to `runtime`. The web layer
is a form over the dataclass — no behavior lives only in the UI.

**Stack:** Python 3.12, Typer, FastAPI, Jinja2, vanilla JS + Ace editor (no frontend build step).

---

## Source resolution

Modelled on `PackageDownloader` in Altinity's `clickhouse-regression`
(`helpers/cluster.py:1439`), with one significant deviation — see *Runtime* below.

| Source form | Handling |
|---|---|
| `docker://image:tag` | run that image directly |
| `https://….deb` | download → unpack (ar + tar) → `usr/bin/clickhouse` |
| `https://….tgz` / `.tar.gz` | download → untar → `usr/bin/clickhouse` |
| `https://…/clickhouse` | download → `chmod +x` |
| `/path/to/clickhouse` | use in place |
| `/path/to/pkg.deb` / `.tgz` | unpack |

`url://` is accepted as an alias for `https://`, and `file://` for a local path, matching the
regression repo's conventions.

### Cache

```
$XDG_CACHE_HOME/chlab/            (default ~/.cache/chlab)
  artifacts/<key>/<filename>      downloaded .deb / .tgz / raw binary
  bin/<key>/clickhouse            extracted, executable
  bin/<key>/meta.json             {source, version, resolved_at}
```

`<key>` is a truncated SHA-256 of the source string; for local paths the file's size and mtime
are mixed in, so editing a local binary invalidates its cache entry. A resolved source is
re-resolved only on `--force`.

### Version detection

Authoritative version comes from asking the binary: `clickhouse server --version`. Tried on the
host first (fast, no Docker needed); on failure — wrong architecture, missing loader — it is
retried inside a container. For `docker://` sources it runs inside that image. Parsing the
version out of a Docker tag is a last-resort fallback only, and warns.

Determining a version means *executing* the artifact, so where that happens matters. A binary
just fetched from a URL is probed **in a container first** — running it on the host would make
`chlab up -c http://…` remote code execution on the host. A binary the user already had on disk
is probed on the host first instead: they chose that file, and requiring Docker to read a local
build's version would break working offline. Either way the answer is cached.

`--clickhouse-version` overrides detection entirely, as in `regression.py`. `--sha256` verifies
a download before it is unpacked; plain `http://` sources warn.

Downloads are checked against `Content-Length`. `http.client` does not raise when a body ends
short, so without that check a connection dropped mid-transfer would be renamed into place and
cached as a complete artifact forever. Unpacking streams throughout: a 790 MB binary is
extracted from a 236 MB `.deb` in about 20 MB of RSS.

---

## Runtime

**Deviation from the regression repo.** `helpers/cluster.py:1676-1703` runs `docker build` per
package to bake the binary into a base image. Because `clickhouse-common-static` is a static
binary, that build is unnecessary: chlab builds **one** base image and bind-mounts the resolved
binary read-only at `/usr/bin/clickhouse`. Every version reuses the same image, and startup
drops from minutes to seconds.

The base image is deliberately thin: Ubuntu plus CA certificates, a timezone, and the standard
ClickHouse directories. It contains no ClickHouse, no stock configuration, **no `clickhouse`
user, and no entrypoint** — the entrypoint is set per service in the compose file, because
Keeper and the server run the same binary with different arguments. The server therefore runs
as root in base-image mode and writes root-owned files into its data volume. For a local lab
whose volumes are disposable that is an accepted trade, not an oversight; a non-root user would
also make existing volumes unreadable on upgrade.

Docker-image sources are exempt: those run **as that image**, with only generated configs
mounted in. If you point chlab at an image, you want to test that image — not a binary ripped
out of it.

### Topology

`nodes = shards × replicas`, named `clickhouse1…N` and `keeper1…N` (matching the regression
repo's convention).
Each node gets macros `{shard}` and `{replica}`; a generated `remote_servers.xml` defines
`default_cluster`.

Keeper reuses the *same resolved binary* in `clickhouse keeper` mode — no second download.
Defaults to one keeper node, auto-enabled when `replicas > 1`; `--keeper 3` for quorum testing.

### Ports

Allocated per instance at creation from a scanned-free range and persisted in the registry, so
reconnecting to a named instance always hits the same port.

---

## Milestones

All eight are implemented.

1. **Resolver + cache** — `chlab resolve <source>`, for all source forms.
2. **Runtime, single node** — base image, compose generation, `up` / `down` / `status` / `logs`.
3. **Topology** — shards, replicas, keeper, generated XML.
4. **Named instances** — registry, port allocation, concurrent versions.
5. **Users / RBAC, databases, synthetic seed.**
6. **Web UI** — setup wizard → spec, instance dashboard.
7. **SQL editor** — query pane, per-node targeting, fan-out-and-diff across nodes, history.
8. **Saved profiles** — name a topology, relaunch it against any binary.

Both open questions were answered yes: node `:8123/play` links appear on the dashboard, the
instance page and `chlab status`; `chlab client <name> [node]` opens `clickhouse-client`.

## Decisions made during implementation

**Users are SQL objects, not XML.** Users declared in `users.xml` are read-only to the RBAC
system, so `GRANT` against them fails. chlab creates every declared user with `CREATE USER`
after start, in the local access directory, where grants behave normally. Only the `default`
user is declared in XML.

**Admin uses `GRANT CURRENT GRANTS`, not `GRANT ALL`.** `default` does not itself hold every
privilege `ALL` covers — `SHOW NAMED COLLECTIONS SECRETS` among them — and cannot grant what
it lacks. `CURRENT GRANTS` passes on exactly what the granting user has.

**No `ON CLUSTER`.** All DDL runs on every node individually. `ReplicatedMergeTree` and the
`Replicated` database engine are built for each replica to register itself, so nothing needs
the distributed DDL queue — one less moving part, and no dependency on queue health.

**Readiness is checked in three stages.** Keeper binds its client port only after its Raft
election settles, and a server that answers `/ping` has not necessarily established its Keeper
session yet — replicated DDL fails outright rather than retrying. `up` therefore waits for
Keeper's `ruok`, then each server's `/ping`, then a live `system.zookeeper` read per node.

**Role presets.** `--user name[:password][:role]` where role is `none`, `readonly`,
`readwrite` (the default) or `admin`. `--grant user:PRIVILEGES ON target` adds anything else.
Users are created `IDENTIFIED WITH sha256_password`, so the server stores a hash rather than the
cleartext. `spec.json` and `profiles.json` still record the password you chose, so both are
written `0600` under a `0700` state directory.

**Seed data matches its own sharding key.** The demo inserts place each row on the shard the
Distributed table's `cityHash64(user_id)` selects. Splitting rows by `number % shards` would
total correctly but put them on shards the sharding key says they are not on, and
`optimize_skip_unused_shards=1` would then silently return partial results — a trap in a tool
whose job is checking whether a build behaves correctly.

**The web UI checks the `Host` header.** Binding to loopback is not a defence against DNS
rebinding: a page the user visits can point a hostname it controls at `127.0.0.1` and reach the
API same-origin. The API runs SQL and deletes clusters, so it answers only on loopback names.
The SQL editor's Ace build is vendored rather than loaded from a CDN, which also makes the UI
work offline.

## Reproduction toolkit

**Status: in progress.**

### Purpose

PR audit reviews of ClickHouse report defects in prose: *"a plain `OPTIMIZE TABLE` still
records the new metadata file"*, *"a plain column's bloom read downloads the previous column's
fallback body"*. Someone then has to decide whether each finding is real. chlab's job is to
make that cheap: everything a finding needs to be reproduced — the build, object storage, an
Iceberg catalog, the right settings, a way to look inside the containers — is one command
away, and a reproduction can be written down and replayed by someone else.

chlab is the toolbox, not the investigator. It does not read reviews, fetch PR builds or
decide verdicts; whoever drives it (a developer, or any agent pointed at `chlab --help`)
does. That sets the bar for the interface: every capability is a plain, discoverable
command; every command that reports something has `--json`; failures exit non-zero with the
server's own error code; nothing requires an interactive terminal.

### What reproductions actually need

Taken from three audited PRs (Altinity/ClickHouse #2588, #2792, #2551):

| Need | Example |
|---|---|
| object storage with files in it | Parquet files with particular bloom-filter layouts, read over S3 |
| request-level evidence | count of S3 GETs per query, from `ProfileEvents` and `query_log` |
| an Iceberg catalog | `CREATE TABLE ... AS` from a `DataLakeCatalog` table |
| catalog-less Iceberg | `IcebergLocal`, `icebergS3` over a path |
| files inside the server | read `metadata/v4.metadata.json` with `jq` after an `OPTIMIZE` |
| restarts | "engine settings are lost when the table is reopened" |
| query/profile settings | `allow_insert_into_iceberg`, `allow_experimental_iceberg_compaction` |
| several buckets and credentials | cross-bucket external paths, restricted access |
| the same steps on two builds | "is this new in the PR, or already on the base branch?" |

### Shape

**Companion services** are add-ons to an instance's compose project, chosen with
`--with`:

```bash
chlab up -c <build> --name lab --with s3 --with iceberg-rest
```

Each service is a small module that contributes: a compose fragment, a readiness check, the
ClickHouse configuration it needs (named collections, `remote_url_allow_hosts`), and its
entry in `chlab env`. They join the existing pipeline — `ClusterSpec` gains a `services`
field, topology/compose/runtime treat them like keepers. No service is started unless asked.

| Service | Image | Notes |
|---|---|---|
| `s3` | `rustfs/rustfs:1.0.3` | Matches clickhouse-regression's Iceberg suite. Path-style access only. Buckets created at start (`--bucket`, default `warehouse`). |
| `iceberg-rest` | `apache/iceberg-rest-fixture` | Backed by an SQLite *file*: the default in-memory store breaks under concurrent requests. |
| `ice-rest` | `altinity/ice-rest-catalog` | Altinity's catalog; bearer-token auth. |
| `spark` | `tabulario/spark-iceberg` | Opt-in and heavy (20–40s JVM start per call). Only for what pyiceberg cannot write: format v3, deletion vectors, merge-on-read deletes. |
| later | toxiproxy, Azurite, Kafka | Latency/fault injection in front of S3 first. |

Implemented as `iceberg-rest` → `apache/iceberg-rest-fixture:1.9.1` and `ice-rest` →
`altinity/ice-rest-catalog:0.16.0`; `spark` is still to come.

Glue is TBD: the only local option, LocalStack Pro, needs a paid token. Nessie, Polaris,
Hive and Unity have no prior art in clickhouse-regression and are not planned.

**A tools container, not host dependencies.** Writing Iceberg data needs pyiceberg and
pyarrow; inspecting buckets needs an S3 client. These live in one `chlab/tools` image, built
once like `chlab/base`, attached to the instance's network with the catalog and S3
endpoints already in its environment. chlab itself stays dependency-light, and a script that
works for one person works for everyone.

```bash
chlab s3 put lab ./data.parquet warehouse/data/   # also ls, cat, rm, mb
chlab py lab make_table.py                        # pyiceberg, wired to the instance
```

**ClickHouse sees the services by name.** Every node gets named collections for what is
running (`s3`, `iceberg_rest`), so SQL stays short — `icebergS3(s3, filename = 'data')` — and
`default` is granted what it needs to use them. (`SHOW NAMED COLLECTIONS SECRETS` is the
grant a CI run already tripped over.)

**`chlab env`** prints everything a person or script needs to talk to an instance:
endpoints from the host and from inside the network, credentials, container names, and
ready-to-paste SQL for each service, version-gated where ClickHouse changed its syntax
(`DataLakeCatalog` from 25.3, `Iceberg` before; `icebergS3` from 24.9).

### Reproductions as files

A reproduction is a directory a reviewer can hand to a developer:

```
repro/2792-binpack/
  repro.yaml        topology, services, settings, buckets
  01-setup.sql
  02-optimize.sql
  03-check.sh       runs inside a node via `chlab exec`
  04-data.py        runs in the tools container
```

Steps are plain `.sql`, `.sh` and `.py` files run in name order, each with an optional
expectation (an error code, an exact result, a result equal across builds).
`chlab repro run <dir> -c <build> [--compare <other build>]` runs them on fresh instances and
writes a JSON result plus a markdown transcript. No DSL: the files are what you would have
typed anyway.

### Decisions made for object storage

**Services contribute; nothing branches on their names.** A service is a class in
`chlab.core.services` providing its compose definition, `config.d` overlays for the nodes, a
readiness probe, one-time setup, and its `chlab env` entry. Each gets a stride of ten host
ports above the keeper range, which is why keepers are now capped at nine. An instance with no
services keeps exactly the port span it had before, so the blocks of existing instances still
describe what they reserved.

**`mc` in a throwaway container, not an S3 client on the host.** Bucket and user setup and the
`chlab s3` commands all run `mc` on the instance's network. It runs as the invoking user, so
files it downloads into a mounted directory are not owned by root.

**Named collections carry the root credentials.** `s3(s3, filename = 'x')` is short enough to
type in every reproduction. Using a collection needs `NAMED COLLECTION ON <name>`; `default`
has `named_collection_control`, and chlab's `admin` role passes on `CURRENT GRANTS`, but the
`readwrite` and `readonly` roles deliberately get nothing on S3. Reproductions about access
control need that boundary exactly where the grants put it.

**Scoped identities are real policies.** `--s3-user reader:pw:readonly:warehouse` becomes a
RustFS user with a policy on `arn:aws:s3:::warehouse` and `.../*`. Verified: that identity
reads `warehouse`, is refused listing `other`, and is refused writes. Secrets are checked to
be at least eight characters, because RustFS otherwise fails setup with "invalid secret key
length" long after the command that asked for it.

**ProfileEvents are summed by `initial_query_id` across the cluster.** `s3Cluster` and
`Distributed` queries do part of their work on other nodes; asking only the node that
received the query undercounts. Verified on a two-node `s3Cluster` read: 2 GETs on one node,
1 on the other, 3 reported. Every node's log is flushed first. Bare binaries now get a
`query_log` in their generated config; stock images already have one.

**Caches change counts.** Antalya builds answer `count()` over Parquet from cached footers,
so a repeated `count()` can show zero GETs. A reproduction about request counts should read
data and set `enable_filesystem_cache = 0`, or compare cold runs.

### Decisions made for Iceberg

**Two catalogs, one interface.** `iceberg-rest` and `ice-rest` share a `RestCatalog` base;
they differ in image, port, token and one detail that is easy to get wrong: ClickHouse
addresses the Apache fixture as `http://iceberg-rest:8181/v1` and ice-rest at its root, while
pyiceberg takes the root for both and appends `/v1` itself. Both keep their catalog database
on a named volume — the fixture's default in-memory SQLite is per connection, so concurrent
requests would see different, empty catalogs — and both survive `stop`/`start`.

**The tools image is the only place data is created.** `chlab/tools` is `python:3.12-slim`
with clickhouse-regression's pins (pyiceberg 0.9.0, pyiceberg-core 0.4.0, pyarrow 20.0.0,
boto3 1.34.146) plus mc and jq, built from a Dockerfile piped to `docker build` with no
context, tagged by the Dockerfile's hash. pyiceberg's `[pyarrow]` extra cannot be combined
with pyarrow 20, which is why the packages are installed plainly, as the regression suite
does. Each service contributes its environment (`PYICEBERG_CATALOG__<NAME>__…`,
`AWS_ENDPOINT_URL`, `MC_HOST_s3`), so a script needs no configuration. The `chlab_tools`
helper is mounted from the chlab checkout rather than baked in, so it always matches the
chlab that runs it.

**Version gating lives with the snippets.** `DataLakeCatalog` from 25.3 (`Iceberg` before),
`icebergS3` from 24.9 (`iceberg` before). A version that does not parse — `head` — counts as
new, not old.

### Phases

1. **Interaction primitives** — done. `exec`, `cp`, `restart`, `query --file` and
   `--setting`, `up --query-setting`, server error codes in output and JSON, `env --json`.
2. **Object storage** — done. The service framework, `s3` with buckets and scoped
   identities, the `chlab s3` commands, named collections, `query --events`.
3. **Iceberg** — done. `iceberg-rest`, `ice-rest`, the tools image, `chlab py` and
   `chlab tools`, version-gated SQL for catalog and catalog-less use.
4. **Reproduction files:** `chlab repro run`, with `--compare`.
5. **On demand:** `spark`, toxiproxy, Azurite, cluster-function setups for `s3Cluster` /
   `icebergS3Cluster`.

## Possible next steps

Not built, in rough order of usefulness: TLS, S3/MinIO-backed disks, cluster-wide `ON CLUSTER`
DDL as an option, importing real sample datasets, `clickhouse-keeper` as a separate binary
source, RPM support, and comparing query results across two *instances* rather than across
nodes of one.

Known limits, stated rather than hidden: port allocation probes and then binds, so two `chlab
up` runs started in the same instant can still pick the same block; `--setting` writes into
`config.d` and is not validated against the server's actual setting names, only against what
can be a valid XML element; and `resolve` needs Docker for network sources, since that is where
their version is probed.
