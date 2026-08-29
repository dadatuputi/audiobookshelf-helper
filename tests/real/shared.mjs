/**
 * Assertions shared by the Chromium and Firefox real-server suites.
 *
 * These are the ones that actually demonstrate the extension works: a badge
 * existing proves the content script ran, and nothing more. Clicking it drives
 * the whole stack - page, content script, background, native host, the absh
 * engine, the Audiobookshelf API - and then checks the file that landed on
 * disk. That is the claim worth making.
 */
import { readdirSync, existsSync, statSync } from "node:fs";
import { join } from "node:path";

/** Every file the device holds, relative to the books folder. */
export function deviceFiles(device, subdir = "AUDIOBOOKS") {
  const root = join(device, subdir);
  if (!existsSync(root)) return [];
  const out = [];
  const walk = (dir, prefix) => {
    for (const name of readdirSync(dir)) {
      const full = join(dir, name);
      if (statSync(full).isDirectory()) walk(full, `${prefix}${name}/`);
      else out.push(`${prefix}${name}`);
    }
  };
  walk(root, "");
  return out.sort();
}

/** The card showing a given book, found by the title in its cover alt text. */
export function cardFor(page, title) {
  return page.locator('[id^="book-card-"]')
    .filter({ has: page.locator(`img[alt^="${title},"]`) })
    .first();
}

/** Poll until the predicate holds, so a slow copy does not fail the run. */
export async function until(fn, { timeout = 45_000, every = 500 } = {}) {
  const deadline = Date.now() + timeout;
  let last;
  for (;;) {
    last = await fn();
    if (last) return last;
    if (Date.now() > deadline) return last;
    await new Promise((r) => setTimeout(r, every));
  }
}
