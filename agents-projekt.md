# AGENTS.md — STT-Button Project Rules

Applies only to this repo (git@github.com:tsii/Parakeet-on-copilot-key.git, branch
`master`). The global AGENTS.md applies additionally.

## Lessons from This Session (Context)
- ALWAYS collect subagent results (`list` → `get_result`); never end the turn with
  an open, needed run without a `schedule` wakeup.
- Verify expert plans instead of trusting them: two Kimi analyses (CapsLock) were
  wrong; a ground-truth experiment settled the question.

## Workflow / Models
- Planning & debugging: openrouter/moonshotai/kimi-k3.1-pro
- Implementation: TDD chains with cortecs/glm-5.3
- Review: cortecs/deepseek-v4-pro-0813

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
   git@github.com:tsii/Parakeet-on-copilot-key.git (remote `origin`, branch
   `master`). Never push to other remotes.
