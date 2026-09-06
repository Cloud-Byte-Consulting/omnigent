const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const packageConfig = require("../package.json");

describe("Linux packaging", () => {
  it("targets AppImage, deb and pacman", () => {
    const targets = packageConfig.build.linux.target;
    for (const target of ["AppImage", "deb", "pacman"]) {
      assert.ok(targets.includes(target), `missing linux target ${target}`);
    }
  });

  it("names artifacts with the Linux suffix", () => {
    assert.equal(
      packageConfig.build.linux.artifactName,
      "${productName}-${version}-${arch}-linux.${ext}",
    );
  });

  it("pins pacman depends to packages in the official Arch repos", () => {
    const depends = packageConfig.build.pacman?.depends;
    assert.ok(Array.isArray(depends), "build.pacman.depends must be an explicit list");
    assert.ok(!depends.includes("http-parser"), "http-parser is not in the Arch repos");
    assert.ok(depends.includes("gtk3") && depends.includes("nss"));
  });
});
