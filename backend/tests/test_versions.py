"""The Python and uv versions are written in several files: the laptop, CI, the Docker image and Lambda.
A bot or a person can change one of them alone and every other check stays green (Dependabot's PR #15
moved the Docker image to Python 3.14 that way). These tests fail as soon as one place differs."""

import re
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).parent.parent
REPO_DIR = BACKEND_DIR.parent

# uv reads this file on the laptop and in CI, so it's the reference for every other place
PYTHON_VERSION = (BACKEND_DIR / ".python-version").read_text().strip()


def versions_in(path: str, pattern: str) -> list[str]:
    versions = re.findall(pattern, (REPO_DIR / path).read_text(), re.MULTILINE)
    # Finding nothing must fail too: otherwise a reformatted file would silently pass
    assert versions, f"no version found in {path}: update the pattern in this test"
    return versions


@pytest.mark.parametrize(
    ("path", "pattern"),
    [
        ("backend/pyproject.toml", r'^requires-python = ">=([\d.]+)"'),
        ("backend/pyproject.toml", r'^python_version = "([\d.]+)"'),  # mypy
        ("backend/Dockerfile", r"^FROM python:([\d.]+)-"),  # both stages
        ("backend/deploy.py", r"lambda/python:([\d.]+)"),  # the image that builds the Lambda zip
        ("terraform/main.tf", r'runtime\s*=\s*"python([\d.]+)"'),  # the Lambda runtime itself
    ],
)
def test_python_version_is_the_same_everywhere(path, pattern):
    assert set(versions_in(path, pattern)) == {PYTHON_VERSION}


def test_uv_version_is_the_same_everywhere():
    workflow_uv = r'^\s+version: "([\d.]+)"'  # the setup-uv step's input
    found = {
        "backend/Dockerfile": versions_in("backend/Dockerfile", r"astral-sh/uv:([\d.]+)@"),
        ".github/workflows/ci.yml": versions_in(".github/workflows/ci.yml", workflow_uv),
        ".github/workflows/evals.yml": versions_in(".github/workflows/evals.yml", workflow_uv),
        ".github/workflows/deploy.yml": versions_in(".github/workflows/deploy.yml", workflow_uv),
    }

    assert len({version for versions in found.values() for version in versions}) == 1, found
