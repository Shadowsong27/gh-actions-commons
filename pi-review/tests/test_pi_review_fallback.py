"""Tests for the shared pi reviewer's shell — model fallback, security posture,
model attribution, and the CI-green / docs-only gate (HARN-136 and the cost gates).

These do NOT re-implement the logic — they extract the real steps straight out of
`.github/workflows/pi-pr-review.yml` and execute them against stubs. Testing a copy of
the shell would let the copy and the shipped workflow drift, which is exactly the
failure mode the standalone-reviewer repos kept hitting with `cp`'d reviewer files.
This test used to live in a consumer repo (agentic-conductor) guarding its LOCAL copy;
it now lives beside the single shared copy it validates.

Covered:
  * model chain — first-usable, fall-through, total exhaustion, whitespace entries;
  * the post job names the model that ACTUALLY answered (transparency property);
  * the security posture needles survive edits;
  * the gate's Gate 2 (docs-only skip) and reviewability guards, run against the real
    gate script with stubbed GitHub APIs.

Node is required (the post + gate steps are github-script JS). Runs with a stub `pi`
and the REAL extractor, so it needs no self-hosted runner or provider auth.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "pi-pr-review.yml"
EXTRACTOR = REPO / "pi-review" / "pi-extract-review.py"
FAILURE_MARKER = "<!-- pi-pr-review-failed -->"

_HAVE_NODE = subprocess.run(["which", "node"], capture_output=True).returncode == 0

# A stub `pi`: writes an events file whose shape depends on the requested model.
# "good" in the model name -> a usable review; anything else -> an empty response
# with zero input tokens, which the real extractor classifies as a failure.
_STUB_PI = """#!/usr/bin/env python3
import json, sys
model = ""
argv = sys.argv[1:]
for i, a in enumerate(argv):
    if a == "--model" and i + 1 < len(argv):
        model = argv[i + 1]
usable = "good" in model
content = [{"type": "text", "text": "## Findings\\n\\nNo findings. (%s)" % model}] if usable else []
evt = {
    "type": "message_end",
    "message": {
        "role": "assistant",
        "content": content,
        "usage": {"input": 100 if usable else 0, "output": 10 if usable else 0,
                  "reasoning": 0, "cacheRead": 0, "cacheWrite": 0},
    },
}
print(json.dumps(evt))
"""


def _review_step_script() -> str:
    """Pull the `Run pi review` step's shell out of the real workflow."""
    wf = yaml.safe_load(WORKFLOW.read_text())
    for step in wf["jobs"]["review"]["steps"]:
        if step.get("name") == "Run pi review":
            return step["run"]
    raise AssertionError("'Run pi review' step not found in the workflow")


