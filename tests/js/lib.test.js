import { describe, it, expect, beforeAll } from "vitest";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
let ABSH;

beforeAll(() => {
  // lib.js is a classic script; run it in a sandbox and grab the export.
  const code = readFileSync(resolve(here, "../../extension/src/lib.js"), "utf8");
  // A bare vm context has the JS builtins but no web APIs; originPattern
  // parses with URL, so hand it in.
  const sandbox = { module: { exports: {} }, globalThis: {}, URL };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox);
  ABSH = sandbox.module.exports;
});

describe("baseUrl", () => {
  it("strips trailing slashes", () => {
    expect(ABSH.baseUrl("http://x:13378/")).toBe("http://x:13378");
    expect(ABSH.baseUrl("http://x:13378///")).toBe("http://x:13378");
  });
  it("leaves a clean url alone", () => {
    expect(ABSH.baseUrl("http://x:13378")).toBe("http://x:13378");
  });
  it("tolerates empty input", () => {
    expect(ABSH.baseUrl(undefined)).toBe("");
  });
});

describe("originPattern", () => {
  it("narrows to the configured origin, not every site", () => {
    expect(ABSH.originPattern("http://media.local:13378")).toBe("http://media.local:13378/*");
  });
  it("keeps https and the port", () => {
    expect(ABSH.originPattern("https://abs.example.com:8443/")).toBe("https://abs.example.com:8443/*");
  });
  it("drops any path", () => {
    expect(ABSH.originPattern("http://x:1/library/main")).toBe("http://x:1/*");
  });
  it("rejects a non-http scheme", () => {
    expect(() => ABSH.originPattern("file:///etc")).toThrow(/http/);
  });
  it("rejects junk", () => {
    expect(() => ABSH.originPattern("not a url")).toThrow();
  });
});

describe("libraryPattern", () => {
  // Audiobookshelf is commonly reached through a reverse proxy that puts it
  // under a path. The browser URL then carries that prefix whether or not the
  // server itself is configured with a base path - and dropping it here
  // registered the content script for a URL that never matches, so the whole
  // in-page UI was silently absent with no error anywhere.
  it("keeps the path the server is reached under", () => {
    expect(ABSH.libraryPattern("https://h/audiobookshelf"))
      .toBe("https://h/audiobookshelf/library/*");
  });

  it("keeps it with a trailing slash too", () => {
    expect(ABSH.libraryPattern("https://h/audiobookshelf/"))
      .toBe("https://h/audiobookshelf/library/*");
  });

  it("handles more than one path segment", () => {
    expect(ABSH.libraryPattern("https://h/media/abs")).toBe("https://h/media/abs/library/*");
  });

  it("adds nothing when the server is at the root", () => {
    expect(ABSH.libraryPattern("http://h:13378")).toBe("http://h:13378/library/*");
  });


  it("scopes the content script to that server's library pages", () => {
    expect(ABSH.libraryPattern("http://media.local:13378/")).toBe("http://media.local:13378/library/*");
  });
});

describe("formatBytes", () => {
  it("keeps small numbers whole", () => expect(ABSH.formatBytes(512)).toBe("512B"));
  it("uses one decimal below ten units", () => expect(ABSH.formatBytes(1536)).toBe("1.5KB"));
  it("rounds larger values", () => expect(ABSH.formatBytes(120 * 1048576)).toBe("120MB"));
  it("handles zero and junk", () => {
    expect(ABSH.formatBytes(0)).toBe("0B");
    expect(ABSH.formatBytes(undefined)).toBe("0B");
  });
});

describe("compareVersions", () => {
  // The order semver gives, oldest first. Every pair is checked both ways, so
  // this is the whole ordering rather than a few spot checks.
  const ORDER = ["0.9.0", "1.0.0-alpha.1", "1.0.0-alpha.3", "1.0.0-alpha.10",
                 "1.0.0-alpha.beta", "1.0.0-beta.1", "1.0.0-rc.1", "1.0.0",
                 "1.0.1", "1.10.0", "2.0.0"];

  it("orders prereleases the way semver does, not the way strings sort", () => {
    ORDER.forEach((a, i) => ORDER.forEach((b, j) => {
      expect(ABSH.compareVersions(a, b), `${a} vs ${b}`).toBe(Math.sign(i - j));
    }));
  });

  it("reads a tag's leading v as the tag, not the version", () => {
    expect(ABSH.compareVersions("v1.0.0-alpha.10", "1.0.0-alpha.9")).toBe(1);
    expect(ABSH.compareVersions("v1.0.0", "1.0.0")).toBe(0);
  });

  it("says nothing about dev or unknown - neither is behind a release", () => {
    for (const odd of ["dev", "unknown", "", null, undefined, "1.0", "1.0.0.1"]) {
      expect(ABSH.compareVersions("9.9.9", odd)).toBeNull();
      expect(ABSH.compareVersions(odd, "1.0.0")).toBeNull();
    }
  });
});

