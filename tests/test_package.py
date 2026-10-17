from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts import build


class PackageBuildTests(unittest.TestCase):
    def _project(self, parent: Path) -> Path:
        root = parent / "project"
        references = root / "src" / "stateripple" / "references"
        references.mkdir(parents=True)
        (root / "src" / "stateripple" / "SKILL.md").write_text("skill\n", encoding="utf-8")
        for name in ("cli-contract.md", "result-interpretation.md", "routing.md", "safety-contract.md"):
            (references / name).write_text(name + "\n", encoding="utf-8")
        return root

    def test_build_is_deterministic_when_repeated(self) -> None:
        # Given: the same canonical Skill sources.
        with tempfile.TemporaryDirectory() as temporary:
            root = self._project(Path(temporary))

            # When: the distribution is built twice.
            first = build.build(root)
            first_archive = (root / "dist" / build.ARCHIVE_NAME).read_bytes()
            second = build.build(root)

            # Then: hashes and archive bytes remain identical.
            self.assertEqual(first, second)
            self.assertEqual(first_archive, (root / "dist" / build.ARCHIVE_NAME).read_bytes())

    def test_build_refuses_unmanaged_output(self) -> None:
        # Given: user-owned content in an unmanaged dist directory.
        with tempfile.TemporaryDirectory() as temporary:
            root = self._project(Path(temporary))
            (root / "dist").mkdir()
            (root / "dist" / "user.txt").write_text("preserve\n", encoding="utf-8")

            # When/Then: build refuses replacement and preserves the bytes.
            with self.assertRaises(build.BuildError):
                build.build(root)
            self.assertEqual((root / "dist" / "user.txt").read_text(encoding="utf-8"), "preserve\n")

    def test_archive_contains_only_allowlisted_sources(self) -> None:
        # Given: a valid source tree.
        with tempfile.TemporaryDirectory() as temporary:
            root = self._project(Path(temporary))

            # When: the package is built.
            build.build(root)

            # Then: the archive names match the signed manifest file list.
            manifest = json.loads((root / "dist" / "manifest.json").read_text(encoding="utf-8"))
            with zipfile.ZipFile(root / "dist" / build.ARCHIVE_NAME) as archive:
                self.assertEqual(archive.namelist(), [entry["path"] for entry in manifest["files"]])

    def test_build_preserves_modified_previously_generated_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._project(Path(temporary))
            build.build(root)
            output = root / 'dist/stateripple/SKILL.md'
            output.write_text('user modification\n')
            with self.assertRaises(build.BuildError):
                build.build(root)
            self.assertEqual(output.read_text(),'user modification\n')

    def test_build_preserves_extra_empty_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._project(Path(temporary))
            build.build(root)
            extra = root / 'dist/user-directory'
            extra.mkdir()
            with self.assertRaises(build.BuildError):
                build.build(root)
            self.assertTrue(extra.is_dir())

    def test_package_integrity_rejects_tampering_and_unexpected_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._project(Path(temporary))
            build.build(root)
            build.verify_distribution(root)
            (root / 'dist/stateripple/SKILL.md').write_text('altered\n')
            with self.assertRaises(build.BuildError):
                build.verify_distribution(root)
            (root / 'src/stateripple/references/private.md').write_text('do not ship\n')
            with self.assertRaises(build.BuildError):
                build.build(root)


if __name__ == "__main__":
    unittest.main()
