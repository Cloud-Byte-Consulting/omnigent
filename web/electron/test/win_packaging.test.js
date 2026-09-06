const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const packageConfig = require("../package.json");

describe("Windows packaging", () => {
  it("names the NSIS installer like the other platforms", () => {
    assert.equal(
      packageConfig.build.win.artifactName,
      "${productName}-${version}-${arch}-win.${ext}",
    );
  });

  it("targets a per-user NSIS installer", () => {
    assert.ok(packageConfig.build.win.target.includes("nsis"));
  });

  it("uses the Windows icon", () => {
    assert.equal(packageConfig.build.win.icon, "icons/icon.ico");
  });
});
