# Codex Goal Guardian Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build, install, and publicly publish update-resilient desktop
heartbeat plus Windows/WSL2 CLI recovery for Codex Goals.

**Architecture:** A Python 3.10+ standard-library package owns health transitions, state locking, App Server JSON-RPC, candidate selection, and recovery. Windows Task Scheduler hosts the primary periodic checks; WSL receives a native install plus an optional user timer. A supported Codex plugin contributes a diagnostic Skill and non-blocking Stop hook.

**Tech Stack:** Python standard library, PowerShell 5+, POSIX shell, Codex App Server JSON-RPC, `unittest`, Git, GitHub CLI.

---

### Task 1: State model and outage transitions

**Files:**
- Create: `src/codex_goal_guardian/config.py`
- Create: `src/codex_goal_guardian/state.py`
- Test: `tests/test_state.py`

**Steps:**
1. Write tests for unknown, down, first healthy, confirmed healthy, and repeated healthy transitions.
2. Run `python3 -m unittest tests.test_state -v` and verify failure.
3. Implement JSON configuration, atomic state writes, corruption fail-closed behavior, and outage generations.
4. Re-run the focused test and verify pass.

### Task 2: App Server client

**Files:**
- Create: `src/codex_goal_guardian/app_server.py`
- Create: `tests/fixtures/fake_app_server.py`
- Test: `tests/test_app_server.py`

**Steps:**
1. Write a fake JSON-RPC server test covering `initialize`, `initialized`, list, Goal get, read, resume, and turn start.
2. Verify the test fails without the client.
3. Implement a timeout-bounded subprocess client with notification tolerance and stderr capture.
4. Verify request ordering and deterministic `clientUserMessageId`.

### Task 3: Candidate selection and recovery engine

**Files:**
- Create: `src/codex_goal_guardian/engine.py`
- Create: `src/codex_goal_guardian/health.py`
- Test: `tests/test_engine.py`
- Test: `tests/test_health.py`

**Steps:**
1. Write tests rejecting active turns, inactive Goals, stale threads, and repeated outage/thread pairs.
2. Write tests accepting network-failed and idle active-Goal threads.
3. Implement health probes and the selection predicate.
4. Implement re-read, `thread/resume`, grace check, optional `turn/start`, and successful state persistence.
5. Run the focused suite.

### Task 4: CLI and diagnostics

**Files:**
- Create: `src/codex_goal_guardian/cli.py`
- Create: `src/codex_goal_guardian/__init__.py`
- Create: `src/codex_goal_guardian/__main__.py`
- Create: `pyproject.toml`
- Test: `tests/test_cli.py`

**Steps:**
1. Test `doctor`, `run-once --dry-run`, `watch`, and `hook-record` argument routing.
2. Implement human and JSON output without exposing auth or prompt content.
3. Add explicit Windows-shim-under-WSL detection.
4. Run all Python tests.

### Task 5: Windows and WSL installers

**Files:**
- Create: `installers/windows/install.ps1`
- Create: `installers/windows/uninstall.ps1`
- Create: `installers/wsl/install.sh`
- Create: `installers/wsl/uninstall.sh`
- Create: `scripts/run-windows.ps1`
- Create: `scripts/run-wsl.sh`
- Test: `tests/test_installers.py`

**Steps:**
1. Test installer text invariants: stable user paths, named owned tasks/units, dry-run support, and no WindowsApps/versioned extension path.
2. Implement copy-only installation and narrow uninstall.
3. Implement Windows interval tasks for native Windows and WSL targets.
4. Implement optional WSL systemd user service/timer.
5. Run Windows and WSL dry-runs.

### Task 6: Plugin, Hook, and documentation

**Files:**
- Create: `plugin/codex-goal-guardian/.codex-plugin/plugin.json`
- Create: `plugin/codex-goal-guardian/skills/codex-goal-guardian/SKILL.md`
- Create: `plugin/codex-goal-guardian/hooks/hooks.json`
- Create: `README.md`
- Create: `SECURITY.md`
- Create: `.gitignore`
- Create: `LICENSE`

**Steps:**
1. Write a specific plugin manifest and Skill.
2. Add a Stop hook that delegates only to an installed external Guardian and always remains non-blocking.
3. Document installation, safe defaults, recovery semantics, and limitations.
4. Validate the plugin with the official plugin-creator validator.

### Task 7: Live compatibility and installation

**Steps:**
1. Update the native Windows Codex CLI from 0.123.0 to the current stable 0.145.0.
2. Run `doctor` against Windows and WSL App Servers.
3. Run both installers in dry-run mode.
4. Install the Windows and WSL runtimes/tasks.
5. Run observe-only checks; do not force-resume a healthy live thread.
6. Verify task definitions, installed paths, logs, and config contain no credentials.

### Task 8: Publish publicly

**Steps:**
1. Run the full test suite and plugin validation.
2. Inspect `git status`, staged diff, and secret scan.
3. Commit implementation on `main`.
4. Use authenticated WSL `gh repo create codex-goal-guardian --public --source . --remote origin --push`, or change the existing repository visibility after confirming the tree contains no secrets.
5. Verify GitHub visibility is `PRIVATE`, remote HEAD matches local HEAD, and no unintended files were pushed.
