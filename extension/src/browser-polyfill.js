/* Minimal cross-browser shim.
 *
 * Firefox exposes `browser.*` with promises. Chrome exposes `chrome.*`; since
 * MV3 its APIs are promise-based too, so aliasing is enough for what this
 * extension uses (storage, runtime, action). Deliberately tiny - pulling in
 * the full webextension-polyfill would be more than this needs. */
(function (g) {
  if (typeof g.browser === "undefined" && typeof g.chrome !== "undefined") {
    g.browser = g.chrome;
  }
})(typeof globalThis !== "undefined" ? globalThis : self);
