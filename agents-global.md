# AGENTS.md — Global Harness & Delegation Rules (pi)

Applies to every project. Delegation is complete only when the verified
result has reached the operator — not when the subagent was started
or internally marked `done`.

## Why These Rules
- Failure a: Turn ended after subagent spawn; result never collected → completed
  expert result sat unnoticed, and the human had to point it out.
- Failure b: `wait_all(960)` blocked the agent → operator could not
  get a response, contrary to "always run in the background and remain responsive".

## Enforced vs. Recommended
- HARD (extension `.pi/extensions/no-wait-all.ts`): every
  `subagent(action=wait_all)` is forcibly rewritten to `list`; timeout and
  runIds are removed. A blocking `wait_all` does not exist.
- RECOMMENDED (this file = policy): all other rules. Violations are
  prohibited, not technically impossible.

## Rules

R1 Wait Mechanism. When waiting for a subagent result, NEVER use
   `wait_all` (forcibly redirected to `list`); instead use `subagent(action=list)`
   for status/runId and `subagent(action=get_result)` for the result.
   (enforced — extension)
   The only true wait mechanism is the `schedule` wakeup (R5/R6).

R2 Model Binding. When a specific model is required, use
   `subagent(action=spawn, model=provider/id)`, never `delegate_task`
   with a template. After spawning, use `list` to verify identity; if it differs,
   `stop` and spawn again; never present another result as the requested model.
   (recommended)

R3 Result in the Same Turn. If a result is needed for the current turn,
   call `subagent(action=get_result)` BEFORE the turn ends.
   Never end the turn with merely an announcement that you are waiting. (recommended)

R4 Verify Results. When `get_result` has delivered a result, verify its completeness
   and relevance to the task, then report to the operator. Unverified ≠ collected.
   (recommended)

R5 Asynchronous Run. When deliberately working asynchronously, set a one-time
   `schedule` wakeup (+N min) with the runId and collection instructions BEFORE
   the turn ends. No open, needed run without a wakeup.
   (recommended)

R6 Wakeup Fires. When a wakeup fires, first call `subagent(action=list)`;
   `done` → `get_result` and process; `running` → exactly ONE limited
   follow-up wakeup, then report honestly instead of chaining wakeups. (recommended)

R7 End-of-Turn Check. Before EVERY turn ends: no self-started run may remain open without
   (a) a verified, collected result, (b) a scheduled wakeup, or
   (c) an explicitly justified handoff. Otherwise, collect it now or set a wakeup.
   (recommended)

R8 Responsiveness First. If blocking is likely (long-running `bash`,
   `delegate_task`, `wait_all` with a high timeout), execute only with the operator's
   explicit approval to wait. Default: `spawn` → remain responsive;
   before any blocking operation, announce its duration and how to abort it. (recommended)
