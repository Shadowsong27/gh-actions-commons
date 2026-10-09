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


# --------------------------------------------------------------------------
# Gate, workflow_dispatch mode: the rebuttal re-review. Drives the REAL gate script
# with stubbed PR, workflow-run and comment APIs.
# --------------------------------------------------------------------------

_DISPATCH_HARNESS = """
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const fs = require('fs');
const script = fs.readFileSync(process.argv[2], 'utf8');
const input = JSON.parse(process.argv[3]);
const outputs = {};
const listFiles = Symbol('listFiles');
const listComments = Symbol('listComments');
const listRuns = Symbol('listWorkflowRunsForRepo');
const github = {
  rest: {
    pulls: {
      listFiles,
      get: async ({ pull_number }) => {
        if (!input.pr || pull_number !== input.pr.number) throw new Error('Not Found');
        return { data: input.pr };
      },
    },
    issues: { listComments },
    repos: { listPullRequestsAssociatedWithCommit: async () => ({ data: [] }) },
    actions: { listWorkflowRunsForRepo: listRuns },
  },
  paginate: async (fn) => {
    if (fn === listFiles) return input.files.map((f) => ({ filename: f }));
    if (fn === listRuns) return input.runs;
    if (fn === listComments) {
      if (input.commentsFail) throw new Error('boom');
      return input.comments;
    }
    return [];
  },
};
const context = { eventName: 'workflow_dispatch', workflow: 'pi PR Review', repo: { owner: 'o', repo: 'r' }, payload: {} };
const core = { setOutput: (k, v) => { outputs[k] = v; }, info: () => {}, warning: () => {} };
const fn = new AsyncFunction('github', 'context', 'core', 'process', script);
fn(github, context, core, process)
  .then(() => { console.log(JSON.stringify(outputs)); })
  .catch((e) => { console.error(e); process.exit(1); });
"""

_HEAD = "b" * 40


def _review_comment(sha: str, at: str, failed: bool = False) -> dict:
    body = "<!-- pi-pr-review -->\n" + ("<!-- pi-pr-review-failed -->\n" if failed else "")
    body += f"Reviewed commit: {sha}\n## Findings\n- [Medium] x"
    return {
        "user": {"login": "github-actions[bot]"},
        "author_association": "NONE",
        "body": body,
        "created_at": at,
    }


def _human_comment(at: str, assoc: str = "OWNER") -> dict:
    return {
        "user": {"login": "someone"},
        "author_association": assoc,
        "body": "Rebuttal: false alarm. Evidence: ...",
        "created_at": at,
    }


def _ci_run(conclusion: str = "success", name: str = "CI", n: int = 1) -> dict:
    return {"name": name, "status": "completed", "conclusion": conclusion, "run_number": n}


