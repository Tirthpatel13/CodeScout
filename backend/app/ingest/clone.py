"""Phase 1: get the code onto disk.

A cloned repository is untrusted input. Nothing here runs anything from the repo,
no path is ever interpolated into a shell string, and git runs with credential
helpers and hooks disabled.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path

GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_ASKPASS": "true",
    "HOME": "/nonexistent",
}


class CloneError(RuntimeError):
    pass


@dataclass(slots=True)
class CloneResult:
    path: Path
    commit_sha: str


async def _run(*args: str, cwd: Path | None = None, timeout: float = 600.0) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=GIT_ENV,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        raise CloneError(f"git timed out after {timeout}s") from None
    if proc.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise CloneError(detail[-1] if detail else f"git exited {proc.returncode}")
    return stdout.decode("utf-8", errors="replace").strip()


def build_clone_url(owner: str, name: str, token: str | None) -> str:
    if token:
        # Token in the URL, never logged. Callers must not echo this value.
        return f"https://x-access-token:{token}@github.com/{owner}/{name}.git"
    return f"https://github.com/{owner}/{name}.git"


async def clone_repo(
    *,
    url: str,
    dest: Path,
    branch: str | None = None,
    depth: int = 1,
) -> CloneResult:
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)

    args = [
        "git",
        "clone",
        "--depth",
        str(depth),
        "--single-branch",
        "--no-tags",
        "--config",
        "core.hooksPath=/dev/null",
        "--config",
        "credential.helper=",
    ]
    if branch:
        args += ["--branch", branch]
    args += [url, str(dest)]

    await _run(*args)
    sha = await _run("git", "rev-parse", "HEAD", cwd=dest)
    return CloneResult(path=dest, commit_sha=sha)


async def head_commit(path: Path) -> str:
    return await _run("git", "rev-parse", "HEAD", cwd=path)


def cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
