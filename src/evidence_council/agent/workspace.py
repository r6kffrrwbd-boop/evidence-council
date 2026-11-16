"""Isolated workspace: the agent never edits your project in place.

Modes
-----
* ``copy`` (default): the project is copied into ``<run>/workspace``. Version-control
  folders, dependency folders and secret-looking files (``.env``, ``*.pem`` ...) are not
  copied. At the end you get ``changes.patch`` to review and apply yourself.
* ``worktree``: ``git worktree add -b ec-agent/<run-id>`` creates a new branch checked out
  in ``<run>/workspace``. Only committed content is present. The agent never commits,
  pushes or switches your current branch.
* ``readonly``: the project itself, for Intake, which only reads.

Snapshots (full file copies under ``<run>/snapshots/<name>``) make rollback exact.
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..errors import CouncilError, EXIT_USAGE

IGNORE_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__", ".mypy_cache",
               ".pytest_cache", ".ruff_cache", ".tox", ".ec-agent", ".idea", ".vscode", "dist", "build", ".next"}
IGNORE_FILES = {".DS_Store"}
IGNORE_SUFFIXES = (".pyc", ".pyo")
SECRET_FILE_GLOBS = [".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", "*.keystore",
                     "credentials*", "secrets.*", "*.sqlite", "*.db", ".netrc", ".npmrc", ".pypirc"]
MAX_SNAPSHOT_BYTES = 2_000_000


def _copy(src: Path, dst: Path) -> None:
    """Copy contents and permission bits only (macOS may refuse to copy extended attributes/flags)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    try:
        shutil.copymode(src, dst)
    except OSError:
        pass


class ToolError(Exception):
    """An agent tool call that cannot be honoured; the message is returned to the model."""


def is_secret_name(rel: str) -> bool:
    name = Path(rel).name
    return any(fnmatch.fnmatch(name, g) for g in SECRET_FILE_GLOBS)


def _walk(root: Path) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS)
        for f in sorted(filenames):
            if f in IGNORE_FILES or f.endswith(IGNORE_SUFFIXES):
                continue
            p = Path(dirpath, f)
            if p.is_symlink():
                continue
            out.append(p.relative_to(root).as_posix())
    return out


def _read_text(p: Path) -> str | None:
    try:
        data = p.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8000]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


