import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from package_fabric_build import ArtifactError
from publish_modrinth import multipart, publish, release_plan, verify_remote


class ModrinthPublishingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jar = self.root / "quickshulkermore-1.0.2-mc26.1-26.1.2-fabric.jar"
        with zipfile.ZipFile(self.jar, "w") as archive:
            archive.writestr("fabric.mod.json", json.dumps({
                "id": "quickshulker", "version": "1.0.2-26.1",
                "depends": {"minecraft": "~26.1", "fabricloader": ">=0.18.4", "fabric-api": "*"},
            }))
        self.manifest = {"version": "1.0.2", "commit": "abc", "artifacts": [{
            "artifact": self.jar.name, "sha256": hashlib.sha256(self.jar.read_bytes()).hexdigest(),
            "minecraft_version": "26.1", "minecraft_versions": ["26.1", "26.1.1", "26.1.2"],
            "loader": "fabric",
        }]}
        self.write_manifest()
        self.notes = "中文更新日志\r\n\r\n- 保持原文。\r\n"

    def write_manifest(self):
        (self.root / "release-manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def plan(self):
        return release_plan(self.root, "GTAQipTs", self.notes)

    def remote(self, payload):
        return {**payload, "id": "version-id", "files": [{
            "filename": self.jar.name,
            "hashes": {"sha512": hashlib.sha512(self.jar.read_bytes()).hexdigest()},
        }]}

    def test_shared_range_changelog_and_required_dependency(self):
        _, payload = self.plan()[0]
        self.assertEqual(payload["game_versions"], ["26.1", "26.1.1", "26.1.2"])
        self.assertEqual(payload["changelog"], self.notes)
        self.assertEqual(payload["version_number"], "1.0.2+mc26.1-26.1.2-fabric")
        self.assertEqual(payload["dependencies"], [{"project_id": "P7dR8mSH", "dependency_type": "required"}])

    def test_corrupt_jar_is_rejected_before_upload(self):
        self.jar.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ArtifactError, "checksum mismatch"):
            self.plan()

    def test_path_traversal_and_duplicate_names_rejected(self):
        self.manifest["artifacts"][0]["artifact"] = "../outside.jar"
        self.write_manifest()
        with self.assertRaisesRegex(ArtifactError, "Invalid or duplicate"):
            self.plan()
        self.manifest["artifacts"][0]["artifact"] = self.jar.name
        self.manifest["artifacts"].append(self.manifest["artifacts"][0].copy())
        self.write_manifest()
        with self.assertRaisesRegex(ArtifactError, "Invalid or duplicate"):
            self.plan()

    def test_empty_release_rejected(self):
        self.manifest["artifacts"] = []
        self.write_manifest()
        with self.assertRaisesRegex(ArtifactError, "Incomplete"):
            self.plan()

    def test_remote_hash_or_metadata_conflict_rejected(self):
        path, payload = self.plan()[0]
        remote = self.remote(payload)
        verify_remote(remote, path, payload)
        remote["files"][0]["hashes"]["sha512"] = "wrong"
        with self.assertRaisesRegex(ArtifactError, "file conflicts"):
            verify_remote(remote, path, payload)
        remote = self.remote(payload)
        remote["game_versions"] = ["26.1"]
        with self.assertRaisesRegex(ArtifactError, "game_versions"):
            verify_remote(remote, path, payload)

    def test_rerun_skips_matching_remote_version(self):
        plans = self.plan()
        with patch("publish_modrinth.request", side_effect=[
            {"slug": "quickshulkermore", "status": "draft"},
            [{"version": v} for v in plans[0][1]["game_versions"]],
            [self.remote(plans[0][1])],
        ]) as api:
            publish(plans, "not-a-real-token")
        self.assertEqual(api.call_count, 3)
        self.assertTrue(all(len(call.args) == 2 for call in api.call_args_list))

    def test_new_upload_is_read_back_and_verified(self):
        plans = self.plan()
        with patch("publish_modrinth.request", side_effect=[
            {"slug": "quickshulkermore", "status": "draft"},
            [{"version": v} for v in plans[0][1]["game_versions"]], [],
            {"id": "new-version"}, self.remote(plans[0][1]),
        ]) as api:
            publish(plans, "not-a-real-token")
        self.assertEqual(api.call_args_list[3].args[0], "/version")
        self.assertEqual(api.call_args_list[4].args[0], "/version/new-version")

    def test_multipart_preserves_utf8_and_binary(self):
        path, payload = self.plan()[0]
        body, content_type = multipart(path, payload)
        self.assertIn(self.notes.encode("utf-8").replace(b"\r\n", b"\\r\\n"), body)
        self.assertIn(self.jar.read_bytes(), body)
        self.assertIn("boundary=", content_type)


if __name__ == "__main__":
    unittest.main()
