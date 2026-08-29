import { defineConfig } from "@playwright/test";

/* Chromium can load an unpacked extension via --load-extension, so the popup,
 * options page, background handlers and native host are all exercised for real
 * (extension.spec.js). Firefox cannot register a native host under Playwright,
 * so its project verifies that the built add-on installs and that the DOM
 * contract the content script relies on still holds.
 *
 * ABSH_CHROMIUM_PATH / ABSH_FIREFOX_PATH point at a prebuilt browser when the
 * environment ships one instead of the revision Playwright downloads. */
const launchOptions = (envVar) =>
  process.env[envVar] ? { launchOptions: { executablePath: process.env[envVar] } } : {};

export default defineConfig({
  testDir: "tests/e2e",
  timeout: 60_000,
  reporter: [["list"]],
  projects: [
    { name: "chromium", use: { browserName: "chromium", ...launchOptions("ABSH_CHROMIUM_PATH") } },
    { name: "firefox", use: { browserName: "firefox", ...launchOptions("ABSH_FIREFOX_PATH") } },
    /* Against a real Audiobookshelf, built and run by tests/real/setup.mjs.
     * Skips itself when that has not been run, so the default suite needs no
     * build. Longer timeout: the real app is a Nuxt SPA, not a stub. */
    {
      name: "real",
      testDir: "tests/real",
      timeout: 120_000,
      use: { browserName: "chromium", ...launchOptions("ABSH_CHROMIUM_PATH") }
    }
  ]
});
