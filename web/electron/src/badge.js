// App-wide dock/taskbar badge: aggregate per-window unread counts and hand
// the total to the platform. Pure Node so it is testable with stubs.

"use strict";

/**
 * Sum unread counts across distinct pinned origins. Windows on the same
 * origin report the same server-wide number (modulo timing), so take the
 * max per origin instead of adding duplicates; unpinned windows (null
 * origin, the setup page) contribute nothing.
 *
 * @param {Iterable<{origin: string | null, badgeCount: number}>} entries
 * @returns {number}
 */
function aggregateBadgeCount(entries) {
  /** @type {Map<string, number>} */
  const perOrigin = new Map();
  for (const { origin, badgeCount } of entries) {
    if (!origin) continue;
    perOrigin.set(origin, Math.max(perOrigin.get(origin) ?? 0, badgeCount));
  }
  let total = 0;
  for (const count of perOrigin.values()) total += count;
  return total;
}

/** 3x5 pixel glyphs, one row per entry, bit 2 = leftmost pixel. */
const GLYPHS = {
  0: [7, 5, 5, 5, 7],
  1: [2, 6, 2, 2, 7],
  2: [7, 1, 7, 4, 7],
  3: [7, 1, 7, 1, 7],
  4: [5, 5, 7, 1, 1],
  5: [7, 4, 7, 1, 7],
  6: [7, 4, 7, 5, 7],
  7: [7, 1, 1, 1, 1],
  8: [7, 5, 7, 5, 7],
  9: [7, 5, 7, 1, 7],
  "+": [0, 2, 7, 2, 0],
};

const SIZE = 16;

/**
 * Raw 16x16 BGRA bitmap: a red disc with `label` ("1".."9" or "9+")
 * centred in white. Windows draws overlays at 16px logical and scales them
 * with the display DPI, so 1px strokes stay legible at 200%.
 *
 * ponytail: no 2x representation — Electron's Windows path
 * (TaskbarHost::SetOverlayIcon) resamples the 1x bitmap onto a 16x16
 * canvas and ignores other scale factors; add addRepresentation({
 * scaleFactor: 2 }) if a future Electron honours them.
 *
 * @param {string} label
 * @returns {Buffer}
 */
function drawBadge(label) {
  const px = Buffer.alloc(SIZE * SIZE * 4);
  const put = (x, y, b, g, r) => px.set([b, g, r, 0xff], (y * SIZE + x) * 4);
  const c = SIZE / 2;
  for (let y = 0; y < SIZE; y++) {
    for (let x = 0; x < SIZE; x++) {
      if ((x + 0.5 - c) ** 2 + (y + 0.5 - c) ** 2 <= c * c) put(x, y, 0x25, 0x30, 0xd9);
    }
  }
  const x0 = Math.floor((SIZE - (label.length * 4 - 1)) / 2);
  const y0 = Math.floor((SIZE - 5) / 2);
  [...label].forEach((ch, i) => {
    GLYPHS[ch].forEach((row, gy) => {
      for (let gx = 0; gx < 3; gx++) {
        if (row & (4 >> gx)) put(x0 + i * 4 + gx, y0 + gy, 0xff, 0xff, 0xff);
      }
    });
  });
  return px;
}

/**
 * Show `total` on the OS badge; 0 clears it. macOS/Linux use the app-level
 * dock badge. Windows has no app-level badge, so every window gets a
 * taskbar overlay icon plus a description for screen readers.
 *
 * @param {object} opts
 * @param {string} opts.platform `process.platform`
 * @param {{setBadgeCount(n: number): boolean}} opts.app
 * @param {Iterable<{isDestroyed(): boolean, setOverlayIcon(image: unknown, description: string): void}>} opts.windows
 * @param {{createFromBitmap(buffer: Buffer, size: {width: number, height: number}): unknown}} opts.nativeImage
 * @param {number} opts.total
 * @returns {boolean} whether the platform accepted the count
 */
function applyBadge({ platform, app, windows, nativeImage, total }) {
  if (platform !== "win32") return app.setBadgeCount(total);
  const image =
    total > 0
      ? nativeImage.createFromBitmap(drawBadge(total > 9 ? "9+" : String(total)), {
          width: SIZE,
          height: SIZE,
        })
      : null;
  const description = total > 0 ? `${total} pending item${total === 1 ? "" : "s"}` : "";
  for (const win of windows) {
    if (!win.isDestroyed()) win.setOverlayIcon(image, description);
  }
  return true;
}

module.exports = { aggregateBadgeCount, applyBadge };
