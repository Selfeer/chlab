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
chlab s3          buckets and objects of the s3 service
chlab py          run a Python script (pyiceberg, pyarrow, boto3) wired to an instance
chlab tools       run any command (jq, mc, curl) in the same tools container
chlab repro       run a reproduction directory on one build, or compare two
chlab spark       Spark SQL against the instance's Iceberg catalogs
chlab proxy       latency, bandwidth limits and outages between ClickHouse and S3
chlab profiles    saved topologies
chlab resolve     resolve a source without launching
chlab cache       inspect or clear the artifact cache
```

### `chlab up`

| Option | Meaning |
|---|---|
| `-c`, `--clickhouse` | Image, URL, package or binary path. Required unless the profile was saved `--with-build` |
| `-n`, `--name` | Instance name. Derived from the version if omitted |
| `-s`, `--shards` | Number of shards (default 1) |
| `-r`, `--replicas` | Replicas per shard (default 1) |
| `-k`, `--keeper` | Keeper nodes. Defaults to 1 when replicating, 0 otherwise |
| `-u`, `--user` | `name[:password][:role]`. Repeatable |
| `--grant` | `user:PRIVILEGES ON target`. Repeatable |
| `-d`, `--database` | `name[:Atomic\|Replicated]`. Repeatable |
| `--setting` | Server setting (`config.d`) as `key=value`. Repeatable |
| `--query-setting` | Query setting for every session (default profile), e.g. `allow_insert_into_iceberg=1`. Repeatable |
| `-w`, `--with` | Companion service: `s3`, `iceberg-rest`, `ice-rest`, `spark`, `toxiproxy`. Repeatable |
| `--bucket` | Bucket to create in `s3` (default `warehouse`). Repeatable |
| `--s3-user` | Extra S3 identity, `name:secret[:readwrite\|readonly\|writeonly[:bucket,…]]`. Repeatable |
| `--seed` | Create the `demo` database with generated rows |
| `--cluster-name` | Name of the generated cluster (default `default_cluster`) |
| `-p`, `--profile` | Start from a saved profile; `-p` alone lists them and asks which |
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

`--events REGEX` also reports the query's `ProfileEvents`, summed over every node that took
part — the initial query and the pieces `s3Cluster` or a `Distributed` table sent elsewhere:

```bash
chlab query lab "SELECT sum(x) FROM s3(s3, filename = 'data/*.parquet')" -e 'S3GetObject$|ReadBufferFromS3Bytes'
```

### Looking inside

```bash
chlab exec lab -- ls /var/lib/clickhouse/user_files     # first node; exits with the command's code
chlab exec lab --node keeper1 -- cat /etc/clickhouse-keeper/keeper_config.xml
chlab cp ./data.parquet lab:/var/lib/clickhouse/user_files/
chlab cp lab/clickhouse2:/var/lib/clickhouse/user_files/t/metadata ./metadata
chlab restart lab --node clickhouse2                    # e.g. does a setting survive reopen?
chlab env lab --json                                    # ports, containers, network, users
```

## Object storage

`--with s3` starts [RustFS](https://github.com/rustfs/rustfs) next to the cluster — the same
S3 server clickhouse-regression's Iceberg suite uses — and wires every node to it.

```bash
chlab up -c <source> --name lake --with s3 \
  --bucket warehouse --bucket other \
  --s3-user reader:readerpass:readonly:warehouse

chlab query lake "INSERT INTO FUNCTION s3(s3, filename = 'data/t.parquet') SELECT number FROM numbers(1000)"
chlab query lake "SELECT count() FROM s3(s3, filename = 'data/*.parquet')"
chlab s3 ls lake warehouse -r
```

- Inside the network the server is `http://s3:9000`; from the host, see `chlab env lake`.
  Root credentials are `admin` / `password`, as in clickhouse-regression.
- Every node has named collections: `s3` for the first bucket and `s3_<bucket>` for each, so
  `s3(s3_other, filename = 'x.csv')` reads the `other` bucket.
- `--s3-user` identities get a policy scoped to their buckets and access level, so you can test
  what ClickHouse does when storage refuses a request.
- chlab's user roles grant nothing on S3. Add it explicitly:
  `--grant "dev:NAMED COLLECTION ON s3" --grant "dev:READ, WRITE ON S3"`. Older releases spell
  the second one `S3 ON *.*`.

`chlab s3` runs `mc` in a throwaway container on the instance's network; nothing is installed
on the host. Paths are `BUCKET/KEY`.

