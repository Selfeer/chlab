# chlab

Run any ClickHouse build, immediately.

Point chlab at a Docker image, a CI build-artifact URL, a `.deb`, or a local binary, and it
gives you a running, configured cluster you can query. Built for manual testing and SQL
exploration across ClickHouse distributions — Altinity, upstream, Antalya, your own build.

```bash
chlab up -c docker://altinityinfra/clickhouse-server:1984-26.3.12.20001.altinityantalya
chlab up -c https://altinity-build-artifacts.s3.amazonaws.com/PRs/2074/.../clickhouse-common-static_26.3.17.20001.altinityantalya_amd64.deb
chlab up -c ./build/programs/clickhouse
```

Each cluster is a **named instance** with its own ports, so several ClickHouse versions run
side by side and you can compare their behaviour directly.

## Install

Requires Python 3.12+, Docker, and Docker Compose v2.

```bash
git clone https://github.com/Selfeer/chlab.git
cd chlab
pipx install -e .
```

`chlab` is now on your PATH from any directory. `-e` points it at the source, so edits take
effect on the next run.

<details>
<summary>Without pipx</summary>

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/chlab --help          # or: source .venv/bin/activate
```
</details>

Optional: `pipx install -e ".[zstd]"` if you hit `.deb` packages using zstd compression.
Without it, chlab falls back to the `zstd` CLI when present.

Resolving a source from the network also pulls `ubuntu:24.04` once, to read the binary's
version inside a container rather than executing a freshly downloaded file on your host.

## Quick start

```bash
# a 2-shard, 2-replica cluster with Keeper and 100k rows of demo data
chlab up -c docker://clickhouse/clickhouse-server:24.8 --shards 2 --replicas 2 --seed

chlab ls                                          # what's running
chlab query 24-8 "SELECT count() FROM demo.events"
chlab ui                                          # web UI at localhost:7777
chlab rm 24-8 -y                                  # tear it down
```

Omitting `--name` derives one from the source — that cluster lands as `24-8`. A tag like
`1984-26.3.12.20001.altinityantalya` becomes `ch-26-3-12-20001`. Pass `--name` when you want
something you'll remember.

## Sources

| You pass | What runs |
|---|---|
| `docker://image:tag` | that image, unmodified, with only generated configs mounted in |
| `https://….deb` or `.tgz` | downloaded, unpacked, binary bind-mounted into a shared base image |
| `https://…/clickhouse` | downloaded raw binary, same |
| `/path/to/clickhouse` | used in place — tracks a live build tree |
| `/path/to/pkg.deb` | unpacked, same |

`url://` is an alias for `https://`, `file://` for a local path.

Resolved artifacts are cached under `~/.cache/chlab`, so re-running a source is instant. The
version is detected by asking the binary itself, not by parsing a tag.

```bash
chlab resolve <source>          # just resolve and print the path + version
chlab cache ls                  # everything resolved so far
```

## Commands

```
chlab up          launch a cluster
chlab ls          list instances
chlab status      one instance in detail
chlab query       run SQL, one statement or a file of them
chlab client      interactive clickhouse-client
chlab exec        run a command inside a node
chlab cp          copy files into or out of a node
chlab env         endpoints, containers and credentials
chlab logs        server logs
chlab stop        stop containers, keep data
chlab start       start a stopped instance
chlab restart     restart nodes and wait until they are ready
chlab down        remove containers, keep data and definition
chlab rm          delete everything, including data volumes
chlab ui          web interface
chlab profiles    saved topologies
chlab resolve     resolve a source without launching
chlab cache       inspect or clear the artifact cache
```

### `chlab up`

| Option | Meaning |
|---|---|
| `-c`, `--clickhouse` | **Required.** Image, URL, package or binary path |
| `-n`, `--name` | Instance name. Derived from the version if omitted |
| `-s`, `--shards` | Number of shards (default 1) |
| `-r`, `--replicas` | Replicas per shard (default 1) |
| `-k`, `--keeper` | Keeper nodes. Defaults to 1 when replicating, 0 otherwise |
| `-u`, `--user` | `name[:password][:role]`. Repeatable |
| `--grant` | `user:PRIVILEGES ON target`. Repeatable |
| `-d`, `--database` | `name[:Atomic\|Replicated]`. Repeatable |
| `--setting` | Server setting (`config.d`) as `key=value`. Repeatable |
| `--query-setting` | Query setting for every session (default profile), e.g. `allow_insert_into_iceberg=1`. Repeatable |
| `--seed` | Create the `demo` database with generated rows |
| `--cluster-name` | Name of the generated cluster (default `default_cluster`) |
| `-p`, `--profile` | Start from a saved topology |
| `--clickhouse-version` | Skip version detection |
| `--force-resolve` | Re-download, ignoring the cache |
| `--sha256` | Verify the download against a checksum |
| `--recreate` | Relaunch an existing instance in place, keeping its ports and data |

Nodes are named `clickhouse1…N`, filling shard by shard: with `--shards 2 --replicas 2`,
`clickhouse1`/`clickhouse2` are the replicas of shard 1. Keeper nodes are `keeper1…N` and
reuse the same binary — no second download.

**Roles** for `--user` are `none`, `readonly`, `readwrite` (default) or `admin`. Users are
created as SQL objects after start, so grants behave normally.

### `chlab query`

```bash
chlab query lab "SELECT version()"                      # first node
chlab query lab "SELECT 1" --node clickhouse3           # a specific node
chlab query lab --all "SELECT count() FROM demo.events" # every node, with agreement check
chlab query lab "SELECT 1" -u dev --password devpass    # as another user
chlab query lab "SELECT 1" --json                       # machine-readable
chlab query lab "SELECT 1" -s max_threads=1             # query settings, repeatable
chlab query lab -f steps.sql                            # several statements
```

