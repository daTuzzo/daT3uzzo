/*
 * daT3uzzo: the switches for the T3 Code add-ons (each web/*.js file is one add-on).
 * The helper (dat3uzzo/ui.py) serves this script first in every bundle and sets window.__T3UI__
 * before it. This script is never an add-on and always loads, so the switches stay reachable when an
 * add-on is broken or turned off.
 *
 * 1. It runs each enabled add-on as its own <script> element, so an add-on that throws, or that does
 *    not even parse, stops only itself.
 * 2. On settings routes it adds a "daT3uzzo" entry to the side navigation (a clone of the General
 *    entry) and a "daT3uzzo" section at the end of Settings > General, with T3 Code's own class strings
 *    (copied from apps/web/src/components/settings/settingsLayout.tsx, SettingsGroup.tsx, ui/switch.tsx).
 * 3. A switch calls the helper (GET /ui?token=...), which writes ui.json. Then the page reloads, so
 *    add-ons load or unload cleanly.
 *
 * Defensive: every step is in try/catch, one MutationObserver throttled to 5 runs per second, one
 * IntersectionObserver on the section itself, and no changes outside settings routes.
 */
(() => {
  "use strict";
  if (window.__T3UI_LOADED__) return;
  window.__T3UI_LOADED__ = true;
  const S = window.__T3UI__ || {};

  for (const m of Array.isArray(S.load) ? S.load : []) {
    try {
      const s = document.createElement("script");
      s.setAttribute("data-t3ui-mod", String(m.id));
      s.textContent = String(m.code);
      (document.head || document.documentElement).appendChild(s);
    } catch (e) {
      try { console.warn("daT3uzzo: could not start add-on", m && m.id, e); } catch { /* ignore */ }
    }
  }

  try {
    const NAME = "daT3uzzo";
    const MODS = Array.isArray(S.mods) ? S.mods : [];
    const ENABLED = S.enabled !== false;
    const CFG = window.__DAT3UZZO__ || {};
    const PORT = CFG.port || 47613;
    const TOKEN = CFG.token || "";
    const ROOT = typeof S.root === "string" ? S.root : "the daT3uzzo folder";
    const ROUTE = "/settings/general";
    const SID = "dat3uzzo";
    const PULSE = "settings-search-target-pulse"; // T3 Code's own highlight after a settings jump
    const REVEAL_KEY = "t3ui:reveal";
    const C = {
      switchRoot: "inline-flex h-[calc(var(--thumb-size)+2px)] w-[calc(var(--thumb-size)*2-2px)] shrink-0 cursor-pointer items-center rounded-full p-[2px] outline-none transition-[background-color,box-shadow] duration-200 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background data-checked:bg-primary data-unchecked:bg-input [--thumb-size:--spacing(4)] sm:[--thumb-size:--spacing(3.5)]",
      switchThumb: "pointer-events-none block size-[calc(var(--thumb-size)-2px)] shrink-0 origin-left rounded-(--thumb-size) bg-background shadow-sm/5 dark:data-unchecked:bg-muted-foreground [transition:translate_.15s,border-radius_.15s,scale_.1s_.1s,transform-origin_.15s] data-checked:origin-right data-checked:translate-x-[calc(var(--thumb-size)-4px)]",
      section: "space-y-2.5",
      head: "flex min-h-7 flex-wrap items-start justify-between gap-x-4 gap-y-1 px-3 sm:px-4",
      headInner: "flex min-w-0 items-center gap-1",
      h2: "flex min-h-7 items-center gap-2 text-base font-semibold text-foreground",
      card: "relative overflow-visible text-foreground rounded-xl border border-border/60 bg-card/40 shadow-xs/5 *:not-first:border-t *:not-first:border-border/50 [&>[data-slot=settings-row]]:rounded-none",
      row: "group/settings-row @container/settings-row rounded-xl px-3 sm:px-4 aria-disabled:opacity-64 aria-disabled:[&_*]:text-muted-foreground py-3",
      rowGrid: "flex flex-col gap-3 @min-[32rem]/settings-row:grid @min-[32rem]/settings-row:grid-cols-[minmax(0,1fr)_auto] @min-[32rem]/settings-row:items-center @min-[32rem]/settings-row:gap-8",
      rowText: "min-w-0 flex-1 space-y-1",
      rowTitleLine: "flex min-h-5 items-center gap-1.5",
      rowTitle: "text-sm font-medium text-foreground",
      rowDesc: "max-w-2xl text-pretty text-xs leading-normal text-muted-foreground",
      rowCtl: "flex w-full min-w-0 shrink-0 items-center gap-2 @min-[32rem]/settings-row:w-auto @min-[32rem]/settings-row:justify-end",
      tag: "text-xs text-muted-foreground",
      note: "px-3 text-xs text-muted-foreground sm:px-4",
      error: "px-3 text-xs text-destructive sm:px-4",
      status: "max-w-2xl text-pretty text-xs leading-normal text-foreground",
      // Button, variant outline, size sm (apps/web/src/components/ui/button.tsx).
      button: "[--control-icon-color:currentColor] [&_svg]:-mx-0.5 relative inline-flex shrink-0 cursor-pointer items-center justify-center gap-2 whitespace-nowrap rounded-[var(--control-radius)] border font-medium text-base outline-none transition-[box-shadow,scale] [&:not([aria-haspopup]):not(:disabled):not([aria-disabled=true])]:will-change-[scale] [&:active:not([aria-haspopup])]:scale-[0.97] before:pointer-events-none before:absolute before:inset-0 before:rounded-[calc(var(--control-radius)-1px)] focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background disabled:pointer-events-none disabled:opacity-64 sm:text-sm "
        + "[--control-icon-color:var(--contrast-muted-foreground)] border-input bg-popover not-dark:bg-clip-padding text-foreground shadow-xs/5 dark:bg-input/32 [:hover,[data-pressed]]:bg-accent/50 dark:[:hover,[data-pressed]]:bg-input/64 "
        + "h-8 gap-1.5 px-[calc(--spacing(2.5)-1px)] sm:h-7",
    };
    const STATE_COLOR = { failed: "text-destructive", building: "text-muted-foreground" };
    // Lucide "puzzle" (ISC licence).
    const PUZZLE = '<path d="M15.39 4.39a1 1 0 0 0 1.68-.474 2.5 2.5 0 1 1 3.014 3.015 1 1 0 0 0-.474 1.68l1.683 1.682a2.414 2.414 0 0 1 0 3.414L19.61 15.39a1 1 0 0 1-1.68-.474 2.5 2.5 0 1 0-3.014 3.015 1 1 0 0 1 .474 1.68l-1.683 1.682a2.414 2.414 0 0 1-3.414 0L8.61 19.61a1 1 0 0 0-1.68.474 2.5 2.5 0 1 1-3.014-3.015 1 1 0 0 0 .474-1.68l-1.683-1.682a2.414 2.414 0 0 1 0-3.414L4.39 8.61a1 1 0 0 1 1.68.474 2.5 2.5 0 1 0 3.014-3.015 1 1 0 0 1-.474-1.68l1.683-1.682a2.414 2.414 0 0 1 3.414 0z"/>';
    // While the section is in view, the new entry is the current one, so General drops its highlight.
    const CSS = '[data-slot=sidebar-menu]:has(>li>[data-t3ui-nav][data-active=true]) [data-settings-section="/settings/general"][data-active=true]:not(:hover):not(:active){background-color:transparent;color:color-mix(in oklab,var(--contrast-sidebar-muted-foreground) 80%,transparent)}'
      + '[data-slot=sidebar-menu]:has(>li>[data-t3ui-nav][data-active=true]) [data-settings-section="/settings/general"][data-active=true]:not(:hover):not(:active)>svg{color:var(--sidebar-icon-color)}';

    const path = () => {
      try {
        const p = window.__TSR_ROUTER__?.state?.location?.pathname;
        if (typeof p === "string") return p;
      } catch { /* fall through */ }
      return location.pathname;
    };

    function el(tag, cls, text) {
      const n = document.createElement(tag);
      if (cls) n.className = cls;
      if (text != null) n.textContent = text;
      return n;
    }

    let busy = false;
    function change(query, btn, on) {
      if (busy) return;
      busy = true;
      paintSwitch(btn, on); // move the thumb now; the reload follows
      fetch(`http://127.0.0.1:${PORT}/ui?token=${encodeURIComponent(TOKEN)}&${query}`, { cache: "no-store" })
        .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
        .then(() => {
          try { sessionStorage.setItem(REVEAL_KEY, "1"); } catch { /* private window */ }
          location.reload();
        })
        .catch(() => {
          busy = false;
          paintSwitch(btn, !on);
          const err = document.querySelector(`#${SID} [data-t3ui-error]`);
          if (err) err.textContent = "The helper did not answer, so nothing changed. Start it with: python -m dat3uzzo install";
        });
    }

    function paintSwitch(b, on) {
      b.setAttribute("aria-checked", String(on));
      for (const n of [b, b.firstElementChild]) {
        n.toggleAttribute("data-checked", on);
        n.toggleAttribute("data-unchecked", !on);
      }
    }

    function switchEl(label, on, disabled, onFlip) {
      const b = el("button", C.switchRoot);
      b.type = "button";
      b.setAttribute("role", "switch");
      b.setAttribute("aria-label", label);
      b.innerHTML = `<span class="${C.switchThumb}"></span>`;
      paintSwitch(b, on);
      if (disabled) { b.disabled = true; b.setAttribute("data-disabled", ""); }
      b.addEventListener("click", () => { try { onFlip(b); } catch { /* never break T3 */ } });
      return b;
    }

    function row(title, desc, control, disabled, tag) {
      const r = el("div", C.row);
      r.setAttribute("data-slot", "settings-row");
      if (disabled) r.setAttribute("aria-disabled", "true");
      const grid = el("div", C.rowGrid);
      const text = el("div", C.rowText);
      const line = el("div", C.rowTitleLine);
      line.append(el("h3", C.rowTitle, title));
      if (tag) line.append(el("span", C.tag, tag));
      text.append(line);
      if (desc) text.append(el("p", C.rowDesc, desc));
      text.setAttribute("data-t3ui-text", "");
      grid.append(text);
      if (control) {
        const ctl = el("div", C.rowCtl);
        ctl.setAttribute("data-t3ui-ctl", "");
        ctl.append(control);
        grid.append(ctl);
      }
      r.append(grid);
      return r;
    }

    // A client patch row: its status line (active, applies at the next restart, building, failed) and
    // a "Restart T3 now" button while a swap waits for a restart. The helper does the restart.
    function paintStatus(p, b, st) {
      st = st || {};
      p.textContent = String(st.text || "");
      p.className = `${C.status} ${STATE_COLOR[st.state] || ""}`;
      if (b && !b.disabled) b.style.display = st.restart ? "" : "none"; // the class sets inline-flex
    }

    function clientExtras(m, r) {
      const p = el("p");
      p.setAttribute("data-t3ui-status", String(m.id));
      const b = el("button", C.button, "Restart T3 now");
      b.type = "button";
      b.setAttribute("data-t3ui-restart", String(m.id));
      paintStatus(p, b, m.status);
      b.addEventListener("click", () => {
        try {
          b.disabled = true;
          p.textContent = "Closing T3 Code. It opens again after the swap.";
          fetch(`http://127.0.0.1:${PORT}/client/restart?token=${encodeURIComponent(TOKEN)}`, { cache: "no-store" })
            .then((res) => res.json().catch(() => ({})).then((j) => ({ ok: res.ok, j })))
            .then(({ ok, j }) => { if (!ok) { b.disabled = false; p.textContent = String(j.message || "The helper refused."); } })
            .catch(() => { b.disabled = false; p.textContent = "The helper did not answer, so nothing changed."; });
        } catch { /* never break T3 */ }
      });
      const text = r.querySelector("[data-t3ui-text]");
      const ctl = r.querySelector("[data-t3ui-ctl]");
      if (text) text.append(p);
      if (ctl) ctl.prepend(b);
    }

    let polling = 0;
    function pollClient() {
      if (polling || !MODS.some((m) => Array.isArray(m.kinds) && m.kinds.includes("client patch"))) return;
      polling = setInterval(() => {
        try {
          if (!document.getElementById(SID)) return;
          fetch(`http://127.0.0.1:${PORT}/client/state?token=${encodeURIComponent(TOKEN)}`, { cache: "no-store" })
            .then((res) => (res.ok ? res.json() : null))
            .then((j) => {
              if (!j || !j.mods) return;
              for (const p of document.querySelectorAll("[data-t3ui-status]")) {
                const id = p.getAttribute("data-t3ui-status");
                const b = document.querySelector(`[data-t3ui-restart="${CSS.escape(id)}"]`);
                paintStatus(p, b, j.mods[id] || j.mods["*"]);
              }
            })
            .catch(() => {});
        } catch { /* ignore */ }
      }, 5000);
    }

    function buildSection() {
      const sec = el("section", C.section);
      sec.id = SID;
      sec.tabIndex = -1;
      sec.setAttribute("data-t3ui", "");
      const head = el("div", C.head);
      head.setAttribute("data-settings-scroll-target", "");
      const inner = el("div", C.headInner);
      inner.append(el("h2", C.h2, NAME));
      head.append(inner);
      const card = el("div", C.card);
      card.setAttribute("data-slot", "settings-group");
      card.append(row(`All ${NAME} add-ons`,
        "The master switch. When it is off, T3 Code runs without the add-ons below, and this section stays. Each change reloads the page.",
        switchEl(`All ${NAME} add-ons`, ENABLED, false, (b) => change(`enabled=${ENABLED ? 0 : 1}`, b, !ENABLED))));
      for (const m of MODS) {
        const on = m.on !== false;
        const label = String(m.label || m.id);
        const kinds = Array.isArray(m.kinds) ? m.kinds.map(String).join(" + ") : "";
        const r = row(label, String(m.desc || ""),
          switchEl(label, on, !ENABLED, (b) => change(`mod=${encodeURIComponent(m.id)}&on=${on ? 0 : 1}`, b, !on)),
          !ENABLED, kinds);
        if (Array.isArray(m.kinds) && m.kinds.includes("client patch")) {
          try { clientExtras(m, r); } catch { /* the switch still works */ }
        }
        card.append(r);
      }
      pollClient();
      if (!MODS.length) card.append(row("No add-ons found", `Add a .js file to ${ROOT}\\web and reload.`, null, false));
      const note = el("p", C.note,
        `Off switch outside this page: the "${NAME} off" shortcut on the desktop, or python -m dat3uzzo ui off in ${ROOT}. Then press Ctrl+R in T3 Code.`);
      const err = el("p", C.error);
      err.setAttribute("data-t3ui-error", "");
      err.setAttribute("role", "status");
      sec.append(head, card, note, err);
      return sec;
    }

    let current = false;
    function setCurrent(on) {
      current = on;
      const b = document.querySelector("[data-t3ui-nav]");
      if (!b) return;
      b.setAttribute("data-active", on ? "true" : "false");
      if (on) b.setAttribute("aria-current", "location"); else b.removeAttribute("aria-current");
    }

    let io = null;
    let watched = null;
    function watchView(sec) {
      if (watched === sec) return;
      if (io) io.disconnect();
      io = null;
      watched = sec;
      if (!sec || typeof IntersectionObserver !== "function") { setCurrent(false); return; }
      io = new IntersectionObserver((entries) => {
        try {
          for (const e of entries) {
            const half = e.rootBounds ? e.intersectionRect.height >= e.rootBounds.height / 2 : false;
            setCurrent(e.isIntersecting && (e.intersectionRatio >= 0.5 || half));
          }
        } catch { /* ignore */ }
      }, { root: sec.closest("[data-settings-page-scroll]"), threshold: [0, 0.25, 0.5, 0.75, 1] });
      io.observe(sec);
    }

    function reveal(highlight, tries) {
      let n = 0;
      const step = () => {
        try {
          scan();
          const sec = document.getElementById(SID);
          if (!sec) { if (++n < tries) setTimeout(step, 100); return; }
          const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
          (sec.firstElementChild || sec).scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "center" });
          if (!highlight) return; // after a switch reload: scroll only, no focus ring on the whole section
          sec.focus({ preventScroll: true });
          sec.classList.remove(PULSE);
          if (reduce) return;
          void sec.offsetWidth; // restart the animation
          sec.classList.add(PULSE);
          sec.addEventListener("blur", () => sec.classList.remove(PULSE), { once: true });
        } catch { /* ignore */ }
      };
      step();
    }

    function open() {
      if (path() === ROUTE) { reveal(true, 30); return; }
      const router = window.__TSR_ROUTER__;
      if (!router || typeof router.navigate !== "function") return; // a raw pushState would confuse T3
      Promise.resolve()
        .then(() => router.navigate({ to: ROUTE }))
        .catch(() => {})
        .then(() => reveal(true, 30));
    }

    function injectNav() {
      const general = document.querySelector('nav [data-settings-section="/settings/general"]');
      const li = general && general.closest("li");
      if (!li || !li.parentElement) return;
      let mine = document.querySelector("[data-t3ui-nav]");
      if (!mine) {
        const item = li.cloneNode(true);
        mine = item.querySelector("[data-settings-section]");
        if (!mine) return;
        mine.setAttribute("data-settings-section", SID); // T3's arrow keys move through it too
        mine.setAttribute("data-t3ui-nav", "");
        mine.removeAttribute("aria-current");
        const svg = mine.querySelector("svg");
        if (svg) {
          svg.innerHTML = PUZZLE;
          const keep = (svg.getAttribute("class") || "").split(/\s+/).filter((c) => c && !c.startsWith("lucide"));
          svg.setAttribute("class", ["lucide", "lucide-puzzle", ...keep].join(" "));
        }
        const label = mine.querySelector("span");
        if (label) label.textContent = NAME;
        mine.addEventListener("click", (e) => { e.preventDefault(); try { open(); } catch { /* ignore */ } });
      }
      const mineLi = mine.closest("li");
      if (mineLi && mineLi.previousElementSibling !== li) li.after(mineLi);
      setCurrent(current);
    }

    function injectSection() {
      let sec = document.getElementById(SID);
      if (path() !== ROUTE) {
        if (sec) sec.remove();
        watchView(null);
        return;
      }
      const scroll = document.querySelector("[data-settings-page-scroll]");
      const first = scroll && [...scroll.querySelectorAll("section")].find((s) => s.id !== SID && !s.closest(`#${SID}`));
      const box = first && first.parentElement;
      if (!box) return;
      if (!sec) sec = buildSection();
      if (sec.parentElement !== box || box.lastElementChild !== sec) box.appendChild(sec);
      watchView(sec);
    }

    function scan() {
      if (!path().startsWith("/settings")) { watchView(null); return; }
      if (!document.querySelector("style[data-t3ui]")) {
        const st = el("style");
        st.setAttribute("data-t3ui", "");
        st.textContent = CSS;
        document.head.appendChild(st);
      }
      injectNav();
      injectSection();
    }

    let timer = 0;
    const schedule = () => {
      if (!timer) timer = setTimeout(() => { timer = 0; try { scan(); } catch { /* never break T3 */ } }, 200);
    };
    const start = () => {
      try {
        new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true });
        schedule();
        let back = false;
        try { back = sessionStorage.getItem(REVEAL_KEY) === "1"; sessionStorage.removeItem(REVEAL_KEY); } catch { /* ignore */ }
        if (back) reveal(false, 150); // after a switch reload, show the section again (T3 may take a few seconds)
      } catch { /* ignore */ }
    };
    if (document.body) start(); else document.addEventListener("DOMContentLoaded", start, { once: true });

    // Read-only hook for tests and troubleshooting.
    window.__T3UI_API__ = { state: () => ({ enabled: ENABLED, mods: MODS.map((m) => ({ ...m })), current }), open };
  } catch (e) {
    try { console.warn("daT3uzzo: settings switches failed", e); } catch { /* ignore */ }
  }
})();
