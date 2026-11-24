# Altinity/ClickHouse CI build artifacts

Where a PR's CI builds end up, and how to fetch them. Two layouts, depending on where the PR
branch lives.

## Which layout a PR uses

```bash
gh pr view <PR> --repo Altinity/ClickHouse --json headRepositoryOwner,isCrossRepository \
  --jq '"\(.headRepositoryOwner.login) cross_repo=\(.isCrossRepository)"'
```

- `cross_repo=false` (branch in Altinity/ClickHouse): builds go to **S3**.
- `cross_repo=true` (a fork, workflow "Community PR"): CI has no S3 credentials, and the build job
  log shows an empty `S3_ARTIFACT_BUCKET:`. Builds are **GitHub Actions artifacts**. Nothing
  is listed under `PRs/<PR>/` on S3.

## S3: PRs from Altinity branches

Layout: `https://altinity-build-artifacts.s3.amazonaws.com/PRs/<PR>/<full sha>/<job dir>/<file>`.
List what a commit's CI produced:

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

Pass a URL straight to `chlab resolve`, `chlab up -c` or `clickhouse:` in `repro.yaml`.

## GitHub Actions artifacts: PRs from forks

Find the run of the PR head, then list its artifacts:

```bash
SHA=<full sha>
gh api "repos/Altinity/ClickHouse/commits/$SHA/check-runs?per_page=100" --paginate \
  --jq '.check_runs[]|select(.name=="Build (amd_release)" and .conclusion=="success")|.details_url'
# .../actions/runs/<RUN_ID>/job/<JOB_ID>
gh api repos/Altinity/ClickHouse/actions/runs/<RUN_ID>/artifacts --paginate \
  --jq '.artifacts[]|"\(.name)\t\(.size_in_bytes/1048576|floor) MB\texpired=\(.expired)"'
```

| Artifact | Size | Contents |
|---|---|---|
| `CH_AMD_RELEASE_STRIPPED` | ~190 MB | `clickhouse-stripped`, the release binary without debug info: **default** |
| `CH_AMD_RELEASE` | ~250 MB | release binary |
| `DEB_AMD_RELEASE` | ~590 MB | every `.deb` of the release build, zipped |
| `CH_AMD_BINARY` | ~1.3 GB | `amd_binary`, with debug info |

Downloads ran at about 1 MB/s (190 MB in 3 minutes). Download into a directory of its own, not
`/tmp`, and rename the binary: chlab takes a path to a binary.

```bash
mkdir -p ~/chlab-builds/pr<PR>-<sha10> && cd ~/chlab-builds/pr<PR>-<sha10>
gh run download <RUN_ID> --repo Altinity/ClickHouse -n CH_AMD_RELEASE_STRIPPED -D .
mv clickhouse-stripped clickhouse
./clickhouse local -q "SELECT value FROM system.build_options WHERE name = 'GIT_HASH'"   # also unpacks it
```

Then use `clickhouse: /home/<you>/chlab-builds/pr<PR>-<sha10>/clickhouse` in `repro.yaml`.
GitHub artifacts expire: about 90 days after the run, as seen on PR 2832 (`expires_at` in the
artifacts listing). Check `expired` before planning a "before" run against an old head.

## Job logs

```bash
gh api repos/Altinity/ClickHouse/actions/jobs/<JOB_ID>/logs > job.log   # GitHub Actions log
curl -s https://altinity-build-artifacts.s3.amazonaws.com/PRs/<PR>/<sha>/<job dir>/job.log   # S3 PRs only
```