def _run_chain(tmp_path: Path, models: str) -> tuple[str, str, int]:
    """Execute the real step shell with a stub `pi`. Returns (model_file, review, rc).

    The shared workflow stages the extractor into RUNNER_TEMP before this step; the
    consumer repo's local copy sat at `.github/scripts/`. Here we stage the repo's own
    `pi-review/pi-extract-review.py` where the step expects it.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "pi"
    stub.write_text(_STUB_PI)
    stub.chmod(0o755)

    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    (runner_temp / "prompt.md").write_text("review this")
    (runner_temp / "pr.diff").write_text("diff --git a/x b/x\n")
    # The step runs `python3 "$RUNNER_TEMP/pi-extract-review.py"`; stage the real one.
    (runner_temp / "pi-extract-review.py").write_text(EXTRACTOR.read_text())

    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "RUNNER_TEMP": str(runner_temp),
        "PI_MODELS": models,
        "GITHUB_TOKEN": "",
    }
    # GitHub Actions' default shell for `run:` blocks.
    proc = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", _review_step_script()],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )
    model_file = (
        (runner_temp / "pi-model.txt").read_text()
        if (runner_temp / "pi-model.txt").exists()
        else ""
    )
    review = (
        (runner_temp / "pi-review.md").read_text()
        if (runner_temp / "pi-review.md").exists()
        else ""
    )
    return model_file, review, proc.returncode


class TestModelFallbackChain:
    def test_first_model_usable_no_fallback(self, tmp_path: Path) -> None:
        model, review, rc = _run_chain(tmp_path, "good-one,bad-two,bad-three")
        assert rc == 0
        assert model == "good-one"
        assert FAILURE_MARKER not in review
        assert "No findings" in review

    def test_falls_through_to_second(self, tmp_path: Path) -> None:
        model, review, rc = _run_chain(tmp_path, "bad-one,good-two,bad-three")
        assert rc == 0
        assert model == "good-two", "must report the model that ACTUALLY answered"
        assert FAILURE_MARKER not in review

    def test_falls_through_to_third(self, tmp_path: Path) -> None:
        model, review, rc = _run_chain(tmp_path, "bad-one,bad-two,good-three")
        assert rc == 0
        assert model == "good-three"
        assert FAILURE_MARKER not in review

    def test_total_exhaustion_preserves_the_failure_signal(
        self, tmp_path: Path
    ) -> None:
        """The HARN-97 signal must survive a fully-exhausted chain.

        If fallback swallowed the failure body, a dead chain would publish a
        comment that reads clean — reintroducing the exact bug HARN-97 removed.
        """
        model, review, rc = _run_chain(tmp_path, "bad-one,bad-two,bad-three")
        assert rc == 0, (
            "step must exit 0 so the artifact uploads and the comment is published"
        )
        assert FAILURE_MARKER in review
        assert "REVIEW DID NOT RUN" in review
        assert model.startswith("NONE")
        assert "bad-one,bad-two,bad-three" in model, "must name every model tried"

    def test_tolerates_whitespace_and_empty_entries(self, tmp_path: Path) -> None:
        model, review, rc = _run_chain(tmp_path, "bad-one, ,  good-two ,")
        assert rc == 0
        assert model == "good-two"
        assert FAILURE_MARKER not in review

    def test_single_model_chain_still_works(self, tmp_path: Path) -> None:
        model, review, rc = _run_chain(tmp_path, "good-only")
        assert rc == 0
        assert model == "good-only"


class TestWorkflowContract:
    """Guards on the wiring the loop depends on."""

    def test_artifact_carries_the_model_file(self) -> None:
        """post/ cannot name the real model unless pi-model.txt is uploaded."""
        wf = yaml.safe_load(WORKFLOW.read_text())
        upload = next(
            s
            for s in wf["jobs"]["review"]["steps"]
            if s.get("name") == "Upload review artifact"
        )
        assert "pi-model.txt" in upload["with"]["path"]
        assert "pi-review.md" in upload["with"]["path"]

    def test_no_stale_single_model_references(self) -> None:
        """PI_MODEL was replaced by PI_MODELS; a leftover would read as empty."""
        raw = WORKFLOW.read_text()
        assert "process.env.PI_MODEL}" not in raw
        assert '"$PI_MODEL"' not in raw

    @pytest.mark.parametrize(
        "needle",
        ["contents: read", "extraheader", 'GITHUB_TOKEN: ""', "--no-approve"],
    )
    def test_security_posture_intact(self, needle: str) -> None:
        """HARN-79 posture must survive edits to this workflow."""
        assert needle in WORKFLOW.read_text()


# --------------------------------------------------------------------------
# Seam: review job writes pi-model.txt -> post job reads it into the comment.
#
# Both halves were individually covered above, but the CONTRACT BETWEEN them was
# not — a post job that posts "unknown" or the configured first model would keep
# every test above green while breaking the transparency property that justifies
# doing fallback in the harness at all.
# --------------------------------------------------------------------------

_NODE_HARNESS = """
const fs = require('fs');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const script = fs.readFileSync(process.argv[2], 'utf8');
const captured = {};
const github = {
  paginate: async () => [],
  rest: { issues: {
    createComment: async (a) => { captured.body = a.body; },
    updateComment: async (a) => { captured.body = a.body; },
    listComments: () => {},
  } },
};
const context = {
  repo: { owner: 'o', repo: 'r' },
  payload: { pull_request: { number: 1, head: { sha: 'a'.repeat(40) } } },
};
const core = { setFailed: (m) => { captured.failed = m; }, warning: () => {}, info: () => {} };
const fn = new AsyncFunction('github', 'context', 'core', 'require', 'process', script);
fn(github, context, core, require, process)
  .then(() => { console.log(JSON.stringify(captured)); })
  .catch((e) => { console.error(e); process.exit(1); });
