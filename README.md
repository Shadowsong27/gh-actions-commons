# gh-actions-commons

Composite actions for CI/CD workflows across Shadowsong27 repositories.

## Available Actions

### `opencode-review`

Runs an OpenCode AI review on a PR diff and posts the result as a comment.

**Usage:**

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

**Inputs:**

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `model` | no | `openai/gpt-5.5` | OpenCode model for review |
| `prompt-file` | no | `""` | Path to repo-specific prompt additions |
| `base-ref` | no | `github.base_ref` | Base ref for diff generation |
| `pr-number` | no | PR number | PR number for review title |
| `head-sha` | no | Head SHA | Commit SHA being reviewed |

The action bundles a **default prompt** with general review rules. If `prompt-file` is provided, its contents are appended under a "Repository-specific rules" section.

## Testing locally

https://github.com/nektos/act
