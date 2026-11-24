---
name: pr-issue-repro
description: Reproduce a finding posted on an Altinity/ClickHouse pull request (an audit issue, a review comment), or check a developer's claim that it is fixed, by running the PR's CI build locally with chlab. Use when the user links a PR comment and asks "is this fixed", "reproduce this issue", "verify the fix", or "check this audit finding". Covers choosing and downloading the right build, writing a chlab reproduction, before/after comparisons, faults chlab cannot inject, and the report.
---

# PR issue reproduction with chlab

Turn a finding on a PR into a runnable check, and run it against the PR's CI build. The
answer is one of the following:

- **Fixed:** the check passes on the current head, and ideally fails on the head the
  finding was written against.
- **Not fixed:** the check fails on the current head.
- **Reproduced:** for a new finding, the bug shows up on the current head.
- **Not reproducible as written:** say why, and what would be needed.

chlab usage: <https://github.com/Selfeer/chlab/wiki/Usage-Guide>, section 12
"Reproductions". Read it once. This skill covers what the wiki does not: which build to
use, and the mistakes that cost time in practice.

**Do not post anything to GitHub unless the user asks.** Report in chat, or in a file
when asked.

---

## Step 1: Read the finding, the reply and the code

```bash
gh api repos/Altinity/ClickHouse/issues/comments/<COMMENT_ID> --jq '"\(.user.login) \(.created_at)\n\(.body)"'
gh pr view <PR> --repo Altinity/ClickHouse --json headRefOid,baseRefName,commits \
  --jq '{headRefOid,baseRefName,commits:[.commits[]|"\(.oid[0:10]) \(.committedDate) \(.messageHeadline)"]}'
gh api repos/Altinity/ClickHouse/issues/<PR>/comments --paginate \
  --jq '.[]|select(.created_at > "<finding date>")|"\(.user.login) \(.created_at)\n\(.body[0:1500])"'
gh pr diff <PR> --repo Altinity/ClickHouse > /tmp/pr<PR>.diff
gh api "repos/Altinity/ClickHouse/contents/<path>?ref=<head sha>" --jq .content | base64 -d > /tmp/file.cpp
```

Write down the following:

- **Review head:** the SHA the finding was written against. Audits name it.
- **Current head:** the SHA the "fixed" claim refers to.
- **Fix commits:** the commits in between, usually named after the finding.
- **Mechanism:** read the code at the current head before writing any SQL. A static read
  often shows that the fix restructured the code, so the trigger in the finding no
  longer applies as written, or that part of the fix direction was skipped. For
  example, the fix covers one call site and not its sibling. Report skipped parts
  separately, as static findings.
- **Test hooks:** the PR's new failpoints (`src/Common/FailPoint.cpp`) and tests. The
  PR's own integration test is usually the best template for the reproduction.

## Step 2: Pick the builds

### Use the release `.deb`, not the `amd_binary`

| Artifact | Size | Use |
|---|---|---|
| `build_amd_release/clickhouse-common-static_<version>_amd64.deb` | ~240 MB | **default** |
| `build_amd_binary/clickhouse` | ~1.35 GB (debug info) | only when the release build left no package |

Downloads from `altinity-build-artifacts` ran at about 1.4 MB/s. Resolving the `.deb`
took 3 minutes and the `amd_binary` took 19–30 minutes, for the same code (same
`GIT_HASH`). Failpoints work in the release build too: `SYSTEM ENABLE FAILPOINT` works,
and an unknown name is rejected with `BAD_ARGUMENTS`.

List what a commit's CI produced. The pattern is in `.claude/skills/_shared/ci-urls.md`:

```bash
PR=<PR>; SHA=<full sha>
for b in build_amd_release build_amd_binary; do
  curl -s "https://altinity-build-artifacts.s3.amazonaws.com/?list-type=2&prefix=PRs/$PR/$SHA/$b/" \
  | python3 -c "
import sys,re
for k,s in re.findall(r'<Key>([^<]+)</Key>.*?<Size>(\d+)</Size>', sys.stdin.read()):
    n = k.rsplit('/', 1)[1]
    if n.startswith('clickhouse-common-static_') or n == 'clickhouse':
        print('%-18s %5d MB  https://altinity-build-artifacts.s3.amazonaws.com/%s' % ('$b', int(s)/2**20, k))"
done
```

If `build_amd_release` has only `job.log`, the release build failed, for example on an
infra timeout. Then the `amd_binary` is the only package for that SHA. Say so in the
report: it is the same code, packaged differently.

### Before and after

- **After:** the current head.
- **Before:** the review head, so the same check can show "fails before, passes after".
  This is the strongest result. Check that the review head's build exists.
- **Before is impossible** when the reproduction depends on failpoints added by the fix.
  The old build rejects `SYSTEM ENABLE FAILPOINT <new name>`. Then add a control step
  that proves the check would catch the bug (see Step 4), and state the limit in the
  report: "this shows the fix works, not that the bug existed".

