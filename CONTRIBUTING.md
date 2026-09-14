# Contributing

Thanks for your interest in contributing to a Briar Systems project.

## Workflow
- Open or find an issue first. Substantial work should be tracked by an issue before a pull request.
- Branch from `dev` using `feat/<n>` or `fix/<n>`. Hotfixes branch from `main`.
- Write commit messages as [Conventional Commits](https://www.conventionalcommits.org).
- Open a draft pull request early, link it to its issue with `Closes #<n>`, and mark it ready when the work is complete.
- Pull requests merge into `dev`. `main` takes integration merges from `dev` for releases.

## Issues
Issues follow an orthogonal, faceted tagging system across five sets:
- SemVer magnitude: `patch`, `minor`, `major`
- Kind of work: `feature`, `fix`, `removal`, `chore`, `performance`
- Where (domain or location): `testing`, `tooling`, `doc` (omitted for core code)
- Severity and state: `critical`, `blocked`, `security`
- Discussion: `discussion` (design proposals, RFCs, and open debates)

Tags mix and match across sets (for example, `patch`, `fix`, `tooling`). When filing an issue, select the applicable tags in the sidebar. Milestones are not used for tracking in-flight work.

## Versioning
Repositories strictly follow [semantic versioning](https://semver.org/) (`vMAJOR.MINOR.PATCH`):
- `MAJOR`: breaking changes requiring consumer or API updates
- `MINOR`: backward-compatible new features, capabilities, or performance enhancements
- `PATCH`: backward-compatible fixes, documentation, and internal maintenance

SemVer magnitude is tracked directly on issues and pull requests via conventional commit scopes and issue tags, and releases are tagged on `main` following integration merges from `dev`.

## Before you push
- Keep changes small and self-contained.
- Make sure the build and the tests pass.

Project-specific guidance lives in each repository.
