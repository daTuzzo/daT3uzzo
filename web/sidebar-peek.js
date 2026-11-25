// @mod-label Sidebar peek
// @mod-desc While the main sidebar is closed, pointing at its button slides it in over the page. It slides away when the pointer leaves it, or after you open a thread from it.
/*
 * T3 Code sidebar peek add-on.
 * T3 Code keeps a closed sidebar mounted, parked off-screen and inert (apps/web/src/components/ui/sidebar.tsx).
 * A peek moves that same element back on-screen with one attribute and lifts `inert`. T3 Code's own
 * state stays "collapsed": the page under the sidebar does not move, nothing is saved, and the button
 * and its shortcut still open the sidebar for good.
 * Every hook fails quietly: if a marker is missing, T3 Code works as before.
 *
 * Performance: one `pointerover` listener classifies the element under the pointer. The slide is one
 * short `left` transition on a fixed element, so the page under it is not laid out again. It lasts
 * T3 Code's panel animation time when that setting is on, else 150 ms, and it is off with reduced motion.
 */
(() => {
  "use strict";
  if (window.__T3SP_LOADED__) return;
  window.__T3SP_LOADED__ = true;

  const ATTR = "data-t3sp-peek";
  // The desktop layout only: under 768px the sidebar is a sheet without these attributes.
  const SIDEBAR = '[data-slot="sidebar"][data-side="left"][data-collapsible-mode="offcanvas"]';
  const CONTAINER = '[data-slot="sidebar-container"][data-app-sidebar]';
  const TRIGGER = '[data-sidebar-control] [data-sidebar="trigger"]';
  const EDITABLE = 'input,textarea,select,[contenteditable]:not([contenteditable="false"])';
  // A popover's button in the sidebar, or a menu anywhere outside the app's wrapper (a row's context
  // menu has no button). Not `data-popup-open`: a tooltip sets that on a focused button too.
  const OPEN_POPOVER = '[aria-haspopup][aria-expanded="true"]';
  const OPEN_MENU = '[role="menu"]:not([data-slot="sidebar-wrapper"] [role="menu"])';
  const OPEN_MS = 100; // a pointer that only crosses the button opens nothing
  const CLOSE_MS = 300; // time to cross the gap to a menu, or to come back

  // The attribute is "open" during a peek and "closing" while the sidebar slides away. Both keep it
  // above the page (z-index 45: the sidebar's own is 10, the button's is 50). Every rule needs
  // T3 Code's "collapsed" state, so a sidebar that T3 Code opened is never styled from here.
  const PEEKING = `${SIDEBAR}[data-state="collapsed"][${ATTR}] > ${CONTAINER}`;
  const style = document.createElement("style");
  style.id = "t3sp-style";
  style.textContent = `
${PEEKING} {
  --t3sp-ms: 150ms;
  z-index: 45;
  transition: left var(--t3sp-ms) ease-out, box-shadow var(--t3sp-ms) ease-out;
}
[data-panel-animations="true"] ${PEEKING} { --t3sp-ms: var(--panel-animation-duration, 150ms); }
@media (prefers-reduced-motion: reduce) { ${PEEKING}, [data-panel-animations] ${PEEKING} { --t3sp-ms: 0ms; } }
${SIDEBAR}[data-state="collapsed"][${ATTR}="open"] > ${CONTAINER} {
  left: 0;
  box-shadow: 8px 0 24px -6px rgb(0 0 0 / 0.22);
}`;
  document.head.appendChild(style);

  let peek = null; // { sidebar, path, observer } while the sidebar is peeking
  let openTimer = 0;
  let closeTimer = 0;
  let settleTimer = 0; // removes "closing" once the sidebar has slid away

  const collapsedSidebar = () => {
    const container = document.querySelector(`${SIDEBAR}[data-state="collapsed"] > ${CONTAINER}`);
    return container ? container.parentElement : null;
  };

  // A rename field with the caret in it, or a menu opened from a row, keeps the sidebar there.
  const held = () => {
    const focus = document.activeElement;
    if (focus && peek.sidebar.contains(focus) && focus.matches(EDITABLE)) return true;
    return peek.sidebar.querySelector(OPEN_POPOVER) !== null || document.querySelector(OPEN_MENU) !== null;
  };

  const end = () => {
    if (!peek) return;
    const { sidebar, observer } = peek;
    peek = null;
    clearTimeout(closeTimer);
    closeTimer = 0;
    observer.disconnect();
    document.removeEventListener("keydown", onKeyDown, true);
    document.removeEventListener("click", onClick, true);
    window.navigation?.removeEventListener?.("currententrychange", onNavigate);
    if (sidebar.getAttribute("data-state") !== "collapsed") return sidebar.removeAttribute(ATTR);
    // T3 Code sets `inert` only when its state changes, so a sidebar that is still closed gets it back here.
    sidebar.inert = true;
    const container = sidebar.querySelector(`:scope > ${CONTAINER}`);
    const slideMs = container ? parseFloat(getComputedStyle(container).getPropertyValue("--t3sp-ms")) : 0;
    if (!(slideMs > 0)) return sidebar.removeAttribute(ATTR);
    sidebar.setAttribute(ATTR, "closing");
    settleTimer = setTimeout(() => sidebar.removeAttribute(ATTR), slideMs + 50);
  };

  const begin = () => {
    openTimer = 0;
    const sidebar = collapsedSidebar();
    if (peek || !sidebar) return;
    // The button, its shortcut or a drag opened the sidebar for good: T3 Code owns it again.
    const observer = new MutationObserver(() => {
      if (sidebar.getAttribute("data-state") !== "collapsed") end();
    });
    observer.observe(sidebar, { attributes: true, attributeFilter: ["data-state"] });
    peek = { sidebar, path: location.pathname, observer };
    clearTimeout(settleTimer);
    sidebar.inert = false;
    sidebar.setAttribute(ATTR, "open");
    document.addEventListener("keydown", onKeyDown, true);
    document.addEventListener("click", onClick, true);
    window.navigation?.addEventListener?.("currententrychange", onNavigate);
  };

  const tryClose = () => {
    closeTimer = 0;
    if (!peek) return;
    if (peek.sidebar.isConnected && held()) closeTimer = setTimeout(tryClose, CLOSE_MS);
    else end();
  };

  // Opening a thread, or anything else that changes the page, is the end of the visit.
  function onNavigate() {
    if (peek && location.pathname !== peek.path) end();
  }

  // Browsers without the Navigation API: look at the address after a click has been handled.
  function onClick() {
    setTimeout(onNavigate, 0);
    setTimeout(onNavigate, 400);
  }

  function onKeyDown(event) {
    if (event.key === "Escape" && peek && !held()) end();
  }

  // Menus, tooltips and dialogs render outside the app's wrapper: they never close a peek.
  const zoneOf = (target) => {
    if (!(target instanceof Element)) return "page";
    if (target.closest(TRIGGER)) return "trigger";
    if (peek && peek.sidebar.contains(target)) return "sidebar";
    return target.closest('[data-slot="sidebar-wrapper"]') ? "page" : "popup";
  };

  document.addEventListener("pointerover", (event) => {
    if (event.pointerType !== "mouse") return;
    const zone = zoneOf(event.target);
    if (!peek) {
      if (zone !== "trigger") {
        clearTimeout(openTimer);
        openTimer = 0;
      } else if (!openTimer && collapsedSidebar()) {
        openTimer = setTimeout(begin, OPEN_MS);
      }
      return;
    }
    if (!peek.sidebar.isConnected) return end();
    // A pressed button is a drag (a thread being moved, the width rail): wait for it to end.
    if (zone === "page" && event.buttons === 0) {
      if (!closeTimer) closeTimer = setTimeout(tryClose, CLOSE_MS);
    } else {
      clearTimeout(closeTimer);
      closeTimer = 0;
    }
  }, { passive: true });
})();
