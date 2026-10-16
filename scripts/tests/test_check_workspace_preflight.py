"""Unit tests for scripts/check_workspace_preflight.py (RHIDP-17923)."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import pytest
import yaml

from scripts.check_workspace_preflight import (
    check_backstage_compatibility,
    check_package_metadata,
    get_minor_version,
    run_preflight,
)

ROOT = Path(__file__).resolve().parents[2]


def test_get_minor_version():
    assert get_minor_version("1.54.9") == "1.54"
    assert get_minor_version("1.52.0-rc.1") == "1.52"
    assert get_minor_version("2.0.1") == "2.0"
    assert get_minor_version("") == ""
    assert get_minor_version(None) == ""


def test_check_backstage_compatibility_real_tech_radar():
    ws_path = ROOT / "workspaces/tech-radar"
    res = check_backstage_compatibility(ws_path, target_backstage_version="1.54.9")
    assert res["passed"] is True
    assert res["is_minor_match"] is True
    assert res["source_version"] == "1.54.5"
    assert res["override_version"] is None
    assert res["effective_version"] == "1.54.5"


def test_check_backstage_compatibility_override_real_roadie():
    ws_path = ROOT / "workspaces/roadie-backstage-plugins"
    res = check_backstage_compatibility(ws_path, target_backstage_version="1.54.9")
    assert res["passed"] is True
    assert res["is_minor_match"] is True
    assert res["source_version"] == "1.55.2"
    assert res["override_version"] == "1.54.6"
    assert res["effective_version"] == "1.54.6"


def test_check_backstage_compatibility_mismatch_real_x2a():
    ws_path = ROOT / "workspaces/x2a"
    res = check_backstage_compatibility(ws_path, target_backstage_version="1.54.9")
    assert res["passed"] is False
    assert res["is_minor_match"] is False
    assert res["reason"] == "backstage_version_mismatch"
    assert "Backstage version mismatch" in res["error"]


def test_check_backstage_compatibility_allow_incompatible():
    ws_path = ROOT / "workspaces/x2a"
    res = check_backstage_compatibility(
        ws_path, target_backstage_version="1.54.9", allow_incompatible=True
    )
    assert res["passed"] is True
    assert res["is_minor_match"] is False


def test_check_backstage_compatibility_missing_source_json(tmp_path: Path):
    ws = tmp_path / "workspaces/test-ws"
    ws.mkdir(parents=True)
    res = check_backstage_compatibility(ws, target_backstage_version="1.54.9")
    assert res["passed"] is False
    assert res["reason"] == "invalid_metadata_schema"
    assert "Missing required file: source.json" in res["error"]


def test_check_package_metadata_real_tech_radar():
    ws_path = ROOT / "workspaces/tech-radar"
    res = check_package_metadata(ws_path)
    assert res["passed"] is True
    assert res["file_count"] == 2
    assert len(res["errors"]) == 0


def test_check_package_metadata_missing_plugins_list(tmp_path: Path):
    ws = tmp_path / "workspaces/test-ws"
    ws.mkdir(parents=True)
    (ws / "metadata").mkdir()
    res = check_package_metadata(ws)
    assert res["passed"] is False
    assert res["reason"] == "invalid_metadata_schema"
    assert any("plugins-list.yaml" in e for e in res["errors"])


def test_check_package_metadata_missing_metadata_dir(tmp_path: Path):
    ws = tmp_path / "workspaces/test-ws"
    ws.mkdir(parents=True)
    (ws / "plugins-list.yaml").write_text("plugins/test:\n", encoding="utf-8")
    res = check_package_metadata(ws)
    assert res["passed"] is False
    assert res["reason"] == "missing_package_metadata"


def test_check_package_metadata_empty_metadata_dir(tmp_path: Path):
    ws = tmp_path / "workspaces/test-ws"
    ws.mkdir(parents=True)
    (ws / "plugins-list.yaml").write_text("plugins/test:\n", encoding="utf-8")
    (ws / "metadata").mkdir()
    res = check_package_metadata(ws)
    assert res["passed"] is False
    assert res["reason"] == "missing_package_metadata"


def test_check_package_metadata_invalid_schemas(tmp_path: Path):
    ws = tmp_path / "workspaces/test-ws"
    ws.mkdir(parents=True)
    (ws / "plugins-list.yaml").write_text("plugins/test:\n", encoding="utf-8")
    meta_dir = ws / "metadata"
    meta_dir.mkdir()

    # 1. Invalid YAML
    (meta_dir / "bad-yaml.yaml").write_text("invalid: [unclosed bracket", encoding="utf-8")

    # 2. Bad kind and apiVersion
    (meta_dir / "bad-kind.yaml").write_text(
        yaml.dump(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "test"},
                "spec": {},
            }
        ),
        encoding="utf-8",
    )

    # 3. Missing spec fields and invalid role
    (meta_dir / "bad-spec.yaml").write_text(
        yaml.dump(
            {
                "apiVersion": "extensions.backstage.io/v1alpha1",
                "kind": "Package",
                "metadata": {"name": "bad-spec"},
                "spec": {
                    "packageName": "",
                    "version": "not-semver",
                    "dynamicArtifact": "",
                    "backstage": {
                        "role": "unrecognized-role",
                        "supportedVersions": "",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    res = check_package_metadata(ws)
    assert res["passed"] is False
    assert res["reason"] == "invalid_metadata_schema"
    error_text = "\n".join(res["errors"])
    assert "bad-yaml.yaml: YAML syntax error" in error_text
    assert "bad-kind.yaml: invalid apiVersion" in error_text
    assert "bad-kind.yaml: invalid kind" in error_text
    assert "bad-spec.yaml: missing or invalid 'spec.packageName'" in error_text
    assert "bad-spec.yaml: invalid 'spec.version'" in error_text
    assert "bad-spec.yaml: missing or invalid 'spec.dynamicArtifact'" in error_text
    assert "bad-spec.yaml: invalid 'spec.backstage.role'" in error_text
    assert (
        "bad-spec.yaml: missing or invalid 'spec.backstage.supportedVersions'"
        in error_text
    )


def test_run_preflight_end_to_end_pass():
    res = run_preflight(workspace="workspaces/tech-radar", repo_root=ROOT)
    assert res["preflight_passed"] is True
    assert res["failure_reason"] is None
    assert res["stages"]["backstage_compatibility"]["passed"] is True
    assert res["stages"]["package_metadata"]["passed"] is True


def test_run_preflight_end_to_end_fail_backstage():
    res = run_preflight(workspace="workspaces/x2a", repo_root=ROOT)
    assert res["preflight_passed"] is False
    assert res["failure_reason"] == "backstage_version_mismatch"
    assert "Backstage version mismatch" in res["failure_details"]


def test_run_preflight_cli_execution(tmp_path: Path):
    output_file = tmp_path / "github_output.txt"
    summary_file = tmp_path / "github_summary.md"

    cmd = [
        "python3",
        str(ROOT / "scripts/check_workspace_preflight.py"),
        "--workspace",
        "workspaces/tech-radar",
        "--github-output",
        str(output_file),
        "--github-step-summary",
        str(summary_file),
        "--json",
    ]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["preflight_passed"] is True

    output_content = output_file.read_text(encoding="utf-8")
    assert "preflight-passed=true" in output_content
    assert "effective-backstage-version=1.54.5" in output_content

    summary_content = summary_file.read_text(encoding="utf-8")
    assert "Stage 1 Static Preflight Gate: Passed" in summary_content
    assert "tech-radar" in summary_content


def test_auto_publish_pr_has_preflight_gate():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/auto-publish-pr.yaml").read_text(encoding="utf-8")
    )
    on_block = workflow.get("on", workflow.get(True))
    assert "preflight-passed" in on_block["workflow_call"]["outputs"]
    assert "preflight-failure-reason" in on_block["workflow_call"]["outputs"]

    jobs = workflow["jobs"]
    assert "preflight" in jobs

    preflight_job = jobs["preflight"]
    assert "preflight-passed" in preflight_job["outputs"]
    assert "failure-reason" in preflight_job["outputs"]

    export_job = jobs["export"]
    assert "preflight" in export_job["needs"]
    assert "needs.preflight.result == 'success'" in export_job["if"]

    add_comment_job = jobs["add-completion-comment"]
    assert "preflight" in add_comment_job["needs"]

