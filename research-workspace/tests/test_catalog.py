"""Offline allowlist and packaged-source trust boundary checks."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from research_workspace import catalog


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.document = json.loads((catalog.ROOT / "environments.json").read_text())
        shutil.copytree(catalog.ROOT / "recipes", self.root / "recipes")
        self.write()
        self.root_patch = patch.object(catalog, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def write(self):
        (self.root / "environments.json").write_text(json.dumps(self.document))

    def reject(self):
        self.write()
        with self.assertRaises(ValueError):
            catalog.list_environments()

    def test_active_availability_requires_exact_configured_image(self):
        self.assertFalse(any(entry["available"] for entry in catalog.list_environments()))
        images = {entry["runtime"]["id"]: entry["image"] for entry in self.document["environments"]}
        entries = catalog.list_environments(images)
        self.assertEqual([entry["id"] for entry in entries if entry["available"]],
                         ["summary-python-v1", "summary-r-v2"])
        images["r"] = "r-base@sha256:" + "f" * 64
        self.assertEqual([entry["id"] for entry in catalog.list_environments(images) if entry["available"]],
                         ["summary-python-v1"])

    def test_resolves_archived_exact_source_without_substituting_corrected_r(self):
        archived = self.document["environments"][2]
        entry = catalog.resolve_environment(archived["recipe"], archived["recipe_sha256"], archived["image"])
        self.assertEqual(entry["status"], "archived")
        content = catalog.recipe_source(entry)
        self.assertEqual(hashlib.sha256(content).hexdigest(), "91db760044f6879d8171d34db57d5138e9255ce4d16bf489c919990dc65da3fd")
        self.assertNotEqual(content, (self.root / "recipes/recipe.R").read_bytes())
        with self.assertRaises(ValueError):
            catalog.resolve_environment(entry["recipe"], "0" * 64, entry["image"])
        with self.assertRaises(ValueError):
            catalog.resolve_environment(entry["recipe"], entry["recipe_sha256"], "r-base@sha256:" + "0" * 64)

    def test_returned_records_are_independent_copies(self):
        entry = catalog.list_environments()[0]
        entry["limits"]["cpus"] = 999
        self.assertEqual(catalog.list_environments()[0]["limits"]["cpus"], 1)

    def test_source_is_rechecked_after_resolution(self):
        entry = catalog.list_environments()[0]
        (self.root / entry["source"]).write_text("print('untrusted replacement')\n")
        with self.assertRaises(ValueError):
            catalog.recipe_source(entry)

    def test_caller_cannot_supply_a_path_or_modified_metadata(self):
        for key, value in (("source", "recipes/recipe.R"), ("name", "Unreviewed name"), ("extra", "unexpected"), ("available", "yes")):
            with self.subTest(key=key):
                entry = catalog.list_environments()[0]
                entry[key] = value
                with self.assertRaises(ValueError):
                    catalog.recipe_source(entry)

    def test_rejects_unexpected_fields_and_schema(self):
        original = deepcopy(self.document)
        for place, key, value in (("catalog", "extra", 1), ("catalog", "schema_version", True),
                                  ("entry", "extra", 1), ("entry", "version", True)):
            with self.subTest(place=place, key=key):
                self.document = deepcopy(original)
                target = self.document if place == "catalog" else self.document["environments"][0]
                target[key] = value
                self.reject()

    def test_duplicate_json_keys_and_nonfinite_numbers_are_rejected(self):
        for data in ('{"schema_version":1,"schema_version":1,"environments":[]}',
                     '{"schema_version":NaN,"environments":[]}',
                     '{"schema_version":Infinity,"environments":[]}'):
            with self.subTest(data=data):
                (self.root / "environments.json").write_text(data)
                with self.assertRaises(ValueError):
                    catalog.list_environments()

    def test_malformed_or_oversized_catalog_is_rejected(self):
        for content in (b"\xff", b"{" + b" " * catalog.MAX_CATALOG_BYTES, b""):
            with self.subTest(size=len(content)):
                (self.root / "environments.json").write_bytes(content)
                with self.assertRaises(ValueError):
                    catalog.list_environments()

    def test_catalog_symlink_is_rejected(self):
        (self.root / "environments.json").rename(self.root / "outside.json")
        (self.root / "environments.json").symlink_to(self.root / "outside.json")
        with self.assertRaises(ValueError):
            catalog.list_environments()

    def test_unsafe_recipe_paths_are_rejected(self):
        original = self.document["environments"][0]["source"]
        for path in ("../recipes/recipe.py", "/tmp/recipe.py", "recipes//recipe.py", "recipes/../recipe.py", "recipes\\recipe.py", "other/recipe.py"):
            with self.subTest(path=path):
                self.document["environments"][0]["source"] = path
                self.reject()
        self.document["environments"][0]["source"] = original

    def test_leaf_and_parent_symlinks_are_rejected(self):
        source = self.root / "recipes/recipe.py"
        source.rename(self.root / "outside.py")
        source.symlink_to(self.root / "outside.py")
        with self.assertRaises(ValueError):
            catalog.list_environments()
        source.unlink()
        (self.root / "outside.py").rename(source)
        (self.root / "recipes").rename(self.root / "outside-recipes")
        (self.root / "recipes").symlink_to(self.root / "outside-recipes", target_is_directory=True)
        with self.assertRaises(ValueError):
            catalog.list_environments()

    def test_special_and_oversized_recipe_files_are_rejected(self):
        source = self.root / "recipes/recipe.py"
        source.unlink()
        os.mkfifo(source)
        with self.assertRaises(ValueError):
            catalog.list_environments()
        source.unlink()
        source.write_bytes(b"#" * (catalog.MAX_RECIPE_BYTES + 1))
        self.document["environments"][0]["recipe_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        self.reject()

    def test_mutable_images_and_wrong_runtime_are_rejected(self):
        original = deepcopy(self.document)
        for image in ("python:3.12", "python@sha256:" + "A" * 64, "python@sha256:abc", "sha256:" + "0" * 64):
            with self.subTest(image=image):
                self.document = deepcopy(original)
                self.document["environments"][0]["image"] = image
                self.reject()
        self.document = deepcopy(original)
        self.document["environments"][0]["runtime"]["id"] = "r"
        self.reject()

    def test_limits_and_validation_cannot_drift_from_execution_contract(self):
        original = deepcopy(self.document)
        for key, value in (("cpus", True), ("timeout_seconds", 31), ("memory_mb", 512), ("max_input_rows", 1001)):
            with self.subTest(key=key):
                self.document = deepcopy(original)
                self.document["environments"][0]["limits"][key] = value
                self.reject()
        self.document = deepcopy(original)
        self.document["environments"][0]["validation"]["relative_tolerance"] = 0.01
        self.reject()

    def test_unreviewed_dependencies_or_false_rebuild_claim_are_rejected(self):
        original = deepcopy(self.document)
        self.document["environments"][0]["dependencies"]["additional_packages"] = ["numpy"]
        self.reject()
        self.document = deepcopy(original)
        self.document["environments"][0]["provenance"]["clean_rebuild"]["status"] = "PASS"
        self.reject()

    def test_duplicate_identity_or_missing_active_version_is_rejected(self):
        original = deepcopy(self.document)
        duplicate = deepcopy(self.document["environments"][0])
        duplicate.update(id="summary-python-v2", version=2, status="archived")
        self.document["environments"].append(duplicate)
        self.reject()
        self.document = deepcopy(original)
        self.document["environments"][0]["status"] = "archived"
        self.reject()

    def test_configured_image_mapping_is_bounded(self):
        for value in ([], {"node": "node@sha256:" + "0" * 64}, {"python": "python:latest"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                catalog.list_environments(value)


if __name__ == "__main__":
    unittest.main()