### Start downloads first

Start the downloads before writing any step, in the background, one `chlab resolve` per
build:

```bash
chlab resolve <url>        # background it; it caches under ~/.cache/chlab
```

### Confirm which code you downloaded

PR CI builds the PR head merged into the base branch:

```bash
<cached binary> local -q "SELECT value FROM system.build_options WHERE name IN ('VERSION_FULL','GIT_HASH')"
gh api repos/Altinity/ClickHouse/commits/<GIT_HASH> --jq '.commit.message' | head -1
# Expect: "Merge <PR head sha> into <base sha>"
```

If `chlab repro run` fails with "could not determine the ClickHouse version", the binary
is a self-extracting raw binary that has not unpacked itself yet. Run it once on the
host, as above, and retry.

## Step 3: Write the reproduction

Keep it somewhere persistent, not in `/tmp`, for example
`~/chlab-repro/<PR>-<short-slug>/`. Start from the chlab scaffold:

```bash
chlab repro init ~/chlab-repro/<PR>-<slug> --title "..." --with s3
```

### `repro.yaml`

```yaml
title: "PR 2729: a DataLakeCatalog table read by a secondary query returns its rows"   # quote it: titles contain ':'
clickhouse: <current head .deb URL>
compare: <review head .deb URL>          # omit when "before" is impossible
shards: 2
replicas: 1
with: [s3, iceberg-rest]
query_settings:
  allow_experimental_database_iceberg: 1
```

### Rules for the steps

- **Expectations describe correct behaviour.** A reproduced bug is a failed step. The
  same directory then becomes the regression check for the fix.
- **One question per step**, so the report table reads as a list of facts.
- **Every `.py` file in the directory is a step.** Put helper modules in a subdirectory
  (`lib/delta.py`) and add `sys.path.insert(0, "lib")` at the top of each step that
  imports them.
- **Tag queries with `log_comment`** (`-- chlab: setting log_comment=<tag>`). Later steps
  can then find them in `system.query_log`, including the secondary queries on other
  nodes: use `NOT is_initial_query` there.
- **Prove the path, not only the result.** Add a step that shows the code path in
  question ran:
  - `ProfileEvents` from `system.query_log`;
  - a line from the server log;
  - the scanned file count compared with a control query.

  A correct result alone can come from a different path.
- **Add a control step** that shows the check would catch the bug. For example, show
  that the newer table version really exists, or that a new connection really succeeds
  under the same fault.

### Concurrency inside a step

A `.sql` step blocks, so put "pause a query, change something, release it" in one `.py`
step:

```python
import threading
from chlab_tools import query
query("SYSTEM ENABLE FAILPOINT <pauseable>")
t = threading.Thread(target=lambda: result.update(rows=query("SELECT ...", settings={"log_comment": "x"})))
result = {}; t.start()
query("SYSTEM WAIT FAILPOINT <pauseable> PAUSE")
# ... commit a new version, change data ...
query("SYSTEM NOTIFY FAILPOINT <pauseable>")
t.join(120)
```

Always disable failpoints in a `finally:` block.

### Data the companion services do not create

- **Iceberg:** use pyiceberg in a `.py` step (`chlab_tools.catalog()`), or `.spark` steps.
- **Delta Lake:** chlab has no Delta support. Write a minimal table by hand: Parquet files
  from pyarrow, plus `_delta_log/<version:020d>.json` with `protocol`, `metaData`, `add`
  and, if needed, `domainMetadata` actions. Upload with `chlab_tools.s3()`. A
  liquid-clustered table needs writer features `["domainMetadata", "clustering"]` and a
  `delta.clustering` domain metadata entry. Read it with
  `ENGINE = DeltaLake(s3, filename = '<prefix>/')`.
- **Extra clusters:** for example remote-only clusters, so the initiator is not a
  shard. Write a `remote_servers` file and copy it into `clickhouse1`'s
  `/etc/clickhouse-server/config.d/`. It is picked up without a restart; check
  `system.clusters` a few seconds later.

### Run

```bash
chlab repro check <dir>                  # validate without launching anything
chlab repro run <dir> --keep             # keep the instances to inspect failures
chlab repro run <dir>                    # final clean run; its report.md is the evidence
```

Read `results/<timestamp>/result.json`: each step has `step`, `exit_code`,
`error_code`, `output`, `error_output` and `diff`. Before calling a failure a reproduced
bug, read its `error_output`. Check that it is the finding's error and not a mistake in
your own step.

## Step 4: Gotchas that cost time

