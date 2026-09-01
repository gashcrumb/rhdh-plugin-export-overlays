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

## 2. Parsing Publish Results (`pr-actions.yaml`)

Look for comments with `### Action 'publish' Execution Result`:

```bash
LAST_PUBLISH_COMMENT=$(echo "${PR_JSON}" | jq -r '
  [.comments[] | select(.body | contains("### Action '\''publish'\'' Execution Result"))] | last | .body // empty
')
```

### Analysis Heuristics:
1. **Success**:
   - Contains: `✅ All requested packages were exported successfully` or table of published OCI tags (`ghcr.io/redhat-developer/rhdh-plugin-export-overlays/<pkg>:pr_${PR_NUMBER}__...`).
   - Outcome: Proceed to smoke testing or E2E testing.
2. **Backstage Version Mismatch**:
   - Contains: `is not compatible with targeted Backstage version` or `Backstage version mismatch`.
   - Outcome: Issue `/override-backstage`.
3. **Stale `versions.json`**:
   - Contains: `versions.json does not match base branch`.
   - Outcome: Issue `/update-versions`.
4. **Missing Package Metadata**:
   - Contains: `No Package entity found for exported plugin <pkg>` or `missing metadata/<pkg>.yaml`.
   - Outcome: Use `metadata-remediation` skill to generate `metadata/<pkg>.yaml` and re-run `/publish`.
5. **YAML / Schema Error in Metadata**:
   - Contains: `YAML parsing error` or `failed validation against schema`.
   - Outcome: Fix YAML syntax in `workspaces/<workspace>/metadata/*.yaml` and re-run `/publish`.

---

## 3. Parsing Smoke Test Results (`workspace-tests.yaml`)

Look for comments with `### Action 'smoketest' Execution Result` or `Smoke Test Summary`:

```bash
LAST_SMOKE_COMMENT=$(echo "${PR_JSON}" | jq -r '
  [.comments[] | select(.body | contains("Action '\''smoketest'\''") or contains("Smoke Test Summary"))] | last | .body // empty
')
```

### Analysis Heuristics:
1. **Success**:
   - Contains: `All plugin packages loaded successfully in container` or 0 failed packages.
   - Outcome: Proceed to E2E check.
2. **Missing Environment Variables**:
   - Look for: `Missing required environment variable: ([A-Z0-9_]+)` or `Plugin skipped loading due to missing environment variable`.
   - Outcome: Use `metadata-remediation` skill to add dummy values to `workspaces/<workspace>/smoke-tests/test.env` and issue `/smoketest`.
3. **Container Crash / Incompatible Export**:
   - Look for: `FATAL`, `Cannot find module`, or `Plugin failed to initialize`.
   - Outcome: If caused by missing export or dependency mismatch, inspect `patches/` or escalate to maintainers.

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