def _run_dispatch_gate(
    tmp_path: Path,
    *,
    pr_number: str = "7",
    comments: list[dict] | None = None,
    runs: list[dict] | None = None,
    required: str = "",
    cap: str = "1",
    comments_fail: bool = False,
    state: str = "open",
) -> dict:
    script_f = tmp_path / "gate.js"
    script_f.write_text(_gate_script())
    harness = tmp_path / "dispatch-harness.js"
    harness.write_text(_DISPATCH_HARNESS)
    pr = {
        "number": 7,
        "state": state,
        "draft": False,
        "head": {"sha": _HEAD, "repo": {"full_name": "o/r"}},
        "base": {"ref": "main"},
    }
    payload = {
        "pr": pr,
        "files": ["src/app.py"],
        "runs": [_ci_run()] if runs is None else runs,
        "comments": comments or [],
        "commentsFail": comments_fail,
    }
    proc = subprocess.run(
        ["node", str(harness), str(script_f), json.dumps(payload)],
        env={
            **os.environ,
            "SKIP_PATHS": "",
            "REQUIRED_WORKFLOWS": required,
            "DISPATCH_PR_NUMBER": pr_number,
            "MAX_REBUTTAL_REVIEWS": cap,
        },
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"gate script failed: {proc.stderr}"
    return json.loads(proc.stdout)


@pytest.mark.skipif(not _HAVE_NODE, reason="node not available")
class TestDispatchGate:
    def test_rebuttal_after_review_is_reviewed(self, tmp_path: Path) -> None:
        out = _run_dispatch_gate(
            tmp_path,
            comments=[
                _review_comment(_HEAD, "2026-10-08T16:00:00Z"),
                _human_comment("2026-10-08T16:05:00Z"),
            ],
        )
        assert out["should_review"] == "true"
        assert out["pr_number"] == "7"
        assert out["head_sha"] == _HEAD

    def test_no_comment_after_last_review_is_refused(self, tmp_path: Path) -> None:
        out = _run_dispatch_gate(
            tmp_path,
            comments=[
                _human_comment("2026-10-08T15:55:00Z"),
                _review_comment(_HEAD, "2026-10-08T16:00:00Z"),
            ],
        )
        assert out["should_review"] == "false"
        assert out["skip_reason"] == "no-new-rebuttal"

    def test_untrusted_comment_is_not_a_rebuttal(self, tmp_path: Path) -> None:
        out = _run_dispatch_gate(
            tmp_path,
            comments=[
                _review_comment(_HEAD, "2026-10-08T16:00:00Z"),
                _human_comment("2026-10-08T16:05:00Z", assoc="NONE"),
            ],
        )
        assert out["skip_reason"] == "no-new-rebuttal"

    def test_cap_reached_after_one_re_review(self, tmp_path: Path) -> None:
        out = _run_dispatch_gate(
            tmp_path,
            comments=[
                _review_comment(_HEAD, "2026-10-08T16:00:00Z"),
                _human_comment("2026-10-08T16:05:00Z"),
                _review_comment(_HEAD, "2026-10-08T16:10:00Z"),
                _human_comment("2026-10-08T16:15:00Z"),
            ],
        )
        assert out["should_review"] == "false"
        assert out["skip_reason"] == "rebuttal-cap-reached"

    def test_cap_is_configurable(self, tmp_path: Path) -> None:
        out = _run_dispatch_gate(
            tmp_path,
            cap="2",
            comments=[
                _review_comment(_HEAD, "2026-10-08T16:00:00Z"),
                _human_comment("2026-10-08T16:05:00Z"),
                _review_comment(_HEAD, "2026-10-08T16:10:00Z"),
                _human_comment("2026-10-08T16:15:00Z"),
            ],
        )
        assert out["should_review"] == "true"

    def test_reviews_of_older_commits_do_not_count(self, tmp_path: Path) -> None:
        """A new head commit resets the cap: older reviews are a different commit."""
        out = _run_dispatch_gate(
            tmp_path,
            comments=[
                _review_comment("c" * 40, "2026-10-08T16:00:00Z"),
                _review_comment("d" * 40, "2026-10-08T16:10:00Z"),
            ],
        )
        assert out["should_review"] == "true"

    def test_failed_review_does_not_count(self, tmp_path: Path) -> None:
        out = _run_dispatch_gate(
            tmp_path,
            comments=[_review_comment(_HEAD, "2026-10-08T16:00:00Z", failed=True)],
        )
        assert out["should_review"] == "true"

    def test_red_ci_is_refused(self, tmp_path: Path) -> None:
        out = _run_dispatch_gate(tmp_path, runs=[_ci_run("failure")])
        assert out["skip_reason"] == "ci-not-green"

    def test_latest_ci_run_wins(self, tmp_path: Path) -> None:
        out = _run_dispatch_gate(
            tmp_path, runs=[_ci_run("failure", n=1), _ci_run("success", n=2)]
        )
        assert out["should_review"] == "true"

    def test_no_ci_runs_is_refused(self, tmp_path: Path) -> None:
        out = _run_dispatch_gate(tmp_path, runs=[])
        assert out["skip_reason"] == "ci-not-green"

    def test_every_ci_workflow_must_be_green(self, tmp_path: Path) -> None:
        out = _run_dispatch_gate(
            tmp_path, runs=[_ci_run(name="CI"), _ci_run("failure", name="Integration")]
        )
        assert out["skip_reason"] == "ci-not-green"

    def test_own_caller_runs_are_ignored(self, tmp_path: Path) -> None:
        """A pull_request-triggered caller has its own (maybe red) run on the head."""
        out = _run_dispatch_gate(
            tmp_path, runs=[_ci_run(name="CI"), _ci_run("failure", name="pi PR Review")]
        )
        assert out["should_review"] == "true"

    def test_skipped_workflow_does_not_block(self, tmp_path: Path) -> None:
        out = _run_dispatch_gate(
            tmp_path, runs=[_ci_run(name="CI"), _ci_run("skipped", name="Deploy")]
        )
        assert out["should_review"] == "true"

    def test_only_skipped_workflows_is_refused(self, tmp_path: Path) -> None:
        out = _run_dispatch_gate(tmp_path, runs=[_ci_run("skipped", name="Deploy")])
        assert out["skip_reason"] == "ci-not-green"

    def test_in_progress_ci_is_refused(self, tmp_path: Path) -> None:
        run = _ci_run(name="CI")
        run.update(status="in_progress", conclusion=None)
        out = _run_dispatch_gate(tmp_path, runs=[run])
        assert out["skip_reason"] == "ci-not-green"

    def test_required_workflows_narrow_the_check(self, tmp_path: Path) -> None:
        out = _run_dispatch_gate(
            tmp_path,
            required="CI",
            runs=[_ci_run(name="CI"), _ci_run("failure", name="Nightly")],
        )
        assert out["should_review"] == "true"

    @pytest.mark.parametrize("raw", ["", "abc", "0", "7; rm -rf /", "-1"])
    def test_invalid_pr_number_is_refused(self, tmp_path: Path, raw: str) -> None:
        out = _run_dispatch_gate(tmp_path, pr_number=raw)
        assert out["skip_reason"] == "invalid-pr-number"

    def test_unknown_pr_is_refused(self, tmp_path: Path) -> None:
        out = _run_dispatch_gate(tmp_path, pr_number="99")
        assert out["skip_reason"] == "no-pr-for-number"

    def test_closed_pr_is_not_reviewable(self, tmp_path: Path) -> None:
        out = _run_dispatch_gate(tmp_path, state="closed")
        assert out["skip_reason"] == "not-reviewable"

    def test_unreadable_comments_fail_closed(self, tmp_path: Path) -> None:
        out = _run_dispatch_gate(tmp_path, comments_fail=True)
        assert out["should_review"] == "false"
        assert out["skip_reason"] == "comment-scan-failed"


# --------------------------------------------------------------------------
# Claude fallback job (opt-in via `claude-fallback-model`). Drives the REAL decide
# and run steps with a stub `npm` that installs a stub `claude`, then the REAL post
# script, so the trigger, the usable/unusable split, the token-leak guard and the
# published attribution are all tested against the shipped workflow.
# --------------------------------------------------------------------------

_FAKE_TOKEN = "sk-ant-oat01-FAKE-test-token"

# Stub `npm`: on `install --prefix <dir> ...` it drops a stub `claude` into
# <dir>/node_modules/.bin. The stub claude's output is chosen by CLAUDE_STUB_MODE.
_STUB_NPM = r"""#!/usr/bin/env bash
prefix=""
while [ $# -gt 0 ]; do
  if [ "$1" = "--prefix" ]; then prefix="$2"; shift; fi
  shift
done
mkdir -p "$prefix/node_modules/.bin"
cat > "$prefix/node_modules/.bin/claude" <<'STUB'
#!/usr/bin/env python3
import json, os, sys
sys.stdin.read()
open(os.environ["CLAUDE_ARGV_LOG"], "w").write(json.dumps(sys.argv[1:]))
mode = os.environ.get("CLAUDE_STUB_MODE", "good")
if mode == "good":
    out = {"is_error": False, "result": "## Findings\n\nNo findings.",
           "usage": {"input_tokens": 50, "output_tokens": 7,
                     "cache_read_input_tokens": 3, "cache_creation_input_tokens": 1}}
elif mode == "leak":
    out = {"is_error": False, "result": "## Findings\n\n" + os.environ["CLAUDE_CODE_OAUTH_TOKEN"]}
elif mode == "error":
    out = {"is_error": True, "result": "rate limited"}
else:
    out = {"is_error": False, "result": "I could not review this."}
print(json.dumps(out))
STUB
chmod +x "$prefix/node_modules/.bin/claude"
"""


def _claude_step(name: str) -> dict:
    wf = yaml.safe_load(WORKFLOW.read_text())
    for step in wf["jobs"]["claude-fallback"]["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"{name!r} step not found in claude-fallback")


def _stage_pi_artifact(runner_temp: Path, model_line: str, *, prompt: bool = True) -> None:
    pi = runner_temp / "pi"
    pi.mkdir(parents=True, exist_ok=True)
    (pi / "pi-model.txt").write_text(model_line)
    (pi / "pi-review.md").write_text(f"{FAILURE_MARKER}\n\n## ⚠️ REVIEW DID NOT RUN")
    if prompt:
        (pi / "prompt.md").write_text("review this")
        (pi / "pr.diff").write_text("diff --git a/x b/x\n")


def _run_decide(tmp_path: Path, model_line: str | None, *, prompt: bool = True) -> str:
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir(exist_ok=True)
    if model_line is not None:
        _stage_pi_artifact(runner_temp, model_line, prompt=prompt)
    out = tmp_path / "gh_output"
    out.write_text("")
    subprocess.run(
        ["bash", "-e", "-c", _claude_step("Decide whether pi exhausted every model")["run"]],
        env={**os.environ, "RUNNER_TEMP": str(runner_temp), "GITHUB_OUTPUT": str(out)},
        check=True,
        capture_output=True,
    )
    return out.read_text().strip()


def _run_claude(tmp_path: Path, mode: str, token: str = _FAKE_TOKEN) -> tuple[Path, int, list]:
    runner_temp = tmp_path / "runner"
    _stage_pi_artifact(runner_temp, "NONE — every model failed: a,b")
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "npm").write_text(_STUB_NPM)
    (bindir / "npm").chmod(0o755)
    argv_log = tmp_path / "argv.json"
    proc = subprocess.run(
        ["bash", "-e", "-c", _claude_step("Run Claude review")["run"]],
        cwd=REPO,
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_TOKEN": "",
            "CLAUDE_CODE_OAUTH_TOKEN": token,
            "CLAUDE_MODEL": "claude-sonnet-5-5",
            "CLAUDE_EFFORT": "high",
            "CLAUDE_CODE_VERSION": "2.1.286",
            "CLAUDE_STUB_MODE": mode,
            "CLAUDE_ARGV_LOG": str(argv_log),
        },
        capture_output=True,
        text=True,
    )
    argv = json.loads(argv_log.read_text()) if argv_log.exists() else []
    return runner_temp, proc.returncode, argv


