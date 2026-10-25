"""Locate bundled data (schemas, template library, skills).

In a source checkout the canonical copies live at the repository root. A wheel
install cannot carry files outside the package, so pyproject's data-files place
copies under ``<prefix>/share/aiwf``. Both layouts are tried; ``AIWF_DATA_DIR``
wins when set.
"""
import os
import site
import sys
import sysconfig
from typing import Iterator


def data_roots() -> Iterator[str]:
    env_dir = os.getenv("AIWF_DATA_DIR")
    if env_dir:
        yield env_dir
    seen = set()
    roots = [os.path.join(sys.prefix, "share", "aiwf")]
    data_dir = sysconfig.get_paths().get("data") or ""
    if data_dir:
        roots.append(os.path.join(data_dir, "share", "aiwf"))
    for root in roots:
        if root not in seen:
            seen.add(root)
            yield root
    try:
        user = os.path.join(site.getuserbase(), "share", "aiwf")
    except Exception:
        user = ""
    if user and user not in seen:
        yield user


def data_candidates(*parts: str) -> Iterator[str]:
    for root in data_roots():
        yield os.path.join(root, *parts)
