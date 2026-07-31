# pi-review

Shared assets for the **pi.dev automated PR reviewer** — the current AI PR
review bot, replacing the deprecated [`opencode-review`](../opencode-review/) action.

| | |
|-|-|
| Entry point | [`.github/workflows/pi-pr-review.yml`](../.github/workflows/pi-pr-review.yml) — a **reusable workflow**, not a composite action |
| This directory | The assets that workflow consumes: the shared base prompt and the review extractor |

> **Why a reusable workflow and not an action?** The reviewer is two jobs, and the split
> is a security boundary (read-only agent job, write-scoped publish job). A composite
> action can only add steps to one job, which would push that boundary into every
> consumer repo where it could silently drift. See the header comment in the workflow.

## Contents

| File | Role |
|------|------|
| [`AGENT-SETUP.md`](AGENT-SETUP.md) | Self-contained, agent-directed install instruction — hand it to an agent to wire the reviewer into a repo. Also readable by hand. |
| `pi-review-prompt.md` | Shared base review prompt: context-gathering rules plus the convergence/severity discipline that stops a PR from never converging. Repo-agnostic — do **not** put repo-specific rules here. |
| `pi-extract-review.py` | Extracts the final review text and aggregates token usage from pi's `--mode json` event stream. **Single authority on "usable review"** — exit `0` means usable, exit `1` drives the failure contract and the model fallback chain. Committed executable. |

Both are pinned by commit: the workflow checks this repo out at `job.workflow_sha` — the
exact commit of the workflow file the caller pinned — so a consumer on `@v1` gets `v1`'s
prompt and extractor, never `main`'s. Where that context value is unavailable (GitHub
Enterprise Server, older runners) the workflow falls back to its `commons-ref` input.

## Usage

See the [root README](../README.md#pi-pr-review-current) for the full setup guide,
prerequisites, inputs, and the failure contract.

Minimal caller:

```yaml
# .github/workflows/pi-pr-review.yml in YOUR repo
name: pi PR Review

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]

jobs:
  pi-review:
    uses: Shadowsong27/gh-actions-commons/.github/workflows/pi-pr-review.yml@main
    permissions:
      contents: read
      pull-requests: write
      issues: write
```

## Editing the prompt

Changes here affect **every** consumer pinned to the ref you push to. Two rules:

- **Additive repo rules belong in the consumer**, in its `repo-prompt-file`
  (default `.github/opencode-pr-review-prompt.md`) — not here.
- **Preserve the "Convergence and severity discipline" section.** Without it the
  reviewer surfaces fresh low-confidence findings every round and no PR ever reaches
  clean. It also defines the `No blocking findings.` convergence signal that automated
  merge gates key on.

## Editing the extractor

`pi-extract-review.py` deliberately shape-checks every nested field and swallows
malformed events. A traceback here would produce a review with **no classified failure
body**, which is precisely the silent-failure mode the script exists to end — a
violation must degrade to "agent did not start", never crash.

Its exit code is load-bearing in two places: the workflow's model fallback loop treats
exit `0` as "stop, this model answered", and the `post` job turns the check red when the
emitted body carries `<!-- pi-pr-review-failed -->`.
