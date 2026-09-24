# adopter preflight

Every caller uses the toolkit at `@main`, so a bad merge to `main` breaks CI across the whole family. Run this tool before merging any `dev` to `main` pull request, and before any pin bump in particular:

```sh
python3 tools/preflight/preflight.py --toolkit origin/dev            # the pin on dev
python3 tools/preflight/preflight.py --toolkit origin/dev --mach v5.2.1
```

It needs `gh` (authenticated), `git`, and Python 3.11 or newer with PyYAML.

## What it does

1. It exports `.github` at `--toolkit` and imports that ref's `plan.py`, `run.py` and seed `verify.py`, so the checks run the code under release.
2. It downloads the mach release for this host (`--mach`, which defaults to the pin at `--toolkit`) and verifies it exactly as `seed-mach` does.
3. It finds the adopters: every non-archived org repo whose `ci.yml` on `branch` references `mach-lib.yml@`, minus the `[exclude]` entries.
4. It clones each adopter shallowly. It checks out submodules only when the adopter's `submodules` input asks for them, as the leg's `submodules` phase does. Initializing them recursively would realize nested `dep/` trees, and `mach dep pull` refuses those.
5. It reads the adopter's `lib` inputs and plans them as a pull request into `dev` and into `main`. Then it runs fmt on the project and its subprojects, and the manifests check for every leg. Each leg's host comes from `[hosts]`. It also reports any committed symlink at or under a `dep` directory.
6. For each repo in `[sample]`, it runs the primary leg's phases (`submodule-tags`, `env`, `manifests`, `dit`, `setup.sh`, `deps`, `build`, `test`, `subprojects`, `fmt`, `all-targets`, `verify.sh`, `teardown.sh`) the way `mach-lib.yml` does for a pull request into `main`. Afterwards it checks that no `dep` symlink appeared.

Output, clones, the seed and per-repo logs go to `.wt/preflight-<tag>/`, which is ignored.

To prove a toolkit change on an adopter branch, or on a `ci.yml` that opts into a new input before the adopter has committed it, point the tool at a checkout of your own instead of its `dev`. The checks and the sample then run there, reading `ci.yml` from that checkout, and the repo is sampled whether or not it is in `[sample]`:

```sh
python3 tools/preflight/preflight.py --toolkit feat/80 --repos boom --local boom=.wt/boom-184
```

## Configuration

`preflight.toml` holds the exclusions and their reasons, the sample set, and the map from `runs-on` labels to hosts. When an adopter uses a new runner label, the preflight reports it as a problem until the label is added there.

## Known local limits

Sample hooks can depend on things that exist only on a hosted runner. When a step fails, read its log before treating the failure as real:

- `mach-tls` `setup.sh` runs `sudo apt-get install gnutls-bin`. If the package is already installed, pass `--path <dir>` with a `sudo` stub in that directory. On a host without `apt-get` the stub has to swallow the install rather than exec it: `case "$1" in apt-get) exit 0;; esac; exec "$@"`
- `boom` and `mach-glfw` `verify.sh` need `xvfb-run`
- `hedge` `verify.sh` binds `127.0.0.1:19100` through `19105`, which fails while anything else holds those ports
- `mach-tls` `verify.sh` runs its interop server on `127.0.0.1:9443`, the same port `hedge`'s interop stack uses. While anything else holds it, every server cell reports `FAILED`

Run the preflight in the foreground. On a loaded machine, background tasks can be killed partway through.