class Workspace:
    def __init__(self, root: Path, source: Path, mode: str, run_dir: Path | None, branch: str | None = None,
                 skipped_secret_files: list[str] | None = None, notes: list[str] | None = None):
        self.root = root.resolve()
        self.source = source.resolve()
        self.mode = mode
        self.run_dir = run_dir
        self.branch = branch
        self.skipped_secret_files = skipped_secret_files or []
        self.notes = notes or []

    # -- creation ------------------------------------------------------------
    @classmethod
    def readonly(cls, source: Path) -> "Workspace":
        src = Path(source).expanduser().resolve()
        if not src.is_dir():
            raise CouncilError(f"project folder not found: {src}", hint="Pass --project /path/to/your/project.", exit_code=EXIT_USAGE)
        return cls(src, src, "readonly", None)

    @classmethod
    def create(cls, source: Path, run_dir: Path, mode: str = "copy", run_id: str = "") -> "Workspace":
        src = Path(source).expanduser().resolve()
        if not src.is_dir():
            raise CouncilError(f"project folder not found: {src}", hint="Check the project path in plan.json.", exit_code=EXIT_USAGE)
        dest = (run_dir / "workspace").resolve()
        if dest.exists():
            raise CouncilError(f"workspace already exists: {dest}", hint="Each run gets a fresh workspace; start a new intake to run again.", exit_code=EXIT_USAGE)
        if mode == "copy":
            skipped = []
            for rel in _walk(src):
                if is_secret_name(rel):
                    skipped.append(rel)
                    continue
                _copy(src / rel, dest / rel)
            dest.mkdir(parents=True, exist_ok=True)
            ws = cls(dest, src, "copy", run_dir, skipped_secret_files=skipped)
        elif mode == "worktree":
            if not (src / ".git").exists():
                raise CouncilError(f"{src} is not a git repository", hint="Use mode 'copy', or run inside a git checkout.", exit_code=EXIT_USAGE)
            branch = f"ec-agent/{run_id or 'run'}"
            dirty = subprocess.run(["git", "-C", str(src), "status", "--porcelain"], capture_output=True, text=True).stdout.strip()
            r = subprocess.run(["git", "-C", str(src), "worktree", "add", "-b", branch, str(dest), "HEAD"], capture_output=True, text=True)
            if r.returncode != 0:
                raise CouncilError(f"could not create git worktree: {r.stderr.strip()[:300]}", hint="Check that the branch name is free and git works in the project.", exit_code=EXIT_USAGE)
            notes = [f"Created branch {branch} in a separate worktree; your current branch was not touched. Nothing was committed."]
            if dirty:
                notes.append("Your project had uncommitted changes; they are NOT in the worktree (only committed content is).")
            ws = cls(dest, src, "worktree", run_dir, branch=branch, notes=notes)
        else:
            raise CouncilError(f"unknown workspace mode '{mode}'", hint="Use 'copy' or 'worktree'.", exit_code=EXIT_USAGE)
        return ws

    # -- path safety -------------------------------------------------------------
    def resolve(self, rel: str, write: bool = False) -> Path:
        if not isinstance(rel, str) or not rel.strip():
            raise ToolError("path is required")
        rel = rel.strip()
        if os.path.isabs(rel) or rel.startswith("~"):
            raise ToolError(f"'{rel}' is absolute; use a path relative to the project root")
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise ToolError(f"'{rel}' is outside the project; only files inside the project are allowed")
        parts = set(Path(rel).parts)
        if parts & IGNORE_DIRS:
            raise ToolError(f"'{rel}' is inside an excluded folder ({', '.join(sorted(parts & IGNORE_DIRS))})")
        if is_secret_name(rel):
            raise ToolError(f"'{rel}' looks like a secrets file; the agent may not read or write it")
        if write and self.mode == "readonly":
            raise ToolError("this agent has read-only access")
        return p

    def files(self) -> list[str]:
        return [f for f in _walk(self.root) if not is_secret_name(f)]

    # -- snapshots / rollback ----------------------------------------------------
    def _snap_dir(self, name: str) -> Path:
        assert self.run_dir is not None
        return self.run_dir / "snapshots" / name

    def snapshot(self, name: str) -> dict[str, str]:
        d = self._snap_dir(name)
        if d.exists():
            shutil.rmtree(d)
        manifest = {}
        for rel in self.files():
            src = self.root / rel
            if src.stat().st_size > MAX_SNAPSHOT_BYTES:
                manifest[rel] = "large:" + hashlib.sha256(src.read_bytes()).hexdigest()
                continue
            _copy(src, d / rel)
            manifest[rel] = hashlib.sha256(src.read_bytes()).hexdigest()
        return manifest

    def restore(self, name: str) -> list[str]:
        """Make the workspace match snapshot ``name`` exactly; returns paths changed."""
        d = self._snap_dir(name)
        if not d.exists():
            raise CouncilError(f"no snapshot '{name}'", exit_code=EXIT_USAGE)
        snap = set(_walk(d))
        changed = []
        for rel in self.files():
            if rel not in snap and not (self.root / rel).stat().st_size > MAX_SNAPSHOT_BYTES:
                (self.root / rel).unlink()
                changed.append(rel)
        for rel in snap:
            src, tgt = d / rel, self.root / rel
            if not tgt.exists() or tgt.read_bytes() != src.read_bytes():
                _copy(src, tgt)
                changed.append(rel)
        return sorted(set(changed))

    def fingerprint(self) -> str:
        h = hashlib.sha256()
        for rel in self.files():
            h.update(rel.encode())
            h.update(hashlib.sha256((self.root / rel).read_bytes()).digest())
        return h.hexdigest()[:16]

    # -- diff -------------------------------------------------------------------------
    def diff(self, against: str = "baseline") -> tuple[str, list[dict[str, Any]]]:
        """Unified diff from snapshot ``against`` to the current workspace."""
        d = self._snap_dir(against)
        before = set(_walk(d))
        after = set(self.files())
        patch: list[str] = []
        changes: list[dict[str, Any]] = []
        for rel in sorted(before | after):
            a_path, b_path = d / rel, self.root / rel
            a = _read_text(a_path) if rel in before else ""
            b = _read_text(b_path) if rel in after else ""
            if rel in before and rel in after and a_path.read_bytes() == b_path.read_bytes():
                continue
            status = "added" if rel not in before else "deleted" if rel not in after else "modified"
            if a is None or b is None:
                changes.append({"path": rel, "status": status, "binary": True, "added": 0, "removed": 0})
                patch.append(f"Binary file {rel} {status}\n")
                continue
            lines = list(difflib.unified_diff(a.splitlines(keepends=True), b.splitlines(keepends=True),
                                              fromfile=f"a/{rel}" if rel in before else "/dev/null",
                                              tofile=f"b/{rel}" if rel in after else "/dev/null"))
            lines = [ln if ln.endswith("\n") else ln + "\n\\ No newline at end of file\n" for ln in lines]
            patch.extend(lines)
            changes.append({"path": rel, "status": status,
                            "added": sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++")),
                            "removed": sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))})
        return "".join(patch), changes
