#!/usr/bin/env python3
"""Stage 1 static preflight check for Backstage compatibility and Package CRD schemas.

Performs fast (<15s) static validation of workspace configuration before container
image builds and dynamic export jobs execute (RHIDP-17923):
1. Backstage version compatibility: statically compares workspace source.json
   (or backstage.json override) against versions.json.
2. Package CRD metadata: statically validates plugins-list.yaml and all metadata/*.yaml
   Package entities for schema compliance, required fields, and valid roles.

Emits structured failure reasons ('backstage_version_mismatch', 'invalid_metadata_schema',
'missing_package_metadata') for consumption by CI and autonomous Fullsend agents.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
import yaml

SEMVER_REGEX = re.compile(
    r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$"
)

VALID_BACKSTAGE_ROLES = {
    "frontend-plugin",
    "backend-plugin",
    "backend-plugin-module",
    "frontend-plugin-module",
    "scaffolder-module",
    "api-report",
    "common-library",
    "web-library",
}


def find_repo_root() -> Path:
    """Locate the repository root directory containing versions.json and workspaces/."""
    candidates = [Path.cwd(), *Path(__file__).resolve().parents]
    for candidate in candidates:
        if (candidate / "versions.json").is_file() and (candidate / "workspaces").is_dir():
            return candidate
    return Path.cwd()


def get_minor_version(version_str: str | None) -> str:
    """Extract major.minor from a semver string (e.g. '1.54.9' -> '1.54')."""
    if not version_str:
        return ""
    parts = str(version_str).strip().split(".")
    if len(parts) >= 2:
        return f"{parts[0]}.{parts[1]}"
    return str(version_str).strip()


def package_name_to_image_name(pkg_name: str) -> str:
    """Derive default container image name from packageName (e.g. '@scope/foo' -> 'scope-foo')."""
    return pkg_name.lstrip("@").replace("/", "-")


def check_backstage_compatibility(
    workspace_path: Path,
    target_backstage_version: str,
    allow_incompatible: bool = False,
) -> dict[str, Any]:
    """Statically check Backstage version compatibility."""
    source_json_path = workspace_path / "source.json"
    backstage_json_path = workspace_path / "backstage.json"

    if not source_json_path.is_file():
        return {
            "passed": False,
            "error": f"Missing required file: {source_json_path.name}",
            "reason": "invalid_metadata_schema",
            "source_version": None,
            "override_version": None,
            "effective_version": None,
            "target_version": target_backstage_version,
            "is_minor_match": False,
            "allow_incompatible": allow_incompatible,
        }

    try:
        with open(source_json_path, encoding="utf-8") as f:
            source_data = json.load(f)
        source_version = source_data.get("repo-backstage-version")
    except Exception as e:
        return {
            "passed": False,
            "error": f"Failed to parse source.json: {e}",
            "reason": "invalid_metadata_schema",
            "source_version": None,
            "override_version": None,
            "effective_version": None,
            "target_version": target_backstage_version,
            "is_minor_match": False,
            "allow_incompatible": allow_incompatible,
        }

    override_version = None
    if backstage_json_path.is_file():
        try:
            with open(backstage_json_path, encoding="utf-8") as f:
                override_data = json.load(f)
            override_version = override_data.get("version")
        except Exception as e:
            return {
                "passed": False,
                "error": f"Failed to parse backstage.json: {e}",
                "reason": "invalid_metadata_schema",
                "source_version": source_version,
                "override_version": None,
                "effective_version": None,
                "target_version": target_backstage_version,
                "is_minor_match": False,
                "allow_incompatible": allow_incompatible,
            }

    effective_version = override_version or source_version
    target_minor = get_minor_version(target_backstage_version)
    effective_minor = get_minor_version(effective_version)

    is_minor_match = bool(
        effective_minor and target_minor and effective_minor == target_minor
    )
    passed = is_minor_match or allow_incompatible
    reason = None if is_minor_match else "backstage_version_mismatch"

    error_msg = None
    if not is_minor_match:
        if override_version:
            error_msg = (
                f"Backstage version mismatch: workspace has backstage.json override '{override_version}' "
                f"(minor '{effective_minor}'), but target release requires '{target_backstage_version}' "
                f"(minor '{target_minor}')."
            )
        elif source_version:
            error_msg = (
                f"Backstage version mismatch: workspace declares Backstage '{source_version}' "
                f"(minor '{effective_minor}'), but target release requires '{target_backstage_version}' "
                f"(minor '{target_minor}'). No valid backstage.json override found."
            )
        else:
            error_msg = (
                f"Backstage version mismatch: workspace source.json does not declare 'repo-backstage-version', "
                f"and no backstage.json override found. Target release requires '{target_backstage_version}'."
            )

    return {
        "passed": passed,
        "is_minor_match": is_minor_match,
        "allow_incompatible": allow_incompatible,
        "source_version": source_version,
        "override_version": override_version,
        "effective_version": effective_version,
        "target_version": target_backstage_version,
        "effective_minor": effective_minor,
        "target_minor": target_minor,
        "reason": reason,
        "error": error_msg,
    }


def check_package_metadata(
    workspace_path: Path, image_repository_prefix: str | None = None
) -> dict[str, Any]:
    """Statically validate plugins-list.yaml and Package CRD schemas in metadata/*.yaml."""
    plugins_list_path = workspace_path / "plugins-list.yaml"
    metadata_dir = workspace_path / "metadata"

    errors: list[str] = []
    warnings: list[str] = []

    # 1. plugins-list.yaml
    if not plugins_list_path.is_file():
        errors.append(f"Missing required plugins-list.yaml at {plugins_list_path.name}")
        return {
            "passed": False,
            "errors": errors,
            "warnings": warnings,
            "reason": "invalid_metadata_schema",
            "file_count": 0,
        }

    try:
        with open(plugins_list_path, encoding="utf-8") as f:
            plugins_list = yaml.safe_load(f)
        if not isinstance(plugins_list, dict) or not plugins_list:
            errors.append(
                "plugins-list.yaml must be a non-empty YAML dictionary mapping plugin paths"
            )
    except Exception as e:
        errors.append(f"Failed to parse plugins-list.yaml: {e}")
        return {
            "passed": False,
            "errors": errors,
            "warnings": warnings,
            "reason": "invalid_metadata_schema",
            "file_count": 0,
        }

    # 2. metadata directory
    if not metadata_dir.is_dir():
        errors.append("Missing metadata directory")
        return {
            "passed": False,
            "errors": errors,
            "warnings": warnings,
            "reason": "missing_package_metadata",
            "file_count": 0,
        }

    metadata_files = sorted(
        list(metadata_dir.glob("*.yaml")) + list(metadata_dir.glob("*.yml"))
    )
    if not metadata_files:
        errors.append("No Package CRD YAML files found in metadata/")
        return {
            "passed": False,
            "errors": errors,
            "warnings": warnings,
            "reason": "missing_package_metadata",
            "file_count": 0,
        }

    # 3. Validate each metadata YAML file
    for mf in metadata_files:
        fname = mf.name
        try:
            with open(mf, encoding="utf-8") as f:
                doc = yaml.safe_load(f)
        except Exception as e:
            errors.append(f"{fname}: YAML syntax error: {e}")
            continue

        if not isinstance(doc, dict):
            errors.append(f"{fname}: root must be a YAML mapping")
            continue

        api_version = doc.get("apiVersion")
        if not api_version or not str(api_version).endswith("/v1alpha1"):
            errors.append(
                f"{fname}: invalid apiVersion '{api_version}', expected 'extensions.backstage.io/v1alpha1'"
            )

        kind = doc.get("kind")
        if kind != "Package":
            errors.append(f"{fname}: invalid kind '{kind}', expected 'Package'")

        metadata_block = doc.get("metadata")
        if not isinstance(metadata_block, dict):
            errors.append(f"{fname}: missing or invalid 'metadata' block")
        else:
            stem_name = metadata_block.get("name")
            if not stem_name or not isinstance(stem_name, str):
                errors.append(f"{fname}: 'metadata.name' must be a non-empty string")

        spec_block = doc.get("spec")
        if not isinstance(spec_block, dict):
            errors.append(f"{fname}: missing or invalid 'spec' block")
            continue

        pkg_name = spec_block.get("packageName")
        if not pkg_name or not isinstance(pkg_name, str):
            errors.append(f"{fname}: missing or invalid 'spec.packageName'")

        version = spec_block.get("version")
        if not version or not SEMVER_REGEX.match(str(version)):
            errors.append(
                f"{fname}: invalid 'spec.version' '{version}', must be valid semver"
            )

        dynamic_artifact = spec_block.get("dynamicArtifact")
        if not dynamic_artifact or not isinstance(dynamic_artifact, str):
            errors.append(f"{fname}: missing or invalid 'spec.dynamicArtifact'")
        elif image_repository_prefix and dynamic_artifact.startswith("oci://ghcr.io/"):
            expected_image = package_name_to_image_name(pkg_name or "")
            ref_no_tag = dynamic_artifact.rsplit(":", 1)[0]
            expected_ref = f"oci://{image_repository_prefix}/{expected_image}"
            if ref_no_tag != expected_ref:
                errors.append(
                    f"{fname}: OCI reference mismatch: expected \"{expected_ref}\" but got \"{ref_no_tag}\""
                )

        bs_block = spec_block.get("backstage")
        if not isinstance(bs_block, dict):
            errors.append(f"{fname}: missing or invalid 'spec.backstage' block")
        else:
            role = bs_block.get("role")
            if not role or role not in VALID_BACKSTAGE_ROLES:
                errors.append(
                    f"{fname}: invalid 'spec.backstage.role' '{role}', must be one of {sorted(VALID_BACKSTAGE_ROLES)}"
                )
            supported_versions = bs_block.get("supportedVersions")
            if not supported_versions or not isinstance(supported_versions, str):
                errors.append(
                    f"{fname}: missing or invalid 'spec.backstage.supportedVersions'"
                )

    passed = len(errors) == 0
    reason = None if passed else "invalid_metadata_schema"

    return {
        "passed": passed,
        "errors": errors,
        "warnings": warnings,
        "reason": reason,
        "file_count": len(metadata_files),
    }


def run_preflight(
    workspace: str,
    target_backstage_version: str | None = None,
    versions_file: str | None = None,
    allow_incompatible_backstage: bool = False,
    image_repository_prefix: str | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Execute the complete Stage 1 static preflight check."""
    root = repo_root or find_repo_root()

    # Resolve workspace path flexibly (supports direct directory paths, 'workspaces/name', or 'name')
    raw_path = Path(workspace)
    if raw_path.is_dir():
        ws_path = raw_path.resolve()
        if ws_path.parent.name == "workspaces":
            ws_clean = f"workspaces/{ws_path.name}"
        else:
            ws_clean = str(raw_path).rstrip("/")
    else:
        candidate = root / workspace
        if candidate.is_dir():
            ws_path = candidate.resolve()
            ws_clean = workspace.strip().rstrip("/")
        else:
            ws_clean = workspace.strip().rstrip("/")
            if not ws_clean.startswith("workspaces/"):
                ws_clean = f"workspaces/{ws_clean}"
            ws_path = (root / ws_clean).resolve()

    if not ws_path.is_dir():
        return {
            "workspace": ws_clean,
            "preflight_passed": False,
            "failure_reason": "invalid_metadata_schema",
            "failure_details": f"Workspace directory not found: {workspace}",
            "stages": {},
        }

    # Resolve target Backstage version
    target_bs = target_backstage_version
    if not target_bs:
        if versions_file:
            v_path = Path(versions_file).resolve()
        elif (ws_path.parent.parent / "versions.json").is_file():
            v_path = ws_path.parent.parent / "versions.json"
        else:
            v_path = root / "versions.json"

        if not v_path.is_file():
            return {
                "workspace": ws_clean,
                "preflight_passed": False,
                "failure_reason": "invalid_metadata_schema",
                "failure_details": f"versions.json not found at {v_path}",
                "stages": {},
            }
        with open(v_path, encoding="utf-8") as f:
            v_data = json.load(f)
        target_bs = v_data.get("backstage")
        if not target_bs:
            return {
                "workspace": ws_clean,
                "preflight_passed": False,
                "failure_reason": "invalid_metadata_schema",
                "failure_details": f"No 'backstage' key found in {v_path}",
                "stages": {},
            }

    # Run checks
    bs_result = check_backstage_compatibility(
        ws_path, target_bs, allow_incompatible=allow_incompatible_backstage
    )
    meta_result = check_package_metadata(
        ws_path, image_repository_prefix=image_repository_prefix
    )

    preflight_passed = bs_result["passed"] and meta_result["passed"]

    failure_reasons: list[str] = []
    failure_details: list[str] = []

    if not bs_result["passed"]:
        if bs_result.get("reason"):
            failure_reasons.append(bs_result["reason"])
        if bs_result.get("error"):
            failure_details.append(bs_result["error"])

    if not meta_result["passed"]:
        if meta_result.get("reason") and meta_result["reason"] not in failure_reasons:
            failure_reasons.append(meta_result["reason"])
        failure_details.extend(meta_result["errors"])

    primary_reason = ", ".join(failure_reasons) if failure_reasons else None
    details_str = "\n".join(failure_details)

    return {
        "workspace": ws_clean,
        "preflight_passed": preflight_passed,
        "failure_reason": primary_reason,
        "failure_details": details_str,
        "stages": {
            "backstage_compatibility": bs_result,
            "package_metadata": meta_result,
        },
    }


def write_github_outputs(result: dict[str, Any], output_path: str) -> None:
    """Write preflight outputs to GITHUB_OUTPUT."""
    bs_stage = result.get("stages", {}).get("backstage_compatibility", {})
    meta_stage = result.get("stages", {}).get("package_metadata", {})

    with open(output_path, "a", encoding="utf-8") as f:
        f.write(f"preflight-passed={str(result['preflight_passed']).lower()}\n")
        f.write(f"failure-reason={result['failure_reason'] or ''}\n")
        f.write(
            f"effective-backstage-version={bs_stage.get('effective_version') or ''}\n"
        )
        f.write(f"target-backstage-version={bs_stage.get('target_version') or ''}\n")
        f.write(
            f"has-backstage-override={str(bool(bs_stage.get('override_version'))).lower()}\n"
        )
        f.write(f"metadata-files-count={meta_stage.get('file_count', 0)}\n")

        # Multiline details
        details = result.get("failure_details", "")
        f.write("failure-details<<EOF\n")
        f.write(f"{details}\n")
        f.write("EOF\n")


def write_step_summary(result: dict[str, Any], summary_path: str) -> None:
    """Write preflight markdown report to GITHUB_STEP_SUMMARY."""
    bs = result.get("stages", {}).get("backstage_compatibility", {})
    meta = result.get("stages", {}).get("package_metadata", {})

    passed = result["preflight_passed"]
    icon = "✅" if passed else "❌"
    headline = "Passed" if passed else "Failed"

    lines = [
        f"### {icon} Stage 1 Static Preflight Gate: {headline}",
        f"**Workspace:** `{result['workspace']}`",
        "",
        "| Check | Status | Details |",
        "|---|---|---|",
    ]

    # Backstage row
    if bs.get("passed"):
        if bs.get("is_minor_match"):
            override_note = " (override)" if bs.get("override_version") else ""
            lines.append(
                f"| Backstage Compatibility | ✅ Compatible | `{bs.get('effective_version')}`{override_note} matches target `{bs.get('target_version')}` |"
            )
        else:
            lines.append(
                f"| Backstage Compatibility | ⚠️ Allowed | Declares `{bs.get('effective_version')}` vs target `{bs.get('target_version')}` (E2E-only PR override) |"
            )
    else:
        lines.append(
            f"| Backstage Compatibility | ❌ Incompatible | `{bs.get('effective_version') or 'none'}` does not match target `{bs.get('target_version')}` |"
        )

    # Package metadata row
    if meta.get("passed"):
        lines.append(
            f"| Package Metadata | ✅ Valid | {meta.get('file_count', 0)} Package CRD(s) verified |"
        )
    else:
        err_count = len(meta.get("errors", []))
        lines.append(
            f"| Package Metadata | ❌ Invalid | {err_count} schema/file error(s) |"
        )

    if not passed and result.get("failure_details"):
        lines.append("")
        lines.append("#### Diagnostics & Remediations")
        for err in result["failure_details"].splitlines():
            lines.append(f"- {err}")

        reason = result.get("failure_reason")
        if reason == "backstage_version_mismatch":
            lines.append(
                "\n> **Remediation:** Add `workspaces/<workspace>/backstage.json` with `{\"version\": \""
                + str(bs.get("target_version"))
                + "\"}` or post `/override-backstage` on the PR."
            )
        elif reason in ("invalid_metadata_schema", "missing_package_metadata"):
            lines.append(
                "\n> **Remediation:** Correct Package CRD YAML files in `workspaces/<workspace>/metadata/` ensuring all required fields are present and valid."
            )

    with open(summary_path, "a", encoding="utf-8") as f:
        f.write("\n" + "\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stage 1 static preflight check for Backstage compatibility and Package schemas."
    )
    parser.add_argument(
        "-w",
        "--workspace",
        required=True,
        help="Target workspace path (e.g. 'workspaces/tech-radar' or 'tech-radar')",
    )
    parser.add_argument(
        "--target-backstage",
        default=None,
        help="Target Backstage release version (defaults to versions.json .backstage)",
    )
    parser.add_argument(
        "--versions-file",
        default=None,
        help="Path to versions.json (defaults to repo root versions.json)",
    )
    parser.add_argument(
        "--allow-incompatible-backstage",
        action="store_true",
        help="Warn instead of fail when Backstage version mismatches (used on E2E-only PRs)",
    )
    parser.add_argument(
        "--image-repository-prefix",
        default=None,
        help="Expected image repository prefix to validate OCI references (e.g. 'ghcr.io/owner/repo')",
    )
    parser.add_argument(
        "--github-output",
        default=os.environ.get("GITHUB_OUTPUT"),
        help="Path to $GITHUB_OUTPUT file",
    )
    parser.add_argument(
        "--github-step-summary",
        default=os.environ.get("GITHUB_STEP_SUMMARY"),
        help="Path to $GITHUB_STEP_SUMMARY file",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output structured JSON to stdout",
    )

    args = parser.parse_args()

    result = run_preflight(
        workspace=args.workspace,
        target_backstage_version=args.target_backstage,
        versions_file=args.versions_file,
        allow_incompatible_backstage=args.allow_incompatible_backstage,
        image_repository_prefix=args.image_repository_prefix,
    )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        status_str = "PASSED" if result["preflight_passed"] else "FAILED"
        print(f"=== Stage 1 Static Preflight: {status_str} ===")
        print(f"Workspace: {result['workspace']}")
        bs = result["stages"].get("backstage_compatibility", {})
        print(
            f"Backstage: effective={bs.get('effective_version')} target={bs.get('target_version')} match={bs.get('is_minor_match')}"
        )
        meta = result["stages"].get("package_metadata", {})
        print(
            f"Package Metadata: valid={meta.get('passed')} files={meta.get('file_count')}"
        )

        if not result["preflight_passed"]:
            print(f"\nFailure Reason: {result['failure_reason']}")
            print("Details:")
            for line in result["failure_details"].splitlines():
                print(f"  - {line}")

    if args.github_output and os.path.exists(os.path.dirname(args.github_output) or "."):
        write_github_outputs(result, args.github_output)

    if args.github_step_summary and os.path.exists(
        os.path.dirname(args.github_step_summary) or "."
    ):
        write_step_summary(result, args.github_step_summary)

    return 0 if result["preflight_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
