// Tests for the app-wide badge (src/badge.js), run with `node --test` (no
// extra deps). Covers the per-origin aggregation and how the total reaches
// the OS: `app.setBadgeCount` on macOS/Linux, a per-window taskbar overlay
// with an accessible description on Windows.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { aggregateBadgeCount, applyBadge } = require("../src/badge");

const A = "http://localhost:8000";
const B = "https://dbc-x.cloud.databricks.com";

function fakeWindow() {
  const overlays = [];
  return {
    overlays,
    isDestroyed: () => false,
    setOverlayIcon: (image, description) => overlays.push({ image, description }),
  };
}

function fakeApp() {
  const badgeCounts = [];
  return { badgeCounts, setBadgeCount: (n) => (badgeCounts.push(n), true) };
}

const nativeImage = {
  createFromBitmap: (buffer, { width, height }) => ({ buffer, width, height }),
};

function pixel(image, x, y) {
  const i = (y * image.width + x) * 4;
  return [...image.buffer.subarray(i, i + 4)]; // BGRA
}

describe("aggregateBadgeCount", () => {
  it("counts one server's duplicate windows once and sums distinct servers", () => {
    assert.equal(
      aggregateBadgeCount([
        { origin: A, count: 2 },
        { origin: A, count: 2 },
        { origin: B, count: 3 },
      ]),
      5,
    );
  });

  it("takes the max per origin and skips unpinned windows", () => {
    assert.equal(
      aggregateBadgeCount([
        { origin: A, count: 1 },
        { origin: A, count: 4 },
        { origin: null, count: 9 },
      ]),
      4,
    );
    assert.equal(aggregateBadgeCount([]), 0);
  });
});

describe("applyBadge on Windows", () => {
  it("overlays every window with the total and an accessible description", () => {
    const windows = [fakeWindow(), fakeWindow(), fakeWindow()];
    const app = fakeApp();
    applyBadge({ platform: "win32", app, windows, nativeImage, total: 5 });
    for (const win of windows) {
      assert.equal(win.overlays.length, 1, "each window gets exactly one overlay call");
      const { image, description } = win.overlays[0];
      assert.equal(description, "5 pending items");
      assert.deepEqual([image.width, image.height], [16, 16]);
      assert.equal(image.buffer.length, 16 * 16 * 4, "16x16 BGRA bitmap");
      assert.equal(pixel(image, 0, 0)[3], 0, "corner is transparent (round badge)");
      assert.equal(pixel(image, 8, 1)[3], 0xff, "disc is opaque");
      // The centre column of a "5" glyph's top bar is white.
      assert.deepEqual(pixel(image, 7, 5), [0xff, 0xff, 0xff, 0xff]);
    }
    assert.deepEqual(app.badgeCounts, [], "app-level badge is not used on Windows");
  });

  it("singular description and 9+ glyph still carry the exact count", () => {
    const one = fakeWindow();
    applyBadge({ platform: "win32", app: fakeApp(), windows: [one], nativeImage, total: 1 });
    assert.equal(one.overlays[0].description, "1 pending item");

    const many = fakeWindow();
    applyBadge({ platform: "win32", app: fakeApp(), windows: [many], nativeImage, total: 12 });
    assert.equal(many.overlays[0].description, "12 pending items");
    assert.equal(many.overlays[0].image.width, 16);
  });

  it("clears the overlay on every window when the total drops to zero", () => {
    const windows = [fakeWindow(), fakeWindow()];
    applyBadge({ platform: "win32", app: fakeApp(), windows, nativeImage, total: 5 });
    applyBadge({ platform: "win32", app: fakeApp(), windows, nativeImage, total: 0 });
    for (const win of windows) {
      assert.deepEqual(win.overlays.at(-1), { image: null, description: "" });
    }
  });

  it("skips destroyed windows", () => {
    const gone = { ...fakeWindow(), isDestroyed: () => true };
    applyBadge({ platform: "win32", app: fakeApp(), windows: [gone], nativeImage, total: 2 });
    assert.deepEqual(gone.overlays, []);
  });
});

describe("applyBadge on macOS and Linux", () => {
  for (const platform of ["darwin", "linux"]) {
    it(`${platform}: sets the app badge count and never touches window overlays`, () => {
      const windows = [fakeWindow(), fakeWindow()];
      const app = fakeApp();
      assert.equal(applyBadge({ platform, app, windows, nativeImage, total: 5 }), true);
      applyBadge({ platform, app, windows, nativeImage, total: 0 });
      assert.deepEqual(app.badgeCounts, [5, 0]);
      for (const win of windows) assert.deepEqual(win.overlays, []);
    });
  }
});
