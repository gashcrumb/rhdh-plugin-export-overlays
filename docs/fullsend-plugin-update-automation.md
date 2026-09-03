# Fullsend Automated Plugin Update Agent — Architecture & Operational Guide

## 1. Executive Summary

In `rhdh-plugin-export-overlays`, automated workflows (`update-plugins-repo-refs.yaml`) periodically open pull requests to update upstream plugin repository references from `@backstage-community`, `@red-hat-developer-hub`, and `@roadiehq`.

Handling these update PRs follows a predictable, highly repetitive verification and remediation lifecycle:
1. Triggering dynamic plugin packaging and export via the `/publish` slash command.
2. Checking publish results for Backstage version compatibility mismatches and generating `backstage.json` overrides.
3. Repairing missing or invalid metadata files (`workspaces/*/metadata/*.yaml`) and container references.
4. Evaluating workspace smoke test results (`workspace-tests.yaml`) and supplying missing runtime environment variables (`smoke-tests/test.env`).
5. Triggering Playwright E2E tests (`/test e2e-ocp-helm`) for workspaces with an `e2e-tests/` suite.
6. Diagnosing and resolving E2E test failures or escalating breaking changes to human reviewers.

The **Plugin Update Agent** automates this entire lifecycle using [Fullsend](https://github.com/fullsend-ai/fullsend). It operates autonomously within GitHub Actions, evaluating CI outputs, applying file remediations directly to the PR branch, executing slash commands, and handing off clean, verified PRs for final human review and merge.

---

## 2. Architecture & Lifecycle State Machine

```
                   [PR Created / Labeled `workspace-update`]
                                       │
                                       ▼
                       ┌───────────────────────────────┐
                       │ CI Bridge / Fullsend Trigger  │
                       └───────────────┬───────────────┘
                                       │
                                       ▼
                       ┌───────────────────────────────┐
                       │  Agent: Evaluate PR State     │
                       └───────────────┬───────────────┘
                                       │
         ┌─────────────────────────────┼─────────────────────────────┐
         ▼                             ▼                             ▼
   [Not Published]            [Publish Failed ❌]           [Publish Succeeded ✅]
    Post `/publish`                    │                             │
                                       ▼                             ▼
                              [Diagnose Failure]            [pr-actions dispatches
                              - Stale versions.json:        smoke-tests & CI bridge
                                Post `/update-versions`     polls workspace-tests]
                              - Backstage Incompat:                  │
                                Create backstage.json &              ▼
                                update metadata YAML        [Evaluate Smoke Tests]
                              - Missing/Bad YAML:                    │
                                Generate Package CRD         ┌───────┴───────┐
                                       │                     ▼               ▼
                              [post-script commits &    [Passed ✅]     [Failed ❌]
                               pushes to PR branch]          │               │
                                       │                     │      Diagnose logs:
                                       ▼                     │      - Missing test.env:
                                 Re-run `/publish`           │        Create test.env
                                                             │      - Bad config/YAML:
                                                             │        Fix Package YAML
                                                             │      - Fatal/Upstream:
                                                             │        Escalate
                                                             │               │
                                                             │      [post-script commits &
                                                             │       pushes, re-runs
                                                             │       `/smoketest`]
                                                             │               │
                                                             └───────┬───────┘
                                                                     │
                                                                     ▼
                                                         [Check: Has `e2e-tests/`?]
                                                                     │
                                                   ┌─────────────────┴─────────────────┐
                                                   ▼ (No)                              ▼ (Yes)
                                         ┌───────────────────┐               ┌───────────────────┐
                                         │ Mark Ready for    │               │ Check Prow E2E    │
                                         │ Review            │               │ Status            │
                                         └───────────────────┘               └─────────┬─────────┘
                                                                                       │
                                                                     ┌─────────────────┴─────────────────┐
                                                                     ▼                                   ▼
                                                              [Not Run / Pending]                  [E2E Failed ❌]
                                                              Post `/test e2e-ocp-helm`            Run E2E Triage:
                                                                                                   - flake -> `/retest`
                                                                                                   - test bug -> fix spec
                                                                                                   - product bug -> skip
                                                                                                   - complex -> escalate
                                                                                                         │
                                                                                             [E2E Passed ✅]
                                                                                                         │
                                                                                                         ▼
                                                                                               ┌───────────────────┐
                                                                                               │ Mark Ready for    │
                                                                                               │ Merge & Reviewers │
                                                                                               └───────────────────┘
```

### Core Operational Principles
- **Zero Local Builds:** The agent never runs local Docker, Podman, or Yarn builds on the runner. All exports, container builds, smoke tests, and E2E runs are executed remotely in GitHub Actions and OpenShift CI via standard PR slash commands.
- **Single-Workspace Scoping:** Each agent invocation operates strictly within the workspace modified by the PR (`workspaces/<workspace-name>/`).
- **Deterministic Host Execution:** The agent runs inside a sandboxed container without write permissions to GitHub or Git remotes. Remediation files and slash commands are emitted as structured JSON (`agent-result.json`) and executed by a host-runner post-script with authenticated credentials.

---

## 3. Event Trigger Architecture & CI Bridge

Automating a multi-stage CI loop on GitHub Actions requires overcoming built-in platform event suppression:
1. **Comment Suppression:** GitHub Actions suppresses `issue_comment` events when comments are created by the default `GITHUB_TOKEN`.
2. **Dispatch Suppression:** Workflows triggered via `workflow_dispatch` by `GITHUB_TOKEN` do not emit downstream `workflow_run` events.

To ensure seamless execution, the agent uses a **dual-entry trigger architecture**:

```
[PR Labeled / User Command] ──► .github/workflows/fullsend.yaml (PR Shim) ────┐
                                                                              ▼
[CI Completed (Publish / Smoke)] ──► .github/workflows/plugin-update-ci-bridge.yaml ──► reusable-dispatch.yml
```

### 3.1 Direct PR Lifecycle Trigger (`.github/workflows/fullsend.yaml`)
- Listens for `pull_request_target` when the `workspace-update` label is added, or `issue_comment` when an authorized user posts `/fs-plugin-update`.
- Includes a strict bot filter (`comment.user.type != 'Bot'`) to prevent bot notification comments from triggering infinite loops.

### 3.2 CI Completion Bridge (`.github/workflows/plugin-update-ci-bridge.yaml`)
- Listens for `workflow_run: completed` on `Pull Request Actions` and `Workspace Smoke Tests`.
- Matches the workflow run to the target PR and verifies the `workspace-update` label.
- **Synchronous Smoke Test Polling:** When `Pull Request Actions` completes with success and dispatches `workspace-tests.yaml`, the bridge actively polls the status of `workspace-tests.yaml` (up to 10 minutes, polling every 5 seconds).
- Once smoke tests complete, the bridge resolves the PR head SHA and directly invokes Fullsend's `reusable-dispatch.yml` with a pre-computed execution matrix (`role: coder`, `agent: plugin-update`).
- This guarantees the agent is awakened with both published image artifacts and smoke test results ready to evaluate simultaneously.

---

## 4. Code & Configuration Layout

The agent integration is located under `.fullsend/` and `.github/workflows/`:

```
.github/workflows/
├── fullsend.yaml                                  # Event shim for PR labels and manual slash commands
└── plugin-update-ci-bridge.yaml                   # CI Bridge listening to workflow_run completions

.fullsend/
├── config.yaml                                    # Registers 'plugin-update' in per-repo config
├── rhdh/
│   ├── agents/
│   │   └── plugin-update.md                      # Agent system prompt & decision state machine
│   ├── env/
│   │   ├── gcp-vertex.env                        # Vertex AI region (global) & environment config
│   │   ├── rhdh-toolchain.env                    # Node and Yarn toolchain definitions
│   │   └── yarn-proxy.env                        # Package registry proxy settings
│   ├── harness/
│   │   └── plugin-update.yaml                    # Harness config (model, timeouts, lifecycle hooks)
│   ├── policies/
│   │   └── plugin-update.yaml                    # Sandboxed filesystem & network security policy
│   ├── schemas/
│   │   └── plugin-update-result.schema.json      # JSON Schema for agent-result.json
│   ├── scripts/
│   │   ├── pre-plugin-update.sh                  # Host pre-script: generates pr-input.json
│   │   ├── post-plugin-update.sh                 # Host post-script: gitleaks, commits, comments
│   │   └── validate-output-schema.sh             # Agent self-validation script during execution
│   └── skills/
│       ├── e2e-failure-analysis/                 # Prow GCS log downloader and diagnostics
│       ├── playwright-trace/                     # Playwright trace archive inspection
│       ├── plugin-pr-triage/                     # Structured PR comment & CI result parser
│       └── metadata-remediation/                 # Package CRD & backstage.json generator
```

### 4.1 Host Runner Pre-Script (`pre-plugin-update.sh`)
Runs before the agent sandbox is spawned. It queries the GitHub PR API, resolves the affected workspace, detects whether Playwright E2E suites exist (`workspaces/<workspace>/e2e-tests/`), and writes `/tmp/workspace/pr-input.json`. This file is mounted into the sandbox so the agent has instant access to PR context on Turn 1.

### 4.2 Agent Prompt & State Machine (`plugin-update.md`)
Guides the Claude Haiku agent through PR evaluation:
1. Reads `pr-input.json`.
2. Inspects published artifacts and compatibility tables from bot comments.
3. Checks container boot logs and smoke test results.
4. Applies file modifications for missing `backstage.json`, outdated Package metadata, or missing `test.env`.
5. Emits `agent-result.json` adhering to `plugin-update-result.schema.json`.

### 4.3 Host Runner Post-Script (`post-plugin-update.sh`)
Executes on the GitHub Actions runner after the sandbox exits:
1. **Secret Scanning:** Runs `gitleaks detect` on `agent-result.json` and all modified files before touching the git tree.
2. **Path Validation:** Verifies that all modified files reside strictly within `workspaces/<affected-workspace>/`.
3. **Commit & Push:** Checks out the PR head branch, copies remediations from the sandbox output directory, commits them under `fullsend-ai[bot]`, and pushes to the PR branch using authenticated credentials.
4. **Slash Command Execution:** Posts required slash commands (`/publish`, `/override-backstage`, `/update-versions`, `/smoketest`, `/test e2e-ocp-helm`) or escalation comments via `gh pr comment`.
5. **PR Label Updates:** Synchronizes PR labels based on stage transitions.

---

## 5. Remediation Rules & Scenarios

### 5.1 Backstage Version Compatibility Mismatch
When the upstream plugin's Backstage version differs from the target release (e.g. plugin is on Backstage `1.53.0` while target is `1.54.4`):
- The agent creates `workspaces/<workspace>/backstage.json` containing `{"repo-backstage-version": "<target-version>"}`.
- Reconciles `spec.dynamicArtifact` and `spec.version` in `workspaces/<workspace>/metadata/*.yaml`.
- The post-script commits the changes to the PR branch and re-triggers `/publish`.

### 5.2 Missing Package CRD Metadata
When new packages are added upstream or metadata files are missing:
- The agent inspects `package.json` in the upstream repository at the pinned `repo-ref`.
- Generates `workspaces/<workspace>/metadata/<package-name>.yaml` using standard `kind: Package` templates.
- Populates `packageName`, `dynamicArtifact`, `version`, `backstage.role`, and `spec.support`.

### 5.3 Missing Environment Variables in Smoke Tests
When container smoke tests fail due to unpopulated configuration placeholders:
- The agent inspects container startup error logs.
- Generates `workspaces/<workspace>/smoke-tests/test.env` with dummy values for required environment variables.
- Post-script commits `test.env` and dispatches `/smoketest`.

### 5.4 Playwright E2E Test Failures
For workspaces with an `e2e-tests/` directory:
- The agent invokes `/test e2e-ocp-helm` once publish and smoke tests pass.
- If Prow reports failure, the agent leverages `e2e-failure-analysis` and `playwright-trace` to classify the failure:
  - **Test Fix:** Adjusts locators or timing in `workspaces/<workspace>/e2e-tests/tests/specs/`.
  - **Product Bug:** Adds `test.skip(!!process.env.E2E_NIGHTLY_MODE, "<reason>")`.
  - **Infra Flake:** Issues `/retest`.
  - **Unresolvable:** Escalates to workspace CODEOWNERS.

---

## 6. Guardrails & Safety Controls

| Guardrail | Enforcement Mechanism |
|---|---|
| **Filesystem Sandboxing** | Agent sandbox is restricted by Landlock; writes are limited to temporary sandbox space. |
| **Workspace Boundary Check** | Post-script strictly enforces that modified paths match `^workspaces/<affected-workspace>/`. |
| **Command Whitelist** | Policy explicitly whitelists valid slash commands (`/publish`, `/smoketest`, `/test e2e-ocp-helm`, etc.). |
| **Secret Scanning** | Post-script runs `gitleaks` against all modified files and result payloads before committing. |
| **Retry & Loop Limits** | Hard thresholds prevent infinite retry loops: max 2 `/publish` attempts, max 2 `/override-backstage` attempts, max 1 E2E fix attempt. Exceeding limits triggers human escalation. |

---

## 7. Manual Operations & Debugging

### Triggering the Agent Manually
To trigger the agent on an existing plugin update PR:
1. Post the slash command `/fs-plugin-update` on the pull request.
2. Alternatively, remove and re-add the `workspace-update` label.

### Inspecting Workflow Runs
```bash
# View CI Bridge workflow runs
gh run list --workflow=plugin-update-ci-bridge.yaml --repo redhat-developer/rhdh-plugin-export-overlays

# View Fullsend dispatch runs
gh run list --workflow=fullsend.yaml --repo redhat-developer/rhdh-plugin-export-overlays

# Download agent execution transcript
gh run download <run-id> -n transcript
```
