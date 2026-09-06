// Tests for e2e/desktopHarness.js's dependency gate: a missing electron /
// playwright is a skip by default and a hard failure under
// OMNIGENT_E2E_REQUIRE, so CI can never go green on a skipped journey.

"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { desktopDepsAvailable } = require("../e2e/desktopHarness");

const resolveAll = () => "ok";
const noElectron = (id) => {
  if (id === "electron") throw new Error(`Cannot find module '${id}'`);
  return "ok";
};

describe("desktopDepsAvailable", () => {
  it("reports a missing dep as a skip by default", () => {
    assert.deepEqual(desktopDepsAvailable({ env: {}, resolve: noElectron }), {
      ok: false,
      missing: ["electron"],
    });
  });

  it("throws instead of skipping when OMNIGENT_E2E_REQUIRE is set", () => {
    assert.throws(
      () => desktopDepsAvailable({ env: { OMNIGENT_E2E_REQUIRE: "1" }, resolve: noElectron }),
      /OMNIGENT_E2E_REQUIRE.*electron/,
    );
  });

  it("is ok when everything resolves, required or not", () => {
    const deps = { env: { OMNIGENT_E2E_REQUIRE: "1" }, resolve: resolveAll };
    assert.deepEqual(desktopDepsAvailable(deps), { ok: true, missing: [] });
  });
});
