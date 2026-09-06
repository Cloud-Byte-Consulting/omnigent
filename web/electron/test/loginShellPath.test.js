// Tests for src/loginShellPath.js — the login-shell PATH resolver that patches
// process.env.PATH for GUI-launched Electron (see #1933). Run with `node --test`
// (no extra deps). These exercise the REAL module: resolveLoginShellPath takes
// execFileSync/os/env/platform as injectable deps, so most outcomes are driven
// with mocks. One block spawns the real fish/bash/sh binaries under an isolated
// HOME to prove the printf line is valid in every shell. A source-guard at the
// end pins the main.js wiring so it can't silently regress to a bare PATH replace.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  resolveLoginShellPath,
  mergePath,
  extractPath,
  isLikelyLoginPath,
  stripAnsi,
} = require("../src/loginShellPath");

const START = "__OMNIGENT_PATH_START__";
const END = "__OMNIGENT_PATH_END__";
const ESC = String.fromCharCode(27);

// A stripped launchd-style PATH (the input state this whole module exists to fix).
const STRIPPED = "/usr/bin:/bin:/usr/sbin:/sbin";
const zshUser = { userInfo: () => ({ shell: "/bin/zsh" }) };

describe("extractPath", () => {
  it("pulls the PATH out from between the delimiters", () => {
    const out = `${START}/opt/homebrew/bin:/usr/bin${END}`;
    assert.equal(extractPath(out), "/opt/homebrew/bin:/usr/bin");
  });

  it("ignores an rc-file banner printed around the delimited value", () => {
    const out = `Last login: whenever\nnvm loaded\n${START}/opt/homebrew/bin:/usr/bin${END}\n`;
    assert.equal(extractPath(out), "/opt/homebrew/bin:/usr/bin");
  });

  it("strips ANSI color codes inside the delimited value", () => {
    const out = `${START}${ESC}[32m/opt/homebrew/bin:/usr/bin${ESC}[0m${END}`;
    assert.equal(extractPath(out), "/opt/homebrew/bin:/usr/bin");
  });

  it("returns null when markers are absent or non-string", () => {
    assert.equal(extractPath("no markers here"), null);
    assert.equal(extractPath(`${START}only start marker`), null);
    assert.equal(extractPath(undefined), null);
    assert.equal(extractPath(`${START}${END}`), null); // empty
  });
});

describe("stripAnsi", () => {
  it("removes SGR escape sequences", () => {
    assert.equal(stripAnsi(`${ESC}[1;32mgreen${ESC}[0m`), "green");
  });
  it("leaves plain text untouched", () => {
    assert.equal(stripAnsi("/opt/homebrew/bin:/usr/bin"), "/opt/homebrew/bin:/usr/bin");
  });
});

describe("isLikelyLoginPath (fast-path skip)", () => {
  it("is true on darwin when a Homebrew dir is already present", () => {
    assert.equal(isLikelyLoginPath({ PATH: "/opt/homebrew/bin:/usr/bin" }, "darwin"), true);
    assert.equal(isLikelyLoginPath({ PATH: "/usr/local/bin:/usr/bin" }, "darwin"), true);
  });
  it("is false on darwin for a stripped PATH", () => {
    assert.equal(isLikelyLoginPath({ PATH: STRIPPED }, "darwin"), false);
  });
  it("is false off darwin (never skip on Linux)", () => {
    assert.equal(isLikelyLoginPath({ PATH: "/opt/homebrew/bin" }, "linux"), false);
  });
});

