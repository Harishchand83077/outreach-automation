/**
 * The service worker, which does one thing: open the side panel when the button is clicked.
 *
 * It holds no state, no token and no listener on any page. `activeTab` is granted by the click
 * itself, so the panel -- not this file -- is what asks to read the page, and only after the
 * person presses Suggest a second time inside it.
 */

chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {
  // Older Chrome without `setPanelBehavior`: the click still opens the panel declared in the
  // manifest. Swallowed rather than logged, because a service worker that throws on install
  // is restarted repeatedly by Chrome for a cosmetic failure.
});
