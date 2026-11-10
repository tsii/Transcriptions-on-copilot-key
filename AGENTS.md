# AGENTS.md — STT-Button Project Rules

Applies only to this repo (git@github.com:tsii/Transcriptions-on-copilot-key.git, branch
`master`). The global AGENTS.md applies additionally.

## Lessons from This Session (Context)
- ALWAYS collect subagent results (`list` → `get_result`); never end the turn with
  an open, needed run without a `schedule` wakeup.
- Verify expert plans instead of trusting them: two Kimi analyses (CapsLock) were
  wrong; a ground-truth experiment settled the question.

## Planning ownership (operator order 2026-11-05 21:19)
P9 The orchestrator does NOT design or plan. Every planning task goes to a planner
   subagent; the orchestrator then IMPLEMENTS the planner's plan as written. Problems
   found along the chain (design gaps, blockers, contradictions, failing assumptions)
   are escalated BACK TO THE PLANNER (steer / re-brief), not decided by the
   orchestrator. Only genuine operator decisions may be raised to the operator, and
   only those the planner explicitly flagged. Existing orchestrator-authored drafts
   (e.g. `.pi/plans/plan_summary_mode.md`) are non-binding inputs for the planner.

## Workflow / Models
- Planning & debugging: openrouter/kimi-k3.1-pro
- Implementation: TDD chains with cortecs/glm-5.3
- Review: cortecs/deepseek-v4-pro-0813

P8 Model Fallback (operator order 2026-11-05). The map lives in
   `.pi/model-fallback.json`; probe it with
   `.venv/bin/python tools/model_fallback_probe.py` (state file
   `.pi/state/model-availability.json`, exit code 10 = a state flipped).
   Active pairs:
   `my/work-zai-org-glm-5.3-verda` -> `cortecs/glm-5.3`,
   `my/work-sipgate-coding-ultra` -> `cortecs/kimi-k3`,
   `openrouter/kimi-k3.1-pro` -> `cortecs/kimi-k3`,
   `cortecs/deepseek-v4.1-flash` -> `cortecs/deepseek-v4-pro-0813`.
   Before delegating to a primary that the probe marks DOWN, delegate to the
   fallback instead and state in the report which model actually ran (global
   R2: never present a fallback result as the ordered model). When a later
   probe shows the primary UP again, switch BACK to the primary without being
   asked. Check every 15 minutes while delegations are open.
   Ground truth 2026-11-05: a delegation to `openrouter/moonshotai/kimi-k3.1-pro`
   produced three empty assistant turns and one `Request timed out.` while the
   plain chat/completions endpoint answers in 2-5 s (HTTP 200, also for a
   19 kB prompt) - so "endpoint UP" does NOT mean "usable for delegation".
   Treat an agentic probe (one real tool call) as the deciding signal.
   Re-tested 2026-11-10 23:44: a one-tool agentic probe on the primary
   `openrouter/kimi-k3.1-pro` completed correctly (it returned the real output of
   `git log --oneline -1`), so by the rule above Kimi tasks go BACK to that primary.
   Note what the probe does NOT prove: the 2026-11-05 failures appeared in multi-step
   runs, so the first multi-step Kimi delegation after this probe must be watched and
   its result verified, and the report must name the model that actually ran.
   Brief size matters: a 19 kB prompt works on the plain endpoint, and a short
   delegation prompt plus a one-tool task succeeded on the same model, while a
   delegation whose prompt carried an ~11.5 kB inline brief produced only empty
   turns. So for Kimi: keep the delegation prompt short and put the details in a
   file the subagent reads itself (`.pi/briefs/*.md`).
   Long single writes are the other failure mode: on 2026-11-05 a planning run
   hung 45+ minutes inside one `write` of a ~10 kB plan and never produced the
   file (ground truth: transcript shows the started `write`, no file on disk).
   When delegating a document write, instruct the subagent to write it in
   sections (several small write/patch calls) instead of one large write.
   Ground truth 2026-11-06: `my/work-zai-org-glm-5.3-verda` ended SILENTLY THREE
   times (`sa-b15943d6`, `sa-4471f5bf`, `sa-2f4e0e49`) - run reports `done`,
   final message empty or just an announcement, ZERO file writes, work tree
   unchanged - while the same model answered the one-tool probe ("PROBE-B9c
   ok") and the probe marked it UP. So: an UP probe and a correct probe answer
   are NOT delegation-fitness proof; the deciding signal is a completed
   multi-step run. After the second silent death run the chain on
   `cortecs/glm-5.3` (carried B9b, B9c, B10a, B10b, B10r, B12, B11a attempt) and
   say in every report which model actually ran (global R2).

## Rules

P1 Python & Layout. When changing code, use Python 3.12 with the src layout
   (`src/stt_button/`) and the venv `.venv/bin/python`.

P2 Test Command. When running tests, use EXCLUSIVELY
   `.venv/bin/python -m pytest`. Never use system Python or other runners.

P3 TDD. When implementing a fix or feature, write the test first,
   document RED (test fails), then GREEN (`.venv/bin/python -m
   pytest`). Never commit code without a corresponding test.

P4 Style. When creating new modules, functions, or docstrings, write them in English.

P5 Security Boundaries. If an operation requires system privileges (apt, /etc, keyd,
   systemd, sudo), do NOT execute it without explicit operator approval.
   The app must never toggle CapsLock without asking (default
   `capslock_autocorrect=false` → warn only).

P6 Service. Start, stop, or enable the service only with explicit operator
   approval. Currently: started manually; autostart intentionally not configured yet.

P7 Git. When committing/pushing, push ONLY to
   git@github.com:tsii/Transcriptions-on-copilot-key.git (remote `origin`, branch
   `master`). Never push to other remotes. Operator confirmed on 2026-11-10 that this
   is the correct URL (the repo was renamed from `Parakeet-on-copilot-key`).
