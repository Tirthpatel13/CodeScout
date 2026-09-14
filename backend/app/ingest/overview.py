"""Phase 6: the repo overview card.

Every agent turn gets this for free in its system prompt. It costs one LLM call
per index run and saves 2-3 tool calls on the average question, which is the
best latency trade in the pipeline.

The structural half is computed deterministically; only the prose summary comes
from the model, so the overview is still useful when the LLM call fails.
"""

from __future__ import annotations

import json
from typing import Any

from app.ingest.walk import WalkedFile
from app.providers.base import LLMProvider

ENTRYPOINT_HINTS = (
    "main.py",
    "__main__.py",
    "app.py",
    "manage.py",
    "wsgi.py",
    "asgi.py",
    "main.go",
    "cmd/",
    "index.js",
    "index.ts",
    "server.ts",
    "server.js",
    "main.ts",
)
MANIFESTS = (
    "pyproject.toml",
    "requirements.txt",
    "setup.py",
    "package.json",
    "go.mod",
    "Cargo.toml",
    "Gemfile",
    "pom.xml",
    "build.gradle",
)

SUMMARY_PROMPT = """You are summarising a code repository for an engineer who has \
never seen it. You are given its file tree, manifests, and README excerpt.

Write JSON with exactly these keys:
  "purpose": one sentence on what this project does.
  "packages": array of {"path": str, "role": str} for the top-level source \
directories, at most 8, role being a short phrase.
  "conventions": array of at most 4 short strings — notable patterns an engineer \
should know before reading the code.

Reply with JSON only, no prose, no code fence."""


def structural_overview(files: list[WalkedFile]) -> dict[str, Any]:
    languages: dict[str, int] = {}
    for f in files:
        key = f.language or f.kind
        languages[key] = languages.get(key, 0) + f.loc

    entrypoints = [
        f.path for f in files if any(f.path.endswith(h) or h in f.path for h in ENTRYPOINT_HINTS)
    ][:10]
    manifests = [f.path for f in files if f.path.split("/")[-1] in MANIFESTS][:10]

    top_dirs: dict[str, int] = {}
    for f in files:
        head = f.path.split("/")[0] if "/" in f.path else "(root)"
        top_dirs[head] = top_dirs.get(head, 0) + 1

    readme = next(
        (f for f in files if f.path.lower() in ("readme.md", "readme.rst", "readme.txt")), None
    )

    return {
        "languages": dict(sorted(languages.items(), key=lambda kv: -kv[1])),
        "total_files": len(files),
        "total_loc": sum(f.loc for f in files),
        "entrypoints": entrypoints,
        "manifests": manifests,
        "top_level_dirs": dict(sorted(top_dirs.items(), key=lambda kv: -kv[1])[:20]),
        "readme_path": readme.path if readme else None,
    }


def _context_blob(files: list[WalkedFile], structural: dict[str, Any]) -> str:
    parts: list[str] = [
        "FILE TREE (first 300 paths):",
        "\n".join(f.path for f in files[:300]),
        "",
        f"LANGUAGES BY LOC: {json.dumps(structural['languages'])}",
    ]
    by_path = {f.path: f for f in files}
    for manifest in structural["manifests"][:3]:
        f = by_path.get(manifest)
        if f:
            parts += ["", f"--- {manifest} ---", f.text[:3000]]
    readme_path = structural.get("readme_path")
    if readme_path and readme_path in by_path:
        parts += ["", "--- README (excerpt) ---", by_path[readme_path].text[:4000]]
    return "\n".join(parts)


def _extract_json(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("```")[1]
        if stripped.startswith("json"):
            stripped = stripped[4:]
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        parsed = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def build_overview(
    files: list[WalkedFile],
    llm: LLMProvider | None = None,
) -> dict[str, Any]:
    overview = structural_overview(files)
    if llm is None or not files:
        return overview

    try:
        response = await llm.complete(
            system=SUMMARY_PROMPT,
            messages=[{"role": "user", "content": _context_blob(files, overview)}],
            max_tokens=1200,
        )
        parsed = _extract_json(response.text)
        if parsed:
            overview["purpose"] = parsed.get("purpose")
            overview["packages"] = parsed.get("packages", [])
            overview["conventions"] = parsed.get("conventions", [])
            overview["summary_cost_usd"] = round(llm.cost_usd(response.usage), 6)
    except Exception as exc:  # the structural overview is still worth having
        overview["summary_error"] = str(exc)[:200]

    return overview


def render_for_prompt(overview: dict[str, Any] | None, repo_slug: str) -> str:
    """Compact text form injected into the agent's system prompt."""
    if not overview:
        return f"Repository: {repo_slug}\n(No overview available.)"

    lines = [f"Repository: {repo_slug}"]
    if overview.get("purpose"):
        lines.append(f"Purpose: {overview['purpose']}")
    langs = overview.get("languages") or {}
    if langs:
        top = ", ".join(f"{k} ({v} LOC)" for k, v in list(langs.items())[:5])
        lines.append(f"Languages: {top}")
    lines.append(
        f"Size: {overview.get('total_files', 0)} files, {overview.get('total_loc', 0)} LOC"
    )
    if overview.get("packages"):
        lines.append("Packages:")
        lines += [f"  - {p.get('path')}: {p.get('role')}" for p in overview["packages"][:8]]
    if overview.get("entrypoints"):
        lines.append("Entrypoints: " + ", ".join(overview["entrypoints"][:6]))
    if overview.get("manifests"):
        lines.append("Manifests: " + ", ".join(overview["manifests"][:6]))
    if overview.get("conventions"):
        lines.append("Conventions:")
        lines += [f"  - {c}" for c in overview["conventions"][:4]]
    return "\n".join(lines)
