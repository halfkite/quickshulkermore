#!/usr/bin/env python3
"""Publish verified release artifacts to Modrinth without rebuilding them."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

from package_fabric_build import ArtifactError, validate_fabric_jar

API = "https://api.modrinth.com/v2"
USER_AGENT = "halfkite/quickshulkermore (GitHub release publisher)"
DEPENDENCIES = {"fabric-api": "P7dR8mSH"}
RUNTIME_DEPENDENCIES = {"minecraft", "java", "fabricloader"}


def request(path, token, body=None, content_type=None):
    headers = {"User-Agent": USER_AGENT, "Authorization": token}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(API + path, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # Never print request headers or credentials, including in error paths.
        raise ArtifactError(f"Modrinth {req.method} {path}: HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise ArtifactError(f"Modrinth {req.method} {path}: connection failed") from exc


def release_plan(directory, project_id, changelog):
    manifest = json.loads((directory / "release-manifest.json").read_text(encoding="utf-8"))
    if not manifest.get("version") or not manifest.get("commit") or not manifest.get("artifacts"):
        raise ArtifactError("Incomplete verified release manifest")
    plans = []
    names = set()
    for entry in manifest["artifacts"]:
        name = entry["artifact"]
        if Path(name).name != name or "/" in name or "\\" in name or name in names:
            raise ArtifactError(f"Invalid or duplicate artifact name: {name}")
        names.add(name)
        path = directory / name
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ArtifactError(f"Release checksum mismatch: {name}")
        validate_fabric_jar(path)
        if entry["loader"] != "fabric" or not entry.get("minecraft_versions"):
            raise ArtifactError(f"Invalid target metadata: {name}")
        with zipfile.ZipFile(path) as archive:
            metadata = json.loads(archive.read("fabric.mod.json"))
        if metadata.get("version") != f"{manifest['version']}-{entry['minecraft_version']}":
            raise ArtifactError(f"JAR version does not match release: {name}")
        dependencies = []
        for dependency in metadata.get("depends", {}):
            if dependency in RUNTIME_DEPENDENCIES:
                continue
            if dependency not in DEPENDENCIES:
                raise ArtifactError(f"No Modrinth mapping for required dependency: {dependency}")
            dependencies.append({"project_id": DEPENDENCIES[dependency], "dependency_type": "required"})
        game_range = "-".join(dict.fromkeys((entry["minecraft_versions"][0], entry["minecraft_versions"][-1])))
        number = f"{manifest['version']}+mc{game_range}-fabric"
        if len(number) > 32:
            raise ArtifactError(f"Modrinth version number exceeds 32 characters: {number}")
        plans.append((path, {
            "project_id": project_id,
            "name": f"{manifest['version']} · Minecraft {game_range} · Fabric",
            "version_number": number,
            "changelog": changelog,
            "dependencies": dependencies,
            "game_versions": entry["minecraft_versions"],
            "loaders": ["fabric"],
            "version_type": "release",
            "featured": False,
            "file_parts": ["file"],
            "primary_file": "file",
        }))
    if {p.name for p in directory.glob("*.jar")} != names:
        raise ArtifactError("JAR set differs from the verified release manifest")
    return plans


def verify_remote(remote, path, payload):
    for field in ("project_id", "version_number", "changelog", "version_type"):
        if remote.get(field) != payload[field]:
            raise ArtifactError(f"Existing Modrinth version conflicts on {field}: {payload['version_number']}")
    for field in ("game_versions", "loaders"):
        if sorted(remote.get(field, [])) != sorted(payload[field]):
            raise ArtifactError(f"Existing Modrinth version conflicts on {field}: {payload['version_number']}")
    dependencies = {(d.get("project_id"), d.get("dependency_type")) for d in remote.get("dependencies", [])}
    expected = {(d["project_id"], d["dependency_type"]) for d in payload["dependencies"]}
    if dependencies != expected:
        raise ArtifactError(f"Existing Modrinth dependencies conflict: {payload['version_number']}")
    files = remote.get("files", [])
    if (len(files) != 1 or files[0].get("filename") != path.name
            or files[0].get("hashes", {}).get("sha512") != hashlib.sha512(path.read_bytes()).hexdigest()):
        raise ArtifactError(f"Existing Modrinth file conflicts: {payload['version_number']}")


def multipart(path, payload):
    boundary = "quickshulkermore-" + uuid.uuid4().hex
    prefix = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"data\"\r\n"
              "Content-Type: application/json\r\n\r\n").encode()
    file_header = (f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                   f"filename=\"{path.name}\"\r\nContent-Type: application/java-archive\r\n\r\n").encode()
    return (prefix + json.dumps(payload, ensure_ascii=False).encode("utf-8") + file_header
            + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode(),
            f"multipart/form-data; boundary={boundary}")


def publish(plans, token):
    project_id = plans[0][1]["project_id"]
    project = request(f"/project/{urllib.parse.quote(project_id, safe='')}", token)
    print(f"Modrinth project: {project['slug']} (status: {project['status']})")
    available = {tag["version"] for tag in request("/tag/game_version", token)}
    for _, payload in plans:
        if not set(payload["game_versions"]) <= available:
            raise ArtifactError(f"Unknown Modrinth Minecraft version: {payload['game_versions']}")
    existing = request(f"/project/{project_id}/version", token)
    pending = []
    # Preflight every existing version before creating anything. Re-running after
    # a partial upload is safe; differing files or metadata require intervention.
    for path, payload in plans:
        matches = [v for v in existing if v["version_number"] == payload["version_number"]]
        if len(matches) > 1:
            raise ArtifactError(f"Duplicate remote version: {payload['version_number']}")
        if matches:
            verify_remote(matches[0], path, payload)
            print(f"Already verified: {path.name}")
        else:
            pending.append((path, payload))
    for path, payload in pending:
        body, content_type = multipart(path, payload)
        remote = request("/version", token, body, content_type)
        verify_remote(request(f"/version/{remote['id']}", token), path, payload)
        print(f"Published and verified: {path.name} (version {remote['id']})", flush=True)
    print(f"Verified {len(plans)} Modrinth versions; uploaded {len(pending)}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--changelog-file", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        with args.changelog_file.open(encoding="utf-8", newline="") as stream:
            changelog = stream.read()
        plans = release_plan(args.artifact_dir, args.project_id, changelog)
        if args.dry_run:
            for path, payload in plans:
                print(json.dumps({"file": path.name, **payload}, ensure_ascii=False))
        else:
            token = os.environ.get("MODRINTH_TOKEN", "").strip()
            if not token:
                raise ArtifactError("MODRINTH_TOKEN is required")
            publish(plans, token)
    except (ArtifactError, OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        print(f"Modrinth publish failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
