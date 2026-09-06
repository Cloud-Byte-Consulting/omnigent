# Desktop Shell — Windows Plan

**Status:** proposed; implementation and TDD cycles not started
**Baseline:** `fork/main` @ `aadd89b4f` (upstream `omnigent-ai/omnigent` main merged 2026-09-05)
**Sibling plan:** [`desktop-linux-plan.md`](desktop-linux-plan.md) — independent, shares one CI workflow file (see Phase 3).
**Target:** Windows 10/11 x64. arm64 deferred.

## Goal

Ship an installable, self-updating Windows build of the Electron shell (`web/electron`) with the same features as macOS minus the platform's real limits: on Windows the Python side runs in the documented "degraded mode" (`README.md` → *Windows (native)*): `omnigent server`, the web UI and SDK harnesses work; tmux/PTY native harnesses and bwrap sandboxing do not. The desktop inherits exactly those limits and adds none.

## Today (on `main`)

* `web/electron/package.json` → `build.win` targets `nsis` with `icons/icon.ico` (7 sizes, fine) and the generic update feed. `pnpm run build:win` exists and has never run in CI.
* Windows-aware code already present: `app.setAppUserModelId("ai.omnigent.desktop")` (notifications/taskbar attribution), `whichName` uses `where` on win32, `loginShellPath` is a no-op on win32, deep links via `second-instance`/argv, `about_window` resolves the packaged icon on win32, `flashFrame` attention cue.
* Python side: `omnigent/_platform.py` (`IS_WINDOWS`), `omnigent/inner/_proc.py` (process groups, psutil tree kill), Job Objects in `runtime/harnesses/process_manager.py`, `tzdata` dep marker, and a non-blocking `windows.yml` CI lane (import + `--help` + two hard test files). `omnigent server --background` spawns via `_proc.spawn_kwargs()`.
* Install on Windows is `uv tool install --python 3.12 omnigent` (the `curl | sh` one-liner is POSIX-only). uv puts `omnigent.exe` in `%USERPROFILE%\.local\bin`.
* Data dirs match already: both the CLI and `src/omnigent_cli.js` hardcode `<home>/.omnigent` (`os.homedir()` ↔ `Path.home()`), so `auth_tokens.json`, daemon records and the pidfile are found without change.
* `updateBadge()` only calls `app.setBadgeCount`, which does not implement a Windows taskbar overlay. Badge parity requires Windows-specific work in Phase 2.
* No Windows signing configuration is recorded here; confirm identity and credential ownership in Phase 4 (the macOS Developer ID is `Databricks, Inc.`).

## Approach

Same ladder as Linux: electron-builder's NSIS target, electron-updater's built-in NSIS update path, existing `node --test` suite. The Windows-specific work is the handful of places where the shell assumes POSIX process semantics or POSIX paths.

Use TDD for each behavior/configuration change, one increment at a time:

1. **Red:** add the smallest regression test and run it against unchanged code. Confirm the intended failure; a missing dependency or a platform setup error is not evidence of the bug.
2. **Green:** implement the smallest fix, rerun the focused test, then the affected existing tests. Refactor only while those checks pass.
3. **Verify:** complete the phase's native build/manual checks before proceeding. Record the failing command/assertion, passing rerun and platform in the implementation PR's Test Plan; list checks awaiting a later phase or unavailable Windows environment as pending, with the dependency named.

For verify-only items, leave working code alone. Turn a demonstrated failure into a runnable regression before fixing it. Test process calls and observable behavior rather than matching source strings; use the existing Node test runner, with no new test framework.

## Phase 1 — Packaging config (`web/electron/package.json`)

1. First add a failing packaging test for `build.win.artifactName: "${productName}-${version}-${arch}-win.${ext}"`, then set it and rerun the test. Keep the NSIS target assertion as existing-behavior coverage; verify the generated installer and metadata after building.
2. Keep NSIS defaults (`oneClick`, per-user install, no admin). electron-builder registers the `omnigent://` protocol from `build.protocols` and creates the Start Menu shortcut with the `appId` as AUMID, which is what `setAppUserModelId` needs for toasts. No `nsis` block required.
3. Signing: none in Phase 1. Test unsigned installation in a disposable VM, noting SmartScreen or local policy restrictions. Inspect the generated updater configuration; defer the unsigned N → N+1 check to Phase 4's staging rehearsal. Do not assume signature verification behavior merely from the running executable being unsigned. Public signing is Phase 4.

## Phase 2 — Runtime fixes (`web/electron/src`)

Each behavior change follows the red/green sequence above and the focused coverage under Tests.

