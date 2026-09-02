---
name: plugin-pr-triage
description: Extract and analyze publish results, Backstage compatibility mismatches, smoke test container logs, and Prow test statuses from GitHub PR comments and workflow runs.
allowed-tools: Bash(gh:*),Bash(jq:*),Bash(grep:*),Bash(sed:*),Bash(curl:*)
---

# Plugin PR Triage Skill

This skill provides step-by-step guidance for extracting and analyzing CI comments and workflow feedback in `rhdh-plugin-export-overlays`.

---

## 1. Fetching PR Metadata and Comments

Always fetch all comments and files from the target PR:

```bash
PR_JSON=$(gh pr view "${PR_NUMBER}" --json number,title,baseRefName,headRefName,state,labels,comments,statusCheckRollup,files)

# Extract modified files
echo "${PR_JSON}" | jq -r '.files[].path'

# Extract all comment bodies
echo "${PR_JSON}" | jq -r '.comments[].body'
```

---

## 2. Parsing Publish Results (`pr-actions.yaml` or `Publish workflow`)

Look for comments with `[Publish workflow]` or `### Action 'publish' Execution Result`:

```bash
LAST_PUBLISH_COMMENT=$(echo "${PR_JSON}" | jq -r '
  [.comments[] | select(.body | contains("[Publish workflow]") or contains("### Action '\''publish'\'' Execution Result") or contains("#### Publishing process"))] | last | .body // empty
')
```

### Analysis Heuristics:
1. **Backstage Version Incompatibility**:
   - Indicator: Contains `#### Backstage-incompatible workspaces` listing the PR's workspace (e.g., `workspaces/<workspace> | 1.53.0`), or `is not compatible with targeted Backstage version`.
   - Action: Issue `/override-backstage`.
   - Outcome: `stage: "publish_evaluation"`, `status: "pending_ci"`, `slash_command: "/override-backstage"`.
2. **Metadata Validation Errors**:
   - Indicator: Contains `#### Metadata Validation` with `❌ Found X validation error(s)`.
   - Specific Error Types:
     - *OCI reference mismatch* (e.g., `expected "oci://ghcr.io/..." but got "... "`):
       - If accompanied by Backstage incompatibility, `/override-backstage` will fix and re-align tags.
       - Otherwise, update `spec.dynamicArtifact` in `workspaces/<workspace>/metadata/*.yaml` to match expected tags and issue `/publish`.
     - *Missing Package metadata* (`No Package entity found for exported plugin <pkg>` or missing YAML):
       - Use `metadata-remediation` skill to generate `metadata/<pkg>.yaml`, stage file, and issue `/publish`.
     - *YAML / Schema syntax errors*:
       - Correct invalid YAML syntax in `workspaces/<workspace>/metadata/*.yaml`, stage file, and issue `/publish`.
3. **Stale `versions.json`**:
   - Indicator: Contains `versions.json does not match base branch`.
   - Action: Issue `/update-versions`.
4. **Publish Succeeded (Green)**:
   - Indicator: `Publishing process: ✅ Finished successfully` with `0` validation errors and no incompatible workspaces.
   - Action:
     - If smoke test not yet run: Issue `/smoketest`.
     - If smoke test already passed and `has_e2e_tests: true`: Issue `/test e2e-ocp-helm`.
     - If all tests complete: Set `status: "ready_for_review"`.

---

## 3. Parsing Smoke Test Results (`workspace-tests.yaml`)

Look for comments with `### Action 'smoketest' Execution Result`, `Smoke Test Summary`, `[Smoke tests workflow]`, or `[Smoke test workflow]`:

```bash
LAST_SMOKE_COMMENT=$(echo "${PR_JSON}" | jq -r '
  [.comments[] | select(.body | contains("Action '\''smoketest'\''") or contains("Smoke Test Summary") or contains("Smoke tests workflow") or contains("Smoke test workflow"))] | last | .body // empty
')
```

### Analysis Heuristics:
1. **Success**:
   - Contains: `All plugin packages loaded successfully in container`, `0 failed packages`, or `[Smoke tests workflow](...) succeeded`.
   - Outcome: Proceed to E2E check.
2. **Missing Environment Variables**:
   - Look for: `Missing required environment variable: ([A-Z0-9_]+)`, `missing workspace \`smoke-tests/test.env\` file`, or `Plugin skipped loading due to missing environment variable`.
   - Outcome: Use `metadata-remediation` skill to add dummy values to `workspaces/<workspace>/smoke-tests/test.env` and issue `/smoketest`.
3. **Container Crash / Node Error / Assertion Failure**:
   - Look for: `Error logs from container`, `Assertion failed`, `FATAL`, `Cannot find module`, `Segmentation fault`, or `Plugin failed to initialize`.
   - Outcome: If container fails to boot with Node/V8 assertion errors or missing exports, diagnose if a configuration fix (like `test.env`) or patch is possible, or escalate with full trace details to maintainers (`status: "escalate_to_human"`).

---

## 4. Detecting Duplicate or Superseded PRs

When automated discovery runs, newer PRs might be created that supersede earlier ones:

```bash
# Check other open PRs affecting the same workspace
OPEN_PRS=$(gh pr list --state open --json number,title,headRefName,createdAt \
  --jq --arg ws "workspaces/${WORKSPACE}" '[.[] | select(.headRefName | startswith($ws))]')

COUNT=$(echo "${OPEN_PRS}" | jq 'length')
if [[ "${COUNT}" -gt 1 ]]; then
  echo "Multiple open PRs detected for workspace ${WORKSPACE}:"
  echo "${OPEN_PRS}" | jq -r '.[] | "PR #\(.number): \(.title) (Created: \(.createdAt))"'
fi
```

If a newer PR already exists with a higher upstream commit/ref, mark the older PR for `superseded_close`.
