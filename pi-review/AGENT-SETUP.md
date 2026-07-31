---
name: setup-pi-pr-review
description: Install the pi.dev AI pull-request reviewer on a GitHub repository by wiring the gh-actions-commons reusable workflow.
---

# Agent instruction: install the pi.dev PR reviewer

**You are an agent setting this up on a repository.** Follow these steps in order. This
file is self-contained — you do not need any other document.

If you are a human: this is written for an agent, but the commands work by hand. The
prose explanation lives in the [root README](../README.md#pi-pr-review-current).

## What you are installing

An automated PR reviewer. On every non-draft, same-repo pull request it runs the
[pi](https://pi.dev) coding agent over the diff *plus the surrounding checkout*, then
posts one comment containing findings and token usage.

It is a **reusable workflow** consumed from `Shadowsong27/gh-actions-commons`. You add
**one file** to the target repo. Do not copy the reviewer's internals.

---

## ⛔ Read this before anything else

**1. This cannot run on GitHub-hosted runners.** It needs a **self-hosted runner** with
the `pi` binary installed. There is no GitHub App and no marketplace install. If the
target repo has no self-hosted runner, **STOP and tell the user** — this is a hosting
prerequisite you cannot satisfy by editing files.

**2. `pi-models` is required and has no default.** You must set it to models the target
runner's `pi` can actually resolve. There is deliberately no default — one would silently
apply someone else's model names to your repo, and the resulting failure surfaces as an
opaque quota/auth error rather than as the configuration mistake it is. Getting this wrong
is the single most common way this install fails: every entry fails, and the reviewer
posts `⚠️ REVIEW DID NOT RUN` and turns the check red on every PR.

**3. You are consuming a third-party workflow.** `@main` means the upstream owner can
change what runs in your CI at any time. Prefer pinning to a full commit SHA (see
[Pinning](#pinning)).

---

## Step 1 — Verify a self-hosted runner exists and is online

```bash
# repo-level runners
gh api /repos/<owner>/<repo>/actions/runners \
  --jq '.total_count, (.runners[] | {name, status, labels: [.labels[].name]})'

# if none, check org-level
gh api /orgs/<org>/actions/runners \
  --jq '.total_count, (.runners[] | {name, status, labels: [.labels[].name]})'
```

Require at least one runner with `status: "online"`. Note its labels — you will pass them
as `runs-on` if they are not plain `self-hosted`.

**STOP if there is no online self-hosted runner.** Report this to the user rather than
proceeding; the workflow would queue forever.

## Step 2 — Confirm the runner can actually run `pi`

On the runner host (SSH, or ask the user to run it):

```bash
pi --version          # must be >= 0.80.10 — earlier builds lack --exclude-tools/--no-approve
node --version        # must be >= 22 — pi 0.80.x crashes on node 20 (undici cachestorage.js)
git --version; python3 --version; jq --version
```

`pi` must also be **authenticated / configured with a model provider** for the whole user
the runner service runs as (commonly `HOME=/home/<runner-user>`), not just your login
shell.

If you cannot reach the host, say so plainly and continue — Step 6 is the real test — but
**do not report the install as working until Step 6 passes.**

## Step 3 — Decide the model chain

List the models the runner's `pi` can resolve (`pi models` / `pi --help`, or the user's pi
config), then pick an **ordered fallback chain**, best first:

```
<primary-model>,<fallback-model>
```

Rules that make the chain worth having:

- Use **different providers**, not the same model twice. The failure being absorbed is
  quota/outage on one provider, which a same-model retry cannot rescue.
- Two entries is usually enough. One is valid if that's all that's available.

**If you cannot determine any valid model name, ask the user.** Do not guess — there is
no default to fall back on, and the workflow will not start without this input.

## Step 4 — Add the caller workflow

Create `.github/workflows/pi-pr-review.yml` in the **target** repo:

```yaml
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
    with:
      pi-models: <primary-model>,<fallback-model>   # REQUIRED — see Step 3
      # runs-on: my-runner-label                    # only if not plain `self-hosted`
```

The `permissions:` block is the **ceiling** granted to the reusable workflow, and is
required when the repo's default `GITHUB_TOKEN` is read-only. It does **not** weaken the
security split: the internal `review` job declares its own read-only permissions, which
narrow this further.

**Do not add a `concurrency:` block to this caller.** The reusable workflow already
cancels superseded runs. A caller sharing a concurrency group with its callee **deadlocks**
in GitHub Actions.

### Inputs

`pi-models` is required. The rest have working defaults; the workflow also declares a
couple of escape hatches you should not need to touch.

| Input | Default | Use when |
|-------|---------|----------|
| `pi-models` | **required** | Always. Every name must resolve on the runner. |
| `runs-on` | `self-hosted` | Your runner needs a different label. |
| `timeout-minutes` | `20` | Large repos where review exceeds 20 min. |
| `repo-prompt-file` | `.github/opencode-pr-review-prompt.md` | You keep repo-specific rules at another path. |
| `base-prompt-file` | `""` | You want to replace the shared base prompt wholesale. Prefer additive rules instead. |

## Step 5 — Optional: repo-specific review rules

Create `.github/opencode-pr-review-prompt.md` containing **only additional** rules. It is
appended to the shared prompt under a "Repository-specific rules" heading, and skipped
silently when absent. (The filename is historical; change it with `repo-prompt-file`.)

Good content: project conventions, by-design exceptions the reviewer keeps re-flagging,
architecture rules. Do not restate generic review advice — the base prompt covers it.

If the repo has an **existing AI reviewer**, disable it now; do not run two. Prefer
switching its trigger to `on: workflow_dispatch:` over deleting it, so revert is cheap.

## Step 6 — Push, then verify the first real run

Which branch you commit to decides what gets reviewed. For `pull_request` on a same-repo
PR, GitHub reads the workflow **from the PR's head branch**:

- **To review an open PR immediately:** commit onto that PR's head branch.
- **To install for future PRs only:** merge to the default branch.

> If a push is rejected with *"refusing to allow an OAuth App to create or update workflow
> ... without `workflow` scope"*, push over **SSH** (`git@github.com:...`), which is not
> subject to that restriction.

Then read the result. **The comment is an _issue_ comment:**

```bash
gh api repos/<owner>/<repo>/issues/<pr>/comments \
  --jq '[.[] | select(.body | contains("<!-- pi-pr-review -->"))] | last | .body'
```

**Success criterion — all three must hold:**

1. A comment containing `<!-- pi-pr-review -->` exists.
2. It contains a `### Token Usage` section with **non-zero** input tokens.
3. It does **not** contain `<!-- pi-pr-review-failed -->`.

That combination is what proves the runner really has pi, node 22, and a resolvable model.

---

## Guardrails — do not do these

- **Do not report success from a green check.** The `review` check going green means *the
  job ran*, never *the review was clean*. The reviewer is advisory and is not a required
  check. Always read the comment body.
- **Do not use `gh api .../pulls/<n>/reviews` or `gh pr view --json comments`.** Both
  return **empty** for a `github-actions[bot]` poster, and `gh` returns empty rather than
  erroring — so this failure mode looks like "no findings".
- **Do not treat `<!-- pi-pr-review-failed -->` as a pass.** It means the review did not
  run. The red check is the intended signal, not a wiring bug.
- **Do not grant the `review` job write permissions.** A write-scoped `post` job exists
  precisely so the agent stays tokenless. If you hit a permissions error there, you are
  fixing the wrong job.
- **Do not remove the same-repo / non-draft `if:` guard.** On a public repo it is what
  keeps arbitrary fork PRs from executing an agent on your private runner.
- **Do not copy the reusable workflow's internals into the target repo.** Consume it.

## Failure triage

| Symptom | Cause | Fix |
|---|---|---|
| Job queues forever | No online runner with the requested label | Step 1; check `runs-on` matches the label |
| `pi: command not found` | pi not installed for the runner's user | Install pi on the runner host |
| Crash in `undici`/`cachestorage.js` | node < 22 | Upgrade node on the runner |
| `REVIEW DID NOT RUN`, 0 input tokens | Model name unresolvable, or pi unauthenticated | **Almost always `pi-models`** — Step 3 |
| `REVIEW DID NOT RUN`, tokens > 0 | Mid-stream backend failure | Usually transient; a second provider in the chain absorbs it |
| Comment posted but findings look generic | Base prompt only | Add repo rules, Step 5 |
| No comment and no run at all | Workflow not on the PR's head branch, or PR is a draft/fork | Step 6 |

## Pinning

This repo publishes no tags, so `@main` is the only moving ref. For a third-party
dependency in your CI, prefer a full commit SHA:

```bash
gh api repos/Shadowsong27/gh-actions-commons/commits/main --jq .sha
```

```yaml
uses: Shadowsong27/gh-actions-commons/.github/workflows/pi-pr-review.yml@<full-sha>
```

The workflow fetches its own prompt and extractor at the commit of the workflow file you
pinned, so pinning freezes the reviewer's behaviour as a unit.

## Security note to pass on to the user

The review job is hardened — read-only token, checkout credential scrubbed, agent run with
`--no-approve --exclude-tools edit,write`, and a separate write-scoped job that runs no
agent. **But pi retains its `bash` tool**, so a successful prompt injection via PR content
can still execute commands on the self-hosted runner, despite the read-only GitHub token.

Treat the runner as compromisable by anyone who can open a PR that the workflow reviews.
Do not run this on a runner holding credentials you would not expose to a PR author. The
complete fix is sandboxing the agent step (container/VM, no host credentials or network).
