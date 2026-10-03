# Contributing

Bug reports, questions and pull requests are welcome on
[GitHub](https://github.com/django-aiodrf/aiodrf-async-cache). For security issues, follow the
[security policy](SECURITY.md) instead of opening an issue.

## Development setup

The project uses [uv](https://docs.astral.sh/uv/):

```console
git clone https://github.com/django-aiodrf/aiodrf-async-cache.git
cd aiodrf-async-cache
uv sync
```

`uv sync` installs the package in editable mode with the `dev` dependency
group, which includes the test tools, ruff, mypy and nox.

## Running the checks

```console
uv run pytest                    # the test suite; warnings are errors
uv run nox -s lint typecheck     # ruff check, ruff format --check, mypy
uv run nox -s distribution       # the built wheel, installed alone, tested
```

`uv run nox -s tests` runs the suite on Python 3.12, 3.13 and 3.14 with
Django 5.2, 6.0 and 6.1.

The Redis and Valkey tests run against dedicated services only, named by
environment variables. Their fixtures use random key prefixes, and some
tests clear the database:

```console
docker run -d --rm -p 6379:6379 redis:7.4
docker run -d --rm -p 6380:6379 valkey/valkey:8.1
AIODRF_TEST_REDIS_URL=redis://localhost:6379/0 \
AIODRF_TEST_VALKEY_URL=valkey://localhost:6380/0 uv run pytest
```

Sentinel and Cluster run on their own Compose project:
see `tests/services/cache-topologies.md`.

`tools/benchmark.py` times the cache operations against a local Redis; its
docstring says how to run it. Publish results only with the machine they
were measured on.

## Rules for changes

- Use Django's public APIs, and keep Django's semantics and method
  signatures. Do not patch Django or other packages.
- Write the test first, including the failure and cancellation paths the
  change affects.
- User-visible changes update `README.md` and the unreleased section at the
  top of `CHANGELOG.md`.

## Pull requests

Open pull requests against the `dev` branch. `main` receives `dev` at release
time; a workflow closes other pull requests into `main` with a pointer to
`dev`. A pull request from someone other than the maintainer waits for the
maintainer's approval before CI runs.

## Releases

Releases are made by the maintainer:

1. Set `version` in `pyproject.toml` and `__version__` in
   `src/aiodrf_async_cache/__init__.py` to the new version.
2. Give the version's section of `CHANGELOG.md` its release date:
   `## [X.Y.Z] - YYYY-MM-DD`.
3. Merge `dev` into `main`.
4. Run `python tools/release.py vX.Y.Z`, which checks that the three
   versions agree and prints the release notes, then push the tag `vX.Y.Z`
   on `main`.

The release workflow checks that the tag matches the package version and is
on `main`, builds and checks the distributions, publishes them to PyPI
through trusted publishing once the `pypi` environment is approved, and
creates the GitHub release with that version's changelog section as notes.
