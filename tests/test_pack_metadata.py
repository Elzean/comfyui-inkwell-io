"""The pack's version and licence stay consistent across files."""

import os
import re

PACK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    with open(os.path.join(PACK, name), encoding="utf-8") as f:
        return f.read()


def test_pyproject_version_matches_init():
    init = re.search(r'^__version__\s*=\s*"([^"]+)"', _read("__init__.py"), re.M).group(1)
    project = re.search(r'^version\s*=\s*"([^"]+)"', _read("pyproject.toml"), re.M).group(1)
    assert init == project


def test_requirements_match_pyproject_dependencies():
    reqs = [l.strip() for l in _read("requirements.txt").splitlines() if l.strip() and not l.startswith("#")]
    deps = re.search(r"^dependencies\s*=\s*\[(.*?)\]", _read("pyproject.toml"), re.M | re.S).group(1)
    assert sorted(reqs) == sorted(re.findall(r'"([^"]+)"', deps))


def test_licence_is_mit():
    assert _read("LICENSE").startswith("MIT License")
