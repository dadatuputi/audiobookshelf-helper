import { defineConfig } from "@playwright/test";

/* Chromium can load an unpacked extension via --load-extension, so the content
 * script is exercised for real. Firefox cannot do that through Playwright, so
 * the Firefox project verifies the same DOM contract by injecting the built
 * content script into the page instead. */
export default defineConfig({
  testDir: "tests/e2e",
  timeout: 60_000,
  reporter: [["list"]],
  projects: [
    { name: "chromium", use: { browserName: "chromium" } },
    { name: "firefox",  use: { browserName: "firefox"  } }
  ]
});
