# Releasing Dango

This document describes the release process for publishing new versions to PyPI.

## Version Locations

**Source of truth (must update manually):**

| File | Line | Purpose |
|------|------|---------|
| `pyproject.toml` | 7 | PyPI package version |
| `dango/__init__.py` | 7 | Runtime `__version__` |

**Auto-references `__version__` (no update needed):**
- `cli/main.py` - CLI version option
- `cli/wizard.py` - Project creation
- `cli/init.py` - Version getter
- `web/app.py` - Web UI version display

**Documentation (update for major releases):**
- `CHANGELOG.md` - Release notes
- `README.md` - Feature list
- `install.sh` / `install.ps1` - Installer headers

## Release Process

### 1. Batch Changes

Merge feature/fix PRs to main. Don't bump version per PR.

```
PR #21: fix: bug fix           → merge to main
PR #22: feat: new feature      → merge to main
PR #23: fix: another fix       → merge to main
```

### 2. Create Release PR

When ready to release, create a single release PR:

```bash
git checkout main
git pull origin main
git checkout -b chore/release-vX.Y.Z
```

### 3. Update Version

Edit both files to match:

```bash
# pyproject.toml
version = "X.Y.Z"

# dango/__init__.py
__version__ = "X.Y.Z"
```

### 4. Update CHANGELOG

Add release notes to `CHANGELOG.md`:

```markdown
## [X.Y.Z] - YYYY-MM-DD

### Added
- New feature description

### Fixed
- Bug fix description

### Changed
- Change description
```

Update the comparison links at the bottom of CHANGELOG.md.

### 5. Commit and PR

```bash
git add pyproject.toml dango/__init__.py CHANGELOG.md
git commit -m "chore: release vX.Y.Z"
git push -u origin chore/release-vX.Y.Z
gh pr create --title "chore: release vX.Y.Z" --body "Release vX.Y.Z"
```

### 6. Merge and Tag

After PR is approved and merged:

```bash
git checkout main
git pull origin main
git tag vX.Y.Z
git push --tags
```

### 7. Publish to PyPI

```bash
# Clean previous builds
rm -rf dist/ build/ *.egg-info

# Build
python -m build

# Upload to PyPI
twine upload dist/*
```

### 8. Verify

```bash
pip install getdango==X.Y.Z
dango --version
```

## Versioning Guidelines

We follow [Semantic Versioning](https://semver.org/):

- **MAJOR** (1.0.0): Breaking changes
- **MINOR** (0.1.0): New features, backwards compatible
- **PATCH** (0.0.1): Bug fixes, backwards compatible

Current status: Pre-1.0 (API may change between minor versions)

## Hotfix Process

For urgent fixes that can't wait for a batch release:

1. Create fix PR, merge to main
2. Immediately create release PR with patch bump
3. Follow steps 3-8 above
