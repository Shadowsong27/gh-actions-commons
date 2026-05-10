Review this pull request diff.

Follow the repository's AGENTS.md and documented rules.
Review only the attached diff.

## General checks

- Correctness: look for bugs, behavioral regressions, off-by-one errors, race conditions, and logic flaws.
- Architecture: verify proper layering, domain boundaries, and import patterns. Flag circular dependencies or violations of established conventions.
- Tests: flag missing or weak tests for changed behavior. New logic should have corresponding test coverage.
- Security: identify safety issues, secret leaks, injection risks, or unsafe assumptions.
- Documentation: note drift when behavior changes but docs don't.

## Review priorities (ordered)

1. Correctness bugs and behavioral regressions
2. Architecture violations and improper domain placement
3. Missing/weak tests for changed behavior
4. Security/safety issues
5. Documentation drift when behavior changes

## Output

Use this exact structure:

```markdown
## Findings

- [High] <short title> - `<file>:<line>`
  <1-3 sentences explaining the impact and the concrete fix.>

- [Medium] <short title> - `<file>:<line>`
  <1-3 sentences explaining the impact and the concrete fix.>

- [Low] <short title> - `<file>:<line>`
  <1-3 sentences explaining the impact and the concrete fix.>

## Open Questions

- <Only include if needed; otherwise write "None.">

## Notes

- <Optional brief residual risks, testing gaps, or "No findings." when clean.>
```

- Order findings by severity: High, then Medium, then Low.
- Use High for blockers that break core behavior, data safety, security, or CI.
- Use Medium for likely bugs, behavioral regressions, architecture violations, missing tests for changed behavior.
- Use Low for documentation drift, maintainability issues, or low-risk polish worth fixing before merge.
- Every finding must include a file:line reference when possible.
- If there are no findings, write `## Findings` followed by `No findings.`.
- Be concise and actionable; skip praise.
