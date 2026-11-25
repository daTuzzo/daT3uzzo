// @mod-label Read aloud (text to speech)
// @mod-desc A speaker button on each reply reads it aloud with the voice service you choose (OpenAI, xAI, OpenRouter or ElevenLabs), with a player, word highlight and saved audio. Its helper part is the /speak and /settings endpoints.
/*
 * T3 Code read-aloud mod.
 * Loaded by a marked block that dat3uzzo/patcher.py keeps in T3 Code's index.html.
 * Talks only to the local helper on 127.0.0.1. All controls use T3 Code's own class strings
 * (apps/web/src/components/ui: button, menu, tooltip, switch), so they look native.
 * Icons: Lucide (ISC licence). Every hook fails quietly: if a marker is missing, T3 Code works as before.
 *
 * Performance: no work per frame except one transform on the progress bar. The word highlight
 * moves only when the spoken word changes (binary search over timings, then one layout read
 * and a few transform writes). The page observer is throttled to 5 runs per second.
 */
(() => {
  "use strict";
  if (window.__T3RA_LOADED__) return;
  window.__T3RA_LOADED__ = true;

  const CFG = window.__DAT3UZZO__ || {};
  const PORT = CFG.port || 47613;
  const TOKEN = CFG.token || "";
  const VOICES = ["marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse"];
  const SPEEDS = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2];
  const PREFS_KEY = "t3ra:prefs:v2";
  const ACTION_LABELS = ["Copy message", "Reply to this response", "Fork from this response"];
  const HELPER_DOWN = "The read-aloud helper is not running. Run: python -m dat3uzzo install";
  const SKIP = 'button,svg,script,style,[aria-hidden="true"],[data-thread-find-ignore],[data-t3ra],.katex-mathml';
  const BLOCKS = "p,li,h1,h2,h3,h4,h5,h6,blockquote,tr,pre,[data-language]";
  // Voice services, in the helper's order (dat3uzzo/tts). Their capabilities come from /health.
  const PROVIDER_IDS = ["openai", "xai", "openrouter", "elevenlabs"];
  const TEST_TEXT = "This is how replies will sound in read aloud.";
  // Chromium caps an audio-only media buffer near 12 MB, about 26 minutes at 64 kbps. A saved reply
  // arrives at once, so the player appends at most this far ahead of the play position.
  const AHEAD_S = 600;
  const strMap = (o) => (o && typeof o === "object" && !Array.isArray(o)
    ? Object.fromEntries(Object.entries(o).filter(([k, v]) => PROVIDER_IDS.includes(k) && typeof v === "string" && v.length <= 160))
    : {});

  // ---------------------------------------------------------------- preferences

  const prefs = (() => {
    let p = null;
    try { p = JSON.parse(localStorage.getItem(PREFS_KEY) || "null"); } catch { p = null; }
    if (!p) { // first run of v2: keep voice and speed from v1, start in Exact
      try { p = JSON.parse(localStorage.getItem("t3ra:prefs:v1") || "{}") || {}; } catch { p = {}; }
      delete p.mode;
    }
    return {
      voice: VOICES.includes(p.voice) ? p.voice : "marin",
      mode: p.mode === "adapt" ? "adapt" : "exact",
      speed: SPEEDS.includes(p.speed) ? p.speed : 1,
      highlight: p.highlight !== false,
      follow: p.follow !== false,
      volume: Number.isFinite(p.volume) ? Math.min(1, Math.max(0, p.volume)) : 1,
      muted: p.muted === true,
      colors: ["auto", "nightly", "accent"].includes(p.colors) ? p.colors : "auto",
      // Missing fields mean the preset: OpenAI Realtime, its default model, T3 Code's saved key.
      // `voice` stays the OpenAI voice, as before; other providers keep theirs in voiceBy.
      provider: PROVIDER_IDS.includes(p.provider) ? p.provider : "openai",
      modelBy: strMap(p.modelBy),
      voiceBy: strMap(p.voiceBy),
      keyBy: Object.fromEntries(Object.entries(strMap(p.keyBy)).filter(([, v]) => v === "t3" || v === "own")),
    };
  })();
  // The desktop app names its build: "Nightly", or "Alpha", "Beta" or "Dev" for the others.
  const BUILD_STAGE = (() => {
    try { return window.desktopBridge?.getAppBranding?.()?.stageLabel || ""; } catch { return ""; }
  })();
  const COLOR_LABELS = { auto: "Match T3 Code build", nightly: "Nightly", accent: "Theme accent" };
  const savePrefs = () => { try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch { /* private window */ } };

  // ---------------------------------------------------------------- T3 Code class strings
  // Copied from upstream apps/web/src/components/ui/{button,menu,tooltip,switch}.tsx.

  const C = {
    btn: "[--control-icon-color:currentColor] [&_svg]:-mx-0.5 relative inline-flex shrink-0 cursor-pointer items-center justify-center gap-2 whitespace-nowrap rounded-[var(--control-radius)] border font-medium text-base outline-none transition-[box-shadow,scale] [&:not([aria-haspopup]):not(:disabled):not([aria-disabled=true])]:will-change-[scale] [&:active:not([aria-haspopup])]:scale-[0.97] before:pointer-events-none before:absolute before:inset-0 before:rounded-[calc(var(--control-radius)-1px)] pointer-coarse:after:absolute pointer-coarse:after:size-full pointer-coarse:after:min-h-11 pointer-coarse:after:min-w-11 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background disabled:pointer-events-none disabled:opacity-64 aria-disabled:cursor-not-allowed aria-disabled:opacity-64 sm:text-sm [&_svg:not([class*='text-'])]:text-[var(--control-icon-color)] [&_svg:not([class*='size-'])]:size-4.5 sm:[&_svg:not([class*='size-'])]:size-4 [&_svg]:pointer-events-none [&_svg]:shrink-0",
    primary: "not-disabled:inset-shadow-[0_1px_--theme(--color-white/16%)] border-primary bg-primary text-primary-foreground shadow-primary/24 shadow-xs [:active,[data-pressed]]:inset-shadow-[0_1px_--theme(--color-black/8%)] [:disabled,:active,[data-pressed]]:shadow-none [:hover,[data-pressed]]:bg-primary/90",
    ghostMuted: "border-transparent text-muted-foreground data-pressed:bg-accent [:hover,[data-pressed]]:bg-accent [:hover,[data-pressed]]:text-foreground",
    outline: "[--control-icon-color:var(--contrast-muted-foreground)] border-input bg-popover not-dark:bg-clip-padding text-foreground shadow-xs/5 not-disabled:not-active:not-data-pressed:before:shadow-[0_1px_--theme(--color-black/4%)] dark:bg-input/32 dark:not-disabled:before:shadow-[0_-1px_--theme(--color-white/2%)] dark:not-disabled:not-active:not-data-pressed:before:shadow-[0_-1px_--theme(--color-white/6%)] [:disabled,:active,[data-pressed]]:shadow-none [:hover,[data-pressed]]:bg-accent/50 dark:[:hover,[data-pressed]]:bg-input/64",
    iconSmPill: "size-8 rounded-full before:rounded-full sm:size-7",
    smPill: "h-8 gap-1.5 rounded-full px-2.5 sm:h-7",
    sm: "h-8 gap-1.5 px-[calc(--spacing(2.5)-1px)] sm:h-7",
    tooltip: "relative flex origin-(--transform-origin) rounded-md text-popover-foreground text-xs transition-[scale,opacity] before:pointer-events-none before:absolute before:inset-0 before:rounded-[calc(var(--radius-md)-1px)] before:shadow-[0_1px_--theme(--color-black/4%)] data-ending-style:scale-98 data-starting-style:scale-98 data-ending-style:opacity-0 data-starting-style:opacity-0 dark:before:shadow-[0_-1px_--theme(--color-white/6%)] border bg-popover not-dark:bg-clip-padding shadow-md/5 max-w-80 wrap-anywhere whitespace-normal leading-snug",
    menu: "dropdown-glass relative flex origin-(--transform-origin) rounded-lg shadow-[0_16px_40px_-18px_rgb(0_0_0/55%)] outline-none focus:outline-none dark:shadow-[0_18px_44px_-18px_rgb(0_0_0/80%)] max-w-[calc(100vw-2rem)] w-max min-w-0 transition-[scale,opacity] data-starting-style:scale-98 data-starting-style:opacity-0",
    menuRadio: "grid min-h-8 cursor-pointer items-center gap-2 rounded-sm py-1 ps-2 text-base text-foreground outline-none data-highlighted:bg-accent data-highlighted:text-accent-foreground sm:min-h-7 sm:text-sm [&_svg:not([class*='size-'])]:size-4.5 sm:[&_svg:not([class*='size-'])]:size-4 [&_svg]:pointer-events-none [&_svg]:shrink-0 grid-cols-[1rem_minmax(0,1fr)] pe-4",
    menuItem: "[&>svg]:-mx-0.5 flex min-h-8 cursor-pointer select-none items-center gap-2 rounded-sm px-2 py-1 text-base text-foreground outline-none data-highlighted:bg-accent data-highlighted:text-accent-foreground sm:min-h-7 sm:text-sm [&>svg:not([class*='opacity-'])]:opacity-80 [&>svg:not([class*='size-'])]:size-4.5 sm:[&>svg:not([class*='size-'])]:size-4 [&>svg:not([class*='text-'])]:text-muted-foreground [&>svg]:pointer-events-none [&>svg]:shrink-0",
    menuSubTrigger: "[&>svg:not(:last-child)]:-mx-0.5 flex min-h-8 cursor-pointer items-center gap-2 rounded-sm px-2 py-1 text-base text-foreground outline-none data-highlighted:bg-accent data-popup-open:bg-accent data-highlighted:text-accent-foreground data-popup-open:text-accent-foreground sm:min-h-7 sm:text-sm [&_svg:not([class*='size-'])]:size-4.5 sm:[&_svg:not([class*='size-'])]:size-4 [&_svg:not([class*='text-'])]:text-muted-foreground [&_svg]:pointer-events-none [&>svg]:shrink-0",
    menuLabel: "px-2 py-1.5 font-medium text-muted-foreground text-xs",
    menuSep: "mx-2 my-1 h-px bg-border",
    switchRoot: "inline-flex h-[calc(var(--thumb-size)+2px)] w-[calc(var(--thumb-size)*2-2px)] shrink-0 cursor-pointer items-center rounded-full p-[2px] outline-none transition-[background-color,box-shadow] duration-200 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background data-checked:bg-primary data-unchecked:bg-input [--thumb-size:--spacing(4)] sm:[--thumb-size:--spacing(3.5)]",
    switchThumb: "pointer-events-none block size-[calc(var(--thumb-size)-2px)] shrink-0 origin-left rounded-(--thumb-size) bg-background shadow-sm/5 dark:data-unchecked:bg-muted-foreground [transition:translate_.15s,border-radius_.15s,scale_.1s_.1s,transform-origin_.15s] data-checked:origin-right data-checked:translate-x-[calc(var(--thumb-size)-4px)]",
    // From upstream apps/web/src/components/ui/input.tsx, size "sm" (the wrapper span, then the input).
    inputControl: "relative inline-flex w-full rounded-lg border border-input bg-background not-dark:bg-clip-padding text-base text-foreground shadow-xs/5 ring-ring/24 transition-shadow before:pointer-events-none before:absolute before:inset-0 before:rounded-[calc(var(--radius-lg)-1px)] not-has-disabled:not-has-focus-visible:not-has-aria-invalid:before:shadow-[0_1px_--theme(--color-black/4%)] has-focus-visible:has-aria-invalid:border-destructive/64 has-focus-visible:has-aria-invalid:ring-destructive/16 has-aria-invalid:border-destructive/36 has-focus-visible:border-ring has-autofill:bg-foreground/4 has-disabled:opacity-64 has-[:disabled,:focus-visible,[aria-invalid]]:shadow-none has-focus-visible:ring-[3px] has-focus-visible:ring-inset sm:text-sm dark:bg-input/32 dark:has-autofill:bg-foreground/8 dark:has-aria-invalid:ring-destructive/24 dark:not-has-disabled:not-has-focus-visible:not-has-aria-invalid:before:shadow-[0_-1px_--theme(--color-white/6%)]",
    input: "h-7.5 w-full min-w-0 rounded-[inherit] px-[calc(--spacing(2.5)-1px)] leading-7.5 outline-none placeholder:text-placeholder sm:h-6.5 sm:leading-6.5 [transition:background-color_5000000s_ease-in-out_0s]",
  };

  // Like upstream's cn() (tailwind-merge) for the families these strings share: a later class
  // replaces an earlier one with the same variant prefix and utility family.
  function cls(...parts) {
    const family = (tok) => {
      if (tok.startsWith("[--control-icon-color:")) return "--control-icon-color";
      const cut = tok.search(/:(?![^[]*\])[^:]*$/); // the last variant colon outside brackets
      const prefix = cut >= 0 ? tok.slice(0, cut + 1) : "";
      const util = cut >= 0 ? tok.slice(cut + 1) : tok;
      const m = util.match(/^(rounded|gap|text-(?:xs|sm|base|lg|xl)|h|size|px)(?:-|$)/);
      if (!m) return null;
      return prefix + (m[1].startsWith("text-") ? "text-size" : m[1]);
    };
    const out = [];
    const seen = new Map();
    for (const tok of parts.join(" ").split(/\s+/).filter(Boolean)) {
      const f = family(tok);
      if (f && seen.has(f)) out[seen.get(f)] = null;
      if (f) seen.set(f, out.length);
      out.push(tok);
    }
    return out.filter(Boolean).join(" ");
  }

  // ---------------------------------------------------------------- icons (Lucide)

  const SPEAKER = '<path d="M11 4.702a.705.705 0 0 0-1.203-.498L6.413 7.587A1.4 1.4 0 0 1 5.416 8H3a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2.416a1.4 1.4 0 0 1 .997.413l3.383 3.384A.705.705 0 0 0 11 19.298z"/>';
  const ICON = {
    volume: SPEAKER + '<path d="M16 9a5 5 0 0 1 0 6"/><path d="M19.364 18.364a9 9 0 0 0 0-12.728"/>',
    vol1: SPEAKER + '<path d="M16 9a5 5 0 0 1 0 6"/>',
    volX: SPEAKER + '<line x1="22" x2="16" y1="9" y2="15"/><line x1="16" x2="22" y1="9" y2="15"/>',
    playFill: '<path d="M7 4.8v14.4a1 1 0 0 0 1.53.85l11.5-7.2a1 1 0 0 0 0-1.7L8.53 3.95A1 1 0 0 0 7 4.8z" fill="currentColor" stroke="none"/>',
    pauseFill: '<rect x="6" y="4.5" width="4" height="15" rx="1.2" fill="currentColor" stroke="none"/><rect x="14" y="4.5" width="4" height="15" rx="1.2" fill="currentColor" stroke="none"/>',
    play: '<polygon points="6 3 20 12 6 21 6 3"/>',
    pause: '<rect x="14" y="4" width="4" height="16" rx="1"/><rect x="6" y="4" width="4" height="16" rx="1"/>',
    loader: '<path d="M21 12a9 9 0 1 1-6.219-8.56"/>',
    back: '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><text x="12.4" y="15.3" text-anchor="middle" font-size="8" font-weight="650" fill="currentColor" stroke="none" font-family="system-ui,sans-serif">10</text>',
    fwd: '<path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><text x="11.6" y="15.3" text-anchor="middle" font-size="8" font-weight="650" fill="currentColor" stroke="none" font-family="system-ui,sans-serif">10</text>',
    x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    sliders: '<line x1="21" x2="14" y1="4" y2="4"/><line x1="10" x2="3" y1="4" y2="4"/><line x1="21" x2="12" y1="12" y2="12"/><line x1="8" x2="3" y1="12" y2="12"/><line x1="21" x2="16" y1="20" y2="20"/><line x1="12" x2="3" y1="20" y2="20"/><line x1="14" x2="14" y1="2" y2="6"/><line x1="8" x2="8" y1="10" y2="14"/><line x1="16" x2="16" y1="18" y2="22"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
    alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    chevron: '<path d="m6 9 6 6 6-6"/>',
    chevronRight: '<path d="m9 18 6-6-6-6"/>',
    settings: '<path d="M9.671 4.136a2.34 2.34 0 0 1 4.659 0 2.34 2.34 0 0 0 3.319 1.915 2.34 2.34 0 0 1 2.33 4.033 2.34 2.34 0 0 0 0 3.831 2.34 2.34 0 0 1-2.33 4.033 2.34 2.34 0 0 0-3.319 1.915 2.34 2.34 0 0 1-4.659 0 2.34 2.34 0 0 0-3.32-1.915 2.34 2.34 0 0 1-2.33-4.033 2.34 2.34 0 0 0 0-3.831A2.34 2.34 0 0 1 6.35 6.051a2.34 2.34 0 0 0 3.319-1.915"/><circle cx="12" cy="12" r="3"/>',
    refresh: '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
  };
  const icon = (name, cls = "") =>
    `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" class="${cls}${name === "loader" ? " t3ra-spin" : ""}">${ICON[name]}</svg>`;
  const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const fmt = (t) => {
    if (!Number.isFinite(t) || t < 0) t = 0;
    return `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`;
  };

  // ---------------------------------------------------------------- styles (layout and motion only)

  const style = document.createElement("style");
  style.setAttribute("data-t3ra", "");
  style.textContent = `
.t3ra-spin{animation:t3ra-spin .9s linear infinite}
@keyframes t3ra-spin{to{transform:rotate(360deg)}}
/* T3 Code's top bar is a window drag region, and Electron gives a drag region every click above it,
   even on elements drawn over it. Its own overlays opt out the same way. */
.t3ra-strip,.t3ra-vol,.t3ra-layer{-webkit-app-region:no-drag;app-region:no-drag}
.t3ra-strip,.t3ra-vol{--t3ra-n1:var(--stage-night-highlight,oklch(.707 .157 252));--t3ra-n2:var(--stage-night-secondary,oklch(.6 .182 277));--t3ra-n3:var(--stage-night-tertiary,oklch(.626 .211 306));--t3ra-nl:var(--stage-night-line,oklch(.939 .029 273));--t3ra-ng:var(--stage-night-glow-highlight,oklch(.554 .177 272))}
.t3ra-strip[data-colors="accent"],[data-colors="accent"] .t3ra-vol{--t3ra-n1:var(--primary);--t3ra-n2:var(--primary);--t3ra-n3:var(--primary);--t3ra-nl:var(--primary-foreground,#fff);--t3ra-ng:var(--primary)}
.t3ra-strip[data-colors="accent"] .t3ra-night{color:var(--primary-foreground,#fff)}
.t3ra-night{border-color:transparent;background:linear-gradient(140deg,var(--t3ra-n1),var(--t3ra-n2) 55%,var(--t3ra-n3));color:#fff;box-shadow:inset 0 1px 0 rgb(255 255 255/.24),0 4px 14px -5px color-mix(in oklab,var(--t3ra-ng) 90%,transparent);transition:filter .15s ease,box-shadow .15s ease,scale .1s ease}
.t3ra-night:hover{filter:brightness(1.1) saturate(1.08)}
.t3ra-night:active{box-shadow:inset 0 1px 2px rgb(0 0 0/.2)}
.t3ra-night:focus-visible{box-shadow:0 0 0 2px var(--background),0 0 0 4px var(--ring)}
.t3ra-night svg{filter:drop-shadow(0 1px 1px rgb(0 0 0/.2))}
.t3ra-strip{position:fixed;z-index:45;display:flex;align-items:center;gap:2px;height:40px;padding:4px 4px 4px 8px;border:1px solid color-mix(in oklab,var(--border) 60%,transparent);border-radius:9999px;box-shadow:0 16px 40px -18px rgb(0 0 0/.45);font-variant-numeric:tabular-nums;transform:translateX(-50%);max-width:calc(100vw - 32px);user-select:none;-webkit-user-select:none;transition:opacity .16s ease,transform .2s cubic-bezier(.2,.8,.2,1)}
.t3ra-strip[data-starting]{opacity:0;transform:translateX(-50%) translateY(-6px) scale(.98)}
.t3ra-strip[hidden]{display:none}
.t3ra-time{min-width:34px;padding:0 2px;font-size:12px;line-height:16px;color:var(--muted-foreground);text-align:center}
.t3ra-sep{flex:none;width:1px;height:16px;margin:0 4px;background:color-mix(in oklab,var(--border) 80%,transparent)}
.t3ra-track{position:relative;flex:1 1 auto;width:clamp(96px,22vw,240px);height:28px;margin:0 6px;cursor:pointer;touch-action:none;outline:none}
.t3ra-rail{position:absolute;left:0;right:0;top:50%;height:3px;margin-top:-1.5px;border-radius:9999px;overflow:hidden;background:color-mix(in oklab,var(--foreground) 10%,transparent);transition:height .15s ease,margin-top .15s ease}
.t3ra-track:hover .t3ra-rail,.t3ra-track[data-drag] .t3ra-rail,.t3ra-track:focus-visible .t3ra-rail{height:5px;margin-top:-2.5px}
.t3ra-buf{position:absolute;inset:0;transform-origin:0 50%;transform:scaleX(0);will-change:transform;background:color-mix(in oklab,var(--t3ra-n2) 24%,transparent);transition:transform .35s linear}
.t3ra-fill{position:absolute;inset:0;background:linear-gradient(90deg,var(--t3ra-n1),var(--t3ra-n2) 55%,var(--t3ra-n3));clip-path:inset(0 100% 0 0 round 9999px)}
.t3ra-thumb{position:absolute;top:50%;left:0;width:9px;height:9px;margin:-4.5px 0 0 -4.5px;border-radius:9999px;background:var(--t3ra-nl);box-shadow:0 0 0 1px color-mix(in oklab,var(--t3ra-n2) 60%,transparent),0 1px 6px color-mix(in oklab,var(--t3ra-ng) 70%,transparent);transform:scale(0);transition:transform .15s cubic-bezier(.2,.8,.2,1);pointer-events:none}
.t3ra-vol{position:fixed;display:flex;flex-direction:column;align-items:center;gap:6px;width:36px;padding:12px 0 8px;border:1px solid color-mix(in oklab,var(--border) 60%,transparent);border-radius:9999px;box-shadow:0 16px 40px -18px rgb(0 0 0/.45);font-variant-numeric:tabular-nums;user-select:none;-webkit-user-select:none;transform-origin:50% 0;transition:opacity .14s ease,transform .18s cubic-bezier(.2,.8,.2,1)}
.t3ra-vol[data-starting-style]{opacity:0;transform:translateY(-4px) scale(.96)}
.t3ra-vol[hidden]{display:none}
.t3ra-vol-track{position:relative;width:24px;height:92px;cursor:pointer;touch-action:none;outline:none}
.t3ra-vol-rail{position:absolute;top:0;bottom:0;left:50%;width:4px;margin-left:-2px;border-radius:9999px;overflow:hidden;background:color-mix(in oklab,var(--foreground) 10%,transparent);transition:width .15s ease,margin-left .15s ease}
.t3ra-vol-track:hover .t3ra-vol-rail,.t3ra-vol-track[data-drag] .t3ra-vol-rail{width:6px;margin-left:-3px}
.t3ra-vol-track:focus-visible .t3ra-vol-rail{box-shadow:0 0 0 2px var(--ring)}
.t3ra-vol-fill{position:absolute;inset:0;background:linear-gradient(0deg,var(--t3ra-n1),var(--t3ra-n2) 55%,var(--t3ra-n3))}
.t3ra-vol-thumb{position:absolute;left:50%;top:0;width:10px;height:10px;margin:-5px 0 0 -5px;border-radius:9999px;background:var(--t3ra-nl);box-shadow:0 0 0 1px color-mix(in oklab,var(--t3ra-n2) 60%,transparent),0 1px 6px color-mix(in oklab,var(--t3ra-ng) 70%,transparent);pointer-events:none}
.t3ra-vol-val{min-width:24px;font-size:11px;line-height:14px;color:var(--muted-foreground);text-align:center}
.t3ra-track:hover .t3ra-thumb,.t3ra-track[data-drag] .t3ra-thumb,.t3ra-track:focus-visible .t3ra-thumb{transform:scale(1)}
.t3ra-track:focus-visible .t3ra-rail{box-shadow:0 0 0 2px var(--ring)}
.t3ra-msg{display:flex;align-items:center;gap:6px;min-width:0;max-width:340px;padding:0 8px;font-size:12px;line-height:1.35;color:var(--destructive-foreground,var(--destructive))}
.t3ra-msg[hidden],.t3ra-track[hidden],.t3ra-time[hidden]{display:none}
.t3ra-msg svg{width:14px;height:14px;flex:none}
.t3ra-layer{position:fixed;z-index:130}
.t3ra-hl{position:absolute;inset:0;z-index:-1;pointer-events:none;contain:layout style}
.t3ra-hl>div{position:absolute;left:0;top:0;width:0;height:0;opacity:0;will-change:transform,width,height;transition:transform .17s cubic-bezier(.2,.8,.2,1),width .17s cubic-bezier(.2,.8,.2,1),height .17s cubic-bezier(.2,.8,.2,1),opacity .2s ease,background-color .25s ease,box-shadow .25s ease}
.t3ra-hl>[data-snap]{transition:none}
.t3ra-hl-pill{border-radius:5px;background:color-mix(in oklab,var(--primary) 19%,transparent)}
.t3ra-hl-pill[data-kind="a"]{background:color-mix(in oklab,var(--warning) 24%,transparent)}
.t3ra-hl-block{border-radius:8px;background:color-mix(in oklab,var(--foreground) 3.5%,transparent);transition-duration:.26s!important}
.t3ra-hl-block[data-code]{background:transparent;box-shadow:0 0 0 2px color-mix(in oklab,var(--warning) 45%,transparent)}
@media (prefers-reduced-motion:reduce){.t3ra-hl>div,.t3ra-strip,.t3ra-vol{transition:none}}
@media (max-width:640px){.t3ra-hide-sm{display:none!important}}
[data-t3ra-btn][data-state="loading"],[data-t3ra-btn][data-state="playing"],[data-t3ra-btn][data-state="paused"]{color:var(--primary)!important;opacity:1!important}
.t3ra-set-row{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px 16px;padding:12px 16px}
.t3ra-set-row+.t3ra-set-row{border-top:1px solid color-mix(in oklab,var(--border) 50%,transparent)}
.t3ra-set-text{min-width:0;flex:1 1 260px}
.t3ra-set-title{font-size:14px;font-weight:500;line-height:20px;color:var(--foreground)}
.t3ra-set-desc{margin-top:2px;font-size:12px;line-height:1.45;color:var(--muted-foreground)}
.t3ra-set-ctl{display:flex;align-items:center;gap:6px;flex:none}
.t3ra-set-row[hidden]{display:none}
.t3ra-set-desc .t3ra-bad,.t3ra-danger{color:var(--destructive-foreground,var(--destructive))!important}
.t3ra-key{width:min(260px,60vw)}
.t3ra-swatch{display:inline-block;width:10px;height:10px;margin:0 3px 0 6px;border-radius:3px;vertical-align:-1px}
`;
  document.head.appendChild(style);

  // A top layer for tooltips and menus, above T3 Code's own popups.
  const layer = document.createElement("div");
  layer.setAttribute("data-t3ra", "");
  layer.className = "t3ra-layer";
  const mount = () => { if (!layer.isConnected) document.body.appendChild(layer); };

  const animateIn = (el) => {
    el.setAttribute("data-starting-style", "");
    requestAnimationFrame(() => requestAnimationFrame(() => el.removeAttribute("data-starting-style")));
  };

  // ---------------------------------------------------------------- tooltip (T3 Code tooltip popup)

  let tip = null;
  let tipTimer = 0;
  let tipWarm = 0; // after one tooltip, the next shows at once (as Base UI does)
  function hideTip() {
    clearTimeout(tipTimer);
    if (tip) { tip.remove(); tip = null; tipWarm = Date.now(); }
  }
  function showTip(el) {
    const text = el.getAttribute("data-t3ra-tip") || el.getAttribute("aria-label");
    if (!text || !el.isConnected || el.closest("[data-menu-open]")) return;
    mount();
    tip = document.createElement("div");
    tip.className = C.tooltip;
    tip.style.position = "fixed";
    tip.innerHTML = `<div class="min-w-0 px-2 py-1">${esc(text)}</div>`;
    layer.appendChild(tip);
    const r = el.getBoundingClientRect();
    const w = tip.offsetWidth;
    const h = tip.offsetHeight;
    const below = r.top - h - 8 < 4;
    tip.style.left = `${Math.round(Math.min(Math.max(8, r.left + r.width / 2 - w / 2), innerWidth - w - 8))}px`;
    tip.style.top = `${Math.round(below ? r.bottom + 6 : r.top - h - 6)}px`;
    tip.style.transformOrigin = below ? "50% 0" : "50% 100%";
    animateIn(tip);
  }
  function withTip(el) {
    el.addEventListener("pointerenter", () => {
      clearTimeout(tipTimer);
      tipTimer = setTimeout(() => showTip(el), Date.now() - tipWarm < 400 ? 0 : 500);
    });
    el.addEventListener("pointerleave", hideTip);
    el.addEventListener("pointerdown", hideTip);
    return el;
  }

  // ---------------------------------------------------------------- menu (T3 Code menu popup)

  let menu = null; // { el, anchor, sub: { el, owner } | null }
  function closeSub() {
    if (!menu || !menu.sub) return;
    menu.sub.owner.removeAttribute("data-popup-open");
    menu.sub.el.remove();
    menu.sub = null;
  }
  function closeMenu() {
    if (!menu) return;
    closeSub();
    menu.anchor.removeAttribute("data-pressed");
    menu.anchor.closest("[data-menu-open]")?.removeAttribute("data-menu-open");
    menu.el.remove();
    menu = null;
    document.removeEventListener("pointerdown", onOutside, true);
    document.removeEventListener("keydown", onMenuKey, true);
  }
  const onOutside = (e) => {
    if (!menu) return;
    const t = e.target;
    if (menu.el.contains(t) || menu.anchor.contains(t) || (menu.sub && menu.sub.el.contains(t))) return;
    closeMenu();
  };
  function onMenuKey(e) {
    if (!menu) return;
    const host = menu.sub ? menu.sub.el : menu.el;
    const items = [...host.querySelectorAll("[data-item]")];
    const i = items.indexOf(document.activeElement);
    const go = (k) => { const it = items[(k + items.length) % items.length]; it.focus({ preventScroll: true }); hover(it); };
    const back = () => { const o = menu.sub.owner; closeSub(); o.focus({ preventScroll: true }); hover(o); };
    if (e.key === "Escape") {
      e.preventDefault(); e.stopPropagation();
      if (menu.sub) back(); else { const a = menu.anchor; closeMenu(); a.focus(); }
    } else if (e.key === "ArrowDown") { e.preventDefault(); go(i + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); go(i - 1); }
    else if (e.key === "Home") { e.preventDefault(); go(0); }
    else if (e.key === "End") { e.preventDefault(); go(-1); }
    else if (e.key === "ArrowLeft" && menu.sub) { e.preventDefault(); back(); }
    else if ((e.key === "ArrowRight" || e.key === "Enter" || e.key === " ") && document.activeElement?.hasAttribute("data-item")) {
      if (e.key === "ArrowRight" && !document.activeElement.hasAttribute("data-sub")) return;
      e.preventDefault();
      document.activeElement.click();
    } else if (e.key === "Tab") closeMenu();
  }
  function hover(item) {
    if (!menu) return;
    const host = item && menu.sub && menu.sub.el.contains(item) ? menu.sub.el : menu.el;
    host.querySelectorAll("[data-highlighted]").forEach((n) => n.removeAttribute("data-highlighted"));
    if (item) item.setAttribute("data-highlighted", "");
  }
  function popup() {
    mount();
    const el = document.createElement("div");
    el.className = C.menu;
    el.style.position = "fixed";
    el.setAttribute("role", "menu");
    const inner = document.createElement("div");
    inner.className = "w-full overflow-y-auto p-1";
    inner.style.maxHeight = "min(70vh, 520px)";
    el.appendChild(inner);
    return { el, inner };
  }
  function openSub(owner, spec) {
    if (menu.sub && menu.sub.owner === owner) return;
    closeSub();
    const { el, inner } = popup();
    fill(inner, spec);
    layer.appendChild(el);
    const mr = menu.el.getBoundingClientRect();
    const or = owner.getBoundingClientRect();
    const w = el.offsetWidth;
    const h = el.offsetHeight;
    const right = mr.right - 4 + w <= innerWidth - 8;
    el.style.left = `${Math.round(right ? mr.right - 4 : mr.left - w + 4)}px`;
    el.style.top = `${Math.round(Math.min(Math.max(8, or.top - 5), innerHeight - h - 8))}px`;
    el.style.transformOrigin = right ? "0 0" : "100% 0";
    animateIn(el);
    owner.setAttribute("data-popup-open", "");
    menu.sub = { el, owner };
  }

  // spec items: {label} | {sep} | {radio, value, current, text, note?, onPick} | {toggle, text, checked, onToggle}
  //             | {sub, text, value, items} | {action, text, icon, onPick}
  function fill(inner, spec) {
    for (const it of spec) {
      if (it.sep) {
        const s = document.createElement("div");
        s.className = C.menuSep;
        s.setAttribute("role", "separator");
        inner.appendChild(s);
        continue;
      }
      if (it.label) {
        const l = document.createElement("div");
        l.className = C.menuLabel;
        l.textContent = it.label;
        inner.appendChild(l);
        continue;
      }
      const b = document.createElement("div");
      b.tabIndex = -1;
      b.setAttribute("data-item", "");
      if (it.radio) {
        b.className = C.menuRadio;
        b.setAttribute("role", "menuitemradio");
        const on = it.value === it.current;
        b.setAttribute("aria-checked", String(on));
        const text = it.note
          ? `<span class="col-start-2 flex min-w-0 items-center justify-between gap-6"><span class="truncate">${esc(it.text)}</span><span class="shrink-0 text-muted-foreground text-xs">${esc(it.note)}</span></span>`
          : `<span class="col-start-2 truncate">${esc(it.text)}</span>`;
        b.innerHTML = `<span class="col-start-1 flex items-center justify-center">${on ? icon("check") : ""}</span>${text}`;
        b.addEventListener("click", () => { closeMenu(); it.onPick(it.value); });
      } else if (it.sub) {
        b.className = C.menuSubTrigger;
        b.setAttribute("role", "menuitem");
        b.setAttribute("aria-haspopup", "menu");
        b.setAttribute("data-sub", "");
        b.innerHTML = `<span class="flex-1">${esc(it.text)}</span><span class="text-muted-foreground text-xs">${esc(it.value)}</span>${icon("chevronRight", "size-3.5")}`;
        b.addEventListener("click", () => { openSub(b, it.items()); const f = menu.sub?.el.querySelector('[aria-checked="true"]') || menu.sub?.el.querySelector("[data-item]"); if (f) { f.focus({ preventScroll: true }); hover(f); } });
      } else if (it.toggle) {
        b.className = `${C.menuItem} justify-between gap-6`;
        b.setAttribute("role", "menuitemcheckbox");
        const paint = (on) => {
          b.setAttribute("aria-checked", String(on));
          const state = on ? "data-checked" : "data-unchecked";
          b.innerHTML = `<span>${esc(it.text)}</span><span class="${C.switchRoot}" ${state}=""><span class="${C.switchThumb}" ${state}=""></span></span>`;
        };
        let on = Boolean(it.checked);
        paint(on);
        b.addEventListener("click", () => { on = !on; paint(on); it.onToggle(on); });
      } else {
        b.className = C.menuItem;
        b.setAttribute("role", "menuitem");
        b.innerHTML = `${it.icon ? icon(it.icon) : ""}<span>${esc(it.text)}</span>`;
        b.addEventListener("click", () => { closeMenu(); it.onPick(); });
      }
      b.addEventListener("pointerenter", () => {
        hover(b);
        b.focus({ preventScroll: true });
        if (!menu) return;
        const inMain = menu.el.contains(b);
        if (inMain && it.sub) openSub(b, it.items());
        else if (inMain) closeSub();
      });
      inner.appendChild(b);
    }
  }

  function openMenu(anchor, spec) {
    const same = menu && menu.anchor === anchor;
    closeMenu();
    hideTip();
    if (same) return;
    const { el, inner } = popup();
    menu = { el, anchor, sub: null };
    fill(inner, spec);
    layer.appendChild(el);
    const r = anchor.getBoundingClientRect();
    const w = el.offsetWidth;
    const h = el.offsetHeight;
    const below = r.bottom + 6 + h <= innerHeight - 8;
    el.style.left = `${Math.round(Math.min(Math.max(8, r.left + r.width / 2 - w / 2), innerWidth - w - 8))}px`;
    el.style.top = `${Math.round(below ? r.bottom + 6 : r.top - h - 6)}px`;
    el.style.transformOrigin = below ? "50% 0" : "50% 100%";
    animateIn(el);
    anchor.setAttribute("data-pressed", "");
    anchor.closest(".t3ra-strip")?.setAttribute("data-menu-open", "");
    const first = el.querySelector('[aria-checked="true"]') || el.querySelector("[data-item]");
    if (first) { first.focus({ preventScroll: true }); hover(first); }
    document.addEventListener("pointerdown", onOutside, true);
    document.addEventListener("keydown", onMenuKey, true);
  }

  // ---------------------------------------------------------------- reply text as markdown

  const isCodeBlock = (el) => el.matches("[data-language], pre");
  function codeFence(el) {
    const lang = (el.getAttribute("data-language") || "").trim() ||
      ((el.querySelector("code[class*='language-']") || {}).className || "").replace(/.*language-([\w+-]+).*/, "$1");
    const code = el.querySelector("pre code") || el.querySelector("pre") || el.querySelector("code") || el;
    return ["```" + (/^[\w+-]+$/.test(lang) ? lang : ""), code.textContent.replace(/\n+$/, ""), "```", ""];
  }
  function inline(el) {
    let s = "";
    for (const ch of el.childNodes) {
      if (ch.nodeType === Node.TEXT_NODE) { s += ch.textContent; continue; }
      if (ch.nodeType !== Node.ELEMENT_NODE || ch.matches(SKIP) || isCodeBlock(ch)) continue;
      const tag = ch.tagName;
      if (tag === "BR") s += " ";
      else if (tag === "CODE") s += "`" + ch.textContent + "`";
      else if (tag === "UL" || tag === "OL" || tag === "TABLE" || tag === "IMG") continue;
      else s += inline(ch);
    }
    return s.replace(/\s+/g, " ").trim();
  }
  function tableLines(table) {
    const rows = [...table.querySelectorAll("tr")];
    const cells = (r) => [...r.children].filter((c) => c.tagName === "TD" || c.tagName === "TH")
      .map((c) => inline(c).replace(/\|/g, "/"));
    if (!rows.length) return [];
    const head = cells(rows[0]);
    const out = ["| " + head.join(" | ") + " |", "|" + head.map(() => "---").join("|") + "|"];
    for (const r of rows.slice(1)) out.push("| " + cells(r).join(" | ") + " |");
    return out;
  }
  function listLines(list, out) {
    let n = 1;
    for (const li of list.children) {
      if (li.tagName !== "LI") continue;
      const text = inline(li);
      if (text) out.push((list.tagName === "OL" ? `${n}. ` : "- ") + text);
      n += 1;
      for (const sub of li.querySelectorAll(":scope > ul, :scope > ol")) listLines(sub, out);
      for (const cb of li.querySelectorAll(":scope > [data-language], :scope > pre")) out.push(...codeFence(cb));
    }
  }
  function blocks(node, out) {
    for (const ch of node.childNodes) {
      if (ch.nodeType === Node.TEXT_NODE) {
        const t = ch.textContent.replace(/\s+/g, " ").trim();
        if (t) out.push(t);
        continue;
      }
      if (ch.nodeType !== Node.ELEMENT_NODE) continue;
      const el = ch;
      if (isCodeBlock(el)) { out.push(...codeFence(el)); continue; }
      if (el.matches(SKIP)) continue;
      const tag = el.tagName;
      if (/^H[1-6]$/.test(tag)) { const t = inline(el); if (t) out.push("## " + t, ""); }
      else if (tag === "TABLE") out.push(...tableLines(el), "");
      else if (tag === "UL" || tag === "OL") { listLines(el, out); out.push(""); }
      else if (tag === "P") {
        const t = inline(el);
        const only = [...el.childNodes].filter((n) => n.nodeType === Node.ELEMENT_NODE || n.textContent.trim());
        const boldLine = only.length === 1 && only[0].nodeType === Node.ELEMENT_NODE && /^(STRONG|B)$/.test(only[0].tagName);
        if (t) out.push(boldLine ? "## " + t : t, "");
      }
      else if (tag === "HR" || tag === "IMG" || tag === "BR") continue;
      else if (!el.querySelector("p,div,ul,ol,table,pre,h1,h2,h3,h4,h5,h6,blockquote,[data-language]")) {
        const t = inline(el);
        if (t) out.push(t, "");
      } else blocks(el, out);
    }
  }
  const sourceOf = (id) => document.querySelector(`[data-assistant-citation-source="${CSS.escape(id)}"]`);
  function replyMarkdown(id) {
    const src = sourceOf(id);
    if (!src) return "";
    const out = [];
    blocks(src, out);
    return out.join("\n").replace(/\n{3,}/g, "\n\n").trim();
  }

  // ---------------------------------------------------------------- word highlight

  // Follow-along scrolls only while audio plays. Any scroll the player did not make (wheel, keys,
  // scrollbar, touch, T3 Code itself) holds it until the spoken line is back in view, or until you
  // press play or seek.
  let followHold = false;
  let own = null; // the player's own scroll: { until, lo, hi } in scrollTop
  addEventListener("scroll", (e) => {
    const vp = viewport || document.scrollingElement;
    const t = e.target === document ? document.scrollingElement : e.target;
    if (t !== vp) return;
    // The player's smooth scroll only moves between its start and its target. Anything else is you.
    if (own && Date.now() < own.until && t.scrollTop >= own.lo - 2 && t.scrollTop <= own.hi + 2) return;
    followHold = true;
  }, { capture: true, passive: true });

  // Screen words in reading order, each with its DOM range. A code block is one token.
  function collectTokens(id) {
    const src = sourceOf(id);
    const out = [];
    if (!src) return out;
    (function walk(node) {
      for (const ch of node.childNodes) {
        if (ch.nodeType === Node.TEXT_NODE) {
          const text = ch.textContent;
          const re = /\S+/g;
          let m;
          while ((m = re.exec(text))) {
            const r = document.createRange();
            r.setStart(ch, m.index);
            r.setEnd(ch, m.index + m[0].length);
            out.push({ text: m[0], range: r });
          }
          continue;
        }
        if (ch.nodeType !== Node.ELEMENT_NODE) continue;
        if (isCodeBlock(ch)) {
          const r = document.createRange();
          r.selectNodeContents(ch);
          out.push({ text: "‹code›", range: r, code: ch });
          continue;
        }
        if (ch.matches(SKIP)) continue;
        walk(ch);
      }
    })(src);
    return out;
  }

  const HL = { src: null, layer: null, block: null, pills: [], key: "", ro: null, prev: null };

  function freshTokens() {
    // React can re-render the reply (virtualization, theme): rebuild the ranges when they detach.
    const t = P.tokens[0];
    if (t && t.range.startContainer.isConnected) return true;
    const again = collectTokens(P.id);
    if (again.length !== P.tokens.length) return false;
    P.tokens = again;
    return true;
  }

  function removeHighlight() {
    if (HL.ro) HL.ro.disconnect();
    if (HL.layer) HL.layer.remove();
    if (HL.src && HL.prev) { HL.src.style.position = HL.prev.position; HL.src.style.isolation = HL.prev.isolation; }
    Object.assign(HL, { src: null, layer: null, block: null, pills: [], key: "", ro: null, prev: null });
  }

  function ensureLayer() {
    const src = P.id && sourceOf(P.id);
    if (!src) return false;
    if (HL.src === src && HL.layer && HL.layer.isConnected) return true;
    removeHighlight();
    HL.prev = { position: src.style.position, isolation: src.style.isolation };
    if (getComputedStyle(src).position === "static") src.style.position = "relative";
    src.style.isolation = "isolate"; // the layer sits behind the text, above the page
    const lay = document.createElement("div");
    lay.className = "t3ra-hl";
    lay.setAttribute("data-t3ra", "");
    lay.setAttribute("aria-hidden", "true");
    HL.block = document.createElement("div");
    HL.block.className = "t3ra-hl-block";
    lay.appendChild(HL.block);
    for (let i = 0; i < 3; i++) {
      const p = document.createElement("div");
      p.className = "t3ra-hl-pill";
      lay.appendChild(p);
      HL.pills.push(p);
    }
    src.prepend(lay);
    Object.assign(HL, { src, layer: lay, key: "" });
    HL.ro = new ResizeObserver(() => { HL.key = ""; if (P.state !== "playing") paintHighlight(); });
    HL.ro.observe(src);
    return true;
  }

  function place(el, x, y, w, h) {
    const fresh = el.style.opacity !== "1";
    if (fresh) el.setAttribute("data-snap", ""); // appear in place, glide afterwards
    el.style.transform = `translate3d(${x}px,${y}px,0)`;
    el.style.width = `${w}px`;
    el.style.height = `${h}px`;
    el.style.opacity = "1";
    if (fresh) { void el.offsetWidth; el.removeAttribute("data-snap"); }
  }

  function hideMarks() {
    if (!HL.layer) return;
    for (const p of HL.pills) p.style.opacity = "0";
    HL.block.style.opacity = "0";
  }

  // Segments are [screenStart, screenEnd, t0, t1, kind, piece], sorted by t0.
  function activeSegs(t) {
    const s = P.segs;
    let lo = 0;
    let hi = s.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (s[mid][2] <= t) lo = mid + 1; else hi = mid; }
    const out = [];
    const latest = s[lo - 1];
    for (let i = lo - 1; i >= 0 && i >= lo - 40; i--) {
      if (t < s[i][3] + 0.12) out.push(s[i]);
      else if (s[i][3] < t - 6) break;
    }
    if (!out.length && latest && t < latest[3] + 1.5) out.push(latest); // hold through short pauses
    return out;
  }

  function paintHighlight() {
    if (!prefs.highlight || !P.audio || !P.segs.length) { hideMarks(); return; }
    const act = activeSegs(P.audio.currentTime);
    const key = act.map((s) => `${s[0]}-${s[1]}${s[4]}`).join(",");
    if (key === HL.key && HL.layer && HL.layer.isConnected) return;
    HL.key = key;
    if (!act.length) { hideMarks(); return; }
    if (!ensureLayer() || !freshTokens()) return;
    HL.key = key;
    // read layout once
    const base = HL.src.getBoundingClientRect();
    const lines = [];
    let blockEl = null;
    let codeEl = null;
    let kind = "a";
    for (const s of act) {
      const a = P.tokens[s[0]];
      const b = P.tokens[s[1] - 1];
      if (!a || !b) continue;
      if (s[4] === "x") kind = "x";
      if (a.code) { codeEl = a.code; continue; }
      const r = document.createRange();
      r.setStart(a.range.startContainer, a.range.startOffset);
      r.setEnd(b.range.endContainer, b.range.endOffset);
      for (const rc of r.getClientRects()) {
        if (rc.width < 1 || rc.height < 1) continue;
        const line = lines.find((l) => Math.abs(l.top - rc.top) < 4);
        if (line) { line.left = Math.min(line.left, rc.left); line.right = Math.max(line.right, rc.right); line.bottom = Math.max(line.bottom, rc.bottom); }
        else lines.push({ left: rc.left, right: rc.right, top: rc.top, bottom: rc.bottom });
      }
      if (!blockEl) {
        const n = a.range.startContainer;
        const el = n.nodeType === Node.ELEMENT_NODE ? n : n.parentElement;
        const blk = el && el.closest(BLOCKS);
        if (blk && HL.src.contains(blk)) blockEl = blk;
      }
    }
    const frame = codeEl || blockEl;
    const fr = frame ? frame.getBoundingClientRect() : null;
    // write
    HL.pills.forEach((p, i) => {
      const l = lines[i];
      if (!l) { p.style.opacity = "0"; return; }
      p.dataset.kind = kind;
      place(p, l.left - base.left - 3, l.top - base.top - 1, l.right - l.left + 6, l.bottom - l.top + 2);
    });
    if (fr) {
      const pad = codeEl ? 3 : 6;
      HL.block.toggleAttribute("data-code", Boolean(codeEl));
      place(HL.block, fr.left - base.left - pad, fr.top - base.top - (codeEl ? 3 : 2), fr.width + pad * 2, fr.height + (codeEl ? 6 : 4));
    } else HL.block.style.opacity = "0";
    follow(lines[0] || fr);
  }

  function follow(rect) {
    if (!rect || !prefs.follow || P.state !== "playing") return;
    const vp = viewport || document.scrollingElement;
    if (!vp) return;
    const box = vp === document.scrollingElement ? { top: 0, bottom: innerHeight } : vp.getBoundingClientRect();
    const top = Math.max(box.top, 0) + 72; // clear of the floating strip
    const bottom = Math.min(box.bottom, innerHeight) - 56;
    const inView = rect.top >= top && rect.bottom <= bottom;
    if (followHold) { if (inView) followHold = false; return; }
    if (inView) return;
    const delta = rect.top - (top + (bottom - top) * 0.32);
    const from = vp.scrollTop;
    const to = Math.min(vp.scrollHeight - vp.clientHeight, Math.max(0, from + delta));
    own = { until: Date.now() + 1500, lo: Math.min(from, to), hi: Math.max(from, to) };
    vp.scrollBy({ top: delta, behavior: "smooth" });
  }

  // ---------------------------------------------------------------- playback

  const P = { id: null, state: "idle", ws: null, audio: null, ms: null, sb: null, url: null, queue: [], chunks: null,
              serverDone: false, generating: false, gotAudio: false, error: "", tokens: [], segs: [], raf: 0, total: 0 };

  const bufferedEnd = () => {
    const b = P.audio && P.audio.buffered;
    return b && b.length ? b.end(b.length - 1) : 0;
  };
  const duration = () => {
    const d = P.audio && P.audio.duration;
    if (P.serverDone && !P.queue.length && Number.isFinite(d)) return d;
    // Audio still held back (AHEAD_S): the helper's total length, from its "done" message.
    if (P.serverDone && P.total) return Math.max(P.total, bufferedEnd());
    return bufferedEnd();
  };

  function teardown() {
    if (P.ws) {
      try { if (P.ws.readyState === 1) P.ws.send(JSON.stringify({ type: "cancel" })); P.ws.close(); } catch { /* closed */ }
    }
    if (P.audio) { try { P.audio.pause(); P.audio.removeAttribute("src"); P.audio.load(); } catch { /* gone */ } }
    if (P.url) URL.revokeObjectURL(P.url);
    cancelAnimationFrame(P.raf);
    removeHighlight();
    Object.assign(P, { ws: null, audio: null, ms: null, sb: null, url: null, queue: [], chunks: null,
                       serverDone: false, generating: false, gotAudio: false, error: "", tokens: [], segs: [], total: 0 });
  }

  function stop(hide) {
    teardown();
    P.state = "idle";
    if (hide) { P.id = null; strip.hide(); }
    render();
    updateButtons();
  }

  function fail(message) {
    P.error = message;
    P.generating = false;
    if (!P.gotAudio) P.state = "error";
    if (P.ws) { try { P.ws.close(); } catch { /* closed */ } P.ws = null; }
    strip.show();
    render();
    updateButtons();
  }

  function startPlayback() {
    if (!P.audio || P.state !== "loading" || bufferedEnd() < 0.25) return;
    P.audio.play().catch(() => { P.state = "paused"; render(); updateButtons(); });
  }

  function pump() {
    const { sb, ms } = P;
    if (P.chunks) { // no Media Source support: play once complete
      if (P.serverDone && P.audio && !P.url && P.chunks.length) {
        P.url = URL.createObjectURL(new Blob(P.chunks, { type: "audio/mpeg" }));
        P.audio.src = P.url;
        P.audio.addEventListener("canplay", startPlayback, { once: true });
      }
      return;
    }
    if (!sb || !ms || ms.readyState !== "open" || sb.updating) return;
    if (P.queue.length) {
      // Far enough ahead: wait. The audio element's timeupdate calls pump() again.
      if (P.audio && bufferedEnd() - P.audio.currentTime > AHEAD_S) return;
      let n = 0;
      let total = 0;
      while (n < P.queue.length && (total < 524288 || n === 0)) total += P.queue[n++].length; // up to 512 KB
      const buf = new Uint8Array(total);
      let at = 0;
      for (const c of P.queue.slice(0, n)) { buf.set(c, at); at += c.length; }
      P.queue = P.queue.slice(n);
      try { sb.appendBuffer(buf); } catch (e) {
        if (e && e.name === "QuotaExceededError") { P.queue.unshift(buf); return; } // retried on timeupdate
        fail("The audio could not be buffered.");
      }
      return;
    }
    if (P.serverDone) { try { ms.endOfStream(); } catch { /* already ended */ } }
  }

  const perf = { frames: 0, frameMs: 0, frameMaxMs: 0, scans: 0, scanMs: 0, scanMaxMs: 0 };
  function loop() {
    cancelAnimationFrame(P.raf);
    const tick = () => {
      const t0 = performance.now();
      try { strip.progress(); paintHighlight(); } catch { /* never break T3 */ }
      const dt = performance.now() - t0;
      perf.frames += 1; perf.frameMs += dt; if (dt > perf.frameMaxMs) perf.frameMaxMs = dt;
      if (P.state === "playing") P.raf = requestAnimationFrame(tick);
    };
    P.raf = requestAnimationFrame(tick);
  }

  // fresh: "Generate again", which skips the saved audio of this reply and replaces it.
  function start(id, fresh = false) {
    teardown();
    followHold = false;
    P.id = id;
    strip.show();
    const md = replyMarkdown(id);
    if (!md) return fail("Scroll to the reply, then press its speaker button again.");
    if (!TOKEN) return fail("Read-aloud is not installed. Run: python -m dat3uzzo install");

    P.state = "loading";
    P.generating = true;
    P.tokens = prefs.highlight ? collectTokens(id) : [];
    const audio = new Audio();
    audio.preservesPitch = true;
    // Loading new media resets playbackRate to defaultPlaybackRate, so set both, and re-apply below.
    audio.defaultPlaybackRate = prefs.speed;
    audio.playbackRate = prefs.speed;
    P.audio = audio;
    applyVolume();
    const keepSpeed = () => { if (audio.playbackRate !== prefs.speed) audio.playbackRate = prefs.speed; };
    audio.addEventListener("loadedmetadata", keepSpeed);
    audio.addEventListener("durationchange", () => strip.progress(true));
    audio.addEventListener("seeked", () => { HL.key = ""; strip.progress(true); paintHighlight(); if (P.audio === audio && P.queue.length) pump(); });
    audio.addEventListener("timeupdate", () => { if (P.audio === audio && P.queue.length) pump(); });
    audio.addEventListener("playing", () => {
      if (P.audio !== audio) return;
      keepSpeed();
      P.state = "playing";
      render();
      updateButtons();
      loop();
    });
    audio.addEventListener("pause", () => {
      if (P.audio === audio && P.state === "playing") { P.state = "paused"; render(); updateButtons(); }
    });
    audio.addEventListener("ended", () => {
      if (P.audio === audio && P.serverDone) { P.state = "ended"; hideMarks(); render(); updateButtons(); }
    });

    if (window.MediaSource && MediaSource.isTypeSupported("audio/mpeg")) {
      const ms = new MediaSource();
      P.ms = ms;
      P.url = URL.createObjectURL(ms);
      audio.src = P.url;
      ms.addEventListener("sourceopen", () => {
        if (P.ms !== ms) return;
        try {
          const sb = ms.addSourceBuffer("audio/mpeg");
          sb.mode = "sequence";
          sb.addEventListener("updateend", () => { if (P.sb === sb) { startPlayback(); strip.progress(true); pump(); } });
          P.sb = sb;
          pump();
        } catch { fail("This window cannot stream audio."); }
      }, { once: true });
    } else {
      P.chunks = [];
    }

    let ws;
    try { ws = new WebSocket(`ws://127.0.0.1:${PORT}/speak?token=${encodeURIComponent(TOKEN)}`); }
    catch { return fail(HELPER_DOWN); }
    ws.binaryType = "arraybuffer";
    P.ws = ws;
    ws.onopen = () => ws.send(JSON.stringify({
      type: "speak", markdown: md, ...choice(),
      ...(fresh ? { fresh: true } : {}),
      ...(P.tokens.length ? { display: P.tokens.map((t) => t.text) } : {}),
    }));
    ws.onmessage = (ev) => {
      if (P.ws !== ws) return;
      if (typeof ev.data === "string") {
        let m = {};
        try { m = JSON.parse(ev.data); } catch { return; }
        if (m.type === "error") fail(m.message || "Read-aloud failed.");
        else if (m.type === "done") {
          P.serverDone = true; P.generating = false;
          P.total = Number.isFinite(m.audio_s) ? m.audio_s : 0;
          pump(); render();
        }
        else if (m.type === "timings" && Array.isArray(m.segs)) {
          // A piece first gets an instant estimate, then exact timings that replace it.
          P.segs = P.segs.filter((s) => s[5] !== m.piece)
            .concat(m.segs.map((s) => [s[0], s[1], s[2], s[3], s[4], m.piece]))
            .sort((a, b) => a[2] - b[2]);
          HL.key = "";
          if (P.state !== "playing") paintHighlight();
        }
        return;
      }
      P.gotAudio = true;
      const chunk = new Uint8Array(ev.data);
      if (P.chunks) P.chunks.push(chunk); else { P.queue.push(chunk); pump(); }
    };
    ws.onclose = () => {
      if (P.ws !== ws) return;
      P.ws = null;
      if (P.error || P.serverDone) return;
      if (P.gotAudio) { P.serverDone = true; P.generating = false; pump(); render(); }
      else fail(HELPER_DOWN);
    };
    render();
    updateButtons();
  }

  function toggle(id) {
    if (P.id !== id || P.state === "idle" || P.state === "error") return start(id);
    if (P.state === "loading") return stop(false);
    if (P.state === "playing") { P.audio.pause(); return; }
    followHold = false;
    HL.key = "";
    if (P.state === "ended") P.audio.currentTime = 0;
    P.audio.play().catch(() => {});
  }

  function seekTo(t) {
    if (!P.audio) return;
    P.audio.currentTime = Math.min(Math.max(0, t), Math.max(0, bufferedEnd() - 0.05));
    if (P.state === "ended") { P.state = "paused"; updateButtons(); }
    HL.key = "";
    followHold = false;
    strip.progress(true);
    render();
  }

  function setSpeed(s) {
    prefs.speed = s;
    savePrefs();
    if (P.audio) { P.audio.defaultPlaybackRate = s; P.audio.playbackRate = s; }
    render();
  }

  function applyVolume() {
    if (!P.audio) return;
    P.audio.volume = prefs.volume;
    P.audio.muted = prefs.muted;
  }

  function setVolume(v, save = true) {
    prefs.volume = Math.round(Math.min(1, Math.max(0, v)) * 100) / 100;
    prefs.muted = false;
    applyVolume();
    strip.paintVol();
    if (save) savePrefs();
  }

  function toggleMute() {
    if (prefs.muted || prefs.volume === 0) {
      prefs.muted = false;
      if (prefs.volume === 0) prefs.volume = 0.5;
    } else prefs.muted = true;
    applyVolume();
    strip.paintVol();
    savePrefs();
  }

  function applyColors() {
    const night = prefs.colors === "nightly" || (prefs.colors === "auto" && BUILD_STAGE === "Nightly");
    strip.root.dataset.colors = night ? "nightly" : "accent";
    layer.dataset.colors = strip.root.dataset.colors;
  }

  function setColors(value) {
    prefs.colors = value;
    savePrefs();
    applyColors();
    renderSettings();
  }

  // A change to what is spoken starts the current reply again (saved pieces of the old choice stay).
  const restartIfActive = () => {
    if (P.id && (P.state === "playing" || P.state === "paused" || P.state === "loading")) start(P.id);
    else render();
  };

  function setVoiceOrMode(key, value) {
    if (key === "voice") return setVoice(value);
    if (prefs[key] === value) return;
    prefs[key] = value;
    savePrefs();
    restartIfActive();
  }

  // ---------------------------------------------------------------- voice service choice

  // The provider list and capabilities come from the helper's /health. Before it answers, only the
  // preset is known, and every other provider counts as Exact only.
  const meta = (pid = prefs.provider) => (health && health.providers || []).find((p) => p.id === pid) || null;
  const providerLabel = (pid = prefs.provider) => meta(pid)?.label || { openai: "OpenAI Realtime", xai: "xAI Grok voice", openrouter: "OpenRouter", elevenlabs: "ElevenLabs" }[pid];
  const providerName = (pid = prefs.provider) => meta(pid)?.name || { openai: "OpenAI", xai: "xAI", openrouter: "OpenRouter", elevenlabs: "ElevenLabs" }[pid];
  const providerModes = (pid = prefs.provider) => meta(pid)?.caps?.modes || (pid === "openai" ? ["adapt", "exact"] : ["exact"]);
  const curMode = () => (providerModes().includes(prefs.mode) ? prefs.mode : "exact");
  const curModel = (pid = prefs.provider) => prefs.modelBy[pid] || meta(pid)?.defaultModel || (pid === "openai" ? "gpt-realtime-2.1" : "");
  const curVoice = (pid = prefs.provider) => (pid === "openai" ? prefs.voice : prefs.voiceBy[pid] || meta(pid)?.defaultVoice || "");
  const t3Slot = (pid = prefs.provider) => Boolean(meta(pid)?.caps?.t3_key ?? pid !== "elevenlabs");
  // Unset: the helper picks T3 Code's key when it has one, else the separate key.
  const curKeySource = (pid = prefs.provider) => (t3Slot(pid) ? prefs.keyBy[pid] || "" : "own");
  // The provider fields of a speak message. The helper checks each one (tts.select).
  const choice = () => ({
    provider: prefs.provider, model: curModel(), voice: curVoice(), mode: curMode(),
    ...(curKeySource() ? { keySource: curKeySource() } : {}),
  });

  const VOICE_LISTS = {}; // "provider|model" -> [{ id, label, note }], fetched once per session
  const listKey = (pid = prefs.provider) => `${pid}|${pid === "openrouter" ? curModel(pid) : ""}`;
  function voiceList(pid = prefs.provider) {
    if (pid === "openai") return VOICES.map((v) => ({ id: v, label: cap(v), note: v === "marin" || v === "cedar" ? "natural" : "" }));
    return VOICE_LISTS[listKey(pid)] || null;
  }
  const voiceLabel = (pid = prefs.provider) => {
    const v = curVoice(pid);
    // Before the list loads, the helper's fixed fallback voices (/health) still name the default.
    return [...(voiceList(pid) || []), ...(meta(pid)?.voices || [])].find((x) => x.id === v)?.label
      || (v ? (pid === "elevenlabs" ? "Voice" : v) : "Default");
  };
  function loadVoices(pid = prefs.provider, force = false) {
    if (pid === "openai") return Promise.resolve();
    const k = listKey(pid);
    if (VOICE_LISTS[k] && !force) return Promise.resolve();
    // xAI and ElevenLabs list the voices of an account: no key known, no request.
    if (pid !== "openrouter" && S.keys && pid === prefs.provider) {
      const src = effSource();
      const has = src === "own" ? ownSaved(pid) : ["value", "reference"].includes(t3State(pid));
      if (!has) { S.voiceError[k] = "Save the key above to list your voices."; renderSettings(); return Promise.resolve(); }
    }
    return ask({ type: "voices.list", provider: pid, model: curModel(pid), ...(curKeySource(pid) ? { keySource: curKeySource(pid) } : {}) })
      .then((r) => {
        if (r.type !== "voices") { S.voiceError[k] = r.message || ""; return; }
        if (Array.isArray(r.voices) && r.voices.length) VOICE_LISTS[k] = r.voices;
        S.voiceError[k] = r.error || "";
        // OpenRouter voices belong to a model: keep a valid one.
        if (pid === "openrouter" && VOICE_LISTS[k] && !VOICE_LISTS[k].some((v) => v.id === prefs.voiceBy[pid])) {
          prefs.voiceBy[pid] = VOICE_LISTS[k][0].id;
          savePrefs();
        }
        renderSettings();
      });
  }
  const MODEL_LISTS = {}; // provider -> [{ id, label, note }]; fixed lists come from /health
  function modelList(pid = prefs.provider) {
    if (MODEL_LISTS[pid]) return MODEL_LISTS[pid];
    const fixed = meta(pid)?.models;
    return fixed && fixed.length ? fixed : null;
  }
  function loadModels(pid = prefs.provider) {
    if (pid !== "openrouter" || MODEL_LISTS[pid]) return Promise.resolve();
    return ask({ type: "models.list", provider: pid }).then((r) => {
      if (r.type === "models" && Array.isArray(r.models) && r.models.length) MODEL_LISTS[pid] = r.models;
      S.modelError = r.error || (r.type === "models" ? "" : r.message || "");
      renderSettings();
    });
  }

  function setProvider(pid) {
    if (prefs.provider === pid || !PROVIDER_IDS.includes(pid)) return;
    prefs.provider = pid;
    S.keyMode = "";
    savePrefs();
    loadModels(pid).then(() => loadVoices(pid));
    restartIfActive();
  }
  function setModel(m) {
    if (curModel() === m) return;
    prefs.modelBy[prefs.provider] = m;
    savePrefs();
    loadVoices();
    restartIfActive();
  }
  function setVoice(v) {
    if (curVoice() === v) return;
    if (prefs.provider === "openai") prefs.voice = v;
    else prefs.voiceBy[prefs.provider] = v;
    savePrefs();
    restartIfActive();
  }
  function setKeySource(src) {
    if (curKeySource() === src) return;
    prefs.keyBy[prefs.provider] = src;
    S.keyMode = "";
    savePrefs();
    loadVoices(prefs.provider, true);
    restartIfActive();
  }
  function regenerate() {
    closeMenu();
    if (P.id) start(P.id, true);
  }

  function setFlag(key, on) {
    prefs[key] = on;
    savePrefs();
    if (key === "highlight" && !on) hideMarks();
    if (key === "highlight" && on && P.id && !P.tokens.length) P.tokens = collectTokens(P.id);
    renderSettings();
  }

  const speedMenu = () => [{ label: "Speed" },
    ...SPEEDS.map((s) => ({ radio: true, value: s, current: prefs.speed, text: `${s}×`, onPick: setSpeed }))];

  const MENU_VOICES = 20;
  const voiceItems = () => {
    const list = voiceList();
    if (!list) loadVoices(); // shown the next time the menu opens
    const shown = (list || [{ id: curVoice(), label: voiceLabel() }]).slice(0, MENU_VOICES);
    const items = shown.map((v) => ({
      radio: true, value: v.id, current: curVoice(), text: v.label, note: v.note || "", onPick: setVoice,
    }));
    if (list && list.length > MENU_VOICES) {
      items.push({ sep: true }, { action: true, text: "More voices in settings", icon: "settings", onPick: openSettings });
    }
    return items;
  };
  // Only what the chosen service can do: one reading mode means no Reading section at all.
  const optionsMenu = () => [
    ...(providerModes().length > 1 ? [
      { label: "Reading" },
      { radio: true, value: "exact", current: curMode(), text: "Exact", onPick: (v) => setVoiceOrMode("mode", v) },
      { radio: true, value: "adapt", current: curMode(), text: "Adapt for listening", onPick: (v) => setVoiceOrMode("mode", v) },
      { sep: true },
    ] : []),
    { label: providerLabel() },
    { sub: true, text: "Voice", value: voiceLabel(), items: voiceItems },
    { sep: true },
    { toggle: true, text: "Highlight words", checked: prefs.highlight, onToggle: (on) => setFlag("highlight", on) },
    { toggle: true, text: "Follow along", checked: prefs.follow, onToggle: (on) => setFlag("follow", on) },
    { sep: true },
    ...(P.id ? [{ action: true, text: "Generate again", icon: "refresh", onPick: regenerate }] : []),
    { action: true, text: "Read aloud settings", icon: "settings", onPick: openSettings },
  ];

  // T3 Code's router (TanStack Router) registers itself as window.__TSR_ROUTER__. Its navigate() is the
  // same call T3 Code's own "Voice settings" links make. A raw pushState confuses its history index.
  function openSettings() {
    closeMenu();
    const router = window.__TSR_ROUTER__;
    if (!router || typeof router.navigate !== "function") return;
    Promise.resolve()
      .then(() => router.navigate({ to: "/settings/voice" }))
      .catch(() => {})
      .then(() => {
        let tries = 0;
        const reveal = () => {
          const sec = document.getElementById("t3ra-settings");
          if (sec) sec.scrollIntoView({ behavior: "smooth", block: "start" });
          else if (++tries < 30) setTimeout(reveal, 100); // the section is added on the next scan
        };
        reveal();
      });
  }

  // ---------------------------------------------------------------- the strip

  const strip = (() => {
    const B = (variant, size, act, label, inner, extra = "") =>
      `<button type="button" class="${cls(C.btn, size, variant, extra)}" data-act="${act}" aria-label="${label}">${inner}</button>`;
    const root = document.createElement("div");
    root.className = "t3ra-strip surface-glass";
    root.hidden = true;
    root.setAttribute("data-t3ra", "");
    root.setAttribute("role", "region");
    root.setAttribute("aria-label", "Read aloud");
    root.innerHTML = [
      B("t3ra-night", C.iconSmPill, "play", "Play", ""),
      B(C.ghostMuted, C.iconSmPill, "back", "Back 10 seconds", icon("back"), "t3ra-hide-sm"),
      B(C.ghostMuted, C.iconSmPill, "fwd", "Forward 10 seconds", icon("fwd"), "t3ra-hide-sm"),
      `<span class="t3ra-time" data-el="cur">0:00</span>`,
      `<div class="t3ra-track" data-el="track" role="slider" tabindex="0" aria-label="Seek" aria-valuemin="0" aria-valuemax="0" aria-valuenow="0">`
        + `<div class="t3ra-rail"><div class="t3ra-buf" data-el="buf"></div><div class="t3ra-fill" data-el="fill"></div></div>`
        + `<div class="t3ra-thumb" data-el="thumb"></div></div>`,
      `<div class="t3ra-msg" data-el="msg" hidden></div>`,
      `<span class="t3ra-time" data-el="dur">0:00</span>`,
      `<span class="t3ra-sep"></span>`,
      B(C.ghostMuted, C.smPill, "speed", "Playback speed", "", "text-xs sm:text-xs tabular-nums").replace("<button", '<button aria-haspopup="menu"'),
      B(C.ghostMuted, C.iconSmPill, "vol", "Mute", ""),
      B(C.ghostMuted, C.iconSmPill, "options", "Voice and reading options", icon("sliders")).replace("<button", '<button aria-haspopup="menu"'),
      B(C.ghostMuted, C.iconSmPill, "close", "Stop and close", icon("x")),
    ].join("");
    const q = (s) => root.querySelector(s);
    const el = {
      play: q('[data-act="play"]'), speed: q('[data-act="speed"]'), cur: q('[data-el="cur"]'), dur: q('[data-el="dur"]'),
      track: q('[data-el="track"]'), buf: q('[data-el="buf"]'), fill: q('[data-el="fill"]'), thumb: q('[data-el="thumb"]'),
      msg: q('[data-el="msg"]'), vol: q('[data-act="vol"]'),
    };
    // The volume button opens its slider on hover, so it gets no tooltip.
    root.querySelectorAll('button:not([data-act="vol"])').forEach(withTip);

    // volume: a vertical slider that drops down from the volume button on hover
    const vol = document.createElement("div");
    vol.className = "t3ra-vol surface-glass";
    vol.hidden = true;
    vol.setAttribute("data-t3ra", "");
    vol.innerHTML = `<div class="t3ra-vol-track" role="slider" tabindex="0" aria-orientation="vertical" aria-label="Volume" aria-valuemin="0" aria-valuemax="100">`
      + `<div class="t3ra-vol-rail"><div class="t3ra-vol-fill"></div></div><div class="t3ra-vol-thumb"></div></div>`
      + `<span class="t3ra-vol-val" aria-hidden="true"></span>`;
    const vt = vol.querySelector(".t3ra-vol-track");
    const vfill = vol.querySelector(".t3ra-vol-fill");
    const vthumb = vol.querySelector(".t3ra-vol-thumb");
    const vval = vol.querySelector(".t3ra-vol-val");
    let volTimer = 0;
    let volDrag = false;
    function paintVol() {
      const v = prefs.muted ? 0 : prefs.volume;
      const name = v === 0 ? "volX" : v < 0.5 ? "vol1" : "volume";
      if (el.vol.dataset.icon !== name) { el.vol.innerHTML = icon(name); el.vol.dataset.icon = name; }
      el.vol.setAttribute("aria-label", v === 0 ? "Unmute" : "Mute");
      vfill.style.clipPath = `inset(${(1 - v) * 100}% 0 0 0 round 9999px)`;
      vthumb.style.top = `${(1 - v) * 100}%`;
      vval.textContent = String(Math.round(v * 100));
      vt.setAttribute("aria-valuenow", String(Math.round(v * 100)));
      vt.setAttribute("aria-valuetext", v === 0 ? "Muted" : `${Math.round(v * 100)}%`);
    }
    function openVol() {
      clearTimeout(volTimer);
      if (!vol.hidden || root.hasAttribute("data-menu-open")) return;
      hideTip();
      mount();
      layer.appendChild(vol);
      vol.hidden = false;
      const b = el.vol.getBoundingClientRect();
      const s = root.getBoundingClientRect();
      const h = vol.offsetHeight;
      const below = s.bottom + 6 + h < innerHeight - 4;
      vol.style.left = `${Math.round(b.left + b.width / 2 - vol.offsetWidth / 2)}px`;
      vol.style.top = `${Math.round(below ? s.bottom + 6 : s.top - h - 6)}px`;
      vol.style.transformOrigin = below ? "50% 0" : "50% 100%";
      animateIn(vol);
    }
    const closeVol = () => { clearTimeout(volTimer); volDrag = false; vol.hidden = true; };
    const closeVolSoon = () => { clearTimeout(volTimer); volTimer = setTimeout(() => { if (!volDrag) vol.hidden = true; }, 220); };
    const volKeys = (e) => {
      const step = { ArrowUp: 0.05, ArrowRight: 0.05, ArrowDown: -0.05, ArrowLeft: -0.05, PageUp: 0.2, PageDown: -0.2 }[e.key];
      const v = prefs.muted ? 0 : prefs.volume;
      if (step) { e.preventDefault(); setVolume(v + step); openVol(); }
      else if (e.key === "Home" && e.currentTarget === vt) { e.preventDefault(); setVolume(0); }
      else if (e.key === "End" && e.currentTarget === vt) { e.preventDefault(); setVolume(1); }
      else if (e.key === "Escape") { closeVol(); el.vol.focus(); }
    };
    const wheelVol = (e) => { e.preventDefault(); setVolume((prefs.muted ? 0 : prefs.volume) + (e.deltaY < 0 ? 0.05 : -0.05)); };
    el.vol.addEventListener("pointerenter", (e) => { if (e.pointerType !== "touch") openVol(); });
    el.vol.addEventListener("pointerleave", closeVolSoon);
    el.vol.addEventListener("keydown", volKeys);
    el.vol.addEventListener("wheel", wheelVol, { passive: false });
    vol.addEventListener("pointerenter", () => clearTimeout(volTimer));
    vol.addEventListener("pointerleave", closeVolSoon);
    vol.addEventListener("wheel", wheelVol, { passive: false });
    vt.addEventListener("keydown", volKeys);
    const vfrac = (e) => {
      const r = vt.getBoundingClientRect();
      return Math.min(1, Math.max(0, (r.bottom - e.clientY) / Math.max(1, r.height)));
    };
    vt.addEventListener("pointerdown", (e) => {
      volDrag = true;
      vt.setPointerCapture(e.pointerId);
      vt.setAttribute("data-drag", "");
      setVolume(vfrac(e), false);
    });
    vt.addEventListener("pointermove", (e) => { if (volDrag) setVolume(vfrac(e), false); });
    const endVolDrag = () => {
      if (!volDrag) return;
      volDrag = false;
      vt.removeAttribute("data-drag");
      savePrefs();
      if (!vol.matches(":hover") && !el.vol.matches(":hover")) closeVolSoon();
    };
    vt.addEventListener("pointerup", endVolDrag);
    vt.addEventListener("pointercancel", endVolDrag);
    // A tap outside closes it on touch screens, where there is no hover to leave.
    document.addEventListener("pointerdown", (e) => {
      if (!vol.hidden && !vol.contains(e.target) && !el.vol.contains(e.target)) closeVol();
    }, true);
    paintVol();

    // seek bar: pointer drag with live preview, keyboard steps
    let drag = false;
    let preview = 0;
    const frac = (e) => {
      const r = el.track.getBoundingClientRect();
      return Math.min(1, Math.max(0, (e.clientX - r.left) / Math.max(1, r.width)));
    };
    el.track.addEventListener("pointerdown", (e) => {
      if (!P.audio || duration() <= 0) return;
      drag = true;
      el.track.setPointerCapture(e.pointerId);
      el.track.setAttribute("data-drag", "");
      preview = frac(e) * duration();
      progress(true);
    });
    el.track.addEventListener("pointermove", (e) => { if (drag) { preview = frac(e) * duration(); progress(true); } });
    const endDrag = (e, commit) => {
      if (!drag) return;
      drag = false;
      el.track.removeAttribute("data-drag");
      if (commit) seekTo(frac(e) * duration());
    };
    el.track.addEventListener("pointerup", (e) => endDrag(e, true));
    el.track.addEventListener("pointercancel", (e) => endDrag(e, false));
    el.track.addEventListener("keydown", (e) => {
      if (!P.audio) return;
      const t = P.audio.currentTime;
      const step = { ArrowLeft: -5, ArrowRight: 5, PageDown: -30, PageUp: 30 }[e.key];
      if (step) { e.preventDefault(); seekTo(t + step); }
      else if (e.key === "Home") { e.preventDefault(); seekTo(0); }
      else if (e.key === "End") { e.preventDefault(); seekTo(duration()); }
    });

    root.addEventListener("click", (e) => {
      const b = e.target.closest("[data-act]");
      if (!b) return;
      const act = b.getAttribute("data-act");
      if (act === "play") { if (P.id) toggle(P.id); }
      else if (act === "back") seekTo((P.audio ? P.audio.currentTime : 0) - 10);
      else if (act === "fwd") seekTo((P.audio ? P.audio.currentTime : 0) + 10);
      else if (act === "close") stop(true);
      else if (act === "vol") { toggleMute(); if (e.pointerType === "touch") openVol(); }
      else if (act === "speed") { closeVol(); openMenu(b, speedMenu()); }
      else if (act === "options") { closeVol(); openMenu(b, optionsMenu()); }
    });

    let lastSecond = -1;
    let lastDur = -1;
    function progress(force) {
      const d = duration();
      const t = drag ? preview : (P.audio ? P.audio.currentTime : 0);
      const f = d > 0 ? Math.min(1, t / d) : 0;
      el.fill.style.clipPath = `inset(0 ${(1 - f) * 100}% 0 0 round 9999px)`;
      el.thumb.style.left = `${f * 100}%`;
      const sec = Math.floor(t);
      if (force || sec !== lastSecond) {
        lastSecond = sec;
        el.cur.textContent = fmt(t);
        el.track.setAttribute("aria-valuenow", String(Math.round(t)));
        el.track.setAttribute("aria-valuetext", `${fmt(t)} of ${fmt(d)}`);
      }
      const dd = Math.floor(d);
      if (force || dd !== lastDur) {
        lastDur = dd;
        el.dur.textContent = fmt(d);
        el.track.setAttribute("aria-valuemax", String(Math.round(d)));
        // Generated audio shows as the lighter part of the bar while the reply is still being made.
        const total = P.serverDone ? d : Math.max(d, bufferedEnd());
        el.buf.style.transform = `scaleX(${P.audio && total > 0 ? (P.serverDone ? 1 : Math.min(1, bufferedEnd() / (total + 8))) : 0})`;
      }
    }

    let shown = false;
    function show() {
      if (!root.isConnected) document.body.appendChild(root);
      if (shown) return;
      shown = true;
      root.hidden = false;
      root.setAttribute("data-starting", "");
      placeStrip();
      requestAnimationFrame(() => requestAnimationFrame(() => root.removeAttribute("data-starting")));
    }
    function hide() { shown = false; root.hidden = true; closeMenu(); hideTip(); closeVol(); }

    function paint() {
      const loading = P.state === "loading";
      const playing = P.state === "playing";
      const name = loading ? "loader" : playing ? "pauseFill" : "playFill";
      if (el.play.dataset.icon !== name) { el.play.innerHTML = icon(name); el.play.dataset.icon = name; }
      el.play.setAttribute("aria-label", loading ? "Cancel" : playing ? "Pause" : P.state === "ended" ? "Play again" : "Play");
      el.play.disabled = !P.id;
      const sp = `${prefs.speed}×`;
      if (el.speed.textContent !== sp) el.speed.textContent = sp;
      const err = P.error;
      el.msg.hidden = !err;
      el.track.hidden = Boolean(err);
      el.cur.hidden = Boolean(err);
      el.dur.hidden = Boolean(err);
      if (err && el.msg.dataset.text !== err) { el.msg.innerHTML = icon("alert") + `<span>${esc(err)}</span>`; el.msg.dataset.text = err; }
      progress(true);
    }
    return { root, show, hide, paint, progress, paintVol, closeVol, get shown() { return shown; } };
  })();

  applyColors();

  function render() {
    strip.paint();
    renderSettings();
  }

  // ---------------------------------------------------------------- settings section (Settings > Voice)

  let health = null;
  let healthAt = 0;
  function refreshHealth(force = false) {
    if (!force && Date.now() - healthAt < 5000) return Promise.resolve();
    healthAt = Date.now();
    return fetch(`http://127.0.0.1:${PORT}/health`, { cache: "no-store" })
      .then((r) => r.json()).then((h) => { health = h; renderSettings(); })
      .catch(() => { health = null; renderSettings(); });
  }

  // The settings socket: keys, voices, models and saved audio (dat3uzzo/tts/messages.py).
  // A key goes to the helper only inside a key.set message body; no answer ever holds one.
  const SET = { ws: null, seq: 0, wait: new Map() };
  function ask(msg) {
    return new Promise((resolve) => {
      if (!TOKEN) return resolve({ type: "error", message: HELPER_DOWN });
      const id = ++SET.seq;
      SET.wait.set(id, resolve);
      if (!SET.ws || SET.ws.readyState > 1) {
        let sock;
        try { sock = new WebSocket(`ws://127.0.0.1:${PORT}/settings?token=${encodeURIComponent(TOKEN)}`); }
        catch { SET.wait.delete(id); return resolve({ type: "error", message: HELPER_DOWN }); }
        SET.ws = sock;
        sock.onmessage = (ev) => {
          let m = null;
          try { m = JSON.parse(ev.data); } catch { return; }
          const done = SET.wait.get(m.id);
          if (done) { SET.wait.delete(m.id); done(m); }
          else if (m.type === "error") { for (const r of SET.wait.values()) r(m); SET.wait.clear(); }
        };
        sock.onclose = () => {
          if (SET.ws === sock) SET.ws = null;
          for (const r of SET.wait.values()) r({ type: "error", message: HELPER_DOWN });
          SET.wait.clear();
        };
      }
      const ws = SET.ws;
      const send = () => { try { ws.send(JSON.stringify({ ...msg, id })); } catch { /* onclose answers */ } };
      if (ws.readyState === 1) send(); else ws.addEventListener("open", send, { once: true });
    });
  }

  const S = { keys: null, store: "", keyError: "", keyMode: "", cache: null, cacheError: "", clearArm: 0,
              voiceError: {}, modelError: "", test: null, testStatus: "" };
  const KEY_RE = /^(?![a-z][a-z\d+.-]*:\/\/)[!-~]{1,4096}$/i; // T3 Code's CloudSpeechApiKey pattern
  const fmtBytes = (b) => (b >= 1 << 30 ? `${+(b / 2 ** 30).toFixed(1)} GB` : `${Math.round(b / 2 ** 20)} MB`);

  function gotKeys(r) {
    if (r.type === "key.status") { S.keys = r.providers || null; S.store = r.store || ""; S.keyError = ""; }
    else S.keyError = r.message || "The helper could not do that.";
    renderSettings();
    return r;
  }
  const loadKeys = () => ask({ type: "key.status" }).then(gotKeys);
  function loadCache(msg = { type: "cache.status" }) {
    return ask(msg).then((r) => {
      if (r.type === "cache") { S.cache = r; S.cacheError = r.error || ""; } else S.cacheError = r.message || "";
      renderSettings();
    });
  }
  const t3State = (pid = prefs.provider) => (S.keys && S.keys[pid] ? S.keys[pid].t3 : null);
  const ownSaved = (pid = prefs.provider) => Boolean(S.keys && S.keys[pid] && S.keys[pid].own);
  // The source the helper uses: the chosen one, else T3 Code's key when it holds one (tts.select).
  const effSource = () => {
    if (!t3Slot()) return "own";
    if (curKeySource()) return curKeySource();
    return S.keys && !["value", "reference"].includes(t3State()) ? "own" : "t3";
  };
  const openaiKeyKnown = () => !S.keys || ["value", "reference"].includes(t3State("openai")) || ownSaved("openai");

  function settingRow(title, desc, control) {
    const row = document.createElement("div");
    row.className = "t3ra-set-row";
    row.innerHTML = `<div class="t3ra-set-text"><div class="t3ra-set-title"></div><div class="t3ra-set-desc"></div></div><div class="t3ra-set-ctl"></div>`;
    row.querySelector(".t3ra-set-title").textContent = title;
    row.querySelector(".t3ra-set-desc").innerHTML = desc;
    row.querySelector(".t3ra-set-ctl").append(...[].concat(control));
    return row;
  }
  const setHtml = (el, html) => { if (el && el.dataset.html !== html) { el.innerHTML = html; el.dataset.html = html; } };
  const setText = (el, text) => { if (el && el.textContent !== text) el.textContent = text; };

  function switchControl(key, label) {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "switch");
    b.setAttribute("aria-label", label);
    b.dataset.pref = key;
    b.className = C.switchRoot;
    b.innerHTML = `<span class="${C.switchThumb}"></span>`;
    b.addEventListener("click", () => setFlag(key, !prefs[key]));
    return b;
  }

  function selectButton(key, label, items) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = cls(C.btn, C.sm, C.outline);
    b.setAttribute("aria-haspopup", "menu");
    b.setAttribute("aria-label", label);
    b.dataset.select = key;
    b.addEventListener("click", () => openMenu(b, items()));
    return b;
  }

  function button(text, variant, onClick, act = "") {
    const b = document.createElement("button");
    b.type = "button";
    b.className = cls(C.btn, C.sm, variant);
    b.textContent = text;
    if (act) b.dataset.act = act;
    b.addEventListener("click", onClick);
    return b;
  }

  // ---- menu items for the new rows

  const providerItems = () => PROVIDER_IDS.map((pid) => ({
    radio: true, value: pid, current: prefs.provider, text: providerLabel(pid), onPick: setProvider,
    note: providerModes(pid).includes("adapt") ? "Exact or Adapt" : pid === "openrouter" ? "Exact, many models" : "Exact",
  }));
  const keySourceItems = () => [
    { radio: true, value: "t3", current: effSource(), text: `T3 Code's saved ${providerName()} key`, onPick: setKeySource },
    { radio: true, value: "own", current: effSource(), text: "Separate key for read-aloud", onPick: setKeySource },
  ];
  function modelItems() {
    const list = modelList();
    if (!list) { loadModels(); return [{ label: S.modelError || "Loading models…" }]; }
    const items = [];
    let maker = "";
    for (const m of prefs.provider === "openrouter" ? [...list].sort((a, b) => a.id.localeCompare(b.id)) : list) {
      const by = prefs.provider === "openrouter" ? m.id.split("/")[0] : "";
      if (by && by !== maker) { maker = by; items.push({ label: by }); }
      items.push({ radio: true, value: m.id, current: curModel(), text: m.label || m.id, note: prefs.provider === "openrouter" ? "" : m.note || "", onPick: setModel });
    }
    return items;
  }
  function voiceItemsAll() {
    const list = voiceList();
    if (!list) { loadVoices(); return [{ label: S.voiceError[listKey()] || "Loading voices…" }]; }
    return list.map((v) => ({ radio: true, value: v.id, current: curVoice(), text: v.label || v.id, note: v.note || "", onPick: setVoice }));
  }
  const modeItems = () => [
    { radio: true, value: "exact", current: curMode(), text: "Exact", onPick: (v) => setVoiceOrMode("mode", v) },
    { radio: true, value: "adapt", current: curMode(), text: "Adapt for listening", onPick: (v) => setVoiceOrMode("mode", v) },
  ];
  const capItems = () => ((S.cache && S.cache.caps) || [256, 512, 1024, 2048, 5120, 10240].map((m) => m * 2 ** 20)).map((c) => ({
    radio: true, value: c, current: S.cache ? S.cache.cap : 2 ** 30, text: fmtBytes(c), onPick: (v) => loadCache({ type: "cache.cap", cap: v }),
  }));

  // ---- actions

  function clearSaved() {
    if (S.clearArm > Date.now()) {
      S.clearArm = 0;
      loadCache({ type: "cache.clear" });
      return;
    }
    S.clearArm = Date.now() + 4000;
    renderSettings();
    setTimeout(renderSettings, 4100);
  }

  function saveKey(input) {
    const value = input.value.trim();
    input.value = ""; // the key leaves the page now; it is never kept here
    if (!KEY_RE.test(value)) { S.keyError = "That is not an API key. Paste the key itself, with no spaces, URL or reference."; return renderSettings(); }
    const pid = prefs.provider;
    ask({ type: "key.set", provider: pid, key: value }).then(gotKeys).then((r) => {
      if (r.type === "key.status") { S.keyMode = ""; loadVoices(pid, true); renderSettings(); }
    });
  }

  function stopTest() {
    const t = S.test;
    S.test = null;
    if (!t) return;
    try { if (t.ws) t.ws.close(); } catch { /* closed */ }
    if (t.audio) t.audio.pause();
    if (t.url) URL.revokeObjectURL(t.url);
  }
  // One short sentence with the chosen service, model, voice and key: the only paid call settings make.
  function testVoice() {
    if (S.test) { stopTest(); S.testStatus = ""; return renderSettings(); }
    if (!TOKEN) { S.testStatus = HELPER_DOWN; return renderSettings(); }
    const t = { ws: null, chunks: [], audio: null, url: null, t0: performance.now(), first: 0 };
    S.test = t;
    S.testStatus = "Speaking…";
    renderSettings();
    const end = (status) => { if (S.test === t) { stopTest(); S.testStatus = status; renderSettings(); } };
    const play = () => {
      if (t.audio || S.test !== t) return;
      t.url = URL.createObjectURL(new Blob(t.chunks, { type: "audio/mpeg" }));
      const a = new Audio(t.url);
      t.audio = a;
      a.preservesPitch = true;
      a.volume = prefs.muted ? 0 : prefs.volume;
      a.playbackRate = prefs.speed;
      a.onended = () => end(`Test done. First audio after ${t.first.toFixed(1)} s.`);
      a.play().catch(() => end("This window could not play the test."));
    };
    let ws;
    try { ws = new WebSocket(`ws://127.0.0.1:${PORT}/speak?token=${encodeURIComponent(TOKEN)}`); }
    catch { return end(HELPER_DOWN); }
    t.ws = ws;
    ws.binaryType = "arraybuffer";
    ws.onopen = () => ws.send(JSON.stringify({ type: "speak", markdown: TEST_TEXT, ...choice() }));
    ws.onmessage = (ev) => {
      if (S.test !== t) return;
      if (typeof ev.data !== "string") {
        if (!t.first) t.first = (performance.now() - t.t0) / 1000;
        t.chunks.push(new Uint8Array(ev.data));
        return;
      }
      let m = {};
      try { m = JSON.parse(ev.data); } catch { return; }
      if (m.type === "error") end(m.message || "The test failed.");
      else if (m.type === "done") play();
    };
    ws.onclose = () => { if (S.test === t && !t.audio) { if (t.chunks.length) play(); else end(HELPER_DOWN); } };
  }

  // ---- the section

  function buildSettings(ref) {
    const sec = document.createElement("section");
    sec.id = "t3ra-settings";
    sec.className = ref.className;
    sec.setAttribute("data-t3ra", "");
    const header = ref.firstElementChild && ref.firstElementChild.cloneNode(true);
    if (header && header.querySelector("h2")) {
      header.querySelectorAll("button, svg").forEach((n) => n.remove());
      header.querySelector("h2").textContent = "Read aloud";
      sec.appendChild(header);
    }
    const card = ref.querySelector('[data-slot="settings-group"]')?.cloneNode(false) || document.createElement("div");
    card.removeAttribute("id");
    card.setAttribute("data-t3ra", "");
    const radios = (key, list) => () => list.map(([value, text, ic]) => ({
      radio: true, value, current: prefs[key], text, icon: ic,
      onPick: (v) => (key === "speed" ? setSpeed(v) : setColors(v)),
    }));
    const tagged = (name, row) => { row.dataset.row = name; return row; };
    card.append(
      tagged("provider", settingRow("Speak with", "Where the reply is turned into speech.",
        selectButton("provider", "Speak with", providerItems))),
      tagged("keysrc", settingRow("API key", "", selectButton("keySource", "API key", keySourceItems))),
      tagged("ownkey", settingRow("Separate key", "", [])),
      tagged("model", settingRow("Model", "", selectButton("model", "Model", modelItems))),
      tagged("voice", settingRow("Voice", "", [
        selectButton("voice", "Voice", voiceItemsAll),
        button("Test voice", C.outline, testVoice, "test"),
      ])),
      tagged("mode", settingRow("Reading mode", "", selectButton("mode", "Reading mode", modeItems))),
      settingRow("Speed", "Playback speed. Changing it in the player saves it here too. Saved audio plays at any speed without being made again.",
        selectButton("speed", "Speed", radios("speed", SPEEDS.map((s) => [s, `${s}×`])))),
      tagged("highlight", settingRow("Highlight words", "", switchControl("highlight", "Highlight words"))),
      settingRow("Follow along", "While audio plays, scrolls the chat to keep the spoken word in view. If you scroll away, it waits until the spoken line is back in view, or until you press play or seek. It never scrolls while paused.",
        switchControl("follow", "Follow along")),
      settingRow("Player colours",
        `<b>Match T3 Code build</b> uses the Nightly colours in T3 Code Nightly and your theme's accent colour in the other builds. This window is ${BUILD_STAGE ? esc(BUILD_STAGE) : "not the desktop app"}.`,
        selectButton("colors", "Player colours", radios("colors", Object.entries(COLOR_LABELS)))),
      tagged("cache", settingRow("Saved audio", "", [
        selectButton("cacheCap", "Saved audio limit", capItems),
        button("Clear saved audio", C.outline, clearSaved, "clear"),
      ])),
      settingRow("Helper", "", []),
    );
    card.lastElementChild.dataset.health = "";
    sec.appendChild(card);
    return sec;
  }

  // The separate-key row: "Saved" with Replace and Remove, a confirm step, or the key field.
  function renderKeyRow(row) {
    const show = !t3Slot() || effSource() === "own";
    row.hidden = !show;
    if (!show) return;
    const name = providerName();
    const store = S.store || "the system key store";
    const saved = ownSaved();
    const mode = S.keyMode === "confirm" && saved ? "confirm" : saved && S.keyMode !== "edit" ? "saved" : "edit";
    setText(row.querySelector(".t3ra-set-title"), t3Slot() ? "Separate key" : "API key");
    const desc = mode === "saved" ? `Saved in ${store} on this PC. Anyone who uses this Windows account can bill this key.`
      : mode === "confirm" ? `Remove the ${name} key from this PC?`
      : `Paste your ${name} API key. The helper keeps it in ${store}, never in a file or in this page.`;
    setHtml(row.querySelector(".t3ra-set-desc"), esc(desc) + (S.keyError ? ` <span class="t3ra-bad">${esc(S.keyError)}</span>` : ""));
    const ctl = row.querySelector(".t3ra-set-ctl");
    const k = `${prefs.provider}|${mode}`;
    if (ctl.dataset.mode === k) return;
    ctl.dataset.mode = k;
    const again = () => { renderSettings(); };
    if (mode === "saved") {
      const word = document.createElement("span");
      word.className = "text-sm text-muted-foreground";
      word.textContent = "Saved";
      ctl.replaceChildren(word,
        button("Replace", C.outline, () => { S.keyMode = "edit"; S.keyError = ""; again(); ctl.querySelector("input")?.focus(); }),
        button("Remove", C.ghostMuted, () => { S.keyMode = "confirm"; again(); }));
    } else if (mode === "confirm") {
      ctl.replaceChildren(
        button("Cancel", C.outline, () => { S.keyMode = ""; again(); }),
        button("Remove key", cls(C.outline, "t3ra-danger"), () => {
          const pid = prefs.provider;
          ask({ type: "key.remove", provider: pid }).then(gotKeys).then(() => { S.keyMode = ""; VOICE_LISTS[listKey(pid)] = undefined; renderSettings(); });
        }));
    } else {
      const wrap = document.createElement("span");
      wrap.className = cls(C.inputControl, "t3ra-key");
      wrap.dataset.slot = "input-control";
      const input = document.createElement("input");
      input.type = "password";
      input.className = C.input;
      input.dataset.slot = "input";
      input.autocomplete = "off";
      input.spellcheck = false;
      input.maxLength = 4096;
      input.placeholder = saved ? "Stored secret, enter a new value to replace" : "Enter API key";
      input.setAttribute("aria-label", `${name} API key`);
      wrap.appendChild(input);
      const save = button("Save key", C.primary, () => saveKey(input));
      save.disabled = true;
      input.addEventListener("input", () => { save.disabled = !KEY_RE.test(input.value.trim()); });
      input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !save.disabled) { e.preventDefault(); saveKey(input); } });
      ctl.replaceChildren(wrap, save, ...(saved ? [button("Cancel", C.outline, () => { input.value = ""; S.keyMode = ""; again(); })] : []));
    }
  }

  function renderSettings() {
    const sec = document.getElementById("t3ra-settings");
    if (!sec) return;
    const pid = prefs.provider;
    const name = providerName();
    const modes = providerModes();
    const models = modelList() || [];
    const label = {
      provider: providerLabel(), keySource: effSource() === "t3" ? `T3 Code's ${name} key` : "Separate key",
      model: models.find((m) => m.id === curModel())?.label || curModel() || "Default",
      voice: voiceLabel(), mode: curMode() === "adapt" ? "Adapt for listening" : "Exact",
      speed: `${prefs.speed}×`, colors: COLOR_LABELS[prefs.colors], cacheCap: S.cache ? fmtBytes(S.cache.cap) : "1 GB",
    };
    sec.querySelectorAll("[data-select]").forEach((b) => {
      setHtml(b, `<span>${esc(label[b.dataset.select])}</span>${icon("chevron", "size-3.5 opacity-60")}`);
    });
    sec.querySelectorAll("[role=switch]").forEach((b) => {
      const on = Boolean(prefs[b.dataset.pref]);
      b.setAttribute("aria-checked", String(on));
      for (const n of [b, b.firstElementChild]) {
        n.toggleAttribute("data-checked", on);
        n.toggleAttribute("data-unchecked", !on);
      }
    });
    const row = (n) => sec.querySelector(`[data-row="${n}"]`);
    const desc = (n) => row(n)?.querySelector(".t3ra-set-desc");

    // API key source (only for the services T3 Code holds keys for)
    row("keysrc").hidden = !t3Slot();
    const st = t3State();
    setHtml(desc("keysrc"), effSource() === "own" ? "Uses the separate key below."
      : st === "value" ? "Uses the key saved under Transcription above."
      : st === "reference" ? "Uses T3 Code's 1Password reference. 1Password may ask you to unlock it."
      : st === "missing" ? `<span class="t3ra-bad">T3 Code has no ${esc(name)} key. Save one under Transcription above, or choose a separate key.</span>`
      : "Checking for a key…");
    renderKeyRow(row("ownkey"));

    // Model: hidden when the service has a single fixed model (xAI)
    row("model").hidden = pid !== "openrouter" && models.length <= 1;
    const mnote = models.find((m) => m.id === curModel())?.note || "";
    setHtml(desc("model"), pid === "openrouter"
      ? `Speech models on OpenRouter. Prices differ per model.${S.modelError ? ` <span class="t3ra-bad">${esc(S.modelError)}</span>` : ""}`
      : esc(mnote || "The model that speaks."));

    // Voice and the test
    const verr = S.voiceError[listKey()] || "";
    setHtml(desc("voice"), (pid === "openai" ? "OpenAI voice. Marin and Cedar sound the most natural."
      : pid === "openrouter" ? "Voices this model offers." : `Voices from your ${esc(name)} account.`)
      + (verr ? ` <span class="t3ra-bad">${esc(verr)}</span>` : ""));
    setText(sec.querySelector('[data-act="test"]'), S.test ? "Stop" : "Test voice");

    // Reading mode: a service that cannot rewrite text offers Exact only
    const modeBtn = sec.querySelector('[data-select="mode"]');
    if (modeBtn) modeBtn.disabled = modes.length < 2;
    setHtml(desc("mode"), modes.length < 2
      ? `${esc(providerLabel())} reads the text as written. <b>Adapt for listening</b> needs OpenAI Realtime, which rewrites the text.`
      : "<b>Exact</b> reads the reply word for word. <b>Adapt</b> cleans it up for listening: numbers, symbols, paths and tables. Both skip code blocks.");

    // Highlight: where the word timing comes from, and what it costs
    const timing = meta()?.caps?.timing || "whisper";
    const cost = timing === "native" ? `Timing comes from ${esc(name)} with the audio, at no extra cost.`
      : pid === "openai" ? "Adds OpenAI speech-to-text timing, about $0.006 per audio minute."
      : openaiKeyKnown() ? "Adds OpenAI speech-to-text timing with your OpenAI key, about $0.006 per audio minute."
      : "No OpenAI key for timing, so the highlight is estimated.";
    setHtml(desc("highlight"), `Marks each word while it is spoken.<span class="t3ra-swatch" style="background:color-mix(in oklab,var(--primary) 40%,transparent)"></span>as written<span class="t3ra-swatch" style="background:color-mix(in oklab,var(--warning) 45%,transparent)"></span>said differently. ${cost}`);

    // Saved audio
    const c = S.cache;
    setHtml(desc("cache"), "Spoken replies are stored on this PC, so playing one again costs nothing and needs no key."
      + (c ? ` ${fmtBytes(c.bytes)} of ${fmtBytes(c.cap)} used. The oldest go first when it is full.` : "")
      + (S.cacheError ? ` <span class="t3ra-bad">${esc(S.cacheError)}</span>` : ""));
    setText(sec.querySelector('[data-act="clear"]'), S.clearArm > Date.now() ? "Click again to clear" : "Clear saved audio");

    // Helper status line
    const hrow = sec.querySelector("[data-health] .t3ra-set-desc");
    if (hrow) {
      const key = effSource() === "t3" ? `T3 Code's ${name} key` : ownSaved() ? "separate key saved" : "no separate key yet";
      setText(hrow, health
        ? `Running on this PC. Speaks with ${providerLabel()}${curModel() && pid !== "xai" ? `, ${curModel()}` : ""}, ${key}.${S.testStatus ? ` ${S.testStatus}` : ""}`
        : "Not running. Start it with: python -m dat3uzzo install (in the daT3uzzo folder).");
    }
  }

  let settingsLoaded = false;
  function injectSettings() {
    const ref = document.querySelector("section#voice-input");
    if (!ref || !ref.parentElement) return;
    let sec = document.getElementById("t3ra-settings");
    if (!sec) {
      sec = buildSettings(ref);
      ref.parentElement.appendChild(sec);
      renderSettings();
      settingsLoaded = false;
    } else if (sec.parentElement !== ref.parentElement || sec !== ref.parentElement.lastElementChild) {
      ref.parentElement.appendChild(sec);
    }
    if (!settingsLoaded) {
      settingsLoaded = true;
      refreshHealth(true).then(() => {
        loadCache();
        loadKeys().then(() => loadModels()).then(() => loadVoices());
      });
    } else refreshHealth();
  }

  // ---------------------------------------------------------------- buttons on replies

  function findActionBar(row) {
    const buttons = [...row.querySelectorAll("button")]
      .filter((b) => !b.hasAttribute("data-t3ra-btn") && !b.closest("[data-assistant-citation-source]"));
    const ref = buttons.find((b) => ACTION_LABELS.includes(b.getAttribute("aria-label")))
      || buttons.find((b) => b.parentElement && b.parentElement.classList.contains("tabular-nums"));
    return ref ? { bar: ref.parentElement, ref } : null;
  }

  function buttonState(id) {
    if (P.id !== id) return "idle";
    return P.state === "loading" ? "loading" : P.state === "playing" ? "playing" : P.state === "paused" ? "paused" : "idle";
  }

  function paintButton(btn) {
    const state = buttonState(btn.getAttribute("data-t3ra-btn"));
    if (btn.dataset.state === state) return;
    btn.dataset.state = state;
    const name = { idle: "volume", loading: "loader", playing: "pause", paused: "play" }[state];
    btn.innerHTML = icon(name, btn.dataset.svgClass || "size-3");
    btn.setAttribute("aria-label", { idle: "Read aloud", loading: "Cancel reading", playing: "Pause reading", paused: "Resume reading" }[state]);
  }

  function updateButtons() {
    document.querySelectorAll("[data-t3ra-btn]").forEach(paintButton);
  }

  function scan() {
    for (const row of document.querySelectorAll("[data-timeline-row-id][data-message-id]")) {
      const kind = row.getAttribute("data-timeline-row-kind");
      if (!(kind === "assistant-meta" || (kind === "message" && row.getAttribute("data-message-role") === "assistant"))) continue;
      let btn = row.querySelector("[data-t3ra-btn]");
      if (btn && !(btn.nextElementSibling && btn.nextElementSibling.tagName === "BUTTON")) continue; // in place
      const found = findActionBar(row);
      if (!found) continue;
      const { bar, ref } = found;
      if (!btn) {
        const id = row.getAttribute("data-message-id");
        btn = document.createElement("button");
        btn.type = "button";
        btn.className = ref.className;
        btn.setAttribute("data-t3ra-btn", id);
        const svgClass = ((ref.querySelector("svg") || { getAttribute: () => "" }).getAttribute("class") || "")
          .split(/\s+/).filter((c) => c && !c.startsWith("lucide")).join(" ");
        btn.dataset.svgClass = svgClass || "size-3";
        btn.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); hideTip(); toggle(id); });
        withTip(btn);
        paintButton(btn);
      }
      // after the last native button, before the timestamp
      const natives = [...bar.children].filter((c) => c.tagName === "BUTTON" && !c.hasAttribute("data-t3ra-btn"));
      const last = natives[natives.length - 1];
      if (last && last.nextElementSibling !== btn) last.after(btn);
    }
    injectSettings();
    placeStrip();
  }

  let viewport = null;
  function placeStrip() {
    if (!strip.shown) return;
    const lane = document.querySelector("[data-timeline-root]");
    if (lane && (!viewport || !viewport.isConnected || !viewport.contains(lane))) {
      viewport = null;
      for (let el = lane.parentElement; el && el !== document.body; el = el.parentElement) {
        const oy = getComputedStyle(el).overflowY;
        if (oy === "auto" || oy === "scroll") { viewport = el; break; }
      }
    }
    const rect = (viewport || lane) ? (viewport || lane).getBoundingClientRect() : null;
    const cx = rect && rect.width > 0 ? rect.left + rect.width / 2 : innerWidth / 2;
    const top = rect && rect.height > 0 ? Math.max(rect.top, 0) + 10 : 12;
    strip.root.style.left = `${Math.round(cx)}px`;
    strip.root.style.top = `${Math.round(top)}px`;
  }

  let scanTimer = 0;
  const scheduleScan = () => {
    if (!scanTimer) scanTimer = setTimeout(() => {
      scanTimer = 0;
      const t0 = performance.now();
      try { scan(); } catch { /* never break T3 */ }
      const dt = performance.now() - t0;
      perf.scans += 1; perf.scanMs += dt; if (dt > perf.scanMaxMs) perf.scanMaxMs = dt;
    }, 200);
  };
  new MutationObserver(scheduleScan).observe(document.body, { childList: true, subtree: true });
  addEventListener("resize", () => { placeStrip(); closeMenu(); hideTip(); strip.closeVol(); HL.key = ""; }, { passive: true });
  render();
  scheduleScan();
  refreshHealth(); // the voice services' capabilities, for the player's Options menu

  // Read-only hook for tests and troubleshooting.
  window.__T3RA_API__ = {
    replyMarkdown,
    perf: () => ({ ...perf, frameAvgMs: perf.frames ? perf.frameMs / perf.frames : 0, scanAvgMs: perf.scans ? perf.scanMs / perf.scans : 0 }),
    state: () => ({ id: P.id, state: P.state, generating: P.generating, serverDone: P.serverDone, error: P.error,
                    time: P.audio ? P.audio.currentTime : 0, buffered: bufferedEnd(), duration: duration(),
                    rate: P.audio ? P.audio.playbackRate : prefs.speed, prefs: { ...prefs },
                    tokens: P.tokens.length, segs: P.segs.map((s) => s.slice()) }),
  };
})();
