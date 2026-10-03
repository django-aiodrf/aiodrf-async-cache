"""Independent package tests; service URLs are opt-in environment variables."""

import shutil
from pathlib import Path

import nox

nox.options.default_venv_backend = "uv"
nox.options.sessions = ["tests", "lint", "typecheck"]
TEST_DEPS = ["pytest", "pytest-django", "pytest-asyncio", "django-redis>=7"]
EXTRAS = "redis,valkey,django-valkey,msgspec"


def install(session, django):
    session.install(f"Django~={django}.0", *TEST_DEPS)
    session.install("." + (f"[{EXTRAS}]" if EXTRAS else ""))


@nox.session(python=["3.12", "3.13", "3.14"])
@nox.parametrize("django", ["5.2", "6.0", "6.1"])
def tests(session, django):
    install(session, django)
    session.run("pytest", *session.posargs)


@nox.session
def lint(session):
    session.install("ruff")
    session.run("ruff", "check", ".")
    session.run("ruff", "format", "--check", ".")


@nox.session
def typecheck(session):
    session.install("mypy", "django-stubs")
    install(session, "5.2")
    session.run("mypy")


@nox.session
def distribution(session):
    project = Path(__file__).parent
    artifacts = Path(session.create_tmp()) / "dist"
    session.install("build")
    session.run(
        "python", "-m", "build", "--installer", "uv", "--outdir", str(artifacts)
    )
    (wheel,) = artifacts.glob("*.whl")
    session.install(str(wheel) + (f"[{EXTRAS}]" if EXTRAS else ""), *TEST_DEPS)
    target = Path(session.create_tmp()) / "consumer"
    shutil.copytree(project / "tests", target / "tests", dirs_exist_ok=True)
    (target / "pytest.ini").write_text(
        "[pytest]\nDJANGO_SETTINGS_MODULE = tests.settings\n"
        "asyncio_mode = auto\nasyncio_default_fixture_loop_scope = function\n"
        "filterwarnings = error\nmarkers =\n    unit: no external service\n    integration: external services\n"
    )
    session.chdir(target)
    session.run(
        "python",
        "-c",
        'import importlib.util; import aiodrf_async_cache; from pathlib import Path; import sys; assert Path(sys.prefix) in Path(aiodrf_async_cache.__file__).parents; assert importlib.util.find_spec("rest_framework") is None; assert importlib.util.find_spec("aiodrf") is None',
    )
    session.run(
        "python", "-m", "pytest", *session.posargs, env={"PYTHONPATH": str(target)}
    )
