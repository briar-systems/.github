# briar-systems/.github

Org-wide community health files, the org profile (`profile/`), and the CI toolkit the mach libraries share.

## Shared CI

The family CI contract:

- each repo has one `.github/workflows/ci.yml`, and a release workflow where one exists
- a pull request into `dev` runs the light tier, a pull request into `main` runs every tier, and `workflow_dispatch` pulls named heavy work onto any ref
- nothing runs on push, and nothing runs on a schedule. Deploy-only workflows are the exception, and so is `release.yml` on a `v*` tag push (see [Releases](#releases))
  - a `pull_request` build tests the merge result, not the branch tip, so merging a green pull request verifies the merged state as it was at that moment. That is why no push trigger is needed
  - if the base moved after the last run, that merged state was never built. `dev` accepts this: the next pull request's merge-result build includes it, so a bad merge shows up at once and costs one fix
  - `main` is deliberately not held to a stricter rule. `main` is ahead of `dev` by every past release merge, so requiring up-to-date branches would leave every release pull request permanently behind and force a back-merge step. The admin merges that cut releases bypass such a check anyway
  - what protects `main` instead: it moves only through release pull requests and hotfixes, and a release pull request runs every tier on its merge result. Before opening one, dispatch the light tier on `dev`'s tip (`gh workflow run CI --ref dev`)
  - the one residual case: a hotfix that lands on `main` while a release pull request is open leaves that pull request's checks built against the old `main`. Close and reopen the pull request before merging. A re-run is not enough, because it reuses the original merge commit
- a `ci.yml` that a release or cd workflow calls also declares a `workflow_call` input `heavy`, and the caller passes `all`. A tag push has no base branch, so without it the release would run only the light tier
- tiering is strict: only `x86_64-linux` is light unless a ruling below says otherwise
- every adoption pull request includes a `mach fmt .` pass, since the light tier checks formatting
- every `ci.yml` ends in a job named exactly `gate` that needs every other job. It fails when a needed job finished `failure` or `cancelled` and passes `skipped`. `gate` is the one required check

The upstream forks, compiler-explorer and infra, are outside the contract.

Per-repo light sets (ruled 2026-09-16):

| repo | light legs | caller input |
| --- | --- | --- |
| mach-std | `x86_64-linux`, `x86_64-windows`, `aarch64-darwin` | `light-legs: '["x86_64-windows", "aarch64-darwin"]'` |
| mach-lsp | `x86_64-linux`, `x86_64-windows` | `light-legs: '["x86_64-windows"]'` |
| .github | `x86_64-linux`, `aarch64-linux` | `light-legs: '["aarch64-linux"]'`, a self-test of the `light-legs` input |
| every other repo | `x86_64-linux` | none |

The toolkit:

| path | what it is |
| --- | --- |
| `.github/workflows/mach-lib.yml` | the reusable library pipeline (`on: workflow_call`) |
| `.github/workflows/mach-release.yml` | the reusable release pipeline a `v*` tag runs |
| `.github/mach-release` | the release script, with its tests |
| `.github/actions/seed-mach` | installs a published mach release after checking its archive against `SHA256SUMS`. The family pin is in `version`. Each use seeds its own directory under `$RUNNER_TEMP` and puts it first on `PATH`, so a job may seed more than once and the last seed wins |
| `.github/actions/gate` | the gate logic |
| `.github/mach-lib` | the plan and leg scripts the workflow runs, with their tests |
| `test/fixture` | the library this repo's own `ci.yml` runs the workflow against, and `release-rehearsal.yml` rehearses a release of |
| `tools/preflight` | the adopter preflight run before every release. It is not part of the caller contract |

### Why the caller owns `gate`

A job inside a called workflow reports its check as `<caller job> / <job>`. A `gate` inside `mach-lib.yml` would report as `lib / gate`, never as `gate`. It also could not need jobs the caller adds. So each `ci.yml` declares the `gate` job itself, and only the logic is shared. The gate action also reads the workflow file the gate is in and fails if any job is missing from `needs:`. Adding a job without gating it is caught on that job's first run.

### Minimal caller

```yaml
name: CI

on:
  pull_request:
  workflow_dispatch:
    inputs:
      heavy:
        description: heavy work to run on this ref
        type: choice
        default: none
        options: [none, all, aarch64-linux, x86_64-windows, aarch64-darwin, x86_64-darwin]

permissions:
  contents: read

jobs:
  lib:
    uses: briar-systems/.github/.github/workflows/mach-lib.yml@main
    with:
      heavy: ${{ inputs.heavy }}

  gate:
    if: always()
    needs: [lib]
    runs-on: ubuntu-latest
    steps:
      - uses: briar-systems/.github/.github/actions/gate@main
        with:
          needs: ${{ toJSON(needs) }}
```

With no other inputs, a pull request into `dev` runs these steps on `x86_64-linux`:

1. seed the pinned mach
2. `mach dep pull .`
3. build and test in debug and release
4. `mach fmt --check .`
5. `mach build . --all-targets --profile release`

A pull request into `main` adds native `aarch64-linux`, `x86_64-windows`, `aarch64-darwin` and `x86_64-darwin` legs.

### Caller requirements

- the caller grants `permissions: contents: read`. The gate reads its own workflow file and the seed reads mach releases through `github.token`, so `permissions: {}` breaks both
- every leg's runner provides Python 3.11 or newer as `python`, because the leg scripts use `tomllib`. The hosted images do. A custom `runs-on` has to provide it as well
- the `gate` job runs on `ubuntu-latest`, or on another runner where `python` can import PyYAML
- the leg job checks the toolkit out to `.mach-lib/` inside the workspace. The seed goes under `$RUNNER_TEMP` and never lands in the workspace

### Override surface

Every override is an input. There is nothing to fork.

| input | default | use |
| --- | --- | --- |
| `heavy` | `none` | forward the caller's dispatch choice. Takes `all`, leg names, or names the caller's own jobs and hooks read |
| `legs` | the five hosts above | JSON array that replaces the host set |
| `extra-legs` | none | JSON array appended to the host set |
| `skip-legs` | none | JSON array of leg names to drop |
| `light-legs` | none | JSON array of leg names to run in the light tier. Only the repos ruled above use it |
| `project` | `.` | the project directory, for a repo whose real project is not the root |
| `profiles` | `["debug", "release"]` | profiles to build and test. Any name works, and each leg checks that the manifests it builds declare it |
| `test` | `true` | run `mach test` on the project |
| `fmt` | `true` | `mach fmt --check` of the project and every subproject, in the light tier, once, on the primary leg |
| `all-targets` | `true` | release build of every manifest target, once, on the primary leg |
| `subprojects` | none | JSON array of other projects to pull, build and test |
| `hooks-dir` | `.github/ci` | where the repo's hooks live |
| `submodules` | `false` | the `actions/checkout` submodules mode |
| `mach-version` | the family pin | a release tag, or `latest` |
| `evidence` | none | paths uploaded as `evidence-<leg>` whatever the outcome |
| `timeout-minutes` | `40` | default leg timeout |

A **leg** is `{"name", "runs-on"}` plus these optional keys:

| key | default | meaning |
| --- | --- | --- |
| `tier` | `light` | `light` or `heavy` |
| `target` | host | passed as `--target` to build and test |
| `runner` | none | passed as `--runner` to test. Requires `target` |
| `build-args` | none | string array appended to every build on the leg, including subproject builds |
| `test-args` | none | string array appended to every test on the leg, including subproject tests |
| `apt` | none | packages installed first. Linux legs only |
| `env` | none | string map exported to every step. Names must be shell variable names, values one line, and `MACH_COMPILER`, `MACH_CI_*` and `MACH_LIB_*` belong to the toolkit |
| `test` | `true` | `false` makes the leg build-only |
| `timeout` | `timeout-minutes` | leg timeout |

Before anything builds, each leg reads the manifest of the project and of every subproject it builds or tests. It fails when one declares no profile the leg uses, or no target named by the leg's `target`. A manifest the leg tests must also declare a target for the leg's host. A build-only project, such as a spirv-only shader, may target another platform. Without that check, mach falls back to a `default = true` target and the tests run a binary the host cannot execute.

The plan refuses a configuration in which no light leg remains after `skip-legs`, because a pull request into `dev` would then build nothing and `gate` would still pass. The primary leg is the first light leg. `fmt` and `all-targets` run on that leg only.

A **subproject** is `{"path"}` plus these optional keys. The path may be a glob such as `examples/*`. Each leg expands it, in sorted order, to every matching directory that holds a `mach.toml`, and the entry's keys apply to every match. A glob that matches no project fails the leg. The plan refuses `**`, because it would reach into `dep/` and `out/`, and it refuses a project listed twice. A new project directory is then covered without an edit to `ci.yml`.

| key | default | meaning |
| --- | --- | --- |
| `pull` | `true` | run `mach dep pull` on it |
| `build` | `false` | build it in every profile |
| `test` | `true` | test it in every profile |
| `fmt` | `true` | include it in the fmt check, whatever its `legs` and `tier` |
| `clean-dep` | `false` | delete its `dep/` before pulling, for a subproject that resolves the library as a non-root |
| `jobs` | mach default | passed as `--jobs` to its tests |
| `legs` | every leg | the legs it runs on |
| `tier` | `light` | `heavy` runs it only on legs running heavy |

`mach test .` never runs another project's tests, so every test project has to be listed here or run by a hook.

**Hooks** are `setup.sh`, `verify.sh` and `teardown.sh` in `hooks-dir`, run with bash from the repo root:

- `setup.sh` runs after the seed, before `dep pull`
- `verify.sh` runs after the standard phases
- `teardown.sh` runs whenever setup was attempted, even after a failure

The directory is reserved for those three names. Any other file there fails the plan, so a misspelled hook cannot be skipped silently. `teardown.sh` without `setup.sh` fails too. Hooks see these variables:

| variable | value |
| --- | --- |
| `MACH_COMPILER` | absolute path of the verified compiler |
| `MACH_CI_LEG` | the leg name |
| `MACH_CI_TIER` | `light` or `heavy` for this leg |
| `MACH_CI_HEAVY` | the heavy selection, `all` on a pull request into `main` |
| `MACH_CI_PRIMARY` | `true` on the primary leg |
| `MACH_CI_TARGET`, `MACH_CI_RUNNER` | the leg's target and runner |
| `MACH_CI_PROJECT` | the project directory |
| `MACH_CI_PROFILES` | the profiles, space separated |

A hook switches on `MACH_CI_LEG` for per-host work. It reads `MACH_CI_TIER` or `MACH_CI_HEAVY` to hold expensive work to the heavy tier.

**Extra jobs** are ordinary jobs in the caller. A heavy-only job uses `if: github.base_ref == 'main' || inputs.heavy == 'all' || inputs.heavy == '<name>'`, and `<name>` goes in the dispatch options. It can seed through `briar-systems/.github/.github/actions/seed-mach@main`. Every extra job goes in `gate`'s `needs:`.

### Releases

A release is a pushed `v*` tag, and `mach-release.yml` publishes it. A called workflow cannot call its caller's `ci.yml`, so the caller's `release.yml` runs the shared workflow twice, around its own full CI:

```yaml
name: Release

on:
  push:
    tags: ['v*']
  workflow_dispatch:

permissions:
  contents: read

jobs:
  verify:
    uses: briar-systems/.github/.github/workflows/mach-release.yml@main
    with:
      stage: verify

  ci:
    needs: verify
    uses: ./.github/workflows/ci.yml
    with:
      heavy: all

  publish:
    needs: [verify, ci]
    uses: briar-systems/.github/.github/workflows/mach-release.yml@main
    permissions:
      contents: write
    with:
      stage: publish
```

A tag push is the one push trigger the contract allows outside deploy-only workflows. A tag has no base branch, so `ci.yml` declares a `workflow_call` input `heavy`, and `inputs.heavy` resolves from whichever trigger started the run:

```yaml
on:
  pull_request:
  workflow_call:
    inputs:
      heavy:
        type: string
        default: none
  workflow_dispatch:
    inputs:
      heavy:
        type: choice
        default: none
        options: [none, all, aarch64-linux, x86_64-windows, aarch64-darwin, x86_64-darwin]
```

`stage: verify` runs first and fails when:

- the tag is not `v` plus `[project].version` from the manifest
- the version is not semver
- the changelog has no single, non-empty `## [X.Y.Z]` section. A date after the heading is fine

It outputs `version`, `tag` and `rehearsal` for the caller's own jobs.

`stage: publish` runs last. It rechecks everything from the same commit rather than trusting passed values. It then:

1. reads the caller's workflow file and fails unless the publish job needs every other job, one job calls `./.github/workflows/ci.yml` with `heavy: all`, and every job other than verify needs verify
2. collects the assets and fails unless they are exactly the declared set. It adds `SHA256SUMS` over them
3. fails when a release or draft for the tag already exists
4. drafts the release, titled with the tag, with the changelog section as its notes and every asset attached
5. checks that the draft holds exactly those assets and notes, then publishes it

A version with a prerelease part is published as a prerelease. A stable release is marked latest only if it is at least every published stable release, so a backport to an older line never takes latest.

A `workflow_dispatch` rehearses the same path. The tag is `v<version>-rehearsal.<run id>`, which is never pushed, and the draft is a prerelease that is deleted once it is checked. The rehearsal needs no tag, and it proves the gates, the build and the upload before a real tag is pushed.

| input | default | use |
| --- | --- | --- |
| `stage` | required | `verify` or `publish` |
| `project` | `.` | the directory whose `mach.toml` holds the version |
| `changelog` | `CHANGELOG.md` | the changelog the notes come from |
| `assets` | none | JSON array of the file names to attach. `{version}` is replaced with the version. Give the same value to both stages |
| `asset-artifacts` | `release-*` | the artifact name pattern the caller's build jobs upload the assets under |
| `checksums` | `true` | attach `SHA256SUMS` when there are assets |
| `keep-rehearsal` | `false` | keep a rehearsal's draft for inspection. Delete it afterwards |

A caller that ships binaries adds its own build jobs. Each job needs `verify`, names its files with `needs.verify.outputs.version`, and uploads them as an artifact matching `asset-artifacts`. The publish job then needs those jobs too:

```yaml
  build:
    needs: verify
    strategy:
      matrix:
        include:
          - { asset: x86_64-linux, runs-on: ubuntu-latest }
          - { asset: x86_64-windows, runs-on: windows-latest }
    runs-on: ${{ matrix.runs-on }}
    steps:
      - uses: actions/checkout@v6
      - uses: briar-systems/.github/.github/actions/seed-mach@main
      - run: ./tools/package.sh "${{ needs.verify.outputs.version }}" "${{ matrix.asset }}" dist
      - uses: actions/upload-artifact@v7
        with:
          name: release-${{ matrix.asset }}
          path: dist/*

  publish:
    needs: [verify, ci, build]
    uses: briar-systems/.github/.github/workflows/mach-release.yml@main
    permissions:
      contents: write
    with:
      stage: publish
      assets: '["mls-{version}-x86_64-linux.tar.gz", "mls-{version}-x86_64-windows.zip"]'
```

This repo's `release-rehearsal.yml` is that shape, run against `test/fixture`.

### Override example

This caller has a live service stack, a subproject that resolves the library as a non-root, a qemu leg, no Windows leg, and a heavy-only job.

```yaml
name: CI

on:
  pull_request:
  workflow_dispatch:
    inputs:
      heavy:
        description: heavy work to run on this ref
        type: choice
        default: none
        options: [none, all, aarch64-linux, aarch64-darwin, x86_64-darwin, riscv64-linux, assurance]

permissions:
  contents: read

jobs:
  lib:
    uses: briar-systems/.github/.github/workflows/mach-lib.yml@main
    with:
      heavy: ${{ inputs.heavy }}
      submodules: "true"
      skip-legs: '["x86_64-windows"]'
      extra-legs: >-
        [{"name": "riscv64-linux", "runs-on": "ubuntu-latest", "tier": "heavy",
          "target": "linux-riscv64", "runner": "qemu-riscv64", "apt": ["qemu-user"]},
         {"name": "x86_64-linux-interop", "runs-on": "ubuntu-latest",
          "apt": ["gnutls-bin"], "env": {"INTEROP": "1"}}]
      subprojects: >-
        [{"path": "test/acme", "clean-dep": true, "jobs": 1, "legs": ["x86_64-linux"]},
         {"path": "test/leakage-negative", "test": false}]
      evidence: |
        test/native/results/
        .tools/run/*.log

  assurance:
    if: github.base_ref == 'main' || inputs.heavy == 'all' || inputs.heavy == 'assurance'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - uses: briar-systems/.github/.github/actions/seed-mach@main
      - run: tools/assurance run && tools/assurance verify

  gate:
    if: always()
    needs: [lib, assurance]
    runs-on: ubuntu-latest
    steps:
      - uses: briar-systems/.github/.github/actions/gate@main
        with:
          needs: ${{ toJSON(needs) }}
```

The `.github/ci/setup.sh` for this caller starts the stack on the linux leg:

```bash
#!/usr/bin/env bash
set -euo pipefail
case "$MACH_CI_LEG" in
  x86_64-linux) test/acme/harness/start.sh ;;
esac
```

`teardown.sh` stops the stack, and `verify.sh` runs the per-host verifier scripts:

```bash
#!/usr/bin/env bash
set -euo pipefail
case "$MACH_CI_LEG" in
  x86_64-linux) bash test/native/verify.sh "$MACH_COMPILER" linux-x86_64 ;;
  aarch64-linux) bash test/native/verify.sh "$MACH_COMPILER" linux-arm64 ;;
  riscv64-linux) bash test/riscv64/verify.sh ;;
esac
```

### Versions

Callers reference the workflow and the gate and seed actions at `@main`. A toolkit change lands on `dev` first, and this repo's `dev` to `main` pull request, which runs every leg, is its release gate. The workflow checks out its actions and scripts at its own commit, so one caller ref pins all of them together. The mach seed pin is `.github/actions/seed-mach/version`. Bumping it is one pull request here, and it moves every caller that has not set `mach-version`.

Every family manifest declares the compiler it needs as `mach = "^5.3"` under `[project]`. The key only exists from mach 5.3 on, and older compilers refuse it (`unknown key 'mach' in [project]`), so having the key already sets a 5.3 floor. Once any adopter declares it, the pin cannot go below v5.3.0, and a caller that sets `mach-version` must name v5.3.0 or later.

Because every caller follows `main`, the toolkit has no version tags. A release is the `dev` to `main` pull request, and the org-wide note in CONTRIBUTING about tagging releases does not apply to this repo. Before that pull request merges, every adopter's `dev` is preflighted against the change with [`tools/preflight`](tools/preflight/README.md). A change to `mach-release.yml` or its script is also rehearsed by dispatching `release-rehearsal.yml` on the branch.
