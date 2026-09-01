#!/usr/bin/env bash
# Post-script: execute plugin-update agent directives and PR actions.
#
# Runs on the GitHub Actions runner AFTER the sandbox is destroyed.
# The agent cannot perform GitHub write operations or git pushes inside the sandbox.
# Instead, it writes directives to agent-result.json, which this script
# executes with runner credentials.
#
# Steps:
#   1. Locate and validate agent-result.json
#   2. Scan result file and modified files for secrets (gitleaks)
#   3. Commit and push any workspace remediations (metadata YAML, test.env, backstage.json, e2e test fixes)
#   4. Execute slash command or commentary on target PR
#   5. Apply label updates
#   6. Post execution summary comment
#
# Required environment variables:
#   GH_TOKEN          — GitHub token
#   REPO_FULL_NAME    — owner/repo (default: redhat-developer/rhdh-plugin-export-overlays)
#   GITHUB_PR_URL     — HTML URL of the target pull request
#
# Optional environment variables:
#   PUSH_TOKEN        — dedicated token with write permissions (falls back to GH_TOKEN)

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GITLEAKS_VERSION="8.30.1"
GITLEAKS_SHA256="551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"

REPO_FULL_NAME="${REPO_FULL_NAME:-redhat-developer/rhdh-plugin-export-overlays}"

: "${GH_TOKEN:?GH_TOKEN is required}"
export GH_TOKEN
echo "::add-mask::${GH_TOKEN}"

PUSH_TOKEN="${PUSH_TOKEN:-${GH_TOKEN}}"
echo "::add-mask::${PUSH_TOKEN}"

export GH_TOKEN="${PUSH_TOKEN}"

PR_URL="${GITHUB_PR_URL:-${GITHUB_ISSUE_URL:-}}"
PR_NUMBER=""
if [[ -n "${PR_URL}" ]]; then
  PR_NUMBER=$(echo "${PR_URL}" | grep -oP '(?<=pull/)[0-9]+' || echo "${PR_URL##*/}")
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

sanitize_for_gha() {
  local text="${1:-}" prev=""
  while [[ "${text}" != "${prev}" ]]; do
    prev="${text}"
    text="${text//::/}"
    text="${text//\%0A/}"
    text="${text//\%0a/}"
    text="${text//\%0D/}"
    text="${text//\%0d/}"
  done
  text="${text//$'\n'/ }"
  text="${text//$'\r'/}"
  echo "${text}"
}

install_gitleaks() {
  if command -v gitleaks >/dev/null 2>&1; then
    return 0
  fi
  echo "Installing gitleaks v${GITLEAKS_VERSION}..."
  mkdir -p "${HOME}/.local/bin"
  if curl -fsSL --proto =https \
    "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" \
    -o /tmp/gitleaks.tar.gz \
    && echo "${GITLEAKS_SHA256}  /tmp/gitleaks.tar.gz" | sha256sum -c --quiet \
    && tar xzf /tmp/gitleaks.tar.gz -C "${HOME}/.local/bin" gitleaks \
    && rm /tmp/gitleaks.tar.gz; then
    export PATH="${HOME}/.local/bin:${PATH}"
    echo "gitleaks installed"
    return 0
  fi
  echo "::error::Failed to install gitleaks"
  return 1
}

add_label() {
  local repo="$1" pr="$2" label="$3"
  local stderr_file
  stderr_file="$(mktemp)"
  if ! gh pr edit "${pr}" --repo "${repo}" --add-label "${label}" 2>"${stderr_file}"; then
    echo "::warning::Failed to add label '${label}' to PR #${pr}: $(sanitize_for_gha "$(cat "${stderr_file}")")"
  fi
  rm -f "${stderr_file}"
}

remove_label() {
  local repo="$1" pr="$2" label="$3"
  local stderr_file
  stderr_file="$(mktemp)"
  if ! gh pr edit "${pr}" --repo "${repo}" --remove-label "${label}" 2>"${stderr_file}"; then
    echo "::warning::Failed to remove label '${label}' from PR #${pr}: $(sanitize_for_gha "$(cat "${stderr_file}")")"
  fi
  rm -f "${stderr_file}"
}

