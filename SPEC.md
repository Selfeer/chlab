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

TLS, S3/MinIO, Kafka, LDAP/Kerberos, RPM packages, ZooKeeper (Keeper only), sanitizer
detection, cross-architecture execution. All additive later; none change the core shape.

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

`--clickhouse-version` overrides detection entirely, as in `regression.py`.

---

## Runtime

**Deviation from the regression repo.** `helpers/cluster.py:1676-1703` runs `docker build` per
package to bake the binary into a base image. Because `clickhouse-common-static` is a static
binary, that build is unnecessary: chlab builds **one** base image (Ubuntu + `clickhouse` user +
standard directories + entrypoint), then bind-mounts the resolved binary read-only at
`/usr/bin/clickhouse`. Every version reuses the same image, and startup drops from minutes to
seconds.

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

## Possible next steps

Not built, in rough order of usefulness: TLS, S3/MinIO-backed disks, cluster-wide `ON CLUSTER`
DDL as an option, importing real sample datasets, `clickhouse-keeper` as a separate binary
source, RPM support, and comparing query results across two *instances* rather than across
nodes of one.
