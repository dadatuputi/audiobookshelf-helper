/**
 * Assertions shared by the Chromium and Firefox real-server suites.
 *
 * These are the ones that actually demonstrate the extension works: a badge
 * existing proves the content script ran, and nothing more. Clicking it drives
 * the whole stack - page, content script, background, native host, the absh
 * engine, the Audiobookshelf API - and then checks the file that landed on
 * disk. That is the claim worth making.
 */
import { readdirSync, rmSync, mkdirSync, existsSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");

/** The first copy asks whether to write Audiobookshelf's tags into it.
 *  Answered here, and remembered, so no later copy in the run asks. */
export async function answerTags(page, expect, write) {
  const ask = page.locator("#absh-ask");
  await expect(ask).toContainText("Embed Metadata", { timeout: 30_000 });
  await ask.locator("input[type=checkbox]").check();
  await ask.locator("button", { hasText: write ? "Embed tags" : "Copy as is" }).click();
  await expect(ask).toHaveCount(0);
}

/** The tags in a file on the device, as the helper reads them
 *  ({title, author, album, ...}). */
export function tagsOf(path) {
  const py = "import json,sys; sys.path.insert(0, sys.argv[1]); from absh import tags; "
    + "print(json.dumps(tags.read(sys.argv[2])))";
  return JSON.parse(execFileSync("python3", ["-c", py, ROOT, path], { encoding: "utf8" }));
}

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

/* ------------------------------------------------- whole-group copies */
/* The same bodies in Chromium and Firefox: the page code is one file, and
 * the bug that reaches only one browser is the one worth a second run. */

/** Audiobookshelf's API as the tests use it, with the test's own key. */
export function absApi(state) {
  return async (path, method = "GET", body) => {
    const r = await fetch(`${state.absUrl}${path}`, { method,
      headers: { Authorization: `Bearer ${state.token}`, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body) });
    if (!r.ok) throw new Error(`${method} ${path}: HTTP ${r.status}`);
    return r.status === 200 ? r.json().catch(() => ({})) : {};
  };
}

/** Navigate inside the app, as people do: no page load. */
export const inApp = (page, path) => page.evaluate((p) => window.$nuxt.$router.push(p), path);

/** Clear the player, folder by folder. */
export function emptyDevice(state) {
  for (const f of deviceFiles(state.device)) {
    rmSync(dirname(join(state.device, "AUDIOBOOKS", f)), { recursive: true, force: true });
  }
}

/** One button on a series, an author, a playlist and a collection page,
 *  each copying what it lists - the series of eleven asking once more first.
 *  Leaves the server as it found it and the player empty. */
export async function oneClickGroups(page, state, expect) {
  expect(deviceFiles(state.device), "device should start empty").toEqual([]);
  const api = absApi(state);
  const lib = state.libraryId;
  const items = (await api(`/api/libraries/${lib}/items?limit=0&minified=1`)).results;
  const byTitle = (t) => items.find((i) => i.media.metadata.title === t);
  const [first, second] = state.titles.map(byTitle);        // Holes, Redwall
  const series = (await api(`/api/libraries/${lib}/series?limit=50`)).results
    .find((x) => x.name === state.series.name);
  const author = (await api(`/api/libraries/${lib}/authors`)).authors
    .find((a) => a.name === first.media.metadata.authorName);
  const zebra = byTitle(state.series.titles[0]);
  const playlist = await api("/api/playlists", "POST", { libraryId: lib, name: "One-Click Playlist",
    items: [{ libraryItemId: second.id }, { libraryItemId: zebra.id }] });
  const col = await api("/api/collections", "POST",
                        { libraryId: lib, name: "One-Click Collection", books: [first.id, second.id] });
  const player = state.device.split(/[\\/]/).pop();
  const box = page.locator("#absh-group");
  const n = state.series.count;

  try {
    // A series of eleven: the button sits in the shelf's own toolbar, and
    // asks once more before copying that many. No leaves it alone.
    await inApp(page, `/library/${lib}/series/${series.id}`);
    const s = box.locator("button", { hasText: "Copy series to" });
    await expect(s).toHaveText(new RegExp(`^Copy series to ${player} · ${n} books, `),
                               { timeout: 30_000 });
    expect(await box.evaluate((el) => el.closest("#toolbar") !== null)).toBe(true);
    await s.click();
    await expect(box.locator(".absh-item-line"))
      .toHaveText(new RegExp(`^Copy ${n} books, \\S+ to ${player}\\?$`));
    await box.locator("button", { hasText: "No" }).click();
    await expect(s).toBeVisible();
    expect(deviceFiles(state.device), "No copied anyway").toEqual([]);
    await s.click();
    await box.locator("button", { hasText: `Yes, copy ${n} books` }).click();
    await expect(box).toContainText(`✓ All ${n} on ${player}`, { timeout: 90_000 });
    const files = deviceFiles(state.device);
    expect(files).toHaveLength(n);
    for (const t of state.series.titles) expect(files.join(" ")).toContain(t);

    // An author: beside the name.
    await inApp(page, `/author/${author.id}`);
    const a = box.locator("button", { hasText: "Copy author's books to" });
    await expect(a).toHaveText(new RegExp(`^Copy author's books to ${player} · 1 book, `),
                               { timeout: 30_000 });
    expect(await box.evaluate(
      (el) => !!el.parentElement.querySelector(":scope > h1"))).toBe(true);
    await a.click();
    await expect(box).toContainText(`✓ All 1 on ${player}`, { timeout: 45_000 });
    expect(deviceFiles(state.device).join(" ")).toContain(first.media.metadata.title);

    // A playlist, one of its two already there: only the other is offered.
    await inApp(page, `/playlist/${playlist.id}`);
    const p = box.locator("button", { hasText: "Copy playlist to" });
    await expect(p).toHaveText(new RegExp(`^Copy playlist to ${player} · 1 left, `),
                               { timeout: 30_000 });
    await expect(box).toContainText(`1 already on ${player}`);
    await p.click();
    await expect(box).toContainText(`✓ All 2 on ${player}`, { timeout: 45_000 });
    expect(deviceFiles(state.device).join(" ")).toContain(second.media.metadata.title);

    // A collection of two, both on it by now: nothing to offer, and it says so.
    await inApp(page, `/collection/${col.id}`);
    await expect(box).toContainText(`✓ All 2 on ${player}`, { timeout: 30_000 });
    await expect(box.locator("button")).toHaveCount(0);
    expect(deviceFiles(state.device)).toHaveLength(n + 2);
  } finally {
    await api(`/api/collections/${col.id}`, "DELETE").catch(() => {});
    await api(`/api/playlists/${playlist.id}`, "DELETE").catch(() => {});
    emptyDevice(state);
  }
}

/** The player pulled out while a run is on: the page says it waits, nothing
 *  is made again on this disk, and plugging it back in finishes the run.
 *  Starts and ends with an empty player. */
export async function unplugMidRun(page, state, expect) {
  expect(deviceFiles(state.device), "device should start empty").toEqual([]);
  const api = absApi(state);
  const lib = state.libraryId;
  const series = (await api(`/api/libraries/${lib}/series?limit=50`)).results
    .find((x) => x.name === state.series.name);
  const player = state.device.split(/[\\/]/).pop();
  const box = page.locator("#absh-group");
  const n = state.series.count;
  try {
    await inApp(page, `/library/${lib}/series/${series.id}`);
    const s = box.locator("button", { hasText: "Copy series to" });
    await expect(s).toBeVisible({ timeout: 30_000 });
    await s.click();
    await box.locator("button", { hasText: `Yes, copy ${n} books` }).click();
    // Out it comes, as the run starts.
    rmSync(state.device, { recursive: true, force: true });

    await expect(box).toContainText(`Paused · plug ${player} back in`, { timeout: 45_000 });
    expect(existsSync(state.device), "the player's folders were made again on this disk")
      .toBe(false);

    // Back in. The background looks for it every few seconds.
    mkdirSync(join(state.device, "AUDIOBOOKS"), { recursive: true });
    await expect(box).toContainText(`✓ All ${n} on ${player}`, { timeout: 90_000 });
    expect(deviceFiles(state.device)).toHaveLength(n);
  } finally {
    mkdirSync(join(state.device, "AUDIOBOOKS"), { recursive: true });
    emptyDevice(state);
  }
}
