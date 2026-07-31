#!/usr/bin/env python3
"""Extract the final review text + aggregate token usage from pi `--mode json`.

Usage: pi-extract-review.py <events.jsonl>
Prints the last assistant message text (the review) followed by a
`### Token Usage` section aggregated across the run's assistant turns.
Exits 0 on a usable review, 1 on an unusable one.
"""

import json
import sys

final = ""
usage_total = {"input": 0, "output": 0, "reasoning": 0, "cacheRead": 0, "cacheWrite": 0}
file_opened = False
has_valid_events = False

try:
    with open(sys.argv[1]) as fh:
        file_opened = True
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Every nested field is shape-checked before use. A malformed event
            # must be SKIPPED, never fatal: an AttributeError/TypeError escaping
            # here would produce a traceback with no classified failure body —
            # precisely the silent-failure mode this script exists to end. The
            # events file is machine-generated, so these shapes should always
            # hold; the guards exist so that a violation degrades to "agent did
            # not start" rather than to a crash.
            if not isinstance(evt, dict):
                continue
            has_valid_events = True
            msg = evt.get("message")
            if not isinstance(msg, dict):
                continue
            if evt.get("type") == "message_end" and msg.get("role") == "assistant":
                content = msg.get("content")
                if isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        text = part.get("text")
                        if (
                            part.get("type") == "text"
                            and isinstance(text, str)
                            and text.strip()
                        ):
                            final = text
                usage = msg.get("usage")
                if isinstance(usage, dict):
                    for key in usage_total:
                        val = usage.get(key)
                        if isinstance(val, int | float) and not isinstance(val, bool):
                            usage_total[key] += int(val)
except (OSError, IndexError, UnicodeDecodeError):
    # OSError covers every unreadable-path case (missing file, a directory,
    # permission denied); UnicodeDecodeError covers a binary/corrupt events
    # file. All of them mean we have no usable events, which is the
    # "agent did not start" bucket — never a traceback with no classified
    # body, which would leave the post job to render its less-specific
    # artifact error instead.
    pass

token_block = [
    "### Token Usage",
    "",
    f"Input tokens: `{usage_total['input']}`",
    f"Output tokens: `{usage_total['output']}`",
    f"Reasoning tokens: `{usage_total['reasoning']}`",
    f"Cache read tokens: `{usage_total['cacheRead']}`",
    f"Cache write tokens: `{usage_total['cacheWrite']}`",
    f"Total tokens: `{usage_total['input'] + usage_total['output'] + usage_total['cacheRead'] + usage_total['cacheWrite']}`",
]

if final.strip():
    # Usable review: neutralize any HTML comment markers in model text so a
    # model that echoes the failure marker cannot forge a failure signal.
    sanitized = final.replace("<!--", "&lt;!--")
    lines = [sanitized, "", *token_block]
    print("\n".join(lines))
    sys.exit(0)

# Unusable: classify into one of three buckets and emit the failure signal.
if not file_opened or not has_valid_events:
    cause = "pi produced no events file — the agent did not start"
elif usage_total["input"] == 0:
    cause = (
        "pi returned no content and consumed 0 input tokens"
        " — the request never reached the model (quota, auth, or transport)"
    )
else:
    cause = (
        "pi consumed tokens but produced no review text"
        " — likely a mid-stream backend failure"
    )

lines = [
    "<!-- pi-pr-review-failed -->",
    "",
    "## ⚠️ REVIEW DID NOT RUN",
    "",
    cause,
    "",
    *token_block,
]
print("\n".join(lines))
sys.exit(1)