class TestClaudeFallbackDecide:
    def test_runs_when_pi_chain_exhausted(self, tmp_path: Path) -> None:
        assert _run_decide(tmp_path, "NONE — every model failed: a,b") == "run=true"

    def test_skips_when_pi_answered(self, tmp_path: Path) -> None:
        assert _run_decide(tmp_path, "litellm/deepseek-v4-flash") == "run=false"

    def test_skips_when_no_artifact(self, tmp_path: Path) -> None:
        assert _run_decide(tmp_path, None) == "run=false"

    def test_skips_when_prompt_missing(self, tmp_path: Path) -> None:
        """An artifact from a pre-fallback reviewer has no prompt.md/pr.diff."""
        assert _run_decide(tmp_path, "NONE — x", prompt=False) == "run=false"


class TestClaudeFallbackRun:
    def test_usable_review_written_with_token_usage(self, tmp_path: Path) -> None:
        rt, rc, _ = _run_claude(tmp_path, "good")
        assert rc == 0
        review = (rt / "claude-review.md").read_text()
        assert "## Findings" in review
        assert "Total tokens: `61`" in review
        assert (rt / "claude-model.txt").read_text().startswith("claude-sonnet-5-5")

    @pytest.mark.parametrize("mode", ["error", "unusable"])
    def test_unusable_output_fails_and_writes_nothing(self, tmp_path: Path, mode: str) -> None:
        rt, rc, _ = _run_claude(tmp_path, mode)
        assert rc != 0
        assert not (rt / "claude-review.md").exists()
        assert not (rt / "claude-model.txt").exists()

    def test_token_leak_is_refused(self, tmp_path: Path) -> None:
        rt, rc, _ = _run_claude(tmp_path, "leak")
        assert rc != 0
        assert not (rt / "claude-review.md").exists()

    def test_missing_secret_fails_loudly(self, tmp_path: Path) -> None:
        rt, rc, argv = _run_claude(tmp_path, "good", token="")
        assert rc != 0
        assert argv == [], "claude must not run without the secret"

    def test_cli_flags_lock_down_tools(self, tmp_path: Path) -> None:
        _, _, argv = _run_claude(tmp_path, "good")
        assert argv[0] == "-p" and argv[1] == "review this", "prompt must precede variadic flags"
        for flag in ["--safe-mode", "--strict-mcp-config", "--disable-slash-commands"]:
            assert flag in argv
        assert argv[argv.index("--allowedTools") + 1] == "Read(./**)"
        denied = argv[argv.index("--disallowedTools") + 1].split(",")
        for tool in ["Bash", "Edit", "Write", "WebFetch", "Agent", "Read(//proc/**)"]:
            assert tool in denied
        assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"
        assert argv[argv.index("--effort") + 1] == "high"