| Symptom | Cause and fix |
|---|---|
| `Invalid qualified name: demo.ns.t (SYNTAX_ERROR)` | ``remote('host', db.`ns.t`)`` does not parse. Use `remote('host', 'db', 'ns.t')`. Findings sometimes quote the broken form. |
| `Broken pipe` at exactly 60 s | `chlab query` (HTTP) gives up after 60 s. For long queries run `chlab exec <inst> -- clickhouse client -q "..."`. |
| `clickhouse-client: executable file not found` | chlab mounts the raw binary. Use `clickhouse client`. |
| `Unknown table ... system.text_log` | chlab does not enable `text_log`, and the server logs at `information`, so trace lines are missing. Use `ProfileEvents` and `system.query_log`, or grep `/var/log/clickhouse-server/clickhouse-server.log` in a `.sh` step with `# chlab: node clickhouse1`. |
| A loop over node names runs once with all the names | zsh does not split `$var` into words. Run such loops with `bash -c '...'`. |
| `zsh: ====== not found` | zsh treats a word starting with `=` as a command lookup. Quote dividers. |
| A stopped node still runs queries | PID 1 is the watchdog (`ClickHouseWatch`). Stop the `clickhouse` process instead: ``kill -STOP $(ps -eo pid,comm \| awk '$2=="clickhouse"{print $1}')``. Send `-CONT` before cleanup. |
| A failpoint "reproduction" passes on the buggy build | Check what the failpoint simulates. A failpoint that throws immediately cannot open a timing window: PR 2912's `pooled_connection_ping_timeout` could not reproduce a race that needed a blocking `Ping`. |

## Step 5: Faults chlab cannot inject

chlab steps run only inside the instance's containers, without `NET_ADMIN`. The nodes
have no `iptables`, and `.chlab` steps cannot run `docker`. For network faults, run a
sidecar in a node's network namespace from a host script:

```bash
printf 'FROM alpine:3.20\nRUN apk add --no-cache iptables iproute2\n' | docker build -q -t repro/netfault:1 -
net() { docker run --rm --net container:chlab-<inst>-clickhouse1 --cap-add NET_ADMIN repro/netfault:1 sh -c "$1"; }
net "ss -Htn state established '( dport = :9000 )'"                          # pooled sockets and their ports
net "iptables -A OUTPUT -p tcp -d <peer ip> --dport 9000 --sport <port> -j DROP"   # kill one existing flow only
net "iptables -F OUTPUT"                                                     # always clean up
```

Dropping by source port kills chosen existing connections while new connections still
work. That is the "old flow dead, server up" case. To map a connection to a query, read
`port` from `system.processes` on the remote node, where it is the initiator's port.

Use a scratch instance (`chlab up -c <url> -n <name> ...`) for this, and keep the
script next to the reproduction. Say in the report that this part is a script, not a
chlab reproduction.

## Step 6: Verify claims about CI before repeating them

If the report says CI did or did not test something, check it in the job logs. Do not
take it from an earlier triage comment. PR 2729 shows why:

- A triage comment said the PR's test "never ran". In fact it ran and passed in a shard
  that had `CLICKHOUSE_USE_OLD_ANALYZER=1`. That sets `allow_experimental_analyzer = 0`
  through `tests/integration/helpers/0_common_enable_old_analyzer.xml`, so the fixed code
  was never reached.
- The job that would have used the new analyzer lost its runner.

Check it like this:

```bash
gh api "repos/Altinity/ClickHouse/commits/<sha>/check-runs?per_page=100" --paginate \
  --jq '.check_runs[]|select(.name|test("Integration"))|"\(.name)\t\(.conclusion)\t\(.html_url)"'
curl -s <artifact base>/<job dir>/job.log | grep -E '(PASSED|FAILED|ERROR) <test file>::<test name>'
gh api repos/Altinity/ClickHouse/check-runs/<job id>/annotations --jq '.[].message'   # "runner lost ..."
```

Link the exact job, and the log line number.

## Step 7: Report

Lead with the verdict in one sentence, then give:

1. **Builds:** commit, version, the artifact used, and `GIT_HASH` showing it is the merge
   commit.
2. **What the check does:** the trigger, in a few lines.
3. **Results:** a table with one row per step and columns for before and after. Copy
   values and errors exactly from `result.json`.
4. **Why it works now**, or why it does not, tied to the code.
5. **Still open:** the parts of the fix direction that were not done, and static-only
   findings. Mark each as static.
6. **Limits:** no before run possible, script instead of chlab, a trigger in the finding
   that did not match, an impact smaller or larger than stated (for example, a failure
   only with `connections_with_failover_max_tries = 1`).
7. **Where the reproduction is**, and the command to rerun it.

When the user asks for a file, the format that worked is a short header, then one `sql`
block. Each statement is followed by its results as comments, `-- Fixed:` and
`-- Before:`, copied exactly from the run.

## Step 8: Clean up

```bash
chlab rm <instance> -y                 # every instance left by --keep or scratch work
docker rmi repro/netfault:1            # if a sidecar image was built
chlab ls                               # nothing of yours left
```

Leave the reproduction directory in place. It is the regression check for the fix.
