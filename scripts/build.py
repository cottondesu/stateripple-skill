#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = []
# ///
# --- How to run ---
# python3 scripts/build.py
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path
from typing import Dict, List

VERSION = "0.1.0"
ARCHIVE_NAME = "stateripple-skill-v0.1.0.zip"
REQUIRED_FILES = {
    "SKILL.md",
    "references/cli-contract.md",
    "references/result-interpretation.md",
    "references/routing.md",
    "references/safety-contract.md",
}


class BuildError(RuntimeError):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def package_entries(source: Path) -> List[Path]:
    """Every file, symlink and special entry under the package source, with nothing ignored.

    The build and the release gate's source digest both start from this list, so a stray file
    that stops the build can never be invisible to the digest.
    """
    if not source.is_dir() or source.is_symlink():
        raise BuildError("src/stateripple must be a real directory")
    return [path for path in sorted(source.rglob("*")) if path.is_symlink() or not path.is_dir()]


def package_source_problems(project_root: Path) -> List[str]:
    """Reasons the package source cannot be built as it stands; empty when it can."""
    source = project_root / "src" / "stateripple"
    try:
        entries = package_entries(source)
    except BuildError as error:
        return [str(error)]
    problems = []
    present = set()
    for path in entries:
        relative = path.relative_to(source).as_posix()
        if path.is_symlink():
            problems.append("symlinks are forbidden in the package: %s" % relative)
        elif not path.is_file() or relative not in REQUIRED_FILES:
            problems.append("unexpected package source: %s" % relative)
        else:
            present.add(relative)
    problems.extend("missing package source: %s" % relative for relative in sorted(REQUIRED_FILES - present))
    return problems


def _source_files(source: Path) -> List[Path]:
    problems = package_source_problems(source.parent.parent)
    if problems:
        raise BuildError("; ".join(problems))
    return [path for path in package_entries(source)]


def _assert_managed_output(dist: Path) -> None:
    if not dist.exists():
        return
    if dist.is_symlink() or not dist.is_dir():
        raise BuildError("dist must be a real directory")
    manifest_path = dist / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        if any(dist.iterdir()):
            raise BuildError("refusing to replace an unmanaged dist directory")
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get('schema_version') != 1 or manifest.get('skill') != 'stateripple' or manifest.get('version') != VERSION:
            raise BuildError('existing build manifest is not this package')
        if {entry['path'] for entry in manifest['files']} != {'stateripple/' + path for path in REQUIRED_FILES} or manifest['archive']['path'] != ARCHIVE_NAME:
            raise BuildError('existing manifest paths violate package allowlist')
        managed = {entry["path"] for entry in manifest["files"]}
        managed.update({manifest["archive"]["path"], "manifest.json"})
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise BuildError("existing build manifest is invalid") from error
    actual = {
        path.relative_to(dist).as_posix()
        for path in dist.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if any(path.is_symlink() for path in dist.rglob("*")):
        raise BuildError("refusing to replace a dist tree containing symlinks")
    if actual != managed:
        raise BuildError("refusing to replace dist with unmanaged content")
    directories = {path.relative_to(dist).as_posix() for path in dist.rglob('*') if path.is_dir()}
    if directories != {'stateripple','stateripple/references'}:
        raise BuildError('refusing to remove unmanaged output directories')
    for entry in manifest['files'] + [manifest['archive']]:
        if _sha256((dist / entry['path']).read_bytes()) != entry['sha256']:
            raise BuildError('refusing to replace modified generated output: ' + entry['path'])


def verify_distribution(project_root: Path) -> None:
    dist = project_root / 'dist'
    _assert_managed_output(dist)
    if not (dist / 'manifest.json').is_file():
        raise BuildError('distribution is missing; run build')
    manifest = json.loads((dist / 'manifest.json').read_text(encoding='utf-8'))
    for entry in manifest['files']:
        relative = Path(entry['path']).relative_to('stateripple')
        if (dist / entry['path']).read_bytes() != (project_root / 'src/stateripple' / relative).read_bytes():
            raise BuildError('distribution differs from canonical source: ' + str(relative))
    with zipfile.ZipFile(dist / ARCHIVE_NAME) as archive:
        if archive.namelist() != [entry['path'] for entry in manifest['files']]:
            raise BuildError('archive member allowlist mismatch')
        for entry in manifest['files']:
            if archive.read(entry['path']) != (dist / entry['path']).read_bytes():
                raise BuildError('archive bytes disagree with package')


def build(project_root: Path) -> Dict[str, str]:
    """Build and return stable hashes for the package and archive."""
    source = project_root / "src" / "stateripple"
    files = _source_files(source)
    dist = project_root / "dist"
    _assert_managed_output(dist)
    with tempfile.TemporaryDirectory(prefix=".build-", dir=project_root) as temp_name:
        stage = Path(temp_name) / "dist"
        package = stage / "stateripple"
        package.mkdir(parents=True)
        entries = []
        for source_file in files:
            relative = source_file.relative_to(source)
            target = package / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            data = source_file.read_bytes()
            target.write_bytes(data)
            target.chmod(0o644)
            entries.append(
                {
                    "path": "stateripple/%s" % relative.as_posix(),
                    "sha256": _sha256(data),
                    "size": len(data),
                }
            )
        archive = stage / ARCHIVE_NAME
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
            for entry in entries:
                data = (stage / entry["path"]).read_bytes()
                info = zipfile.ZipInfo(entry["path"], (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                bundle.writestr(info, data)
        archive_data = archive.read_bytes()
        manifest = {
            "schema_version": 1,
            "skill": "stateripple",
            "version": VERSION,
            "files": entries,
            "archive": {
                "path": ARCHIVE_NAME,
                "sha256": _sha256(archive_data),
                "size": len(archive_data),
            },
        }
        (stage / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        if dist.exists():
            shutil.rmtree(dist)
        os.replace(stage, dist)
    return {"archive": manifest["archive"]["sha256"], "manifest": _sha256((dist / "manifest.json").read_bytes())}


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    try:
        hashes = build(root)
    except (BuildError, OSError) as error:
        print(json.dumps({"status": "FAIL", "error": str(error)}, sort_keys=True))
        return 1
    print(json.dumps({"status": "PASS", "version": VERSION, **hashes}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
