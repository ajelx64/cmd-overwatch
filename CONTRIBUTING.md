# Contributing to cmd-overwatch

Bug reports, fixes, and new features are welcome. Please read this guide before opening
a pull request.

---

## Setup

1. Fork the repository and clone your fork.
2. Install with dev extras:
   ```powershell
   pip install -e ".[dev]"
   ```
3. Copy the example config:
   ```powershell
   copy config.example.toml config.toml
   ```
   Edit `config.toml` to point at local test paths. `config.toml` is gitignored — it
   never reaches the repository.

---

## Running tests and checks

```powershell
# Tests
python -m pytest

# Lint
ruff check .

# Type-check
python -m mypy overwatch/
```

CI runs all three on Python 3.11 and 3.13 for every pull request. PRs must pass CI
before merge.

---

## Pre-commit checks

CI runs a gitleaks secret scan on every push (`.github/workflows/secret-scan.yml`). If
you want to catch leaks locally before pushing, install pre-commit:

```powershell
pip install pre-commit
pre-commit run --all-files
```

---

## Branch and PR conventions

- Branch naming: `feature/<n>-<slug>` for new features, `fix/<slug>` for bug fixes.
- One logical change per PR. Split unrelated changes into separate PRs.
- New behaviour requires tests. PRs that introduce untested code paths will be asked to
  add coverage before merge.
- Keep commit messages in the imperative mood: "add X", "fix Y", "remove Z".

---

## Public-repo cleanliness rules

This is a public repository. The following are hard requirements:

1. **No personal paths or machine-specific details** in any committed file. Use
   generic example paths like `C:/projects/my-project`, `C:/automation/logs`,
   `\MyAutomation\`. If you paste a path from your machine, sanitise it before
   committing.

2. **No usernames, hostnames, or account identifiers** from your local environment.

3. **No real credentials** of any kind, even in test fixtures. Use obviously-fake
   values (`fake-api-key`, `AKIA0000000000EXAMPLE`, etc.).

4. **Test fixtures must use synthetic or temporary data.** Never commit actual log
   files, git history, or task scheduler output from a real machine.

5. Before opening a PR, verify no personal context leaked:
   ```powershell
   git grep -iE "<personal-paths>|<usernames>"
   ```
   Replace the pattern with whatever local identifiers your machine might have
   introduced. The check must return nothing.

---

## Issue reporting

Bug reports are welcome. Please include:

- OS and Python version (`python --version`)
- Relevant sections of `config.toml` (sanitise any real paths before pasting)
- Steps to reproduce
- What you expected vs. what happened
- Any tracebacks from `data/` or the server stdout

Feature requests are also welcome — open an issue describing the use case before
starting implementation so we can discuss the approach.

---

## License

By submitting a pull request you agree that your contribution will be distributed under
the [MIT License](LICENSE). Contributions remain copyright their respective authors.
