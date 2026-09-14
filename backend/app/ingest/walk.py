"""Phase 2: walk the clone and decide what is worth indexing."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pathspec

# Directories that are never source code worth answering questions about.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".vscode",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "bower_components",
        "vendor",
        "third_party",
        "dist",
        "build",
        "out",
        "target",
        "bin",
        "obj",
        ".next",
        ".nuxt",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        "coverage",
        "htmlcov",
        ".gradle",
        "Pods",
        "DerivedData",
    }
)

LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".mts": "typescript",
    ".cts": "typescript",
    ".go": "go",
}

DOC_SUFFIXES = frozenset({".md", ".mdx", ".rst", ".txt", ".adoc"})
CONFIG_SUFFIXES = frozenset({".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".env"})
CONFIG_NAMES = frozenset(
    {
        "Dockerfile",
        "Makefile",
        "Procfile",
        "requirements.txt",
        "pyproject.toml",
        "package.json",
        "go.mod",
        "Cargo.toml",
        "docker-compose.yml",
        "docker-compose.yaml",
    }
)

# Generated or vendored files that are technically source but never informative.
NOISE_PATTERNS = (
    ".min.js",
    ".min.css",
    ".bundle.js",
    ".map",
    "-lock.json",
    ".lock",
    ".pb.go",
    "_pb2.py",
    ".generated.ts",
    ".g.dart",
)


@dataclass(slots=True)
class WalkedFile:
    path: str  # repo-relative, posix separators
    abs_path: Path
    language: str | None
    kind: str  # code | doc | config
    size_bytes: int
    loc: int
    content_hash: str
    text: str


@dataclass(slots=True)
class WalkReport:
    files: list[WalkedFile]
    skipped: dict[str, int]

    @property
    def total_loc(self) -> int:
        return sum(f.loc for f in self.files)


def classify(path: Path) -> tuple[str | None, str] | None:
    """-> (language, kind), or None if the file should be skipped."""
    suffix = path.suffix.lower()
    if path.name in CONFIG_NAMES:
        return None, "config"
    if suffix in LANGUAGE_BY_SUFFIX:
        return LANGUAGE_BY_SUFFIX[suffix], "code"
    if suffix in DOC_SUFFIXES:
        return None, "doc"
    if suffix in CONFIG_SUFFIXES:
        return None, "config"
    return None


def _load_gitignore(root: Path) -> pathspec.PathSpec | None:
    gitignore = root / ".gitignore"
    if not gitignore.is_file():
        return None
    try:
        lines = gitignore.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    return pathspec.PathSpec.from_lines("gitwildmatch", lines)


def _is_binary(blob: bytes) -> bool:
    return b"\x00" in blob[:8192]


def walk_repo(root: Path, *, max_files: int, max_file_bytes: int) -> WalkReport:
    root = root.resolve()
    spec = _load_gitignore(root)
    files: list[WalkedFile] = []
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for abs_path in sorted(root.rglob("*")):
        if len(files) >= max_files:
            skip("file_cap_reached")
            break

        # A cloned repo is untrusted input: never follow a symlink out of it.
        if abs_path.is_symlink():
            skip("symlink")
            continue
        if not abs_path.is_file():
            continue

        try:
            rel = abs_path.resolve().relative_to(root)
        except ValueError:
            skip("outside_root")
            continue

        rel_posix = rel.as_posix()
        if any(part in SKIP_DIRS for part in rel.parts[:-1]):
            skip("ignored_dir")
            continue
        if any(pattern in abs_path.name for pattern in NOISE_PATTERNS):
            skip("generated")
            continue
        if spec is not None and spec.match_file(rel_posix):
            skip("gitignored")
            continue

        classified = classify(abs_path)
        if classified is None:
            skip("unsupported_type")
            continue
        language, kind = classified

        try:
            size = abs_path.stat().st_size
        except OSError:
            skip("stat_failed")
            continue
        if size > max_file_bytes:
            skip("too_large")
            continue
        if size == 0:
            skip("empty")
            continue

        try:
            blob = abs_path.read_bytes()
        except OSError:
            skip("read_failed")
            continue
        if _is_binary(blob):
            skip("binary")
            continue

        text = blob.decode("utf-8", errors="replace")
        files.append(
            WalkedFile(
                path=rel_posix,
                abs_path=abs_path,
                language=language,
                kind=kind,
                size_bytes=size,
                loc=text.count("\n") + 1,
                content_hash=hashlib.sha256(blob).hexdigest(),
                text=text,
            )
        )

    return WalkReport(files=files, skipped=skipped)