"""


def _post_step_script() -> str:
    wf = yaml.safe_load(WORKFLOW.read_text())
    for step in wf["jobs"]["post"]["steps"]:
        if step.get("name") == "Post PR comment":
            return step["with"]["script"]
    raise AssertionError("'Post PR comment' step not found in the workflow")


def _run_post_job(tmp_path: Path, runner_temp: Path) -> dict:
    """Execute the REAL post-job JS against an artifact dir, as Actions would.

    The reusable post step reads the PR context from env (gate outputs), not from the
    event payload, so supply PR_NUMBER / HEAD_SHA and leave SKIP_REASON empty to drive
    the normal (non-skip) review branch.
    """
    script_f = tmp_path / "post.js"
    script_f.write_text(_post_step_script())
    harness = tmp_path / "harness.js"
    harness.write_text(_NODE_HARNESS)
    proc = subprocess.run(
        ["node", str(harness), str(script_f)],
        env={
            **os.environ,
            "RUNNER_TEMP": str(runner_temp),
            "PR_NUMBER": "1",
            "HEAD_SHA": "a" * 40,
        },
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"post script failed: {proc.stderr}"
    return json.loads(proc.stdout)


def _chain_then_post(tmp_path: Path, models: str) -> dict:
    """Full seam: run the review shell, stage its artifact, run the post script."""
    runner_temp = tmp_path / "runner"
    _run_chain(tmp_path, models)
    review_dir = runner_temp / "review"
    review_dir.mkdir(exist_ok=True)
    for name in ("pi-review.md", "pi-model.txt"):
        (review_dir / name).write_text((runner_temp / name).read_text())
    return _run_post_job(tmp_path, runner_temp)


@pytest.mark.skipif(not _HAVE_NODE, reason="node not available")
class TestPublishedModelAttribution:
    def test_comment_names_the_fallback_model_not_the_configured_first(
        self, tmp_path: Path
    ) -> None:
        """The load-bearing property: report who ACTUALLY answered."""
        out = _chain_then_post(tmp_path, "bad-one,good-two,bad-three")
        assert "Model: `good-two`" in out["body"]
        assert "bad-one" not in out["body"], (
            "must not name the configured first model — that is the dishonesty "
            "a gateway-level fallback would have introduced"
        )
        assert "failed" not in out

    def test_comment_names_first_model_when_no_fallback_needed(
        self, tmp_path: Path
    ) -> None:
        out = _chain_then_post(tmp_path, "good-one,bad-two")
        assert "Model: `good-one`" in out["body"]
        assert "failed" not in out

    def test_exhausted_chain_publishes_failure_and_fails_the_job(
        self, tmp_path: Path
    ) -> None:
        out = _chain_then_post(tmp_path, "bad-one,bad-two,bad-three")
        assert FAILURE_MARKER in out["body"], "HARN-97 signal must reach the comment"
        assert "REVIEW DID NOT RUN" in out["body"]
        assert "NONE" in out["body"], "header should show the chain was exhausted"
        assert "failed" in out, "core.setFailed must fire so the check goes red"

    def test_comment_is_published_before_the_job_fails(self, tmp_path: Path) -> None:
        """Design D2 — a red check must never be left without its explanation."""
        out = _chain_then_post(tmp_path, "bad-one,bad-two")
        assert out.get("body"), "comment must exist even though the job failed"
        assert "failed" in out

    def test_missing_model_file_degrades_to_unknown_not_a_crash(
        self, tmp_path: Path
    ) -> None:
        """An older artifact (pre-HARN-136) has no pi-model.txt."""
        runner_temp = tmp_path / "runner"
        runner_temp.mkdir()
        review_dir = runner_temp / "review"
        review_dir.mkdir()
        (review_dir / "pi-review.md").write_text("## Findings\n\nNo findings.")
        out = _run_post_job(tmp_path, runner_temp)
        assert "Model: `unknown`" in out["body"]
        assert "failed" not in out


# --------------------------------------------------------------------------
# Gate: the cost gates run in a read-only `gate` job whose github-script decides
# should_review / skip_reason. This drives the REAL gate script (pull_request mode,
# which needs no CI-run lookup) with stubbed GitHub APIs — so the docs-only skip and
# the reviewability guards are tested against the shipped logic, not a copy.
# --------------------------------------------------------------------------

_GATE_HARNESS = """
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const fs = require('fs');
const script = fs.readFileSync(process.argv[2], 'utf8');
const input = JSON.parse(process.argv[3]);
const outputs = {};
const listFiles = Symbol('listFiles');
const github = {
  rest: {
    pulls: { listFiles },
    issues: { listComments: Symbol('listComments') },
    repos: { listPullRequestsAssociatedWithCommit: async () => ({ data: [] }) },
    actions: { listWorkflowRunsForRepo: Symbol('listWorkflowRunsForRepo') },
  },
  paginate: async (fn) => (fn === listFiles ? input.files.map((f) => ({ filename: f })) : []),
};
const context = {
  eventName: input.eventName,
  repo: { owner: 'o', repo: 'r' },
  payload: { pull_request: input.pr },
};
const core = {
  setOutput: (k, v) => { outputs[k] = v; },
  info: () => {},
  warning: () => {},
};
const fn = new AsyncFunction('github', 'context', 'core', 'process', script);
fn(github, context, core, process)
  .then(() => { console.log(JSON.stringify(outputs)); })
  .catch((e) => { console.error(e); process.exit(1); });