describe("resolveLoginShellPath", () => {
  it("resolves via an interactive+login shell using the passwd-DB shell", () => {
    const calls = [];
    const execFileSync = (shell, args) => {
      calls.push({ shell, args });
      return `${START}/opt/homebrew/bin:/usr/bin${END}`;
    };
    const result = resolveLoginShellPath({
      execFileSync,
      os: zshUser,
      env: { PATH: STRIPPED }, // $SHELL intentionally absent (GUI launch)
      platform: "darwin",
    });
    assert.equal(result, "/opt/homebrew/bin:/usr/bin");
    // Uses os.userInfo().shell, not $SHELL, and runs interactive+login.
    assert.equal(calls[0].shell, "/bin/zsh");
    assert.equal(calls[0].args[0], "-ilc");
    // `${PATH}` is a syntax error in fish; the script must use bare `$PATH`.
    assert.doesNotMatch(calls[0].args[1], /\$\{/);
  });

  it("prefers $SHELL when the passwd DB has no shell", () => {
    const calls = [];
    const execFileSync = (shell) => {
      calls.push(shell);
      return `${START}/x${END}`;
    };
    resolveLoginShellPath({
      execFileSync,
      os: { userInfo: () => ({}) },
      env: { PATH: STRIPPED, SHELL: "/usr/bin/fish" },
      platform: "darwin",
    });
    assert.equal(calls[0], "/usr/bin/fish");
  });

  it("returns null on win32 without spawning anything", () => {
    let spawned = false;
    const result = resolveLoginShellPath({
      execFileSync: () => {
        spawned = true;
        return "";
      },
      os: zshUser,
      env: { PATH: "C:\\Windows" },
      platform: "win32",
    });
    assert.equal(result, null);
    assert.equal(spawned, false);
  });

  it("skips the spawn when PATH already looks complete (fast path)", () => {
    let spawned = false;
    const result = resolveLoginShellPath({
      execFileSync: () => {
        spawned = true;
        return "";
      },
      os: zshUser,
      env: { PATH: "/opt/homebrew/bin:/usr/bin" },
      platform: "darwin",
    });
    assert.equal(result, null);
    assert.equal(spawned, false);
  });

  it("falls through to the next shell when the first fails, and returns null if all fail", () => {
    const tried = [];
    const execFileSync = (shell) => {
      tried.push(shell);
      throw new Error(`cannot spawn ${shell}`);
    };
    const result = resolveLoginShellPath({
      execFileSync,
      os: zshUser,
      env: { PATH: STRIPPED },
      platform: "darwin",
    });
    assert.equal(result, null);
    // Tried the user shell plus POSIX fallbacks (deduped).
    assert.ok(tried.length >= 2);
    assert.ok(tried.includes("/bin/bash"));
  });

  it("recovers a delimited PATH from err.stdout on non-zero exit", () => {
    const execFileSync = () => {
      const err = new Error("shell exited 1");
      err.stdout = `warning: something\n${START}/opt/homebrew/bin${END}`;
      throw err;
    };
    const result = resolveLoginShellPath({
      execFileSync,
      os: zshUser,
      env: { PATH: STRIPPED },
      platform: "darwin",
    });
    assert.equal(result, "/opt/homebrew/bin");
  });
});

// Real shells, real execFileSync. HOME and XDG_CONFIG_HOME point at a temp dir
// so only the rc file written here is sourced; the tool dir has a space in its
// name and is absent from the PATH handed to the shell.
describe("resolveLoginShellPath with real shells", { skip: process.platform === "win32" }, () => {
  const FISH = "/usr/bin/fish";

  function withRc(rcFile, line) {
    const root = mkdtempSync(path.join(os.tmpdir(), "omnigent-login-path-"));
    const toolDir = path.join(root, "my tools", "bin");
    const rc = path.join(root, rcFile);
    for (const dir of [toolDir, path.join(root, "home"), path.dirname(rc)])
      mkdirSync(dir, { recursive: true });
    writeFileSync(rc, line(toolDir) + "\n");
    return {
      toolDir,
      env: {
        PATH: "/usr/bin:/bin",
        HOME: path.join(root, "home"),
        XDG_CONFIG_HOME: path.join(root, "xdg"),
      },
      cleanup: () => rmSync(root, { recursive: true, force: true }),
    };
  }

  function assertResolves(shell, rcFile, line) {
    const { toolDir, env, cleanup } = withRc(rcFile, line);
    try {
      const result = resolveLoginShellPath({
        os: { userInfo: () => ({ shell }) },
        env,
        platform: "linux",
      });
      assert.ok(
        result && result.split(":").includes(toolDir),
        `${shell} PATH lacks ${toolDir}: ${result}`,
      );
    } finally {
      cleanup();
    }
  }

  // CI must install fish so this regression really runs; only a dev box may skip.
  const fishSkip = !existsSync(FISH) && !process.env.CI && `${FISH} not installed`;
  it("finds a spaced dir added only through fish_add_path", { skip: fishSkip }, () => {
    assert.ok(existsSync(FISH), `${FISH} missing in CI`);
    assertResolves(FISH, "xdg/fish/config.fish", (dir) => `fish_add_path "${dir}"`);
  });

  it("finds a dir exported from .bash_profile under bash", () => {
    assertResolves("/bin/bash", "home/.bash_profile", (dir) => `export PATH="${dir}:$PATH"`);
  });

  it("finds a dir exported from .profile under sh", () => {
    assertResolves("/bin/sh", "home/.profile", (dir) => `PATH="${dir}:$PATH"; export PATH`);
  });
});

describe("mergePath", () => {
  it("unions the two, login PATH first, de-duplicating", () => {
    assert.equal(
      mergePath("/usr/bin:/x", "/opt/homebrew/bin:/usr/bin"),
      "/opt/homebrew/bin:/usr/bin:/x",
    );
  });
  it("preserves a current-only dir the login shell lacks (real merge, not replace)", () => {
    assert.equal(mergePath("/app/injected:/usr/bin", "/usr/bin"), "/usr/bin:/app/injected");
  });
  it("tolerates empty inputs", () => {
    assert.equal(mergePath("", "/usr/bin"), "/usr/bin");
    assert.equal(mergePath("/usr/bin", ""), "/usr/bin");
  });
});

// Source-guard: main.js must merge (not replace) the resolved PATH. A behavior
// test can't see main.js wiring, and a bare `process.env.PATH = _loginPath` would
// silently drop app-injected dirs — so pin the call shape here.
describe("main.js wiring", () => {
  const mainSource = readFileSync(path.join(__dirname, "../src/main.js"), "utf8");
  const liveCode = mainSource.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");

  it("merges the login PATH rather than replacing it", () => {
    assert.match(liveCode, /process\.env\.PATH\s*=\s*mergePath\(/);
    assert.doesNotMatch(liveCode, /process\.env\.PATH\s*=\s*_loginPath\b/);
  });
});