# ---------------------------------------------------------------------------
# 1. Locate agent-result.json
# ---------------------------------------------------------------------------
RESULT_FILE=""
for dir in iteration-*/output output .; do
  if [[ -f "${dir}/agent-result.json" ]]; then
    RESULT_FILE="${dir}/agent-result.json"
    break
  fi
done

if [[ -z "${RESULT_FILE}" ]]; then
  echo "::warning::No agent-result.json found"
  exit 0
fi

RESULT_FILE="$(cd "$(dirname "${RESULT_FILE}")" && pwd)/$(basename "${RESULT_FILE}")"
echo "Found agent-result.json: ${RESULT_FILE}"

if ! jq empty "${RESULT_FILE}" 2>/dev/null; then
  echo "::error::agent-result.json is not valid JSON"
  exit 1
fi

PR_NUMBER_IN_RESULT="$(jq -r '.pr_number' "${RESULT_FILE}")"
if [[ -z "${PR_NUMBER}" || "${PR_NUMBER}" == "null" ]]; then
  PR_NUMBER="${PR_NUMBER_IN_RESULT}"
fi

WORKSPACE="$(jq -r '.workspace' "${RESULT_FILE}")"
STAGE="$(jq -r '.stage' "${RESULT_FILE}")"
STATUS="$(jq -r '.status' "${RESULT_FILE}")"
SLASH_COMMAND="$(jq -r '.slash_command // empty' "${RESULT_FILE}")"
COMMENT_BODY="$(jq -r '.comment_body // empty' "${RESULT_FILE}")"
COMMIT_MESSAGE="$(jq -r '.commit_message // empty' "${RESULT_FILE}")"
REASONING="$(jq -r '.reasoning' "${RESULT_FILE}")"

echo "Processing PR #${PR_NUMBER} | Workspace: ${WORKSPACE} | Stage: ${STAGE} | Status: ${STATUS}"

# ---------------------------------------------------------------------------
# 2. Secret Scanning (gitleaks)
# ---------------------------------------------------------------------------
if ! install_gitleaks; then
  echo "::error::Failed to install gitleaks — refusing to proceed without secret scan"
  exit 1
fi

echo "Scanning agent result for secrets..."
SCAN_DIR="$(mktemp -d)"
cp "${RESULT_FILE}" "${SCAN_DIR}/agent-result.json"
if ! gitleaks detect --source "${SCAN_DIR}" --no-git --redact 2>/dev/null; then
  echo "::error::Secret detected in agent-result.json — aborting post-script"
  rm -rf "${SCAN_DIR}"
  exit 1
fi
rm -rf "${SCAN_DIR}"

# ---------------------------------------------------------------------------
# 3. Handle Workspace Remediations (File Commits)
# ---------------------------------------------------------------------------
MODIFIED_COUNT="$(jq '.modified_files | length // 0' "${RESULT_FILE}")"
if [[ "${MODIFIED_COUNT}" -gt 0 ]]; then
  echo "Agent modified ${MODIFIED_COUNT} files — validating paths..."
  
  # Ensure all modified files belong to the allowed workspace
  mapfile -t FILES < <(jq -r '.modified_files[]' "${RESULT_FILE}")
  for f in "${FILES[@]}"; do
    if [[ ! "${f}" =~ ^workspaces/${WORKSPACE}/ ]]; then
      echo "::error::Agent attempted to modify file outside designated workspace: ${f}"
      exit 1
    fi
  done
  
  echo "Staging and committing remediations..."
  git reset --quiet || true

  for f in "${FILES[@]}"; do
    src_file=""
    # Check if file exists in extracted container download directories
    for cand in /tmp/fs-*/target-repo/"${f}" /tmp/fs-*/"${f}" target-repo/"${f}"; do
      if [[ -f "${cand}" ]]; then
        src_file="${cand}"
        break
      fi
    done

    if [[ -n "${src_file}" && "${src_file}" != "${f}" ]]; then
      echo "Copying extracted file ${src_file} -> ${f}"
      mkdir -p "$(dirname "${f}")"
      cp "${src_file}" "${f}"
    fi

    if [[ -f "${f}" ]]; then
      git add "${f}"
    else
      echo "::warning::File ${f} not found to stage"
    fi
  done
  
  COMMIT_MSG="${COMMIT_MESSAGE:-chore(${WORKSPACE}): apply automated plugin update remediations}"
  git config user.name "fullsend-ai[bot]"
  git config user.email "fullsend-ai[bot]@users.noreply.github.com"
  
  if git diff --staged --quiet; then
    echo "No staged file changes to commit."
  else
    git commit -m "${COMMIT_MSG}" -m "Assisted-By: fullsend-ai (plugin-update agent)"
    echo "Pushing changes to PR branch..."
    # Push to PR head branch using authenticated token
    PR_HEAD_REF="$(gh pr view "${PR_NUMBER}" --repo "${REPO_FULL_NAME}" --json headRefName --jq '.headRefName')"
    PUSH_URL="https://x-access-token:${PUSH_TOKEN}@github.com/${REPO_FULL_NAME}.git"
    git push "${PUSH_URL}" "HEAD:${PR_HEAD_REF}"
    echo "Pushed commit to ${PR_HEAD_REF}"
  fi
