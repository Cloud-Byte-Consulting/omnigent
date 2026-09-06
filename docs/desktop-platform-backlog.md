# Desktop Windows and Linux — Approved backlog

> Snapshot of the approved Linear backlog and its execution rules. Live progress belongs on the Linear issues; the platform plans are in [desktop-linux-plan.md](desktop-linux-plan.md) and [desktop-windows-plan.md](desktop-windows-plan.md).

Created and verified in the Agent Harness project: 7 feature issues, 19 child user stories and 37 child tasks, with 61 reciprocal, cycle-free execution dependencies. Implementation and native acceptance are unstarted. Feature issues are parents of user-story issues; user stories are parents of work-item tasks. Matching milestones provide a supplementary project grouping. Native blocked-by/blocks relations on tasks track predecessor/successor order; each task distinguishes execution predecessors from conditional or final-acceptance prerequisites. Assignees, estimates and dates remain unset. The following is a snapshot of the approved Markdown backlog and its implementation references; live progress belongs on the created issues.

# Desktop Windows and Linux — Delivery Backlog

**Status:** approved and created in Linear on 2026-09-06; all 7 features, 19 stories and 37 tasks are in Backlog and unassigned. Implementation and native acceptance remain unstarted.
**Sources:** [Linux plan](desktop-linux-plan.md) and [Windows plan](desktop-windows-plan.md).
**Scope:** Linux desktop delivery centered on CachyOS, and Windows 10/11 x64 desktop delivery with the CLI's documented Windows limitations.
**Linear project:** [Agent Harness](https://linear.app/cloudbyteconsulting/project/agent-harness-2f117eedf7e9/overview).

This document breaks the plans into **features → user stories → tasks (work items)**. Features are parent issues of user stories, and user stories are parent issues of tasks. Linked local IDs below open the corresponding Linear issues; matching milestones are supplementary grouping, not a substitute for parent issues. Assignees, estimates and dates remain unset.

The source plans retain detailed implementation guidance and manual commands. This backlog defines delivery boundaries, acceptance and sequencing. The [Linear project document](https://linear.app/cloudbyteconsulting/document/desktop-windows-and-linux-approved-plan-and-backlog-8e04c02598c5) contains the approved backlog and both plans. Live progress and dependency changes belong on the issues; creating the backlog does not start implementation or release/publication work.

## Feature index

| Feature | Outcome | User stories | Source |
| -- | -- | -- | -- |
| F01 — Installable desktop packages | Users can install and launch the app on either platform | US01–US02 | Both plans, Phase 1 |
| F02 — Local CLI setup | The desktop finds and invokes the CLI correctly | US03–US05 | Linux Phase 2.1; Windows Phase 2.1–2.3 |
| F03 — Local servers and hosting | Users can run local workloads with correct process ownership | US06–US07 | Both manual verification sections; Windows Phase 2.4–2.6 |
| F04 — Native desktop interaction | Display, session links and notifications work on each desktop | US08–US10 | Both plans, Phase 2 and manual verification |
| F05 — Continuous validation | Contributors get meaningful desktop test and build results | US11–US13 | Both plans, Phase 3 |
| F06 — In-app updates | Users can install an update and resume with settings intact | US14–US15 | Linux Phase 2.3; both plans, Phase 4 |
| F07 — Release and installation guidance | Maintainers can publish verified releases and users can find/install them | US16–US19 | Both plans, Phase 4 and open questions |

## Working rules and definition of done

* **BDD:** every work item includes a Given/When/Then acceptance scenario describing observable behavior. Use the scenario to agree on the expected result before starting. For code changes, translate it into the smallest relevant automated test and follow TDD; for native verification, decisions, documentation and external work, retain reproducible steps and recorded evidence. Gherkin here is a specification format, not a request to add a BDD test framework.
* **Change:** implement with TDD. Within the same work item, write/run a failing regression, confirm the intended failure, make the smallest fix, rerun the focused and affected tests, then refactor while green. Tests and implementation are not separate tickets that can be completed independently.
* **Verify:** test existing behavior first. Record the platform, command/steps and observed result. A failed verification is useful evidence but does not satisfy the story's acceptance criteria; preserve a runnable regression before fixing the defect.
* **Decision:** record the choice, accountable owner and affected work items. Do not invent a choice of publisher, signing credentials or release ownership.
* **Conditional:** activate only when the named trigger occurs; otherwise record why it is not needed. These items do not require speculative implementation.
* **External:** work belongs outside this repository. Track the handoff and its acceptance here; completing repository work does not complete the external item.

A story is done when its acceptance criteria pass and its required work items are complete. Attach red/green evidence for changes and native evidence for platform behavior. A missing dependency, skipped required test, clean update-feed 404 or unavailable OS is not a successful behavior check. Keep unavailable checks pending with the missing environment/dependency named.

Use the existing Node test runner and Electron Playwright lane. Reuse working code and installed dependencies. Before implementation commits, run `pre-commit run --all-files` in the supported contributor environment; use WSL2/Linux/macOS for hooks rather than native Windows. Include images/video for UI changes and fill in the repository PR template.

Dependencies below identify prerequisites for acceptance, not a prohibition on starting independent tests or implementation. Windows and Linux work can proceed independently except for shared CI and release ownership. A platform can ship once its own required stories pass; it does not wait for the other platform's release.

Each Linear task records **predecessors (Blocked by)** and **successors (Blocks)** using native issue relations, plus parallel-work candidates and separate acceptance/conditional references. All 61 execution dependencies were read back and verified reciprocal and cycle-free at creation. Parallel candidates must meet their own prerequisites and coordinate shared-file edits; they are not a global stage barrier. WI13 activates only on a demonstrated Windows teardown failure; if activated, add it as a predecessor to WI27, WI28 and WI37 and include its regression in WI23. WI24 waits for the stated green observation period. Shared README task WI33 gates each platform only through that platform's completed section, as explicitly recorded on the release/download tasks.

## [F01 / CLO-284](https://linear.app/cloudbyteconsulting/issue/CLO-284/desktop-f01-installable-desktop-packages) — Installable desktop packages

### [US01 / CLO-276](https://linear.app/cloudbyteconsulting/issue/CLO-276/desktop-us01-install-the-linux-desktop) — Install the Linux desktop

**User story:** As a Linux user, I want an installable desktop package so I can launch Omnigent from my desktop and keep the CLI available separately.

**Depends on:** none.

**Acceptance criteria:**

* Builds produce AppImage, deb and pacman payloads with the planned Linux naming and generated update metadata; every metadata reference resolves to a built payload.
* CachyOS installation creates the expected launcher/icon/protocol metadata; the app launches and the default `omnigent-desktop-electron` executable does not replace the CLI.
* The deb installs on an Ubuntu/Debian test system. Record actual package filenames and platform coverage; CachyOS evidence alone does not establish Ubuntu/Fedora compatibility.

**Work items:**

- [ ] **[WI01 / CLO-302](https://linear.app/cloudbyteconsulting/issue/CLO-302/desktop-wi01-build-appimage-deb-and-pacman-packages-with-consistent) — Change:** Add a failing `linux_packaging.test.js` for pacman target inclusion and artifact naming, update `web/electron/package.json`, then pass the test and build all three formats. Keep the existing executable name.

  ```gherkin
  Given a clean checkout with the documented Linux build prerequisites
  When a contributor builds the Linux distributables
  Then AppImage, deb and pacman payloads use the agreed Linux naming
  And every generated update-metadata reference names an available payload
  ```
- [ ] **[WI02 / CLO-303](https://linear.app/cloudbyteconsulting/issue/CLO-303/desktop-wi02-verify-linux-package-installation-and-desktop-integration) — Verify:** Install the generated pacman/deb packages and launch AppImage in their target environments. Inspect desktop entries, icons, executable paths and update metadata; record actual output extensions and AppImage runtime/integration requirements. Depends on WI01.

  ```gherkin
  Given each Linux package and its documented target environment
  When a user installs or launches it using the documented prerequisites
  Then Omnigent opens with the expected launcher and icon where integration is configured
  And the separately installed omnigent CLI still resolves to the CLI executable
  ```

**Validation:** `node --test web/electron/test/linux_packaging.test.js`; `pnpm --filter ./web/electron run build:linux --publish never`; Linux manual steps 1–2. Session-link routing is covered by US09.

### [US02 / CLO-277](https://linear.app/cloudbyteconsulting/issue/CLO-277/desktop-us02-install-the-windows-desktop) — Install the Windows desktop

**User story:** As a Windows user, I want a per-user installer so I can launch Omnigent from the Start Menu without an administrator installing it for me.

**Depends on:** none; public signing is separate in US17.

**Acceptance criteria:**

* A build produces the NSIS installer, blockmap and `latest.yml` with matching payload names.
* An unsigned development install launches from the Start Menu with the app icon and expected per-user installation behavior. SmartScreen/policy outcomes are recorded explicitly.
* Generated protocol/AUMID and updater configuration are inspected; unsigned installation is not presented as signed release validation.

**Work items:**

- [ ] **[WI03 / CLO-304](https://linear.app/cloudbyteconsulting/issue/CLO-304/desktop-wi03-configure-and-test-windows-nsis-artifact-naming) — Change:** Add the failing `win_packaging.test.js` naming check, configure the Windows artifact name, and pass it while retaining NSIS defaults.

  ```gherkin
  Given a clean checkout with Windows build prerequisites
  When a contributor builds the Windows distributable
  Then the NSIS installer follows the agreed Windows naming
  And its blockmap and latest.yml are present and identify the matching payload
  ```
- [ ] **[WI04 / CLO-305](https://linear.app/cloudbyteconsulting/issue/CLO-305/desktop-wi04-verify-per-user-windows-installation-and-launch) — Verify:** Build and install in a disposable Windows VM, inspect installed metadata and launch the app. Record tested Windows versions; retain pending smoke checks for any advertised version not tested. Depends on WI03.

  ```gherkin
  Given a standard Windows account whose policy permits the unsigned development installer
  When the user installs Omnigent and launches its Start Menu shortcut
  Then the per-user application opens with the expected icon without installation elevation
  And protocol, AUMID and updater configuration match the installed application
  ```

**Validation:** `node --test web/electron/test/win_packaging.test.js`; `pnpm --filter ./web/electron run build:win --publish never`; Windows manual steps 2–3.

## [F02 / CLO-285](https://linear.app/cloudbyteconsulting/issue/CLO-285/desktop-f02-local-cli-setup) — Local CLI setup

### [US03 / CLO-278](https://linear.app/cloudbyteconsulting/issue/CLO-278/desktop-us03-discover-a-cli-installed-through-fish) — Discover a CLI installed through fish

**User story:** As a CachyOS user who configures PATH in fish, I want the desktop to find my CLI when launched from KDE so I do not need to type an executable override.

**Depends on:** WI01–WI02 for packaged acceptance; the resolver fix can start independently.

**Acceptance criteria:**

* The resolver finds a CLI in a custom directory containing spaces added only through `fish_add_path`, absent from the desktop's inherited PATH and built-in fallback directories.
* The regression fails with the current fish command and passes after the fix; bash/sh execution, banner parsing and fallback behavior continue to pass.

**Work items:**

- [ ] **[WI05 / CLO-306](https://linear.app/cloudbyteconsulting/issue/CLO-306/desktop-wi05-discover-fish-only-cli-paths-from-the-linux-desktop) — Change:** Extend `loginShellPath.test.js` to run the real command with isolated fish configuration, observe the syntax/PATH failure, then apply the fish-safe printf fix and rerun the shell tests. Repeat with a KDE-launched packaged build and no CLI override.

  ```gherkin
  Given the CLI is in a custom directory containing spaces added only through fish_add_path
  And the directory is absent from the desktop PATH and built-in discovery fallbacks
  When the user launches Omnigent from KDE with no CLI override
  Then the desktop discovers and can invoke that CLI
  And the resolver's bash/sh and banner-handling regressions still pass
  ```

**Validation:** `node --test web/electron/test/loginShellPath.test.js`; Linux manual step 3. Do not use `~/.local/bin` alone as the fish regression fixture because fallback discovery can conceal the bug.

### [US04 / CLO-279](https://linear.app/cloudbyteconsulting/issue/CLO-279/desktop-us04-find-and-configure-the-windows-cli) — Find and configure the Windows CLI

**User story:** As a Windows user, I want automatic CLI discovery and accurate installation guidance so I can connect the desktop to local functionality.

**Depends on:** WI03–WI04 for packaged acceptance.

**Acceptance criteria:**

* Configured, PATH and fallback discovery routes resolve valid `.exe` paths, including paths containing spaces and the normal uv installation directory.
* Windows candidates exclude POSIX-only paths. `.cmd`/`.bat` selections are rejected with actionable guidance rather than accepted for an invocation that cannot run them.
* Setup and Settings show the README's `uv tool install --python 3.12 omnigent` instruction when the CLI is missing.

**Work items:**

- [ ] **[WI06 / CLO-307](https://linear.app/cloudbyteconsulting/issue/CLO-307/desktop-wi06-resolve-windows-cli-executables-and-reject-batch-scripts) — Change:** First reproduce missing `.exe` fallback discovery and batch-script acceptance in `omnigent_cli.test.js`; fix the shared resolution paths and pass the tests on Windows.

  ```gherkin
  Given a valid Windows CLI executable reachable by a configured path, PATH or fallback location
  When the desktop resolves the CLI through each route, including a path containing spaces
  Then it selects an invocable .exe without passing the command through a shell
  But a .cmd or .bat selection is rejected with actionable .exe guidance
  ```
- [ ] **[WI07 / CLO-308](https://linear.app/cloudbyteconsulting/issue/CLO-308/desktop-wi07-show-the-windows-uv-installation-hint) — Change:** First test the Windows install hint, then update `INSTALL_COMMAND` and verify both consuming screens, retaining the existing non-Windows hint.

  ```gherkin
  Given the Windows desktop cannot find a usable CLI
  When the user opens Setup or Settings > Local CLI
  Then both screens offer uv tool install --python 3.12 omnigent
  And non-Windows installation guidance retains its existing behavior
  ```

**Validation:** `node --test web/electron/test/omnigent_cli.test.js`; Windows manual steps 1 and 4, including a launch with the CLI directory absent from that process's PATH.

### [US05 / CLO-280](https://linear.app/cloudbyteconsulting/issue/CLO-280/desktop-us05-use-local-commands-without-console-flashes) — Use local commands without console flashes

**User story:** As a Windows desktop user, I want background CLI activity to stay in the app so opening settings or connecting a host does not flash console windows.

**Depends on:** US02 and US04 for packaged acceptance.

**Acceptance criteria:**

* CLI discovery, short commands and long-running host startup request hidden subprocess windows.
* The installed app performs those actions without console flashes; invocation remains shell-free.

**Work items:**

- [ ] **[WI08 / CLO-309](https://linear.app/cloudbyteconsulting/issue/CLO-309/desktop-wi08-hide-consoles-during-windows-cli-discovery-and-execution) — Change:** Add failing subprocess-option tests for `whichName()`'s `where` call, `runCli()` and the host spawn, then add `windowsHide: true` at all three sites and verify the packaged GUI. Include the same setting in WI13 if that conditional task becomes necessary.

  ```gherkin
  Given a Windows user is running the installed desktop
  When the app discovers the CLI, checks its status or starts a host
  Then each subprocess requests a hidden console window
  And no console flashes while the user completes those actions
  ```

**Validation:** `node --test web/electron/test/omnigent_cli.test.js web/electron/test/server_manager.test.js`; Windows manual steps 3–5.

## [F03 / CLO-286](https://linear.app/cloudbyteconsulting/issue/CLO-286/desktop-f03-local-servers-and-hosting) — Local servers and hosting

### [US06 / CLO-281](https://linear.app/cloudbyteconsulting/issue/CLO-281/desktop-us06-run-and-stop-local-workloads-on-linux) — Run and stop local workloads on Linux

**User story:** As a Linux user, I want the desktop to start a server and host my machine, then clean up its own workloads when I quit while preserving daemons I started separately.

**Depends on:** US01 and US03.

**Acceptance criteria:**

* Connecting to a server and starting a local server reach the UI; hosting requires the native confirmation and the host becomes online.
* Supported SDK/native sessions can execute a tool.
* Quitting during an active tool removes desktop-owned host/server/runner processes and preserves a separately started daemon.

**Work items:**

- [ ] **[WI09 / CLO-310](https://linear.app/cloudbyteconsulting/issue/CLO-310/desktop-wi09-verify-linux-local-server-hosting-consent-and-tool) — Verify:** Exercise server connection, local startup, hosting consent and supported tool execution from the Linux package; preserve a regression before fixing any discovered defect.

  ```gherkin
  Given an installed Linux desktop with a usable CLI and supported harness prerequisites
  When the user connects to a server or starts a local one and confirms hosting
  Then the UI connects, the host becomes online and a supported session can execute a tool
  And declining hosting confirmation leaves the machine unenrolled by that action
  ```
- [ ] **[WI10 / CLO-311](https://linear.app/cloudbyteconsulting/issue/CLO-311/desktop-wi10-verify-linux-cleanup-of-desktop-owned-workloads) — Verify:** Quit during active execution and inspect host status/processes with a separately owned daemon present. Record ownership and teardown results, fixing only demonstrated failures through TDD. Depends on WI09.

  ```gherkin
  Given a Linux tool is running under a desktop-owned host and an independent daemon is also running
  When the user quits the desktop before the tool finishes
  Then desktop-owned host, server and runner/tool processes terminate
  And the independent daemon remains running
  ```

**Validation:** Linux manual steps 4 and 8; `omnigent host status --json` plus native process inspection.

### [US07 / CLO-282](https://linear.app/cloudbyteconsulting/issue/CLO-282/desktop-us07-run-and-stop-supported-workloads-on-windows) — Run and stop supported workloads on Windows

**User story:** As a Windows user, I want to run supported SDK sessions locally and stop desktop-owned work reliably without hanging on unsupported native harnesses.

**Depends on:** US02, US04 and US05.

**Acceptance criteria:**

* Local startup connects, streams logs with spaces/backslashes in the data path, and displays CLI path/version. Hosting confirmation and SDK tool execution work.
* Disconnect, restart and quit remove the appropriate owned process tree; independently started daemons survive.
* Unsupported tmux/PTY native harness attempts produce the documented error rather than hanging.

**Work items:**

- [ ] **[WI11 / CLO-312](https://linear.app/cloudbyteconsulting/issue/CLO-312/desktop-wi11-verify-windows-local-server-logs-and-supported-sdk) — Verify:** Exercise local server/log streaming, hosting consent, SDK execution and the unsupported native-harness error. Reuse existing log-directory discovery and tilde expansion; add code only after reproducing a defect.

  ```gherkin
  Given the Windows desktop has a usable CLI and a data directory containing spaces
  When the user starts a local server, confirms hosting and runs an SDK session
  Then startup logs stream, the UI connects and the session executes a tool
  But an unsupported native-harness attempt reports the documented error without hanging
  ```
- [ ] **[WI12 / CLO-313](https://linear.app/cloudbyteconsulting/issue/CLO-313/desktop-wi12-verify-windows-process-ownership-and-teardown) — Verify:** Inspect child/grandchild cleanup during active execution for disconnect, restart and quit, including a separate daemon. Record whether existing Python Job Objects are sufficient. Depends on WI11.

  ```gherkin
  Given Windows has a desktop-owned host with an active runner/tool tree and a separate daemon
  When the user disconnects, restarts the host or quits in separate test runs
  Then the old host and its runner/tool tree stop and restart creates only the intended replacement
  And quit also stops any desktop-owned local server while the separate daemon survives
  ```
- [ ] **[WI13 / CLO-314](https://linear.app/cloudbyteconsulting/issue/CLO-314/desktop-wi13-fix-windows-orphan-processes-only-if-teardown-fails) — Conditional change:** Activate only if WI12 finds an orphan. Preserve a failing native process-tree test through the real teardown path, implement `taskkill /T /F /PID` only if it solves that case, handle errors/await completion, and rerun WI12. A mock of the command alone is insufficient. If WI12 passes, mark this item not needed.

  ```gherkin
  Given WI12 has a repeatable Windows orphan-process failure
  When the same child/grandchild workload is stopped through the corrected teardown path
  Then the owned process tree exits and teardown waits for the result without affecting independent daemons
  And a failed termination is handled explicitly rather than reported as successful cleanup
  ```

**Validation:** `node --test web/electron/test/server_manager.test.js web/electron/test/local_server_log_tail.test.js`; Windows manual steps 5–6; retain any new native regression in the Windows CI leg.

## [F04 / CLO-287](https://linear.app/cloudbyteconsulting/issue/CLO-287/desktop-f04-native-desktop-interaction) — Native desktop interaction

### [US08 / CLO-283](https://linear.app/cloudbyteconsulting/issue/CLO-283/desktop-us08-read-a-sharp-linux-desktop-window) — Read a sharp Linux desktop window

**User story:** As a Plasma user with a scaled display, I want readable text and correctly scaled cursors so the desktop is comfortable to use.

**Depends on:** US01.

**Acceptance criteria:**

* The packaged app selects native Wayland in a Wayland session and renders text/cursors correctly at fractional scaling; an X11 smoke check also passes.
* Native frame and close-to-quit behavior work. Existing Electron defaults remain sufficient unless a reproducible failure demonstrates otherwise.

**Work items:**

- [ ] **[WI14 / CLO-315](https://linear.app/cloudbyteconsulting/issue/CLO-315/desktop-wi14-verify-plasma-wayland-x11-and-display-scaling) — Verify:** Record Wayland/X11 and scaling checks on Plasma. Do not add the obsolete `ozone-platform-hint` switch or a test requiring it; preserve a regression before any actual runtime fix.

  ```gherkin
  Given a Plasma Wayland session with fractional display scaling
  When the user opens and interacts with the packaged desktop
  Then the app uses native Wayland with sharp text and correctly scaled cursors
  And a separate X11 run preserves usable rendering, native framing and close-to-quit behavior
  ```

**Validation:** Linux manual step 2 with the active display backend and scaling recorded.

### [US09 / CLO-291](https://linear.app/cloudbyteconsulting/issue/CLO-291/desktop-us09-open-a-session-from-a-desktop-link) — Open a session from a desktop link

**User story:** As a desktop user, I want an `omnigent://` link to open the intended session so I can move directly from a shared link into my work.

**Depends on:** US01 for WI15; US02 for WI16. Server access is required for the target session.

**Acceptance criteria:**

* A real session link opens the intended session from a cold start and focuses the existing app when it is already running, without a duplicate instance.
* Linux testing distinguishes package registration from separately configured AppImage integration; the AppImage test actually launches the image under test.

**Work items:**

- [ ] **[WI15 / CLO-316](https://linear.app/cloudbyteconsulting/issue/CLO-316/desktop-wi15-verify-linux-and-appimage-session-link-routing) — Verify:** Exercise Linux package and integrated AppImage link routing with `xdg-open`, cold and warm. Record which executable the protocol handler targets.

  ```gherkin
  Given a real session link and a Linux protocol handler pointing to the package or AppImage under test
  When the user opens the link with xdg-open with the app closed and then with it running
  Then the intended session opens in the correct app and the running instance gains focus
  And no duplicate app instance is created
  ```
- [ ] **[WI16 / CLO-317](https://linear.app/cloudbyteconsulting/issue/CLO-317/desktop-wi16-verify-windows-session-link-routing) — Verify:** Exercise Windows link routing through `Win+R`, cold and warm, and confirm the native window focuses the right session.

  ```gherkin
  Given the installed Windows app and a valid omnigent session link
  When the user opens the link through Win+R with the app closed and then with it running
  Then the correct session opens and the existing instance is focused on the warm run
  And no duplicate app instance is created
  ```

**Validation:** Linux manual step 5 and Windows manual step 7; rerun `deepLink.test.js` and affected main-process tests if a fix becomes necessary.

### [US10 / CLO-292](https://linear.app/cloudbyteconsulting/issue/CLO-292/desktop-us10-notice-completed-work-and-return-to-it) — Notice completed work and return to it

**User story:** As a user working outside Omnigent, I want a notification and accurate taskbar badge so I can notice completed work and return to the right window.

**Depends on:** US01/US06 for Linux acceptance; US02/US07 for Windows acceptance. The Windows badge unit work can start independently.

**Acceptance criteria:**

* KDE and Windows show the expected attention cue/toast/sound, and clicking the notification focuses the intended window.
* Badge state increments and clears. Duplicate windows for one server do not double-count; distinct server counts aggregate correctly.
* Windows renders an overlay with an accessible count description at 100% and higher scaling; existing macOS/Linux badge behavior remains intact.

**Work items:**

- [ ] **[WI17 / CLO-318](https://linear.app/cloudbyteconsulting/issue/CLO-318/desktop-wi17-verify-kde-notifications-and-badge-behavior) — Verify:** Check KDE notification and badge behavior; inspect installed desktop-file identity first if the badge fails. Preserve a regression before adding a fix.

  ```gherkin
  Given KDE notifications are enabled and Omnigent is not focused
  When work completes and the user subsequently opens the notification and clears pending items
  Then the attention cue, toast and sound appear and clicking returns to the intended window
  And the badge reflects pending work and clears when none remains
  ```
- [ ] **[WI18 / CLO-319](https://linear.app/cloudbyteconsulting/issue/CLO-319/desktop-wi18-implement-the-windows-taskbar-badge-with-correct-counts) — Change:** Add a failing behavior test for Windows overlay set/clear and aggregation, then implement `setOverlayIcon` using the existing badge calculation and pass it. Use window stubs rather than source-string matching; add no badge dependency.

  ```gherkin
  Given two Windows windows report 2 pending items for one server and another reports 3 for a second server
  When the desktop updates its badge
  Then the overlay represents 5 pending items with an accessible count description
  And updating all counts to zero removes the overlay
  ```
- [ ] **[WI19 / CLO-320](https://linear.app/cloudbyteconsulting/issue/CLO-320/desktop-wi19-verify-windows-notifications-and-scaled-taskbar-overlays) — Verify:** Test the Windows notification flow and overlay at multiple scaling settings and across one-/multi-server windows; attach images/video. Depends on WI18.

  ```gherkin
  Given Windows notifications are enabled and the desktop has windows on one or more servers
  When work completes while unfocused and the user clicks its notification
  Then the taskbar cue, sound and overlay reflect the work and the intended window gains focus
  And the overlay remains legible at 100% and higher scaling and disappears when pending work is cleared
  ```

**Validation:** `node --test web/electron/test/main.test.js`; Linux manual step 7 and Windows manual step 8.

## [F05 / CLO-288](https://linear.app/cloudbyteconsulting/issue/CLO-288/desktop-f05-continuous-validation) — Continuous validation

### [US11 / CLO-293](https://linear.app/cloudbyteconsulting/issue/CLO-293/desktop-us11-maintain-one-desktop-validation-workflow) — Maintain one desktop validation workflow

**User story:** As a contributor, I want one desktop validation workflow that responds to relevant changes so regressions and missing artifacts are visible before release.

**Depends on:** no prerequisite for WI20; WI24 needs operating CI legs and the observation period.

**Acceptance criteria:**

* One `.github/workflows/desktop-build.yml` serves both platforms; filters cover desktop/web inputs, workspace manifests/lockfile, workflow/setup changes and Linux Python/e2e inputs.
* Failed required checks fail their job even while the workflow is not a merge gate. CI builds use `--publish never`.
* Any later merge-gate change follows at least one green week and a recorded readiness decision; it is not required to start the workflow.

**Work items:**

- [ ] **[WI20 / CLO-321](https://linear.app/cloudbyteconsulting/issue/CLO-321/desktop-wi20-create-the-shared-desktop-ci-workflow-and-trigger) — Change:** Define the shared workflow and trigger/artifact contract with its first platform leg (WI21 or WI23). Validate representative dependency-only and code changes; fail on missing expected artifacts and preserve the existing non-publishing release boundary.

  ```gherkin
  Given the shared desktop workflow and a relevant code, dependency or workflow-input change
  When CI evaluates that change
  Then the applicable desktop checks run and a failing required check or missing artifact fails its job
  And the run does not publish to the production release feed
  ```
- [ ] **[WI24 / CLO-325](https://linear.app/cloudbyteconsulting/issue/CLO-325/desktop-wi24-review-desktop-merge-gate-readiness-after-one-green-week) — Conditional decision/change:** After the implemented legs have been green for at least a week, review whether to connect the workflow to `merge-ready.yml`. If selected, validate failure propagation before enabling it. Record deferral if the signal is not ready; this is not a release blocker by itself.

  ```gherkin
  Given the implemented desktop CI legs have at least one green week of evidence
  When maintainers assess merge-gate readiness
  Then the decision and evidence are recorded
  And enabling the gate makes a demonstrated required desktop-check failure block merge readiness
  ```

**Validation:** Fresh-checkout recipes, representative workflow-trigger cases, failed-job visibility and artifact inspection. Do not create a test suite that merely compares YAML text.

### [US12 / CLO-294](https://linear.app/cloudbyteconsulting/issue/CLO-294/desktop-us12-validate-linux-builds-and-a-real-desktop-connection-in-ci) — Validate Linux builds and a real desktop connection in CI

**User story:** As a maintainer, I want Linux CI to execute the shell tests and connect journey and build the packages so a green result demonstrates usable desktop behavior.

**Depends on:** WI01 and WI05 for acceptance; implemented alongside WI20 if Linux lands first.

**Acceptance criteria:**

* The full Node suite runs and the real fish regression executes; Linux CI does not skip it because fish is missing.
* Python uses the uv environment; the SPA and desktop pages are built before Electron launches under xvfb with recording prerequisites installed.
* The connect journey executes and produces a recording; all three package formats and update metadata are checked and uploaded.

**Work items:**

- [ ] **[WI21 / CLO-322](https://linear.app/cloudbyteconsulting/issue/CLO-322/desktop-wi21-run-linux-desktop-unit-tests-and-package-builds-in-ci) — Change:** Add the Linux leg with frozen pnpm dependencies, fish, full Node tests, package builds and explicit required artifact checks. Run it in a fresh checkout and fix observed failures without suppressing them.

  ```gherkin
  Given a fresh Linux CI checkout with the documented dependencies
  When the Linux leg runs
  Then the full Node suite and native fish regression execute and all three Linux package formats are built
  And failed tests or missing required payloads/metadata fail the job
  ```
- [ ] **[WI22 / CLO-323](https://linear.app/cloudbyteconsulting/issue/CLO-323/desktop-wi22-run-and-record-the-real-electron-connect-journey-in-linux) — Change:** Wire the existing Electron journey into that leg: Python/uv setup, scoped `OMNIGENT_SKIP_WEB_UI` during dependency installation, explicit `OMNIGENT_PYTHON`, SPA/desktop-page builds, xvfb and recording dependencies. Validate actual execution and upload the recording. Depends on WI21.

  ```gherkin
  Given CI uses the uv Python environment and has built the SPA and bundled desktop pages
  When the real Electron connect journey runs under xvfb
  Then the user journey reaches the server-backed UI and a playable recording is uploaded
  And a skipped journey, missing prerequisite or failed connection cannot produce a passing required check
  ```

**Validation:** The ordered Linux Phase 3 recipe; missing dependencies, skipped journeys and a missing SPA are setup failures, not passing evidence. Product fixes discovered here require a failing regression first.

### [US13 / CLO-295](https://linear.app/cloudbyteconsulting/issue/CLO-295/desktop-us13-validate-windows-tests-and-installer-builds-in-ci) — Validate Windows tests and installer builds in CI

**User story:** As a maintainer, I want native Windows tests and installer builds so Windows path/process regressions are caught before a release.

**Depends on:** WI03, WI06–WI08 and WI18 for acceptance; implemented alongside WI20 if Windows lands first. Include WI13's regression if activated.

**Acceptance criteria:**

* The full Node suite runs on Windows with correct Windows expectations and explicitly POSIX fixtures preserved.
* The job builds and verifies the installer, blockmap and `latest.yml`; all required files are uploaded.
* Any native process-tree regression from WI13 runs as a required step with its necessary Python setup. Linux-only Electron recording does not substitute for native Windows manual acceptance.

**Work items:**

- [ ] **[WI23 / CLO-324](https://linear.app/cloudbyteconsulting/issue/CLO-324/desktop-wi23-run-native-windows-desktop-tests-and-installer-builds-in) — Change:** Add the Windows leg to the shared workflow, validate from a fresh checkout, repair actual platform-test failures, and enforce artifact checks. Keep the shared connect recording in Linux CI; do not skip Windows behavior coverage to make the suite green.

  ```gherkin
  Given a fresh native Windows CI checkout with the documented dependencies
  When the Windows leg runs
  Then the full Node suite, installer build and any activated native teardown regression execute
  And failing tests or a missing installer, blockmap or update metadata fail the job
  ```

**Validation:** The Windows Phase 3 recipe and downloaded CI artifacts. Individual product fixes follow TDD within this item or their owning story.

## [F06 / CLO-289](https://linear.app/cloudbyteconsulting/issue/CLO-289/desktop-f06-in-app-updates) — In-app updates

### [US14 / CLO-296](https://linear.app/cloudbyteconsulting/issue/CLO-296/desktop-us14-update-an-installed-linux-app) — Update an installed Linux app

**User story:** As a Linux user, I want to update my installed app from its current version so I receive fixes without losing settings.

**Depends on:** US01, US06 and WI29 for the installed staging rehearsal; WI25 can start independently.

**Acceptance criteria:**

* Packaged deb/pacman installs remain eligible for updates without `APPIMAGE`, and their packaged update configuration identifies the correct format.
* Installed AppImage, deb and pacman each complete staging N → N+1, including required elevation, relaunch into N+1 and settings retention.
* No-update and download/checksum-error cases are covered. A 404-only test does not satisfy update installation acceptance.

**Work items:**

- [ ] **[WI25 / CLO-326](https://linear.app/cloudbyteconsulting/issue/CLO-326/desktop-wi25-verify-linux-updater-eligibility-and-package) — Verify:** Exercise the existing feed gate with an injected updater and inspect built `package-type`/`app-update.yml`. Preserve support for all three formats; add a regression before any needed fix.

  ```gherkin
  Given a packaged Linux deb or pacman install with valid updater configuration and no APPIMAGE variable
  When the user checks for updates
  Then the request reaches the updater for that package format
  And the app is not classified as a development build or refused solely because APPIMAGE is absent
  ```
- [ ] **[WI26 / CLO-327](https://linear.app/cloudbyteconsulting/issue/CLO-327/desktop-wi26-rehearse-installed-linux-updates-and-integrity-failures) — Verify:** Prepare staging N/N+1 artifacts and perform the installed upgrade/error checks for AppImage, deb and pacman; record prompts and outcomes. Depends on WI25 and WI29.

  ```gherkin
  Given version N is installed and staging offers valid N+1 payloads for each Linux format
  When the user updates and approves any required elevation
  Then the app relaunches into N+1 with its settings retained
  But a separate run with a corrupted payload reports an integrity failure and does not install it
  ```

**Validation:** `node --test web/electron/test/desktop_updater.test.js`; Linux manual step 6 and Phase 4 staging rehearsal.

### [US15 / CLO-297](https://linear.app/cloudbyteconsulting/issue/CLO-297/desktop-us15-update-a-windows-app) — Update a Windows app

**User story:** As a Windows user, I want the installed app to verify and apply updates so I can restart into the new version with my settings intact.

**Depends on:** US02, US07 and WI29; WI28 additionally requires US17.

**Acceptance criteria:**

* Unsigned development N → N+1 behavior is verified against generated configuration, not assumed from the running executable's signature state.
* Signed N → signed N+1 downloads, verifies, installs and relaunches into the new version with settings retained.
* Signature/checksum failures are surfaced and rejected; no-update/error states are also exercised.

**Work items:**

- [ ] **[WI27 / CLO-328](https://linear.app/cloudbyteconsulting/issue/CLO-328/desktop-wi27-rehearse-unsigned-windows-development-updates) — Verify:** Rehearse unsigned development updates in an isolated VM/staging feed and record actual updater behavior; preserve regressions before any fixes.

  ```gherkin
  Given unsigned Windows development version N and a staging feed configured for unsigned development updates
  When the user installs an unsigned N+1 through the desktop updater
  Then the app relaunches into N+1 with settings retained
  And the recorded result includes the actual generated signature-verification configuration
  ```
- [ ] **[WI28 / CLO-329](https://linear.app/cloudbyteconsulting/issue/CLO-329/desktop-wi28-rehearse-signed-windows-updates-and-rejection-cases) — Verify:** Rehearse signed updates and failure cases using the release signing setup; verify shutdown/relaunch and settings retention. Depends on WI31 as well as WI29.

  ```gherkin
  Given signed Windows version N and staging offering N+1 from the authorized publisher
  When the user downloads, verifies and installs the update
  Then the app relaunches into signed N+1 with settings retained
  But separate corrupted or untrusted-publisher payloads are rejected without installation
  ```

**Validation:** `node --test web/electron/test/desktop_updater.test.js`; Windows manual step 9 and Phase 4 staging rehearsal.

## [F07 / CLO-290](https://linear.app/cloudbyteconsulting/issue/CLO-290/desktop-f07-release-and-installation-guidance) — Release and installation guidance

### [US16 / CLO-298](https://linear.app/cloudbyteconsulting/issue/CLO-298/desktop-us16-establish-ownership-of-releases-and-staging) — Establish ownership of releases and staging

**User story:** As a maintainer, I want named release and staging owners so artifacts can be tested and published through the existing manual process.

**Depends on:** none; resolve early so update rehearsals can proceed.

**Acceptance criteria:**

* An accountable owner and location for the current manual upload process are recorded, along with who can prepare an isolated staging feed.
* Staging can serve test payloads/metadata without changing the production feed. The handoff identifies where payload verification and metadata publication happen.

**Work items:**

- [ ] **[WI29 / CLO-330](https://linear.app/cloudbyteconsulting/issue/CLO-330/desktop-wi29-establish-release-ownership-and-an-isolated-staging-feed) — Decision/external setup:** Confirm release/staging ownership and arrange the staging feed using the existing hosting process. Record handoff details and access requirements without storing credentials in this repository. This unlocks WI26–WI28 and release publication; it does not introduce an automated release pipeline.

  ```gherkin
  Given the existing manual release process needs desktop update rehearsal
  When the release and staging owners complete the handoff
  Then a named operator can retrieve test metadata and every referenced payload from isolated staging
  And the handoff records ownership and access requirements without modifying production or committing credentials
  ```

**Validation:** Rehearsal owners can retrieve test metadata and every referenced payload from staging. The production feed is unaffected.

### [US17 / CLO-299](https://linear.app/cloudbyteconsulting/issue/CLO-299/desktop-us17-identify-and-sign-the-windows-publisher) — Identify and sign the Windows publisher

**User story:** As a Windows user, I want a release signed by the intended publisher so I can verify who distributed the application.

**Depends on:** US02 for artifact verification; signing decisions can start independently.

**Acceptance criteria:**

* Authorized publisher, signing method and credential owner are recorded after checking eligibility/integration; Azure signing and purchased certificates remain options until selected.
* Release checks reject unsigned artifacts; the configured release build produces artifacts that pass native signature verification.
* Local development builds remain possible without release credentials; signing stays in build configuration.

**Work items:**

- [ ] **[WI30 / CLO-331](https://linear.app/cloudbyteconsulting/issue/CLO-331/desktop-wi30-select-the-authorized-windows-signing-method-and-owner) — Decision:** Select the signing method and confirm authorized publisher/credential ownership. Provisioning is owned by the authorized organization, not inferred from package metadata.

  ```gherkin
  Given public Windows releases require an authorized signing identity
  When the organization evaluates eligible signing options and selects one
  Then the publisher, method, credential owner and provisioning requirements are recorded
  And WI31 has an actionable authorized signing setup to implement
  ```
- [ ] **[WI31 / CLO-332](https://linear.app/cloudbyteconsulting/issue/CLO-332/desktop-wi31-configure-and-verify-signed-windows-release-artifacts) — Change:** First run a release-signature check against an unsigned artifact and observe rejection, then configure the chosen signing integration and verify signed output passes. Document the setup without credentials. Depends on WI30.

  ```gherkin
  Given release verification rejects an unsigned installer and authorized signing is configured
  When a maintainer builds and verifies a Windows release artifact
  Then native verification identifies the intended publisher and passes the signed artifact
  And unsigned local development builds remain available without release credentials
  ```

**Validation:** Native Windows signature verification of produced artifacts; WI28 separately validates signed updater behavior.

### [US18 / CLO-300](https://linear.app/cloudbyteconsulting/issue/CLO-300/desktop-us18-find-accurate-installation-instructions) — Find accurate installation instructions

**User story:** As a prospective desktop user, I want a clear download choice and platform-specific instructions so I can install the app and understand its supported local capabilities.

**Depends on:** WI02/WI26 for Linux documentation acceptance; WI04/WI31 and US07 for Windows documentation acceptance. Drafting can start earlier.

**Acceptance criteria:**

* Linux guidance recommends a tested installation option and documents actual filenames, pacman installation, AppImage runtime/integration requirements and package update/elevation behavior.
* Windows guidance covers NSIS installation, unsigned-development warnings, signing setup and the documented limits of native hosting/harnesses.
* Download pages link to the correct released artifacts. Repository documentation and external website completion are tracked separately.

**Work items:**

- [ ] **[WI32 / CLO-333](https://linear.app/cloudbyteconsulting/issue/CLO-333/desktop-wi32-choose-the-recommended-cachyos-installation-route) — Decision:** Choose the recommended CachyOS install after package/update verification; current proposal is pacman for native integration and AppImage for portability. Do not describe deb/pacman as inherently non-updatable.

  ```gherkin
  Given recorded CachyOS installation and update results for pacman and AppImage
  When maintainers select the recommended installation route
  Then the choice cites observed integration, portability and elevation behavior
  And guidance accurately describes updater support for the offered formats
  ```
- [ ] **[WI33 / CLO-334](https://linear.app/cloudbyteconsulting/issue/CLO-334/desktop-wi33-document-tested-desktop-installation-and-platform-limits) — Documentation:** Update `web/electron/README.md` with tested Linux and Windows commands, prerequisites, update behavior and platform limits. Its platform sections can land independently as evidence becomes available.

  ```gherkin
  Given a clean account on a documented target platform
  When a user follows that platform's README prerequisites, installation and launch instructions
  Then the documented package launches and the commands and filenames match the verified release candidate
  And the user can identify update behavior and the platform's supported hosting/harness capabilities
  ```
- [ ] **[WI34 / CLO-335](https://linear.app/cloudbyteconsulting/issue/CLO-335/desktop-wi34-publish-and-verify-the-linux-download-page) — External:** Update `omnigent.ai/download/linux` in the website's owning system and verify its published links/instructions. Depends on WI32–WI33 and WI36 for final link checks.

  ```gherkin
  Given a verified Linux release and the agreed CachyOS recommendation
  When a visitor follows the Linux download page
  Then the recommended route and alternatives point to the correct released artifacts
  And the displayed prerequisites and update behavior match the tested instructions
  ```
- [ ] **[WI35 / CLO-336](https://linear.app/cloudbyteconsulting/issue/CLO-336/desktop-wi35-publish-and-verify-the-windows-download-page) — External:** Update `omnigent.ai/download/windows` in the website's owning system and verify its published links/instructions. Depends on WI33 and WI37 for final link checks.

  ```gherkin
  Given a verified signed Windows release
  When a visitor downloads the installer through the Windows download page
  Then the downloaded version and verified publisher match the intended release
  And the page links to accurate installation instructions and native Windows capability limits
  ```

**Validation:** Follow each platform's README from a clean test account. After publication, download through each website page and confirm the artifact/version is the intended release.

### [US19 / CLO-301](https://linear.app/cloudbyteconsulting/issue/CLO-301/desktop-us19-publish-verified-platform-releases) — Publish verified platform releases

**User story:** As a desktop user, I want the download and update feed to offer a complete verified release so installation and updates do not fail on missing payloads.

**Depends on:** WI29 plus the platform-specific acceptance listed below. This is an extension of the existing manual upload process.

**Acceptance criteria:**

* Each platform's native acceptance and CI checks have passed before its release; pending required checks remain release blockers.
* Every payload named by generated update metadata is uploaded to the location its URL references and verified by URL/checksum before metadata is published last.
* Release assets, feed metadata and documented filenames agree. A payload available only as a GitHub asset does not satisfy a relative URL in the generic feed.

**Work items:**

- [ ] **[WI36 / CLO-337](https://linear.app/cloudbyteconsulting/issue/CLO-337/desktop-wi36-publish-verified-linux-desktop-release-payloads-and) — External release:** Publish the verified Linux AppImage/deb/pacman payloads and generated metadata through the current manual process; copy release assets as appropriate and verify downloads. Requires US01, US03, US06, US08, WI15, WI17, US12, US14, WI29, WI32 and the Linux section of WI33.

  ```gherkin
  Given Linux release prerequisites have passed and every feed payload has been uploaded and verified
  When the authorized operator publishes the generated Linux update metadata last
  Then each referenced URL serves the correct payload with the expected checksum
  And users of the supported formats can retrieve the intended release through their download/update route
  ```
- [ ] **[WI37 / CLO-338](https://linear.app/cloudbyteconsulting/issue/CLO-338/desktop-wi37-publish-verified-signed-windows-release-payloads-and) — External release:** Publish the verified signed Windows installer, blockmap and generated metadata through the current manual process and verify downloads. Requires US02, US04–US05, US07, WI16, WI18–WI19, US13, US15, US17, WI29 and the Windows section of WI33.

  ```gherkin
  Given Windows release prerequisites have passed and the signed installer and blockmap are uploaded and verified
  When the authorized operator publishes latest.yml last
  Then its URLs and checksums match the intended signed release
  And a release smoke check retrieves the installer and verifies the intended publisher
  ```

**Validation:** Retrieve published metadata and its payload URLs, compare checksums, and perform the release-specific update/download smoke check. Website rollout WI34/WI35 follows available release artifacts.

## Suggested delivery order

1. Start packaging (US01–US02), CLI fixes (US03–US05), release/staging ownership (US16) and signing selection (WI30). Tests and unit-level badge work can proceed independently of native installers.
2. With packages available, complete local hosting (US06–US07), native interaction (US08–US10) and the platform CI legs (US11–US13). Activate WI13 only on a demonstrated teardown failure.
3. With staging and signing available, complete installed update rehearsals (US14–US15), signing (US17) and the relevant README sections/recommendation (US18).
4. Publish each accepted platform (US19), then finish its external download page. Review merge-gate readiness separately after the green observation period (WI24).

## Deferred scope

These are not active work items or prerequisites for the features above:

* AUR, winget, Flatpak, Snap and MSIX/Store distribution.
* Windows arm64 and managed preferences on either new platform.
* Making tmux/PTY native harnesses or bwrap work on Windows.
* A Linux packaged DevTools override, unless debugging actually requires it; then capture the need and add a small TDD work item.
* Hiding unsupported native harnesses in the SPA. Revisit as separate server/SPA work only if the verified Windows error flow is confusing.
* A new automated release pipeline; builds/tests are automated, publication extends the existing manual process.

## Linear verification and ongoing tracking

* Check that every story has an outcome, observable acceptance criteria and work items, and that every work item has a Given/When/Then scenario; use the source-plan links to review detailed commands.
* Open a linked feature and expand its user stories, then a story's tasks. For example, F01 / [CLO-284](https://linear.app/cloudbyteconsulting/issue/CLO-284/desktop-f01-installable-desktop-packages) contains US01 / [CLO-276](https://linear.app/cloudbyteconsulting/issue/CLO-276/desktop-us01-install-the-linux-desktop), which contains WI01 / [CLO-302](https://linear.app/cloudbyteconsulting/issue/CLO-302/desktop-wi01-build-appimage-deb-and-pacman-packages-with-consistent) and WI02 / [CLO-303](https://linear.app/cloudbyteconsulting/issue/CLO-303/desktop-wi02-verify-linux-package-installation-and-desktop-integration); WI01 Blocks WI02 and WI02 is Blocked by WI01. Each task also lists its execution predecessors, successors and parallel candidates. Record platform results separately for shared stories so one platform's success does not imply the other's.
* Issues were created under the project's CloudByteConsulting team. Assign owners and estimates during planning; none were invented during creation. Keep decisions, conditional work and external handoffs distinguishable from required implementation.
* Mark work complete only when the recorded evidence meets the acceptance criteria. This Markdown backlog starts with no completed implementation or native acceptance checks.
