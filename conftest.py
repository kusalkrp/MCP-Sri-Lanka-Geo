"""
Root conftest.py — host URL patching via pytest_configure.

pytest_configure runs before any test collection or fixture setup, making it
the earliest possible hook to override os.environ before pydantic-settings
creates the Settings() singleton from .env.

Substitutes Docker Compose service hostnames (postgres, redis, qdrant) with
localhost equivalents using the port mappings defined in docker-compose.yml:
    postgres:5432  →  localhost:5433
    redis:6379     →  localhost:6379
    qdrant:6333    →  localhost:6333
"""

from __future__ import annotations

import os
from pathlib import Path


def pytest_configure(config) -> None:  # noqa: ARG001
    """Patch Docker service hostnames → localhost before any app code is imported."""
    _patch_docker_urls_for_host()


def _patch_docker_urls_for_host() -> None:
    try:
        from dotenv import dotenv_values
    except ImportError:
        return  # python-dotenv not installed — skip, assume env is pre-set

    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return

    host_subs = {
        "@postgres:5432": "@localhost:5433",
        "@redis:6379":    "@localhost:6379",
        "//redis:6379":   "//localhost:6379",
        "//qdrant:6333":  "//localhost:6333",
    }

    for key, val in dotenv_values(env_path).items():
        if key in os.environ:
            continue  # explicitly pre-set — don't override
        patched = val
        for old, new in host_subs.items():
            patched = patched.replace(old, new)
        os.environ[key] = patched
