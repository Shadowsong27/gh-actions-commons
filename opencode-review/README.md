# opencode-review

Runs an OpenCode AI review on a PR diff and posts the result as a comment.

## Usage

```yaml
jobs:
  review:
    runs-on: self-hosted
    permissions:
      contents: read
      issues: write
      pull-requests: write
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: Shadowsong27/gh-actions-commons/opencode-review@main
        with:
          model: ${{ vars.OPENCODE_REVIEW_MODEL || 'openai/gpt-5.5' }}
          prompt-file: '.github/opencode-pr-review-prompt.md'  # optional
```

## Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `model` | no | `openai/gpt-5.5` | OpenCode model for review |
| `prompt-file` | no | `""` | Path to repo-specific prompt additions (relative to repo root) |
| `base-ref` | no | `github.base_ref` | Base ref for diff generation |
| `pr-number` | no | PR number | PR number for review title |
| `head-sha` | no | Head SHA | Commit SHA being reviewed |

## Prompt behavior

The action bundles a **default prompt** with general review rules. If `prompt-file` is provided, its contents are appended under a `## Repository-specific rules` section.

Create `.github/opencode-pr-review-prompt.md` in your repo to add project-specific checks. Example:

```markdown
## My-project-specific checks

- Rule 1: description
- Rule 2: description
```

### Convergence guardrail (anti-loop)

The default prompt includes a **"Convergence and severity discipline"** section. Because
the action re-runs on every push and cannot see its own previous reviews, an unconstrained
reviewer tends to surface fresh low-confidence/speculative findings each round, so a PR
never converges to mergeable. The guardrail enforces a confidence gate, calibrated
severity, diff-scoped review (no re-litigating pre-existing patterns), and a `No blocking
findings.` convergence signal so consumers can stop the fix-up loop. Keep this in mind if
you override the prompt — additive `prompt-file` rules should not reintroduce
exhaustive-speculation behavior.

> Structural note: this action posts a **new** comment each run rather than threading
> prior findings into the reviewer's context. Feeding the latest prior review comment back
> into the prompt would let the reviewer avoid re-raising already-addressed items — a
> worthwhile future enhancement beyond the prompt-level guardrail above.