```bash
chlab s3 put lake ./a.parquet ./tables/ warehouse/in/     # files and directories
chlab s3 get lake warehouse/in/ ./out                     # an object or a whole prefix
chlab s3 cat lake warehouse/t/metadata/v3.metadata.json
chlab s3 rm  lake warehouse/in -r
chlab s3 mb  lake scratch
chlab s3 ls  lake other --as reader                       # act as an --s3-user identity
chlab s3 mc  lake -- stat s3/warehouse/in/a.parquet       # anything else mc can do
```

## Iceberg

Three ways ClickHouse meets Iceberg, all available locally:

| | Start with | ClickHouse side |
|---|---|---|
| no catalog, on S3 | `--with s3` | `icebergS3(s3, filename = 'path/to/table')`, `ENGINE = IcebergS3(...)` |
| no catalog, local | nothing | `ENGINE = IcebergLocal('/var/lib/clickhouse/user_files/t/', 'Parquet')` |
| REST catalog | `--with s3 --with iceberg-rest` | `DataLakeCatalog` database |
| ice-rest-catalog | `--with s3 --with ice-rest` | `DataLakeCatalog` database, bearer token |

`iceberg-rest` is Apache's reference catalog (`apache/iceberg-rest-fixture:1.9.1`, format v3
capable); `ice-rest` is Altinity's (`altinity/ice-rest-catalog:0.16.0`). Both match
clickhouse-regression. `chlab env` prints the `CREATE DATABASE` for each catalog in the syntax
the instance's version needs (`Iceberg` before 25.3, `DataLakeCatalog` after):

```bash
chlab up -c <source> --name lake --with s3 --with iceberg-rest
chlab env lake                         # endpoints and ready-to-run SQL for every service
```

### Writing tables: `chlab py`

Tables are written the way users write them — through a catalog, with pyiceberg — in a tools
container that already knows the instance: catalogs, S3 and the ClickHouse nodes are
configured through its environment, and the `chlab_tools` module gives shortcuts. The current
directory is the script's working directory, so it can read local files and leave results.

```python
# make_table.py
import pyarrow as pa
from chlab_tools import catalog, BUCKET, rows

cat = catalog()  # the first catalog; catalog("ice") for ice-rest
cat.create_namespace_if_not_exists("demo")
table = cat.create_table(
    "demo.events", schema=pa.schema([("id", pa.int64())]), location=f"s3://{BUCKET}/events"
)
table.append(pa.table({"id": pa.array(range(100), pa.int64())}))
print(rows("SELECT count() AS c FROM icebergS3(s3, filename = 'events')"))
```

```bash
chlab py lake make_table.py [args...]   # or: chlab py lake - <<'EOF' ... EOF
chlab tools lake -- mc ls -r s3/warehouse/events/metadata
chlab tools lake -- sh -c 'mc cat s3/warehouse/events/metadata/<file>.metadata.json | jq ".\"metadata-log\""'
```

`chlab_tools` provides `catalog(name=None)`, `s3()` (a boto3 client), `query(sql, node=...,
settings=...)`, `rows(sql)`, and `BUCKET`, `BUCKETS`, `CATALOGS`, `NODES`. The image pins
clickhouse-regression's versions: pyiceberg 0.9.0, pyarrow 20.0.0, boto3 1.34.146, plus mc and
jq. It is built once, on first use.

### Spark: format v3, deletion vectors, MERGE

pyiceberg covers most tables. For the rest — format v3, deletion vectors, merge-on-read
deletes and updates, `MERGE INTO`, `rewrite_data_files` — add `--with spark`. It needs a catalog,
and names catalogs as pyiceberg does (`rest`, `ice`), so tables are shared across all three.

```bash
chlab up -c <source> --name lake --with s3 --with iceberg-rest --with spark
chlab spark lake "
  CREATE NAMESPACE IF NOT EXISTS demo;
  CREATE TABLE demo.t (id BIGINT) USING iceberg
    TBLPROPERTIES ('format-version'='3', 'write.delete.mode'='merge-on-read');
  INSERT INTO demo.t SELECT id FROM range(1000);
  DELETE FROM demo.t WHERE id % 10 = 0;"
chlab query lake "SELECT count() FROM icebergS3(s3, filename = 'demo/t')"    # 900
```

It is clickhouse-regression's image (`tabulario/spark-iceberg`, Spark 3.5.5) with the Iceberg
1.9.1 runtime mounted over its 1.8.1 jars, as the regression suite does: 1.8.1 writes v3 tables
the 1.9.1 REST catalog rejects. The jars are fetched from Maven Central once and checked
against pinned SHA-256 sums. Every `chlab spark` call starts a JVM (about 10 s), so batch
statements; `-f file.sql` and `-f -` read them from a file or stdin.

## Faults between ClickHouse and storage