1. **CLI discovery (**`omnigent_cli.js`**).** First reproduce missing `.exe` fallback discovery. On win32 probe `omnigent.exe`/`omni.exe` in Windows-appropriate locations, including `%USERPROFILE%\.local\bin`, and drop POSIX-only directories. Keep invocation shell-free: do not add `.cmd` candidates, and reject `.cmd`/`.bat` paths returned by discovery or configured explicitly with an actionable `.exe` message. Test all three resolution routes, including paths containing spaces. `X_OK` alone is not a Windows executable-format check ([Node documentation](https://nodejs.org/api/child_process.html#spawning-bat-and-cmd-files-on-windows)).
2. **Install hint (**`omnigent_cli.js` `INSTALL_COMMAND`**).** On win32 show `uv tool install --python 3.12 omnigent` (the README's documented path) instead of the `curl | sh` line. One ternary; the setup page and Settings → Local CLI both read this constant.
3. **No console flash.** First capture subprocess options in failing tests, then add `windowsHide: true` to `whichName()`'s `execFileSync("where", ...)`, the `runCli()` exec helper and `server_manager.js`'s host spawn. Include any `taskkill` invocation if Phase 2.4 becomes necessary. Verify actual window behavior in the packaged GUI.
4. **Process teardown (**`server_manager.js` `stopChild`**).** `child.kill("SIGTERM")` terminates the host on Windows without POSIX graceful shutdown. Verify whether the existing Python Job Objects also reap its active runner/tool tree (Manual step 6). If an orphan remains, first preserve the failure in a Windows test spawning a child and grandchild through the real teardown path; then add a win32 `taskkill /T /F /PID <pid>` branch only if it solves that case. Await completion, handle failures and keep ownership boundaries; test disconnect, restart and quit callers. Do not add this branch when existing teardown passes.
5. **Log tail.** Existing `findLiveServerLog()` scans `path.join(localDataDir(), "logs", "server")`; it does not parse the CLI's displayed path, and `localDataDir()` already expands `~`. Verify log streaming with a Windows path containing spaces/backslashes; no new expansion is planned.
6. **Local server lifecycle.** On quit the shell runs `omnigent server stop` for a server it started; on Windows there is no process group, so make sure the stop goes through the CLI (it does today) and not a signal. No change expected; verify only.
7. **Taskbar badge (**`main.js` `updateBadge`**).** First add a failing test that requires a Windows overlay for a nonzero aggregate count and clears it at zero. Reuse the existing per-origin count aggregation, then call the native window `setOverlayIcon` API on Windows, with an accessible count description. Cover duplicate windows for one server and distinct servers; keep macOS/Linux behavior intact. Verify the rendered icon at Windows display scaling settings; no new badge library.

Native frame, close-to-quit behavior, notification sound and click routing remain verify-only items; platform guards alone are not proof that a packaged Windows build works.

## Phase 3 — CI (`.github/workflows/desktop-build.yml`, new or shared)

Windows leg, modelled on `windows.yml` (non-blocking as a merge signal, but failed checks still fail the job). Include desktop/web build inputs, workspace manifests/lockfile and the workflow/setup action in path filters; share them with the Linux plan.

* `windows-latest`, `./.github/actions/setup-pnpm`, `pnpm install --frozen-lockfile --filter ./web/electron --filter web`.
* `node --test web/electron/test` — run the full unit suite on Windows. Tests using actual platform paths must expect Windows semantics; explicitly POSIX fixtures must remain POSIX. Fix actual failures without skipping Windows behavior coverage.
* `pnpm --filter ./web/electron run build:win --publish never`; verify and upload `web/electron/dist/*.exe`, `*.blockmap`, `latest.yml`. Fail when any expected artifact is missing.
* Keep the shared Playwright connect journey in Linux CI for now; Windows desktop installation, notifications, updates and teardown still require the native manual checks below. `xvfb` is a headless Linux prerequisite, not a Windows requirement.
* If Phase 2.4 needs a native Python-backed regression, reuse the Python/uv setup from `windows.yml` and run that test here as a required job step.
* If the Linux plan lands first, add a matrix entry to its workflow instead of a second file.

Run the recipe from a fresh Windows checkout before completing this phase. Preserve a regression for each product failure it reveals, fix it and rerun; missing dependencies or skipped required tests are not green validation.

## Phase 4 — Signing and release

1. **Signing.** Confirm the authorized publisher, credential owner and signing service/certificate. Azure signing and a purchased certificate are candidates; validate eligibility and integration before choosing. Keep local unsigned builds usable without credentials and keep signing in build configuration. Before changing release configuration, add a check that rejects unsigned release artifacts, observe it fail, then configure signing and verify it passes on Windows.
2. **Update rehearsal.** On an isolated staging feed, test unsigned development N → N+1, then install signed version N and update to signed N+1 through the existing `desktop_updater.js` flow. Verify download, signature/checksum failure handling, restart, new version and retained settings. If code needs to change, preserve a failing regression first. A clean 404 does not validate installation.
3. **Feed.** Extend the existing manual upload step with the generated installer and `.blockmap`; verify payload URLs/checksums and publish `latest.yml` last. Confirm ownership of that step before public release.
4. `omnigent.ai/download/windows` is website work; out of repo.
5. `web/electron/README.md`: add a "Windows" subsection under *Build a distributable* (build command, unsigned-build SmartScreen note, signing setup once step 1 lands) and a pointer to the README's *Windows (native)* limits for hosting.

## Tests

* `test/omnigent_cli.test.js`: first fail on missing `.exe` discovery, wrong install hint and absent `windowsHide` in both `where` and CLI execution. Cover configured/PATH/fallback routes, spaces in paths and rejection of batch scripts. Use native Windows execution for actual path semantics; restore any mocks/module state between tests.
* `test/server_manager.test.js`: first fail on host spawn options, then verify the hidden-console setting; preserve existing ownership/auth behavior tests.
* `test/main.test.js`: first fail on missing Windows badge behavior; verify setting/clearing the overlay and count aggregation using window stubs. Do not settle for a `setOverlayIcon` source-string assertion.
* `test/win_packaging.test.js`: fail on missing artifact naming before editing config, then pass; check NSIS remains configured and validate a real installer build.
* `test/local_server_log_tail.test.js` and `test/desktop_updater.test.js`: rerun existing behavior coverage; add a failing regression only when native verification finds a gap.
* Phase 2.4 only if needed: a runnable native process-tree regression must fail before and pass after the fix. A mock asserting that `taskkill` was called is insufficient evidence of cleanup.

From the repository root, after installing dependencies:

```powershell
node --test web/electron/test/omnigent_cli.test.js web/electron/test/server_manager.test.js web/electron/test/main.test.js
# Once added in Phase 1:
node --test web/electron/test/win_packaging.test.js
node --test web/electron/test/local_server_log_tail.test.js web/electron/test/desktop_updater.test.js
node --test web/electron/test
```

Run each relevant focused test at red and green, then the full suite at the phase boundary. Before committing, run `pre-commit run --all-files` in the supported Linux/macOS contributor environment (WSL2 on Windows). These documents specify future validation, not completed implementation checks.

## Manual verification (Windows 11 VM or box)

1. In PowerShell, `uv tool install --python 3.12 omnigent`; confirm `where.exe omnigent` locates the installed `.exe` (normally `%USERPROFILE%\.local\bin\omnigent.exe`).
2. From `web\electron`: `pnpm install --frozen-lockfile`, `pnpm run build:win --publish never` → installer, `.blockmap`, `latest.yml` in `dist`. Confirm metadata names the built installer.
3. Run the installer (expect SmartScreen "More info → Run anyway" until Phase 4). App appears in Start Menu with the icon; no console window flashes on launch or on any CLI call (Phase 2.3).
4. Setup page → gear: auto-detected path is the `.exe` from step 1 with no override typed. Repeat launching with the CLI's directory removed from that process's PATH to exercise fallback discovery. Test a configured `.exe` path containing spaces and a `.cmd` override that must be rejected. In a disposable account without the CLI, confirm the install hint is the `uv` line. Watch for console flashes during discovery as well as CLI calls.
5. "Start a server on this machine" → startup logs stream, connects; Settings → Local CLI shows the path and version. Repeat with a data directory containing spaces.
6. Host menu → connect this machine → native confirm → host online. Start an SDK session with an actively running tool, then **quit the app while it is still running**. Task Manager must show no desktop-owned host/server/runner/tool processes; a separately started daemon must survive. Repeat disconnect and restart. Preserve any orphan reproduction before adding Phase 2.4 code. Confirm a native `omnigent claude` harness fails with the documented unsupported-platform error rather than hanging.
7. Paste an `omnigent://…` session link into `Win+R` → the running window focuses and opens the session; with the app closed, it cold-starts into the session.
8. Finish a turn in an unfocused window → verify taskbar attention cue, overlay badge/count, toast and sound; clicking the toast focuses the right window. Clear pending items and verify overlay removal. Repeat with two windows on one server and windows on distinct servers, at 100% and higher display scaling.
9. Server → Check for Updates → exercise both no-update/error states and the staging N → N+1 installation from Phase 4. Verify relaunch reports N+1 and preserves settings.

## Open questions

* Signing identity: Azure Trusted Signing vs. a purchased cert, and who holds the credentials. Blocks Phase 4 only.
* Whether the SPA's host-selection menu should hide native harnesses when the host reports Windows; today it relies on the CLI error. Server/SPA work, tracked separately if the error path proves confusing in step 6.

## Out of scope

* arm64 Windows builds.
* Managed preferences via Group Policy/registry — no requester.
* MSIX/Store or winget publishing — a winget manifest is a follow-up once a signed installer exists.
* Making tmux/PTY harnesses or bwrap work on Windows; the desktop inherits the CLI's documented limits.
