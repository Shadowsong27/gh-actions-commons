# gh-actions-commons

Shared CI/CD building blocks for Shadowsong27 repositories.

## Catalog

| Unit | Kind | Status | Purpose |
|------|------|--------|---------|
| [`pi-pr-review`](.github/workflows/pi-pr-review.yml) | Reusable workflow | ✅ **Current** | AI PR review via [pi.dev](https://pi.dev). Two-job, hardened. |
| [`opencode-review`](opencode-review/) | Composite action | ⚠️ **Deprecated** | Former AI PR reviewer. Kept as a historical record — see [why](#opencode-review-deprecated). |

---

## `pi-pr-review` (current)

The automated PR reviewer. Posts a single clearly-marked comment with findings and token
stats, and names the model that actually answered.

**It is a reusable workflow, not a composite action.** The reviewer is fundamentally two
jobs and that split is a security boundary: `review` runs the agent with a **read-only**
token, `post` publishes the comment and runs **no agent**. A composite action can only
contribute steps to a single job, which would force every consumer to hand-maintain that
boundary — and one repo forgetting `permissions:` would hand a write-scoped token to a
bash-capable agent reading attacker-influenceable diff content. Centralizing both jobs
here makes the boundary un-droppable.

### Prerequisites

This is **not** a hosted GitHub App and **cannot run on GitHub-hosted runners**. There is
no org-level install; "installing the bot" means adding the caller workflow below.

- A **self-hosted runner**, online, labelled `self-hosted, Linux, X64`.
- On that runner: **`pi` ≥ 0.80.10** (needs `--exclude-tools` and `--no-approve`),
  **node ≥ 22** (pi 0.80.x crashes on node 20 via undici `cachestorage.js`), plus `git`,
  `jq`, `python3`.
- `pi` authenticated and configured with a model provider, for the **user the runner
  service runs as** — not just your login shell.

Where pi, node and the provider config are installed once on a runner host and shared by
every runner on it, individual repos need no per-repo pi install. Confirm a runner is
available — check repo level, then org:

```bash
gh api /repos/<owner>/<repo>/actions/runners \
  --jq '.total_count, (.runners[] | {name,status,labels:[.labels[].name]})'
```

### Setup

> **Setting this up with an agent?** Point it at
> [`pi-review/AGENT-SETUP.md`](pi-review/AGENT-SETUP.md) — a self-contained, agent-directed
> instruction covering preconditions, wiring, verification and failure triage:
>
> ```
> Read https://github.com/Shadowsong27/gh-actions-commons/blob/main/pi-review/AGENT-SETUP.md
> and set up the pi.dev PR reviewer on this repository.
> ```
>
> **`pi-models` is required.** Set it to models your runner's `pi` can actually resolve;
> there is deliberately no default (see [Inputs](#inputs)).

Add **one file** to the consuming repo:

```yaml
# .github/workflows/pi-pr-review.yml
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
      # Required. Ordered fallback chain; every name must resolve on your runner.
      pi-models: <your-primary-model>,<your-fallback-model>
```

The `permissions:` block on the calling job sets the **ceiling** for the reusable
workflow. It is required when your repo's default `GITHUB_TOKEN` permissions are
read-only, otherwise the `post` job cannot comment. It does **not** weaken the boundary:
the `review` job declares its own read-only `permissions` internally, which further
narrows this ceiling.

That's it — no secrets to wire (the workflow uses only `github.token`), and no files to
copy. The prompt and extractor come from this repo, pinned to the commit of the workflow
version you referenced.

Then, optionally:

- **Repo-specific review rules** — create `.github/opencode-pr-review-prompt.md` in your
  repo with only *additional* rules. It is appended under a "Repository-specific rules"
  heading, and skipped silently when absent. (The filename is inherited from the OpenCode
  reviewer so migrating repos keep their rules without a rename.)
- **Disable an existing OpenCode reviewer** — do not run both. Prefer switching its
  trigger to `on: workflow_dispatch:` over deleting the file, so a revert is cheap.

> **Pushing to `.github/workflows/` needs the `workflow` OAuth scope.** If a `gh`-token
> push is rejected with *"refusing to allow an OAuth App to create or update workflow …"*,
> push over **SSH** (`git@github.com:...`), which is not subject to that restriction.

> **Which workflow file GitHub uses:** for `pull_request` on a same-repo PR, GitHub reads
> the workflow from the **PR's head branch**. So commit the caller onto a PR's head branch
> to have pi review that PR immediately (it will review its own install); merge to the
> default branch to install for future PRs only.

> **Do not add a `concurrency:` block to the caller.** This workflow already cancels
> superseded runs internally. If a caller declares a concurrency group that the called
> workflow also uses, GitHub **deadlocks** — the caller holds the group while the called
> run waits for it, and neither starts. The internal group is namespaced
> (`pi-review-reusable-<repo>-<pr>`) specifically so it cannot collide with the
> `pi-review-<pr>` group the old standalone workflow used, but a caller that invents its
> own group is still the consumer's risk to manage.

### Inputs

`pi-models` is required; everything else has a working default.

| Input | Default | Description |
|-------|---------|-------------|
| `pi-models` | **required** | Ordered fallback chain, tried left to right. Put the model you actually want first. Every name must resolve on your runner. |
| `runs-on` | `self-hosted` | Runner label. Must satisfy the prerequisites above. |
| `timeout-minutes` | `20` | Timeout for the review job. |
| `repo-prompt-file` | `.github/opencode-pr-review-prompt.md` | Path in **your** repo with additive repo-specific rules. Skipped silently if absent. |
| `base-prompt-file` | `""` | Path in **your** repo that *replaces* the shared base prompt wholesale. Prefer `repo-prompt-file`; a declared-but-missing file fails the job loudly. |
| `commons-ref` | `main` | Fallback ref for fetching the shared prompt/extractor. Normally unused — see below. |
| `required-workflows` | `""` | **Cost gate 1** (workflow_run callers only). Space/comma-separated CI workflow **names** that must all be green on the reviewed commit. Empty = the workflow that fired the event — correct for a repo with **one** CI workflow. Multi-CI-workflow repos must list them all. See [Cost gates](#cost-gates-opt-in). |
| `skip-paths` | `""` | **Cost gate 2.** Newline/comma-separated globs (`*`, `?`, `**`). When **every** changed file matches, the review is skipped with no model call. Empty = never skip. |
| `skip-comment` | `true` | When a PR is skipped by `skip-paths`, post one upserted note comment so the skip is visible. `false` = skip silently. |

**Model chain semantics.** Prefer entries from *different providers* over retries of one
model: the failure being absorbed is a provider-side quota or outage that can take
minutes-to-hours to clear, which a same-model retry cannot rescue. Crossing API modes is
fine and useful — that is exactly why failover lives in this workflow rather than in an
upstream gateway, which cannot bridge modes mid-failover. The published comment names
**the model that answered**, not the configured first entry.

`pi-models` is **required and has no default**, on purpose. A default would silently apply
one deployment's model names to every consumer, and the failure that produces surfaces as
an opaque quota/auth error rather than as the configuration mistake it is.

**Asset pinning.** The workflow fetches its prompt and extractor from this repo at
`job.workflow_sha` — the exact commit of the workflow file you pinned — so they can't
drift from the logic consuming them. A caller on `@v1` gets `v1`'s prompt. That context
value is attempted rather than trusted (it is unavailable on GitHub Enterprise Server and
on older runners), so the pinned checkout is allowed to fail and `commons-ref` is used
instead; if neither yields the assets, the job fails loudly rather than reviewing with an
empty prompt.

### Cost gates (opt-in)

Each PR review spends a model call (some chains route to metered paid APIs), so two gates
let a repo review **less** without losing coverage. Both are **off by default** — a caller
that changes nothing keeps reviewing every push.

**Gate 1 — review only after CI is green.** The default `pull_request` caller reviews on
every push, including pushes that fail CI, so a flaky integration run gets re-reviewed on
every retry. Instead, trigger the caller on your CI workflow **completing**:

```yaml
# .github/workflows/pi-pr-review.yml
name: pi PR Review

on:
  workflow_run:
    workflows: ["CI"]          # <-- your repo's CI workflow name(s). THIS is the per-repo knob.
    types: [completed]

jobs:
  pi-review:
    uses: Shadowsong27/gh-actions-commons/.github/workflows/pi-pr-review.yml@main
    permissions:
      contents: read
      pull-requests: write
      issues: write
      actions: read            # <-- gate reads other workflows' run status
    with:
      pi-models: <your-primary-model>,<your-fallback-model>
      required-workflows: "CI" # optional; defaults to the workflow that fired. Required if you have >1 CI workflow.
      skip-paths: |            # optional; Gate 2, see below
        **/*.md
        docs/**
```

No model call is spent until the required CI workflow(s) concluded `success` on the
commit. A failing run concludes `failure`, so retries never trigger a review; the review
fires once, after the last required workflow turns green.

**Different repos have different CI — how is that handled?** The trigger lives in *your*
caller, not in this shared workflow, so the CI workflow name(s) are named once, per repo,
in `on.workflow_run.workflows` (and, for more than one, in `required-workflows`). This
shared workflow stays CI-agnostic: it is handed the completed run and verifies the named
workflow(s) are all green on that commit. `workflow_run` re-fires as each named workflow
finishes, and an internal idempotency guard collapses those firings into a single review.

Notes specific to `workflow_run`:

- The caller must live on the **default branch** to fire, and `workflow_run` always runs
  the workflow from the default branch — so a fix to the reviewer on `main` reaches open
  PRs immediately (unlike the `pull_request` trigger, which runs the PR head's copy).
- Grant the caller `actions: read` (the gate reads other workflows' run status) on top of
  the usual `pull-requests: write` / `issues: write`.
- Do **not** also keep a `pull_request`-triggered copy — that reviews twice.

**Gate 2 — skip trivial docs/ops PRs.** Set `skip-paths` (works under either trigger). When
**every** file changed in the PR matches one of the globs, the review is skipped with no
model call; a PR touching any non-matching file is always reviewed. By default one short
note comment is posted so the skip is visible (`skip-comment: false` to silence it).

### Reading the result — the part automation gets wrong

| | |
|-|-|
| Comment marker | `<!-- pi-pr-review -->`, posted by `github-actions[bot]` as an **issue** comment |
| Read it with | `gh api repos/<o>/<r>/issues/<n>/comments` |
| **Not** with | `gh api repos/<o>/<r>/pulls/<n>/reviews` or `gh pr view --json comments` — both return **empty** for a bot poster, and `gh` returns empty rather than erroring, so this fails toward false confidence |
| Comment shape | `## Findings` · `## Open Questions` · `## Notes` · `### Token Usage` |
| Reviewed commit | Stated in the body; compare to `head.sha` to tell a fresh round from a stale re-read |

```bash
# Latest pi review comment on PR <n>
gh api repos/<owner>/<repo>/issues/<n>/comments \
  --jq '[.[] | select(.body|contains("<!-- pi-pr-review -->"))] | last | .body'
```

**A green `review` check means the job ran — never "no findings".** The reviewer is
advisory and is not a required check. The two jobs are separate, so `review` can succeed
while `post` has not yet published; check completion is not "the review is in".

**Polling for a fresh review:** capture the max `github-actions[bot]` comment id **before**
pushing and poll for a newer id. Polling on check status is wrong — immediately after a
push GitHub has not yet created the new run, so a loop keyed on "no check is queued or
in progress" reads the *previous* commit's green checks and exits on iteration 1.
Identical `### Token Usage` counts across "two rounds" means you re-read one comment.

### Failure contract

When the review could not run — pi did not start, quota/auth failure, or a mid-stream
backend crash — the extractor emits `<!-- pi-pr-review-failed -->` with a
`## ⚠️ REVIEW DID NOT RUN` banner and a one-line cause, and the `post` job calls
`core.setFailed(...)` so the check turns **red**.

That red check is the **expected signal, not a wiring error**. A merge gate must treat a
comment containing `<!-- pi-pr-review-failed -->` as a failed review, never a clean pass.
The model chain reduces how often this fires; it does not remove the terminal case.

### Security posture

Preserve all of it:

- **`review` job** — read-only `permissions`; scrubs the persisted checkout credential
  from `.git/config` before invoking the agent; passes `GITHUB_TOKEN: ""` to the pi step.
  It never holds a write-scoped token.
- **`post` job** — write-scoped, runs **no agent**.
- **`--no-approve`** — stops pi trusting PR-controlled project-local `.pi` resources
  (extensions/skills) that would otherwise load before the tool denylist applies.
- **`--exclude-tools edit,write`** — removes pi's mutation tools.
- **Same-repo, non-draft guard** — keeps fork PRs off the private self-hosted runner. Do
  not remove this on public repos.

Never "fix" a permissions error in the `review` job by granting it write scope — the
write-scoped `post` job exists precisely to keep the agent tokenless.

> **⚠️ Known residual (High):** pi keeps its `bash` tool, so a successful prompt injection
> can still run commands on the self-hosted runner (read host secrets, reach any network
> the runner can reach) despite the read-only GitHub token. The complete fix is to sandbox the
> agent step in a container/VM with no host credentials or network — or to restrict tools
> to a read-only allowlist, sacrificing agentic investigation. **Adopting this workflow
> does not close it.** Track per-repo.

---

## `opencode-review` (deprecated)

⚠️ **Do not wire into new repositories.** Superseded by `pi-pr-review` above. It remains
here as a **historical record**, with its files restored verbatim.

**Why it was retired.** On some gateway-routed model paths OpenCode posts **empty**
reviews: a mid-stream backend `response.failed` leaves the run emitting only a preamble
and token stats — no findings — while the workflow still reports success. A green check
therefore did not mean "no findings", it meant "the reviewer silently died". pi.dev
degrades loudly instead, via the failure contract above.

It also runs the agent and posts the comment **in one job**, so the model-influenced step
shares a job with a write-scoped token — the flaw pi.dev's two-job split exists to fix.

**Why the files are back.** The action was deleted outright in `ef27908`. Repos still
referencing `Shadowsong27/gh-actions-commons/opencode-review@main` did not become cleanly
disabled — they began **failing at action resolution** on every PR. Restoring the
directory makes the history legible and lets those references resolve again while each
repo migrates.

**Migrating off it:** add the pi caller workflow above, keep your existing
`.github/opencode-pr-review-prompt.md` (pi appends it automatically), then switch the old
`opencode-review.yml` to `on: workflow_dispatch:` rather than deleting it.

Usage details and inputs, for the record: [`opencode-review/README.md`](opencode-review/README.md).

---

## Contributing

- **Composite actions** live in their own top-level directory with an `action.yml` and a
  local `README.md`, and are consumed as `Shadowsong27/gh-actions-commons/<name>@<ref>`.
- **Reusable workflows** live in `.github/workflows/` with `on: workflow_call`, and are
  consumed as `Shadowsong27/gh-actions-commons/.github/workflows/<file>@<ref>`. Any
  supporting assets get a sibling top-level directory with its own `README.md` (see
  [`pi-review/`](pi-review/)).

Reach for a reusable workflow when the unit spans **multiple jobs** — especially when the
job split carries a security boundary, as with `pi-pr-review`. A composite action cannot
express that, and pushing it into consumers is how the boundary drifts.

Changes here affect every consumer pinned to the ref you push to. Prefer additive,
input-gated changes over edits that alter default behaviour for existing repos.
