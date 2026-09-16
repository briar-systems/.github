# briar-systems/.github

Org-wide community health files, the org profile (`profile/`), and the CI toolkit the mach libraries share.

## Shared CI

The family CI contract:

- each repo has one `.github/workflows/ci.yml`, and a release workflow where one exists
- a pull request into `dev` runs the light tier, a pull request into `main` runs every tier, and `workflow_dispatch` pulls named heavy work onto any ref
- nothing runs on push, and nothing runs on a schedule
- every `ci.yml` ends in a job named exactly `gate` that needs every other job. It fails when a needed job finished `failure` or `cancelled` and passes `skipped`. `gate` is the one required check

The toolkit:

| path | what it is |
| --- | --- |
| `.github/workflows/mach-lib.yml` | the reusable library pipeline (`on: workflow_call`) |
| `.github/actions/seed-mach` | installs a published mach release after checking its archive against `SHA256SUMS`. The family pin is in `version` |
| `.github/actions/gate` | the gate logic |
| `.github/mach-lib` | the plan and leg scripts the workflow runs, with their tests |
| `test/fixture` | the library this repo's own `ci.yml` runs the workflow against |

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

### Override surface

Every override is an input. There is nothing to fork.

| input | default | use |
| --- | --- | --- |
| `heavy` | `none` | forward the caller's dispatch choice. Takes `all`, leg names, or names the caller's own jobs and hooks read |
| `legs` | the five hosts above | JSON array that replaces the host set |
| `extra-legs` | none | JSON array appended to the host set |
| `skip-legs` | none | JSON array of leg names to drop |
| `project` | `.` | the project directory, for a repo whose real project is not the root |
| `profiles` | `["debug", "release"]` | profiles to build and test |
| `test` | `true` | run `mach test` on the project |
| `fmt` | `true` | `mach fmt --check`, once, on the primary leg |
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
| `apt` | none | packages installed first. Linux legs only |
| `env` | none | string map exported to every step |
| `test` | `true` | `false` makes the leg build-only |
| `timeout` | `timeout-minutes` | leg timeout |

The primary leg is the first light leg that runs. `fmt` and `all-targets` run on that leg only.

A **subproject** is `{"path"}` plus these optional keys:

| key | default | meaning |
| --- | --- | --- |
| `pull` | `true` | run `mach dep pull` on it |
| `build` | `false` | build it in every profile |
| `test` | `true` | test it in every profile |
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

Callers reference the toolkit at `@main`. The workflow checks out its actions and scripts at its own commit, so one caller ref pins all of them together. The mach seed pin is `.github/actions/seed-mach/version`. Bumping it is one pull request here, and it moves every caller that has not set `mach-version`.