fi

# ---------------------------------------------------------------------------
# 4. Handle PR Slash Commands & Comments
# ---------------------------------------------------------------------------
if [[ -n "${SLASH_COMMAND}" && "${SLASH_COMMAND}" != "null" ]]; then
  echo "Posting slash command to PR #${PR_NUMBER}: ${SLASH_COMMAND}"
  gh pr comment "${PR_NUMBER}" --repo "${REPO_FULL_NAME}" --body "${SLASH_COMMAND}"
fi

if [[ -n "${COMMENT_BODY}" && "${COMMENT_BODY}" != "null" ]]; then
  echo "Posting detailed comment to PR #${PR_NUMBER}..."
  gh pr comment "${PR_NUMBER}" --repo "${REPO_FULL_NAME}" --body "${COMMENT_BODY}"
fi

# ---------------------------------------------------------------------------
# 5. Handle PR Label Updates
# ---------------------------------------------------------------------------
mapfile -t LABELS_ADD < <(jq -r '.labels_to_add[]? // empty' "${RESULT_FILE}")
for label in "${LABELS_ADD[@]}"; do
  if [[ -n "${label}" ]]; then
    echo "Adding label '${label}' to PR #${PR_NUMBER}"
    add_label "${REPO_FULL_NAME}" "${PR_NUMBER}" "${label}"
  fi
done

mapfile -t LABELS_REMOVE < <(jq -r '.labels_to_remove[]? // empty' "${RESULT_FILE}")
for label in "${LABELS_REMOVE[@]}"; do
  if [[ -n "${label}" ]]; then
    echo "Removing label '${label}' from PR #${PR_NUMBER}"
    remove_label "${REPO_FULL_NAME}" "${PR_NUMBER}" "${label}"
  fi
done

# ---------------------------------------------------------------------------
# 6. Handle Special Status (e.g., superseded PR closure)
# ---------------------------------------------------------------------------
if [[ "${STATUS}" == "superseded_close" ]]; then
  echo "Closing superseded PR #${PR_NUMBER}..."
  gh pr close "${PR_NUMBER}" --repo "${REPO_FULL_NAME}" \
    --comment "Closing PR #${PR_NUMBER} as superseded by a newer upstream plugin update PR."
fi

# ---------------------------------------------------------------------------
# 7. Post Execution Summary Comment
# ---------------------------------------------------------------------------
SUMMARY_MD="<!-- fullsend:plugin-update -->
### 🤖 Fullsend Plugin Update Handler Summary

| Attribute | Value |
|---|---|
| **Workspace** | \`${WORKSPACE}\` |
| **Stage** | \`${STAGE}\` |
| **Status** | \`${STATUS}\` |
| **Slash Command Issued** | \`${SLASH_COMMAND:-None}\` |
| **Files Remediated** | \`${MODIFIED_COUNT}\` |

**Reasoning / Next Steps:**
${REASONING}
"

echo "Posting summary comment to PR #${PR_NUMBER}..."
gh pr comment "${PR_NUMBER}" --repo "${REPO_FULL_NAME}" --body "${SUMMARY_MD}" || true

echo "Fullsend plugin-update post-script completed successfully."
