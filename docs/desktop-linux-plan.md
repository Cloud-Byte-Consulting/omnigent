# Desktop Shell — Linux (CachyOS) Plan

**Status:** proposed; implementation and TDD cycles not started
**Baseline:** `fork/main` @ `aadd89b4f` (upstream `omnigent-ai/omnigent` main merged 2026-09-05)
**Sibling plan:** [`desktop-windows-plan.md`](desktop-windows-plan.md) — independent, shares one CI workflow file (see Phase 3).
**Target:** CachyOS (Arch-based, KDE Plasma on Wayland, `fish` login shell). Ubuntu/Fedora compatibility needs its own smoke checks; a CachyOS pass does not establish it.

## Goal

Ship an installable, self-updating Linux build of the Electron shell (`web/electron`) that reaches parity with the macOS build for everything that is not macOS-specific: connect to a server, start a local server, host this machine, deep links, notifications, badge, multi-window, updates.

## Today (on `main`)

The shell is macOS-first but not macOS-only. What already exists:

* `web/electron/package.json` → `build.linux` targets `AppImage` + `deb`, icon `icons/icon.png` (1024², fine), category `Development`, generic update feed `https://omnigent.ai/_desktop/updates/`. `pnpm run build:linux` exists and has never run in CI.
* macOS-specific behavior is guarded: `afterPack.js` checks the target platform; `afterAllArtifactBuild.js` requires the DMG notarization environment flag; title bar, dock icon, sounds and preference handling check `darwin`. Leave the DMG flag unset for Linux builds. Off macOS the window keeps its native frame and `window-all-closed` quits.
* Deep links: `second-instance` + cold-start argv ingestion exists (`src/main.js` ~3403–3483), `setAsDefaultProtocolClient("omnigent")` is called on every platform.
* Badge (`app.setBadgeCount`, LauncherEntry D-Bus API) and `flashFrame` calls exist. Verify KDE behavior and that the badge's desktop filename matches the installed `.desktop` entry.
* `src/loginShellPath.js` runs on Linux (`os.userInfo().shell` → `-ilc`).
* The Python side (`omnigent server --background`, `omnigent host`, tmux native harnesses, bwrap sandbox) is fully supported on Linux — no server-side work.
* CI: `web-tests.yml` runs exactly one Electron unit test; no workflow builds any desktop artifact on any OS. The current handoff describes macOS builds/uploads as manual and outside this repo; confirm the owner in Phase 4.
* e2e: `web/electron/e2e/` is a Playwright `_electron` lane that already documents `xvfb-run` for headless Linux.
* `pnpm-lock.yaml` resolves Electron **42.7.0**, electron-builder **26.15.3**, and electron-updater **6.8.9**. Electron has selected native Wayland automatically since 38; no `ozone-platform-hint` switch is needed ([Electron breaking changes](https://www.electronjs.org/docs/latest/breaking-changes#removed-electron_ozone_platform_hint-environment-variable)).
* The locked updater selects AppImage/deb/pacman implementations, using `resources/package-type` for installed packages ([6.8.9 source](https://github.com/electron-userland/electron-builder/blob/electron-updater%406.8.9/packages/electron-updater/src/main.ts)). Missing `APPIMAGE` does not mean updates are unsupported.

## Approach

Reuse electron-builder, electron-updater, the existing `node --test` suite and the Playwright Electron lane. Follow TDD for each behavior/configuration change, one small increment at a time:

1. **Red:** add the smallest regression test and run it against the unchanged implementation. Confirm that it fails for the intended behavior, not missing dependencies or setup errors.
2. **Green:** make the smallest change, rerun that test, then run the affected existing tests. Refactor only with those tests passing.
3. **Verify:** complete the phase's build/manual checks before moving on; record the failing command/assertion, passing rerun and platform in the implementation PR's Test Plan. List checks awaiting a later phase or unavailable platform as pending, with the dependency named.

For verify-only items, run the check first. If it passes, leave the code alone; if it fails, preserve a runnable regression before fixing it. Do not manufacture a failing test for working behavior or test source text instead of the shell/process behavior.

## Phase 1 — Packaging config (`web/electron/package.json`)

1. First add a failing packaging test for the `pacman` target and Linux artifact naming, then add `pacman` alongside `AppImage` and `deb`, with `build.linux.artifactName: "${productName}-${version}-${arch}-linux.${ext}"`.
2. Keep the default executable name, `omnigent-desktop-electron`. It comes from the package name and does not collide with the `omnigent` CLI ([builder source](https://github.com/electron-userland/electron-builder/blob/electron-builder%4026.15.3/packages/app-builder-lib/src/linuxPackager.ts)).
3. Build all three targets and inspect the artifacts, `.desktop` entry, icon and `x-scheme-handler/omnigent` MIME type. The locked FPM target passes `pacman` as `${ext}`; expect `.pacman`, not `.pkg.tar.zst`, with this naming pattern. Confirm against actual output before release ([FPM source](https://github.com/electron-userland/electron-builder/blob/electron-builder%4026.15.3/packages/app-builder-lib/src/targets/FpmTarget.ts), [artifact expansion](https://github.com/electron-userland/electron-builder/blob/electron-builder%4026.15.3/packages/app-builder-lib/src/platformPackager.ts)).

No new `linux.desktop` overrides unless artifact inspection demonstrates a missing or incorrect field.

## Phase 2 — Runtime regression and platform verification

1. **Fish PATH regression.** First extend `test/loginShellPath.test.js` to execute the resolver's actual command with fish in an isolated temporary shell configuration. Add a directory containing spaces only through `fish_add_path`; require it in the colon-delimited resolved PATH, with no fallback shell supplying it. Observe failure on `${PATH}`, then change the command to `printf '%s' "<START>$PATH" "<END>"`. Exercise the same command with bash/sh and retain banner parsing/fallback tests. The candidate list already tries `/bin/sh`; do not change `pickShell` without a separate failing case.
2. **Wayland/HiDPI.** Verify native Wayland selection, text and cursor scaling on Plasma; also smoke-test X11. No switch change or source-string test is planned.
3. **Updates.** Keep packaged deb/pacman eligible without `APPIMAGE`. First verify the existing feed gate with an injected updater and inspect packaged `package-type` and `app-update.yml`. Actual installed updates, elevation prompts and restart behavior are Phase 4's staging checks. A failure enters the TDD loop; do not add an AppImage-only gate.
4. **Notifications and badge.** Verify sound, attention cue, badge increment/clear and notification click routing on KDE. If the badge fails, inspect desktop-file identity before adding platform code.
5. **Developer mode.** Packaged Linux DevTools support remains deferred unless debugging requires it; any override gets its own failing behavior test first.

## Phase 3 — CI (`.github/workflows/desktop-build.yml`, new)

One workflow, modelled on `windows.yml`'s "non-blocking signal" stance. Include desktop/web build inputs, workspace manifests and lockfile, the workflow/setup action, and Python/e2e inputs in path filters so dependency-only changes also run it. Linux leg, in order:

* `ubuntu-latest`, `./.github/actions/setup-pnpm`, `pnpm install --frozen-lockfile --filter ./web/electron --filter web`. Install fish and headless Electron prerequisites (`xvfb`, required system libraries and Playwright's recording binary). The fish regression must execute in this lane, not skip because fish is absent.
* Reuse Python/uv setup from `windows.yml`; run `OMNIGENT_SKIP_WEB_UI=true uv sync --locked --group test`, then set `OMNIGENT_PYTHON` to `${{ github.workspace }}/.venv/bin/python`. Do not carry `OMNIGENT_SKIP_WEB_UI` into the server run.
* Run `node --test web/electron/test`. Build the SPA with `pnpm --filter web run build` and the bundled desktop pages with `pnpm --filter ./web/electron run build:overlay` **before** e2e.
* Run `OMNIGENT_PW_NO_SANDBOX=1 xvfb-run -a node --test web/electron/e2e/desktop_connect.e2e.js` using that interpreter. Missing dependencies, a missing SPA, or a skipped journey do not count as a passing phase.
* `pnpm --filter ./web/electron run build:linux --publish never`; assert the three payload formats and generated update metadata exist, then upload them and the e2e recording. Use separate `actions/upload-artifact` paths for `*.AppImage`, `*.deb`, `*.pacman` and `*.yml`; fail on missing expected artifacts.
* Not wired into `merge-ready.yml` until it has been green for a week.

If the Windows plan lands first, this phase adds a matrix entry to the workflow it created rather than a second file.

Run the documented recipe in a fresh checkout before wiring it into CI. If a build/e2e check fails, fix its setup or add a regression for the actual product failure, then rerun it; do not make the individual checks advisory just because the workflow is not yet a merge gate.

## Phase 4 — Release (external + one doc change)

* Rehearse version N → N+1 on an installed AppImage, deb and pacman package using an isolated staging feed. Check download, checksum failure handling, installation/elevation and relaunch into N+1 with settings retained. Reproduce and test any failure before changing updater code.
* Extend the existing manual upload step: upload **all payloads referenced by the generated Linux update metadata**, including deb/pacman, to `_desktop/updates/`. Verify the payload URLs and checksums, then publish the metadata last. Copy installers to GitHub Release assets as appropriate; a payload available only on GitHub cannot satisfy a relative URL in the generic feed.
* `omnigent.ai/download/linux` is website work; out of repo.
* `web/electron/README.md`: document the three targets, actual generated filenames, `sudo pacman -U <package>` and update/elevation behavior. Document AppImage FUSE requirements and the `--appimage-extract-and-run` fallback after verifying them on CachyOS.
* AUR package: skip. → add when someone asks; it is a PKGBUILD that downloads the release AppImage, not code in this repo.

## Tests

* `test/loginShellPath.test.js`: the real fish command fails before the fix and preserves fish-only PATH entries after it; bash/sh, banner parsing and shell fallbacks still pass. Require the native shell case in Linux CI; scope it off Windows while keeping portable resolver tests active there.
* `test/linux_packaging.test.js`: requested pacman target/naming fail before the packaging edit, then pass. A real build validates generated package contents and feed payloads.
* `test/desktop_updater.test.js`: packaged Linux can check updates without `APPIMAGE`; retain existing development, download and error behavior tests. Passing existing behavior is verification, not a claimed red/green change.
* `e2e/desktop_connect.e2e.js`: the SPA-backed connect journey executes and produces a recording. Native package installation and update checks remain required on the target OS.

From the repository root, after installing dependencies:

```bash
node --test web/electron/test/loginShellPath.test.js
# Once added in Phase 1:
node --test web/electron/test/linux_packaging.test.js
node --test web/electron/test/desktop_updater.test.js
node --test web/electron/test
```

Run the relevant focused command at both red and green. Before committing implementation work, run `pre-commit run --all-files` in the contributor environment. These documents specify future validation; they do not claim those implementation checks have passed.

## Manual verification (CachyOS)

1. From the repo root, `pnpm --filter ./web/electron run build:linux --publish never` → `web/electron/dist/` contains AppImage, deb, pacman and generated Linux update metadata. Verify filenames and that every metadata payload exists.
2. `sudo pacman -U web/electron/dist/Omnigent-*-linux.pacman` (select one version if several builds exist); launch from the KDE app menu. Verify native Wayland and sharp text/cursors at fractional scaling; repeat on X11. Inspect the installed `.desktop` entry and confirm the `omnigent` CLI still resolves separately.
3. In a disposable test account with fish as login shell, install the CLI into a custom directory, e.g. `env UV_TOOL_BIN_DIR="$HOME/fish tools/bin" uv tool install --python 3.12 omnigent`, and add it only with `fish_add_path "$HOME/fish tools/bin"`. Keep it out of the desktop session's inherited PATH and leave the app's CLI override empty. Launch from KDE: Setup → gear must discover that custom path. Record failure on the pre-fix build and success on the fixed build; `~/.local/bin` is insufficient because fallback discovery already checks it.
4. "Start a server on this machine" → connects to `http://127.0.0.1:<port>`. Host menu → connect this machine → native confirm dialog → host online.
5. `xdg-open 'omnigent://…'` with a real session link focuses the running window and opens the session; repeat from a cold start. Test AppImage separately with desktop integration configured to point to that image; `chmod +x` alone does not register a protocol handler.
6. Server → Check for Updates: both pacman and AppImage check the feed. On an Ubuntu/Debian test system repeat with deb. Use the staging N → N+1 rehearsal from Phase 4 to verify actual installation; a clean 404 is only error-path coverage.
7. Finish a turn in a non-focused window: verify attention cue, KDE badge increment, toast sound and click routing; clear pending items and confirm the badge clears. Repeat across two windows.
8. While an SDK/native session is running a tool, quit the app: desktop-owned host/server and runner processes disappear; a hand-started daemon survives. Confirm with `omnigent host status --json` and process inspection.

## Open questions

* Recommended CachyOS install: propose pacman for native integration and AppImage for portability. Both have updater support; choose the recommendation after validating installation and elevation behavior.
* Who owns the existing manual `_desktop/updates/` upload step and the staging feed used for update verification.

## Out of scope

* Managed preferences for Linux (`/etc/…` policy file) — no requester.
* Flatpak/Snap — electron-builder can emit them, nobody asked.
* Any change to the web SPA; the `[data-electron-mac]` chrome rules correctly do not apply on Linux.
