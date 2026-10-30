#!/usr/bin/env python3
"""Verify, install, activate, and roll back immutable realm-script releases."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import uuid
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from realm_scripts.release_contract import ContractError, verify_release_bundle  # noqa: E402

SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class ReleaseError(RuntimeError):
    pass


def verify(bundle: Path) -> str:
    try:
        release, _ = verify_release_bundle(bundle)
    except ContractError as exc:
        raise ReleaseError(str(exc)) from exc
    return release


def _root(path: Path) -> Path:
    resolved = path.resolve()
    if resolved == Path("/") or len(resolved.parts) < 3:
        raise ReleaseError(f"refusing unsafe release root: {resolved}")
    return resolved


def install(bundle: Path, root: Path) -> str:
    release = verify(bundle)
    root = _root(root)
    releases = root / "releases"
    destination = releases / release
    if destination.exists() or destination.is_symlink():
        raise ReleaseError(f"refusing to overwrite existing release: {destination}")

    releases.mkdir(parents=True, exist_ok=True)
    staging = releases / f".staging-{release}-{uuid.uuid4().hex}"
    try:
        shutil.copytree(bundle.resolve(), staging)
        verify(staging)
        os.rename(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return release


def _target(root: Path, release: str) -> Path:
    if SAFE_NAME.fullmatch(release) is None:
        raise ReleaseError(f"unsafe release name: {release!r}")
    target = root / "releases" / release
    if not target.is_dir() or target.is_symlink():
        raise ReleaseError(f"installed release does not exist: {target}")
    verify(target)
    return target


def _replace_link(link: Path, target: Path) -> None:
    temporary = link.parent / f".{link.name}-{uuid.uuid4().hex}"
    temporary.symlink_to(target)
    os.replace(temporary, link)


def activate(release: str, root: Path) -> None:
    root = _root(root)
    target = _target(root, release)
    current = root / "current"
    previous = root / "previous"
    old_target = current.resolve(strict=True) if current.is_symlink() else None
    if current.exists() and not current.is_symlink():
        raise ReleaseError(f"current selector is not a symlink: {current}")
    if previous.exists() and not previous.is_symlink():
        raise ReleaseError(f"previous selector is not a symlink: {previous}")
    if old_target is not None and old_target != target:
        _replace_link(previous, old_target)
    _replace_link(current, target)


def rollback(root: Path) -> None:
    root = _root(root)
    current = root / "current"
    previous = root / "previous"
    if not current.is_symlink() or not previous.is_symlink():
        raise ReleaseError("rollback requires current and previous symlinks")
    current_target = current.resolve(strict=True)
    previous_target = previous.resolve(strict=True)
    _target(root, current_target.name)
    _target(root, previous_target.name)
    _replace_link(current, previous_target)
    _replace_link(previous, current_target)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("bundle", type=Path)
    install_parser = commands.add_parser("install")
    install_parser.add_argument("bundle", type=Path)
    install_parser.add_argument("--root", type=Path, required=True)
    activate_parser = commands.add_parser("activate")
    activate_parser.add_argument("release")
    activate_parser.add_argument("--root", type=Path, required=True)
    rollback_parser = commands.add_parser("rollback")
    rollback_parser.add_argument("--root", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "verify":
            print(verify(args.bundle))
        elif args.command == "install":
            print(install(args.bundle, args.root))
        elif args.command == "activate":
            activate(args.release, args.root)
            print(args.release)
        elif args.command == "rollback":
            rollback(args.root)
            print((args.root / "current").resolve(strict=True).name)
    except ReleaseError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