"""


def _gate_script() -> str:
    wf = yaml.safe_load(WORKFLOW.read_text())
    for step in wf["jobs"]["gate"]["steps"]:
        if step.get("name") == "Decide whether to review":
            return step["with"]["script"]
    raise AssertionError("gate 'Decide whether to review' step not found")


def _run_gate(tmp_path: Path, *, files: list[str], skip_paths: str, draft: bool = False) -> dict:
    """Drive the real gate script in pull_request mode. Returns its setOutput map."""
    script_f = tmp_path / "gate.js"
    script_f.write_text(_gate_script())
    harness = tmp_path / "gate-harness.js"
    harness.write_text(_GATE_HARNESS)
    pr = {
        "number": 1,
        "state": "open",
        "draft": draft,
        "head": {"sha": "a" * 40, "repo": {"full_name": "o/r"}},
        "base": {"ref": "main"},
    }
    payload = {"eventName": "pull_request", "pr": pr, "files": files}
    proc = subprocess.run(
        ["node", str(harness), str(script_f), json.dumps(payload)],
        env={**os.environ, "SKIP_PATHS": skip_paths, "REQUIRED_WORKFLOWS": ""},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"gate script failed: {proc.stderr}"
    return json.loads(proc.stdout)


@pytest.mark.skipif(not _HAVE_NODE, reason="node not available")
class TestGateDecision:
    def test_docs_only_diff_is_skipped(self, tmp_path: Path) -> None:
        out = _run_gate(
            tmp_path, files=["README.md", "docs/guide.md"], skip_paths="**/*.md\ndocs/**"
        )
        assert out["should_review"] == "false"
        assert out["skip_reason"] == "docs-only"

    def test_nested_docs_match_double_star(self, tmp_path: Path) -> None:
        out = _run_gate(tmp_path, files=["docs/a/b/c.md"], skip_paths="docs/**")
        assert out["should_review"] == "false"
        assert out["skip_reason"] == "docs-only"

    def test_any_code_file_forces_a_review(self, tmp_path: Path) -> None:
        """A mixed diff (docs + code) must NOT skip — a docs skip would hide the code."""
        out = _run_gate(
            tmp_path, files=["README.md", "src/app.py"], skip_paths="**/*.md\ndocs/**"
        )
        assert out["should_review"] == "true"
        assert out["skip_reason"] == ""

    def test_empty_skip_paths_never_skips(self, tmp_path: Path) -> None:
        out = _run_gate(tmp_path, files=["README.md"], skip_paths="")
        assert out["should_review"] == "true"

    def test_star_stays_within_a_path_segment(self, tmp_path: Path) -> None:
        """`*.md` must not match `docs/x.md` — only `**` crosses a separator."""
        out = _run_gate(tmp_path, files=["docs/x.md"], skip_paths="*.md")
        assert out["should_review"] == "true", "*.md should not cross the slash"

    def test_draft_pr_is_not_reviewable(self, tmp_path: Path) -> None:
        out = _run_gate(tmp_path, files=["src/app.py"], skip_paths="", draft=True)
        assert out["should_review"] == "false"
        assert out["skip_reason"] == "not-reviewable"