describe("updateCheckDue", () => {
  // Written out rather than read from ABSH, which loads in beforeAll - after
  // this body runs - and because the numbers are the decision under test.
  const DAY = 24 * 60 * 60 * 1000;
  const HOUR = 60 * 60 * 1000;
  const now = 1_800_000_000_000;
  const ok = (age, over = {}) => ({ checkedAt: now - age, installed: "1.0.0-alpha.1",
                                    latest: "v1.0.0-alpha.2", error: "", ...over });

  it("checks when there is no answer yet", () => {
    expect(ABSH.updateCheckDue(null, "1.0.0-alpha.1", now)).toBe(true);
    expect(ABSH.updateCheckDue({}, "1.0.0-alpha.1", now)).toBe(true);
  });

  it("asks GitHub at most about once a day on its own", () => {
    expect(ABSH.UPDATE_CHECK_EVERY).toBe(DAY);
    expect(ABSH.UPDATE_RETRY_AFTER).toBe(HOUR);
    expect(ABSH.updateCheckDue(ok(DAY - 1), "1.0.0-alpha.1", now)).toBe(false);
    expect(ABSH.updateCheckDue(ok(DAY), "1.0.0-alpha.1", now)).toBe(true);
  });

  it("always checks when the user asks", () => {
    expect(ABSH.updateCheckDue(ok(1000), "1.0.0-alpha.1", now, true)).toBe(true);
  });

  it("tries a failed check again within the hour, not the day", () => {
    const failed = ok(HOUR - 1, { error: "could not reach the release feed" });
    expect(ABSH.updateCheckDue(failed, "1.0.0-alpha.1", now)).toBe(false);
    expect(ABSH.updateCheckDue({ ...failed, checkedAt: now - HOUR },
                               "1.0.0-alpha.1", now)).toBe(true);
  });

  it("checks again once the helper is a different one", () => {
    // Updated from here or from the command line: the answer was about the
    // copy that is no longer there.
    expect(ABSH.updateCheckDue(ok(1000), "1.0.0-alpha.2", now)).toBe(true);
  });

  it("does not trust an answer from the future", () => {
    expect(ABSH.updateCheckDue(ok(-DAY), "1.0.0-alpha.1", now)).toBe(true);
  });
});

describe("updateState", () => {
  const helper = (over = {}) => ({ ok: true, release: "1.0.0-alpha.1",
                                   updateRefused: null, ...over });
  const check = (over = {}) => ({ checkedAt: 1, installed: "1.0.0-alpha.1",
                                  latest: "v1.0.0-alpha.2", prerelease: true,
                                  error: "", ...over });

  it("offers a newer release to a copy that can take it", () => {
    const s = ABSH.updateState(helper(), check());
    expect(s.kind).toBe("available");
    expect(s.canUpdate).toBe(true);
    expect(s.latest).toBe("v1.0.0-alpha.2");
  });

  it("names the release but offers no button when the copy cannot update", () => {
    const s = ABSH.updateState(helper({ updateRefused: "/opt/absh is not writable by you" }),
                               check());
    expect(s.kind).toBe("available");
    expect(s.canUpdate).toBe(false);
    expect(s.refused).toMatch(/not writable/);
  });

  it("offers no button for a release this copy would refuse", () => {
    // Unsigned, or not signed by a key this copy pins: the updater would say
    // no, so the page says why instead of offering the click.
    const s = ABSH.updateState(helper(), check({
      releaseRefused: "release v1.0.0-alpha.2 is not signed" }));
    expect(s.kind).toBe("available");
    expect(s.canUpdate).toBe(false);
    expect(s.releaseRefused).toMatch(/not signed/);
  });

  it("a copy that pins no signing key cannot update, whatever is out", () => {
    const why = "this copy pins no release-signing key, so it cannot tell a genuine " +
                "release from a forged one and will not install any.";
    const s = ABSH.updateState(helper({ updateRefused: why }), check());
    expect(s.canUpdate).toBe(false);
    expect(s.refused).toBe(why);
  });

  it("works out 'newer' from the live helper, not the cached answer", () => {
    // Just updated: the cache still names alpha.2 as latest, and that is now
    // what is installed.
    expect(ABSH.updateState(helper({ release: "1.0.0-alpha.2" }), check()).kind)
      .toBe("current");
  });

  it("a checkout is neither behind nor current", () => {
    const s = ABSH.updateState(helper({ release: "dev", updateRefused: "a git checkout" }),
                               check());
    expect(s.kind).toBe("unversioned");
    expect(s.refused).toBe("a git checkout");
  });

  it("a failed check keeps whatever it knew before", () => {
    expect(ABSH.updateState(helper(), check({ error: "offline" })).kind).toBe("available");
    expect(ABSH.updateState(helper(), check({ latest: "", error: "offline" })).kind)
      .toBe("failed");
  });

  it("a helper older than in-browser updates says so", () => {
    expect(ABSH.updateState(helper(), { unsupported: true }).kind).toBe("unsupported");
  });

  it("an unreachable helper is its own state", () => {
    expect(ABSH.updateState({ ok: false, error: "not found" }, null))
      .toEqual({ kind: "unreachable", error: "not found" });
  });
});
