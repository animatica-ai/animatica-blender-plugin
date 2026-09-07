# Contributing

Thank you for contributing to this project.

This document describes the general contribution workflow used by the repository. Individual projects may have additional requirements documented in their README or other project documentation.

## Branch Structure

The repository uses a simple two-branch structure:

```text
feature/* ──┐
fix/* ──────┤
chore/* ────┼──→ develop ───→ main
docs/* ─────┤                 │
refactor/* ─┘                 ↓
                         Production / Release
```

### `main`

`main` represents the **stable, production-ready version** of the project.

* Changes should only reach `main` through a pull request from `develop`.
* Direct pushes should generally be disabled.
* Merging into `main` may trigger the project's release or deployment process.
* The branch should always remain in a releasable state.

### `develop`

`develop` is the **integration branch** for ongoing development.

* New work is merged into `develop`.
* CI should run on changes to `develop`.
* `develop` may contain changes that are not yet ready for production.
* Once the changes are considered ready, `develop` is merged into `main`.

### Feature and Work Branches

Create short-lived branches from `develop` for individual changes.

Recommended naming:

```text
feature/<description>
fix/<description>
chore/<description>
docs/<description>
refactor/<description>
```

For example:

```text
feature/add-export-support
fix/handle-invalid-input
docs/update-installation
chore/update-dependencies
```

## Contribution Workflow

### 1. Create a branch

Start from the latest `develop` branch:

```bash
git checkout develop
git pull
git checkout -b feature/my-change
```

### 2. Make your changes

Keep changes focused on the task.

Follow the existing project's:

* Code style
* File and directory structure
* Naming conventions
* Testing practices
* Documentation conventions

Update tests and documentation when appropriate.

Do not commit generated files, build artifacts, credentials, or local configuration unless the project explicitly requires them.

### 3. Test locally

Before opening a pull request, run the checks appropriate for the project.

Typical checks include:

* Tests
* Linting
* Formatting
* Type checking
* Build/package validation
* Documentation checks

Make sure the changes work locally before requesting review.

### 4. Open a Pull Request

Open a pull request from your branch into:

```text
feature/* → develop
```

The pull request should explain:

* What was changed
* Why it was changed
* How it was tested
* Any known limitations or follow-up work

### 5. Review and CI

Pull requests should pass the project's required automated checks.

Address review comments and update the branch as needed.

Do not merge while required checks are failing unless the failure is understood and there is an explicit reason to proceed.

### 6. Merge into `develop`

Once the pull request is approved and checks pass, merge it into `develop`.

The feature branch can then be deleted.

### 7. Promote `develop` to `main`

When the changes in `develop` are ready for production, create a pull request:

```text
develop → main
```

After review and successful CI, merge the pull request.

Merging into `main` may trigger the project's release/deployment automation.

## Releases

Production releases should normally be created from `main`.

Depending on the project, merging into `main` may automatically:

* Determine the release version
* Build artifacts
* Create a Git tag
* Create a GitHub Release
* Publish packages
* Deploy the application
* Update release metadata

Avoid manually creating releases or deployments when the repository provides automation for these tasks.

## Keeping Branches Up to Date

Before opening or updating a pull request, keep your branch reasonably up to date with `develop`.

For example:

```bash
git fetch origin
git rebase origin/develop
```

Follow the repository's existing merge/rebase conventions if they differ.

## Branch Principles

The general rules are:

1. **`main` is production-ready.**
2. **`develop` is the integration branch.**
3. **New work starts from `develop`.**
4. **Feature branches are short-lived.**
5. **Feature branches merge into `develop`.**
6. **`develop` is promoted to `main` through a pull request.**
7. **Production releases come from `main`.**
8. **CI should validate changes before they are merged.**
9. **Production deployments should be automated where possible.**

In short:

```text
New work
   ↓
feature/fix/chore branch
   ↓
Pull Request
   ↓
develop
   ↓
Pull Request
   ↓
main
   ↓
Release / Production
```

## Project-Specific Rules

This document provides the general contribution process.

If the repository's `README.md`, development documentation, or other contributor documentation specifies additional requirements, those requirements take precedence.

### Do not edit the vendored core

`animatica_blender/animatica_core/` is a copy of a named commit of
[motionmcp-client-sdk](https://github.com/animatica-ai/motionmcp-client-sdk),
shared with the 3ds Max and MotionBuilder plugins. It is generated, not
authored here.

An edit made in that directory is lost the next time the copy is synced, and
until then it makes this plugin disagree with the other two about what the
same request means. CI fails the build when the copy has drifted from its pin.

To change anything in there: open a branch on the SDK, land it there, then
move this repo's pin in a separate commit.

```bash
python scripts/sync_core.py --write --ref <new sha>
```

`animatica_blender/CORE-VERSION` records the pin; `make sync-core` checks it.
See [docs/developing.md](docs/developing.md) for the full arrangement.
