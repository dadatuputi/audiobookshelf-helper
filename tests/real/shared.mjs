/**
 * Assertions shared by the Chromium and Firefox real-server suites.
 *
 * These are the ones that actually demonstrate the extension works: a badge
 * existing proves the content script ran, and nothing more. Clicking it drives
 * the whole stack - page, content script, background, native host, the absh
 * engine, the Audiobookshelf API - and then checks the file that landed on
 * disk. That is the claim worth making.
 */
import { readdirSync } from "node:fs";
import { join } from "node:path";

/** Every file the device holds, relative to the books folder.
 *
 *  Called from a polling loop while the native host is adding and deleting
 *  under the same tree, so entries can disappear between listing a directory
 *  and reading it. A directory that is gone by then simply holds nothing:
 *  throwing ENOENT out of the poll instead failed a run outright, in the one
 *  test whose whole point is watching a folder be removed. withFileTypes
 *  closes the other half of the same race - a separate statSync on a name
 *  that has since been unlinked.
 */
export function deviceFiles(device, subdir = "AUDIOBOOKS") {
  const out = [];
  const walk = (dir, prefix) => {
    let entries;
    try {
      entries = readdirSync(dir, { withFileTypes: true });
    } catch (e) {
      if (e.code === "ENOENT" || e.code === "ENOTDIR") return;
      throw e;
    }
    for (const ent of entries) {
      if (ent.isDirectory()) walk(join(dir, ent.name), `${prefix}${ent.name}/`);
      else out.push(`${prefix}${ent.name}`);
    }
  };
  walk(join(device, subdir), "");
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

/** Where a cover's strip sits: on the cover, and clear of the title under it,
 *  which is where it first landed (attached to the card, not the cover). */
export function stripPlacement(card) {
  return card.evaluate((c) => {
    const s = c.querySelector(".absh-badge").getBoundingClientRect();
    const cover = c.querySelector('[id^="cover-area-"]').getBoundingClientRect();
    const title = c.querySelector('[cy-id="title"]')?.getBoundingClientRect();
    return { inCover: s.top >= cover.top && s.bottom <= cover.bottom,
             clearOfTitle: !title || s.bottom <= title.top };
  });
}

/** Whether a book page's buttons sit under the Play people can see. The app
 *  keeps a second, hidden Play in its select-many bar, and following that
 *  one put them over the side rail. */
export function underPlay(page) {
  return page.evaluate(() => {
    const box = document.getElementById("absh-item").getBoundingClientRect();
    const play = [...document.querySelectorAll("button.abs-btn.bg-success")]
      .find((b) => b.getClientRects().length && !b.closest(".modal, .h-16"))
      .getBoundingClientRect();
    return box.top >= play.bottom && Math.abs(box.left - play.left) < 2;
  });
}
