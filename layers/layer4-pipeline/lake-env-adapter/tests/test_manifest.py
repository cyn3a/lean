from __future__ import annotations

import unittest

from lake_env_adapter.manifest import (
    compose_target_manifest,
    convert_package,
    find_package,
    manifest_version,
    package_input_revision,
    package_name,
    package_revision,
    with_revision,
)


class ManifestTests(unittest.TestCase):
    def test_flat_entry_revision(self) -> None:
        package = {"name": "alpha", "type": "git", "rev": "old", "inputRev": "tag"}
        updated = with_revision(package, "new")
        self.assertEqual(package_revision(updated), "new")
        self.assertEqual(package_input_revision(updated), "new")
        self.assertEqual(package_revision(package), "old")

    def test_legacy_entry_revision(self) -> None:
        package = {
            "git": {
                "name": "alpha",
                "url": "https://example.test/alpha",
                "rev": "old",
                "inputRev?": "tag",
            }
        }
        updated = with_revision(package, "new")
        self.assertEqual(package_name(updated), "alpha")
        self.assertEqual(package_revision(updated), "new")
        self.assertEqual(package_input_revision(updated), "new")

    def test_scoped_lookup_falls_back_for_unscoped_legacy_entry(self) -> None:
        manifest = {"packages": [{"name": "alpha", "scope": "", "type": "git"}]}
        self.assertIsNotNone(find_package(manifest, "alpha", "acme"))

    def test_manifest_version_orders_integer_and_string_forms(self) -> None:
        self.assertEqual(manifest_version({"version": 5}), (0, 5, 0))
        self.assertEqual(manifest_version({"version": "1.1.0"}), (1, 1, 0))
        self.assertLess(manifest_version({"version": 7}), manifest_version({"version": "1.0.0"}))

    def test_convert_legacy_record_to_flat_and_back(self) -> None:
        legacy = {
            "git": {
                "url": "https://github.com/leanprover/std4/",
                "subDir?": None,
                "rev": "e8c27f7d",
                "opts": {},
                "name": "std",
                "inputRev?": "main",
                "inherited": False,
            }
        }
        flat = convert_package(legacy, (1, 1, 0))
        self.assertEqual(
            flat,
            {
                "type": "git",
                "name": "std",
                "scope": "",
                "inherited": False,
                "url": "https://github.com/leanprover/std4/",
                "rev": "e8c27f7d",
                "inputRev": "main",
                "subDir": None,
            },
        )
        self.assertNotIn("scope", convert_package(legacy, (0, 7, 0)))
        self.assertEqual(convert_package(legacy, (0, 5, 0)), legacy)
        nested = convert_package(flat, (0, 6, 0))
        self.assertEqual(nested["git"]["inputRev?"], "main")
        self.assertEqual(nested["git"]["rev"], "e8c27f7d")
        self.assertIs(convert_package(flat, (1, 1, 0)) is flat, False)

    def test_compose_target_manifest_uses_template_layout(self) -> None:
        old = {
            "version": 5,
            "packagesDir": "lake-packages",
            "packages": [
                {"git": {"name": "std", "url": "u", "rev": "old", "inputRev?": "main",
                         "subDir?": None, "opts": {}, "inherited": False}},
                {"git": {"name": "old-transitive", "url": "t", "rev": "x", "inputRev?": None,
                         "subDir?": None, "opts": {}, "inherited": True}},
            ],
        }
        selected = {"type": "git", "name": "std", "scope": "", "url": "u", "rev": "new",
                    "inputRev": "main", "subDir": None, "inherited": False}
        template = {
            "version": "1.1.0",
            "packagesDir": ".lake/packages",
            "name": "demo",
            "lakeDir": ".lake",
            "packages": [
                {"type": "git", "name": "batteries", "scope": "", "url": "b", "rev": "bb",
                 "inputRev": "main", "subDir": None, "inherited": True},
                selected,
            ],
        }
        composed = compose_target_manifest(old, [("std", "", selected)], template)
        self.assertEqual(composed["version"], "1.1.0")
        self.assertEqual(composed["lakeDir"], ".lake")
        self.assertEqual(composed["packagesDir"], ".lake/packages")
        self.assertEqual(
            [(p["name"], p["inherited"]) for p in composed["packages"]],
            [("std", False), ("batteries", True)],
        )
        self.assertEqual(composed["packages"][0]["rev"], "new")
        self.assertNotIn("git", composed["packages"][0])
        # Without a template the old behaviour (old layout) is kept.
        untouched = compose_target_manifest(old, [("std", "", selected)], None)
        self.assertEqual(untouched["version"], 5)

    def test_lookup_matches_lake_quoted_identifier(self) -> None:
        package = {
            "name": "«lean-slides»",
            "scope": "0art0",
            "type": "git",
            "rev": "abc123",
        }
        manifest = {"packages": [package]}
        self.assertIs(find_package(manifest, "lean-slides", "0art0"), package)


if __name__ == "__main__":
    unittest.main()