`--with toxiproxy` puts [Toxiproxy](https://github.com/Shopify/toxiproxy) in front of the s3
service. Reads through the `s3_proxy` named collections (`s3_proxy_<bucket>` per bucket) go
through it; `s3` stays direct, so the same files can be read both ways and compared.

```bash
chlab up -c <source> --name slow --with s3 --with toxiproxy
chlab proxy latency slow 500 --jitter 50     # per response; --upstream for requests
chlab query slow "SELECT count() FROM s3(s3_proxy, filename = 'data/*.parquet')" -e 'S3GetObject$'
chlab proxy bandwidth slow 256               # KB/s
chlab proxy timeout slow 0                   # hang every connection
chlab proxy down slow                        # refuse connections: storage is gone
chlab proxy status slow
chlab proxy reset slow                       # no faults, proxy up
```

Latency turns a request count into time: what the 2588 audit measured as "separate GETs
dominate cold reads" is reproducible as seconds, not just a ProfileEvents number.

## Reproductions

A reproduction is a directory: `repro.yaml` describes the cluster, and numbered files are the
steps. `chlab repro run` launches a fresh instance, runs the steps in name order, removes the
instance, and writes `result.json` and a `report.md` short enough to paste under a PR.

```
examples/repro/2792-metadata-log/
  repro.yaml                    title, build, and the cluster — same values as `chlab up`
  01-create.sql                 clickhouse-client --multiquery on a node
  02-optimize.sql
  03-metadata-log.sh            sh -e; here on clickhouse1, via `# chlab: node clickhouse1`
  03-metadata-log.reference     expected output
  04-restart.chlab              chlab commands, without the instance name
  05-count.sql
  05-count.reference
```

```bash
chlab repro init repro/my-finding --with s3     # skeleton
chlab repro check repro/my-finding              # validate without running
chlab repro run repro/my-finding -c <build>
chlab repro run repro/my-finding -c <pr build> --compare <base build>
```

**Write expectations for the correct behaviour**, as a regression test would. A reproduced
bug then shows up as a failed step, and the directory becomes the regression test for the fix
unchanged. `repro run` exits 0 when every expectation held, 1 when any did not, and 2 when a
cluster could not start. With `--compare`, the report also lists every step where the two
builds behaved differently.

| Step | Runs |
|---|---|
| `.sql` | `clickhouse-client --multiquery` on the first node (or `node`), TSV output |
| `.sh` | `sh -e` in the tools container, or inside a node with `node` |
| `.py` | Python in the tools container, with pyiceberg and `chlab_tools` |
| `.chlab` | one chlab command per line, e.g. `restart`, `s3 put ./x warehouse/`, `proxy latency 500`, `cp ./x {instance}:/tmp/` |
| `.spark` | Spark SQL, with `spark` in `with:` |

Steps run with the reproduction directory as their working directory, so data files next to
them (`data.csv`, a crafted Parquet file) are at hand. Nothing a reproduction runs touches the
rest of the host: SQL, `.sh`, `.py` and `.spark` run in containers, and `.chlab` steps may name
only their own instance and read or write only files inside the directory. A build named in
`repro.yaml` is never executed on the host. The containers do have network access. Expectations:

| | |
|---|---|
| `NN-step.reference` | output must equal this file (trailing whitespace ignored) |
| `-- chlab: expect error 497` | the step must fail, with this ClickHouse error code (code optional) |
| `-- chlab: expect contains TEXT` | output must contain TEXT; repeatable |
| `-- chlab: expect empty` | no output |
| `-- chlab: expect ok` | the default: the step succeeds |

Other directives: `node clickhouse2`, `user dev:pw` and `setting key=value` (for `.sql`),
`timeout 300`. In `.sh`, `.py` and `.chlab` files they start with `#` instead of `--`.

`repro.yaml` takes `title`, `clickhouse` and `compare` (default builds; `-c` replaces the
first, `--no-compare` drops the second), and the cluster: `shards`, `replicas`, `keeper`,
`cluster_name`, `users`, `grants`, `databases`, `settings`, `query_settings`, `with`,
`buckets`, `s3_users`, `seed`. An unknown key is an error, so a typo cannot silently launch the
wrong cluster.

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

**Save a setup and launch it again**

```bash
chlab profiles save 2x2-seeded repl                 # the setup, without the build
chlab up -c <any other source> --profile 2x2-seeded

chlab profiles save lake-2x2 lake --with-build      # the setup and its build
chlab up --profile lake-2x2                         # no -c needed; -c still overrides it
chlab up -p                                         # pick from the list of profiles
chlab profiles ls                                   # name, build, topology
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
# loopback only: the API is unauthenticated. From another machine: ssh -L 7777:127.0.0.1:7777 host
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