`--all` runs the same SQL on every node and reports whether they agree — the quickest way to
check replication or spot a node behaving differently.

A failing query exits 1. With `--json` the failure is part of the output, as
`{"node", "error", "code"}`, where `code` is ClickHouse's own error code (`497` for
`ACCESS_DENIED`). `--file` (or `-f -` for stdin) runs the statements through
`clickhouse-client --multiquery` inside the node, stops at the first error, and exits with the
client's code.

### Looking inside

```bash
chlab exec lab -- ls /var/lib/clickhouse/user_files     # first node; exits with the command's code
chlab exec lab --node keeper1 -- cat /etc/clickhouse-keeper/keeper_config.xml
chlab cp ./data.parquet lab:/var/lib/clickhouse/user_files/
chlab cp lab/clickhouse2:/var/lib/clickhouse/user_files/t/metadata ./metadata
chlab restart lab --node clickhouse2                    # e.g. does a setting survive reopen?
chlab env lab --json                                    # ports, containers, network, users
```

## Examples

**Compare two versions on the same query**

```bash
chlab up -c docker://clickhouse/clickhouse-server:24.8 --name v24 --seed
chlab up -c docker://clickhouse/clickhouse-server:25.3 --name v25 --seed

chlab query v24 "SELECT quantileTDigest(0.9)(value) FROM demo.events"
chlab query v25 "SELECT quantileTDigest(0.9)(value) FROM demo.events"
```

**Test RBAC**

```bash
chlab up -c docker://clickhouse/clickhouse-server:24.8 --name rbac \
  -u "reader:pw:readonly" \
  -u "boss:pw:admin" \
  --grant "reader:SELECT ON demo.*" \
  --seed

chlab query rbac "CREATE DATABASE nope" -u reader --password pw   # denied
chlab query rbac "CREATE DATABASE yep"  -u boss   --password pw   # allowed
```

**Check replication across a cluster**

```bash
chlab up -c <source> --name repl --shards 2 --replicas 2 --seed
chlab query repl --all "SELECT getMacro('shard'), count() FROM demo.events_local"
chlab query repl "SELECT table, total_replicas, active_replicas FROM system.replicas"
```

**Reuse a topology against a different build**

```bash
chlab profiles save 2x2-seeded repl
chlab up -c <any other source> --profile 2x2-seeded
```

**Tune server settings**

```bash
chlab up -c <source> --name tuned \
  --setting max_server_memory_usage=4000000000 \
  --setting max_concurrent_queries=200
```

## Where things live

| Path | Contents | Override |
|---|---|---|
| `~/.local/state/chlab` | instances: spec, generated configs, compose file | `CHLAB_STATE_DIR` |
| `~/.cache/chlab` | downloaded artifacts and extracted binaries | `CHLAB_CACHE_DIR` |
| `~/.config/chlab` | saved profiles | `CHLAB_CONFIG_DIR` |

The overrides are useful for throwaway labs and for running several independently:
`CHLAB_STATE_DIR=/tmp/lab chlab up -c …`.

`spec.json` and `profiles.json` record the passwords you pass to `--user`, so both are written
`0600` inside a `0700` directory. Inside the server the passwords are stored hashed
(`sha256_password`), not in cleartext.

## Demo data

`--seed` creates, on every node:

- `demo.events_local` — `MergeTree`, or `ReplicatedMergeTree` when `--replicas > 1`
- `demo.events` — `Distributed` over the cluster

100,000 generated rows are split into disjoint slices across shards, so the distributed table
sees each row exactly once. Columns: `event_time`, `user_id`, `event_type`, `country`, `value`.

## Web UI

```bash
chlab ui                    # localhost:7777, opens a browser
chlab ui --port 8080 --no-open
```

A setup form for topology, users, databases and seed data; a dashboard of running instances
with links to each node's built-in `/play`; and a SQL editor that can target one node or
fan out across all of them and diff the results.

## How it works

`clickhouse-common-static` is a static binary, so chlab builds **one** base image and
bind-mounts the resolved binary into it — no docker build per version, and startup takes
seconds. Docker-image sources are exempt: they run as themselves, with generated configs
mounted in file by file so the image's own `config.d` survives.

Data lives in per-node Docker volumes: `stop`/`start` preserves it, `rm` discards it. Ports
bind to loopback only and stay stable for the life of a named instance.

See [SPEC.md](SPEC.md) for the full design and the decisions behind it.

## Tests

```bash
.venv/bin/python -m pytest              # unit tests: no network, no Docker
.venv/bin/python -m pytest -m docker --run-docker   # end-to-end, launches a real cluster
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

The unit suite builds real `.deb` and `.tgz` archives in a tmpdir, serves them from a local HTTP
server, and drives the web app against a stubbed runtime. The Docker-gated smoke test is the
only one that exercises the generated `config.xml` against an actual server.

## Known issues

- The web UI's layout and dark mode have never been visually reviewed
  ([#1](https://github.com/Selfeer/chlab/issues/1)).
- `chlab client`'s interactive handoff is untested
  ([#3](https://github.com/Selfeer/chlab/issues/3)).
- RPM packages are rejected; only `.deb`, `.tgz`, raw binaries and Docker images work.
- Port allocation probes then binds, so two launches started in the same instant can collide.

Full list: [open issues](https://github.com/Selfeer/chlab/issues).

## License

Apache 2.0 — see [LICENSE](LICENSE).
