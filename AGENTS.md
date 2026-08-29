# Repository Guidelines

The current development source of truth is `Plan_R4.md`. Read it together with `docs/ADR.md`, `docs/CONTEXT.md`, `docs/ARCHITECTURE.md`, and the relevant entries in `docs/Experience.md` and `docs/Log.md` before making changes. `Plan_R1.md`, `Plan_R2.md`, `Plan_R3.md`, `docs/Plan_full.md`, and `docs/STATUS.md` are historical evidence, not parallel active queues.

## Project Structure & Module Organization

- `desktop/src/market_monitor/` contains the Python data collector, provider adapters, DuckDB/Parquet storage, FastAPI API, and CLI.
- `desktop/tests/` contains pytest unit, contract, and API tests. Shared JSON-schema fixtures live in `tests/fixtures/` and schemas in `contracts/`.
- `desktop/web/` is the Vue 3 + TypeScript dashboard; source is in `src/`, browser tests in `e2e/`.
- `android/` is the Kotlin/Jetpack Compose client. Architecture decisions and delivery evidence belong in `docs/`.
- `contracts/` contains the cross-platform JSON Schemas. `scripts/` contains controlled maintenance, verification, export, and raw-download utilities.
- Local datasets, reports, logs, and exports use `data_control/`, `reports/`, `artifacts/`, and `exports/`; they are runtime outputs, not source code.

## Build, Test, and Development Commands

Use the locked Python environment and run commands from the repository root:

```powershell
desktop\.venv\Scripts\python -m pytest desktop\tests
desktop\.venv\Scripts\python -m ruff check desktop\src desktop\tests
desktop\.venv\Scripts\python -m market_monitor serve --data-root data_control --host 127.0.0.1 --port 8765
cd desktop\web; npm run build; npm run test:e2e
powershell -ExecutionPolicy Bypass -File scripts\verify.ps1
```

`verify.ps1` is the full baseline: dependency checks, Ruff, Python tests, Android lint/unit tests, and Debug APK build. It requires the pinned Python 3.11 and JDK 21 configuration; it also handles the temporary ASCII-path mapping needed by Gradle.

Relevant inherited R3/R4 data commands include:

```powershell
desktop\.venv\Scripts\python -m market_monitor kline-cache --data-root data_control
desktop\.venv\Scripts\python -m market_monitor import-tdx-local --data-root data_control --tdx-root C:\tongdaxin
desktop\.venv\Scripts\python -m market_monitor bulk-futures --data-root data_control --tdx-futures-root C:\new_tdxqh
desktop\.venv\Scripts\python scripts\build_offline_html.py --data-root data_control --report-root reports
```

TDX securities imports must use the `tdx-cn-v2` normalization gate. Run `--audit-only` before a source replacement; only rows with explicit price scale, volume multiplier/unit, normalization provenance, and `PASS` quality may enter authoritative Silver.

## Coding Style & Naming Conventions

Python targets 3.11, uses four-space indentation, type hints, `snake_case` functions/modules, and `PascalCase` classes. Run Ruff before committing; do not suppress lint rules without a focused reason. Vue components use `PascalCase.vue`, composables and TypeScript variables use `camelCase`, and routes/views follow the existing `*View.vue` pattern. Keep display text and timestamps in Chinese/Beijing-time format where touching user-facing backend output.

## Testing Guidelines

Add or update pytest tests beside the affected desktop module; test files are named `test_*.py` and functions `test_*`. Use fixtures rather than live providers for deterministic tests. For dashboard changes, run `npm run build`; run Playwright (`npm run test:e2e`) when navigation or visible behavior changes. Do not claim provider coverage without a reproducible probe result.

## Commit & Pull Request Guidelines

Follow the established Conventional Commit style: `feat:`, `fix:`, `perf:`, `docs:`, `build:`, `chore:`, or scoped forms such as `test(f10):`. Keep commits focused. PRs should state the behavior change, affected data/source assumptions, verification commands and results, linked task/issue, and screenshots for user-interface changes. Never commit credentials, `.env` files, local data, generated reports, logs, or exports; use `.env.example` and external configuration instead.

## Data Safety & GitHub Publication

- Before staging or pushing, run `git status --ignored --short`, inspect every untracked file, and verify `.gitignore` covers local databases, Parquet, TDX files, API credentials, reports, artifacts, logs, caches, and exports.
- Scan the exact candidate commit for API keys, passwords, tokens, private keys, personal data, and unusually large files. Test-only synthetic credentials must remain visibly synthetic.
- Never use `git add -f` for an ignored runtime file. Never commit `data_control/`, `reports/`, `artifacts/`, `exports/`, `.env*` other than `.env.example`, `local.properties`, private signing keys, `*.duckdb`, `*.sqlite*`, `*.db`, `*.parquet`, `*.day`, or `*.lc5`.
- Public verification keys explicitly allowlisted by `.gitignore` may be tracked; private keys may not.
- Keep Provider records source-isolated. A raw download or local file is not “in the database” until its normalized Silver rows and quality evidence exist.
- Before a GitHub push, verify the configured remote URL matches the user-approved repository, inspect the staged diff, run `git diff --cached --check`, and confirm the remote commit after pushing.
