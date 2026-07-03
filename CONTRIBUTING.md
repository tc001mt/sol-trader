# Contributing to SOL Trader

Thank you for your interest in improving this project. Contributions are welcome and appreciated.

## How to contribute

1. **Open an issue first** — before writing code, open a GitHub Issue or start a Discussion to describe what you want to change and why. This avoids duplicate work and lets us align on the approach.

2. **Fork, branch, PR** — fork the repo, create a feature branch (`fix/macd-guard`, `feat/new-indicator`, etc.), make your changes, then open a Pull Request against `main`.

3. **Keep PRs focused** — one bug fix or feature per PR. Smaller PRs get reviewed faster.

## What we welcome

- Bug fixes (especially around trade execution, P&L tracking, indicator accuracy)
- New data sources (free APIs only — no paid keys required to run the bot)
- New technical indicators
- Improved AI prompts (in `claude_brain.py`)
- Dashboard improvements (HTML/CSS/JS in `templates/index.html`)
- Documentation improvements

## What to avoid

- PRs that introduce paid API dependencies as required features
- Changes to safety mechanisms without thorough justification (SOL reserve, DRY_RUN default, flash crash guard)
- Hardcoding token mint addresses without verification on Solscan
- Skipping the issue/discussion step for large changes

## Code style

- Python: follow PEP 8, no unnecessary type annotations, no unused imports
- Comments only when the WHY is non-obvious
- No print statements in production code — use the `log` logger
- All user-facing strings and comments in English

## Testing your changes

Before submitting a PR:
- Run with `DRY_RUN=true` for at least one full cycle (20 min)
- Check the dashboard loads without errors
- Verify no `log.error` output in the scheduler logs
- If you changed `data_collector.py`, confirm indicators and prices load correctly via `/api/status`

## License

By contributing, you agree that your contributions will be licensed under the same AGPLv3 license as this project.