class TestClaudeFallbackContract:
    def test_job_is_read_only_and_separate(self) -> None:
        wf = yaml.safe_load(WORKFLOW.read_text())
        job = wf["jobs"]["claude-fallback"]
        assert job["permissions"] == {"contents": "read"}
        assert "inputs.claude-fallback-model != ''" in job["if"], "must stay opt-in"
        checkout = next(s for s in job["steps"] if s.get("name") == "Checkout the reviewed commit")
        assert checkout["with"]["persist-credentials"] is False

    def test_pi_artifact_carries_prompt_and_diff(self) -> None:
        wf = yaml.safe_load(WORKFLOW.read_text())
        upload = next(s for s in wf["jobs"]["review"]["steps"] if s.get("name") == "Upload review artifact")
        assert "prompt.md" in upload["with"]["path"]
        assert "pr.diff" in upload["with"]["path"]

    def test_post_waits_for_fallback(self) -> None:
        wf = yaml.safe_load(WORKFLOW.read_text())
        assert "claude-fallback" in wf["jobs"]["post"]["needs"]


@pytest.mark.skipif(not _HAVE_NODE, reason="node not available")
class TestClaudeFallbackPublished:
    def _post(self, tmp_path: Path, with_claude: bool) -> dict:
        runner_temp = tmp_path / "runner"
        review_dir = runner_temp / "review"
        review_dir.mkdir(parents=True)
        (review_dir / "pi-model.txt").write_text("NONE — every model failed: a,b")
        (review_dir / "pi-review.md").write_text(f"{FAILURE_MARKER}\n\n## ⚠️ REVIEW DID NOT RUN")
        if with_claude:
            c = runner_temp / "claude"
            c.mkdir()
            (c / "claude-review.md").write_text("## Findings\n\nNo findings.")
            (c / "claude-model.txt").write_text("claude-sonnet-5-5 (Claude fallback, effort high)")
        return _run_post_job(tmp_path, runner_temp)

    def test_claude_review_replaces_failure_and_names_model(self, tmp_path: Path) -> None:
        out = self._post(tmp_path, with_claude=True)
        assert "<!-- pi-pr-review -->" in out["body"], "keeps the marker the gate keys on"
        assert "Claude Review (pi fallback)" in out["body"]
        assert "Model: `claude-sonnet-5-5 (Claude fallback, effort high)`" in out["body"]
        assert FAILURE_MARKER not in out["body"]
        assert "failed" not in out

    def test_no_claude_artifact_keeps_pi_failure(self, tmp_path: Path) -> None:
        out = self._post(tmp_path, with_claude=False)
        assert FAILURE_MARKER in out["body"]
        assert "failed" in out
