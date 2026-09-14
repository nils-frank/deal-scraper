"""Conditional LLM escalation for matches rapidfuzz flags as uncertain.

Runs the bundled llama.cpp CLI as a one-shot subprocess against the bundled
quantized model. Never a standing process, never invoked outside this one call
per uncertain (item, offer) pair - see DESIGN.md
"Decisions" (LLM escalation tier).
"""
from __future__ import annotations

import logging
import os
import subprocess
import time

log = logging.getLogger(__name__)

LLAMA_BIN = os.environ.get("LLAMA_CLI_BIN", "/opt/llama/llama-completion")
MODEL_PATH = os.environ.get("LLAMA_MODEL_PATH", "/opt/llama/model.gguf")
# Measured on a Raspberry Pi 4 with Qwen2.5-1.5B-Instruct Q4_K_M: ~43s wall time on a cold
# page cache (model file not yet cached from a prior run), ~18s warm. 30s was too
# tight and would silently drop real matches to "no" via the timeout fallback below -
# caught by testing on the actual target hardware, not assumed from the Mac build box.
TIMEOUT_SECONDS = 90

# Plain completion (no chat template) tested unusable on the bundled model: see the
# Dockerfile comment on the model-fetch stage. --jinja applies Qwen's own chat
# template, which is what makes the instruct-tuned model actually follow instructions
# instead of free-associating.
SYSTEM_PROMPT = (
    "You match a German supermarket flyer offer title against a shopping watchlist "
    "item. A match means the offer is the same product, the same brand, or an explicit "
    '"versch. Sorten" / variants bundle that covers the watchlist item. '
    "Reply with exactly one word: yes or no."
)
PROMPT_TEMPLATE = (
    'Watchlist item: "{watchlist_summary}"\n'
    'Flyer offer title: "{offer_title}"\n'
    "Does the flyer offer match the watchlist item? Answer yes or no:"
)


def _parse_answer(output: str) -> bool | None:
    # --no-display-prompt means stdout is just the generated continuation.
    words = output.strip().lower().split()
    for word in words[:3]:
        word = word.strip(".,!\"'")
        if word == "yes":
            return True
        if word == "no":
            return False
    return None


def escalate(watchlist_summary: str, offer_title: str) -> bool:
    """Returns True if the model says this is a match. Defaults to False (no
    notification) on any infra failure - a missed match is recoverable next
    run, a false-positive push notification is not."""
    if not os.path.exists(LLAMA_BIN) or not os.path.exists(MODEL_PATH):
        log.warning("llama.cpp binary or model missing (%s / %s), skipping escalation", LLAMA_BIN, MODEL_PATH)
        return False

    prompt = PROMPT_TEMPLATE.format(watchlist_summary=watchlist_summary, offer_title=offer_title)
    log.info("Escalating to LLM: item=%r offer=%r", watchlist_summary, offer_title)
    t0 = time.time()
    try:
        result = subprocess.run(
            [
                LLAMA_BIN,
                "-m", MODEL_PATH,
                "--jinja",
                "-st",
                "--no-display-prompt",
                "--simple-io",
                "-c", "512",
                "-n", "8",
                "--temp", "0",
                "-sys", SYSTEM_PROMPT,
                "-p", prompt,
            ],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("llama.cpp escalation failed for %r / %r after %.1fs: %s", watchlist_summary, offer_title, time.time() - t0, exc)
        return False

    elapsed = time.time() - t0
    if result.returncode != 0:
        log.warning("llama.cpp exited %d after %.1fs: %s", result.returncode, elapsed, result.stderr[-500:])
        return False

    answer = _parse_answer(result.stdout)
    if answer is None:
        log.warning("llama.cpp gave unparseable output for %r / %r after %.1fs: %r", watchlist_summary, offer_title, elapsed, result.stdout[-200:])
        return False
    log.info("LLM verdict for item=%r offer=%r: %s (%.1fs)", watchlist_summary, offer_title, answer, elapsed)
    return answer
