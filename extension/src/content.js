/* Injects a "Sync to device" button into the Audiobookshelf library toolbar.
 *
 * Deliberately does NOT scrape ABS's own multi-select state. Book cards are
 * identified in the DOM only as #book-card-{index} - the library item id is
 * never rendered, and selection lives in Vue component state. Reading it would
 * break on any ABS update. The button opens the extension's own picker instead,
 * which asks the API directly and is stable across versions.
 */
(function () {
  const BTN_ID = "absh-sync-btn";

  function makeButton() {
    const b = document.createElement("button");
    b.id = BTN_ID;
    b.type = "button";
    b.className = "absh-btn";
    b.title = "Audiobookshelf Helper - pick books and sync to your player";
    b.innerHTML = '<span class="absh-ico">⤓</span><span>Sync to device</span>';
    b.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      browser.runtime.sendMessage({ type: "openPicker" }).catch(() => {});
      // openPopup() is not always permitted; show a hint as a fallback.
      b.classList.add("absh-hint");
      setTimeout(() => b.classList.remove("absh-hint"), 2500);
    });
    return b;
  }

  function inject() {
    if (document.getElementById(BTN_ID)) return;
    const bar = document.getElementById("toolbar");
    if (!bar) return;
    bar.appendChild(makeButton());
  }

  // The toolbar is re-rendered by Vue on navigation, so keep watching.
  const mo = new MutationObserver(() => inject());
  mo.observe(document.documentElement, { childList: true, subtree: true });
  inject();
})();
