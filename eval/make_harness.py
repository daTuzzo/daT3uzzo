"""Build a stand-in T3 Code page for testing the web script without a paired browser.

It copies the markup T3 Code uses for assistant rows (data-timeline-row-id, data-message-id,
data-assistant-citation-source, the meta row with the Copy button, the code-block wrapper with
data-language) and T3 Code's own stylesheet (see stylesheet()). Output: eval/out/harness.html

--remote: the same pages in eval/out/remote/ with no loader block, as a T3 server on another machine serves
them. There, only the userscript or the browser extension loads the add-ons (dat3uzzo/origins.py).
"""
import shutil
import sys
from pathlib import Path

from dat3uzzo import config, patcher

REMOTE = "--remote" in sys.argv[1:]
_args = [a for a in sys.argv[1:] if a != "--remote"]
OUT = Path(__file__).parent / "out" / ("remote" if REMOTE else "")


def stylesheet() -> str:
    """T3 Code's own stylesheet: a URL or a .css file from the command line, else the largest .css file of
    the newest version in the T3 home. A file is copied next to the pages, so they need no T3 server."""
    if _args and _args[0].startswith(("http://", "https://")):
        return _args[0]
    if _args:
        src = Path(_args[0])
    else:
        versions = sorted((config.t3_home() / "runtime" / "versions").glob("*/client/assets"))
        found = sorted(versions[-1].glob("*.css"), key=lambda f: f.stat().st_size) if versions else []
        if not found:
            sys.exit("No T3 Code stylesheet found. Pass a .css file or a URL, for example "
                     "http://127.0.0.1:3773/assets/<name>.css from a running T3 server.")
        src = found[-1]
    OUT.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, OUT / "t3.css")
    return "t3.css"


css = stylesheet()
LOADER = "" if REMOTE else patcher.loader_block(config.PORT, config.token())
BTN = ("inline-flex shrink-0 items-center justify-center gap-1 rounded-md text-xs font-medium "
       "size-6 text-muted-foreground transition-colors")
COPY_SVG = ('<svg class="lucide lucide-copy size-3" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
            'stroke="currentColor" stroke-width="2"><rect width="14" height="14" x="8" y="8" rx="2"/>'
            '<path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>')
REPLY_SVG = ('<svg class="lucide lucide-reply size-3" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
             'stroke="currentColor" stroke-width="2"><polyline points="9 17 4 12 9 7"/>'
             '<path d="M20 18v-2a4 4 0 0 0-4-4H4"/></svg>')

REPLY = """
<p>The tests failed on the Linux runner because of the time zone. Your code is fine, and the same tests pass on Windows.</p>
<h3><strong>What happened</strong></h3>
<ul>
<li><code>pytest</code> ran 1,318 tests in 6 min 5 s, and 7 failed, all in <code>tests/test_report.py</code>.</li>
<li>Each failure compares a date. The runner uses UTC, and the expected values assume UTC+2, so every date is one day off after 22:00.</li>
<li>The failures started with the change in #5107, which moved the report cut-off from midnight to 23:30.</li>
</ul>
<p>Set the time zone in the test job:</p>
<div class="chat-markdown-codeblock" data-language="yaml">
  <div data-thread-find-ignore=""><span>yaml</span><button aria-label="Copy code">copy</button></div>
  <pre class="shiki"><code><span class="line">env:</span>
<span class="line">  TZ: Europe/Athens</span>
<span class="line">  PYTHONHASHSEED: "0"</span></code></pre>
</div>
<p><strong>Cost and time</strong></p>
<div class="overflow-x-auto"><table>
<thead><tr><th>Item</th><th>Before</th><th>After</th></tr></thead>
<tbody>
<tr><td>Job time</td><td>~14 min</td><td>~9 min</td></tr>
<tr><td>Monthly runner cost</td><td>€3,200</td><td>€2,050</td></tr>
<tr><td>Retries per week</td><td>42</td><td>3</td></tr>
</tbody></table></div>
<p>The larger runner costs $45/month (≈ £36). Speed-up: 1.5x → enough for v3.1. Deadline: 2026-12-12, 09:00 EET.</p>
"""


def row(msg_id: str, body: str) -> str:
    return f"""
<div class="pb-4 group/assistant" data-timeline-row-id="row-{msg_id}" data-timeline-row-kind="message"
     data-message-id="{msg_id}" data-message-role="assistant">
  <div class="relative min-w-0 px-1 py-0.5">
    <div class="text-xs text-muted-foreground">T3 Code</div>
    <div data-thread-find-text="true">
      <div data-assistant-citation-source="{msg_id}" class="chat-markdown">{body}</div>
    </div>
    <div class="mt-1.5 flex items-center gap-2 text-xs tabular-nums opacity-100">
      <button type="button" class="{BTN}" aria-label="Copy message">{COPY_SVG}</button>
      <button type="button" class="{BTN}" aria-label="Reply to this response">{REPLY_SVG}</button>
      <p class="text-muted-foreground text-xs tabular-nums">7:45 PM</p>
    </div>
  </div>
</div>"""


html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>T3 harness</title>
<link rel="stylesheet" href="{css}">
<style>body{{background:var(--background);color:var(--foreground);font-family:system-ui,sans-serif;margin:0}}
.chat-markdown table{{border-collapse:collapse}} .chat-markdown td,.chat-markdown th{{border:1px solid var(--border);padding:4px 8px}}</style>
</head><body>
<div style="height:100vh;display:flex;flex-direction:column">
  <header style="height:48px;border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 16px">Harness thread</header>
  <div id="viewport" style="flex:1;overflow-y:auto">
    <div class="chat-content-lane" data-timeline-root="true" style="max-width:760px;margin:0 auto;padding:72px 16px 16px">
      <div class="pb-4" data-timeline-row-id="row-u1" data-timeline-row-kind="message" data-message-id="u1" data-message-role="user"><p>Why did the tests fail on Linux?</p></div>
      {row("msg-a", REPLY)}
      {row("msg-b", "<p>Short second reply with 3 numbers: 1, 22 and 333.</p>")}
    </div>
  </div>
</div>
{LOADER}
</body></html>"""

out = OUT / "harness.html"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(html, encoding="utf-8")
print(out)

# Stand-in Settings > Voice page, with T3 Code's section and card markup (settingsLayout.tsx, SettingsGroup.tsx).
settings = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>T3 settings harness</title>
<link rel="stylesheet" href="{css}">
<style>body{{background:var(--background);color:var(--foreground);font-family:system-ui,sans-serif;margin:0}}</style>
</head><body>
<div style="max-width:760px;margin:0 auto;padding:24px 16px;display:flex;flex-direction:column;gap:32px">
  <section id="voice-input" tabindex="-1" class="space-y-2.5">
    <div data-settings-scroll-target class="flex min-h-7 flex-wrap items-start justify-between gap-x-4 gap-y-1 px-3 sm:px-4">
      <div class="flex min-w-0 items-center gap-1"><h2 class="flex min-h-7 items-center gap-2 text-base font-semibold text-foreground">Input</h2></div>
    </div>
    <div data-slot="settings-group" class="relative overflow-visible text-foreground rounded-xl border border-border/60 bg-card/40 shadow-xs/5">
      <div class="px-4 py-3"><div class="text-sm font-medium">Microphone</div>
        <p class="mt-0.5 text-xs text-muted-foreground">Voice input uses the first connected microphone in this list.</p></div>
    </div>
  </section>
</div>
{LOADER}
</body></html>"""
(out.parent / "settings.html").write_text(settings, encoding="utf-8")
print(out.parent / "settings.html")

# Stand-in Settings > General page with the side navigation, for the daT3uzzo entry and section.
# Markup and class strings: SettingsSidebarNav.tsx, ui/sidebar.tsx, settingsLayout.tsx (SettingsSection,
# SettingsRow, SettingsPageContainer), SettingsGroup.tsx, WorkspacePageContainer.tsx.
NAV_BTN = ("peer/menu-button flex w-full cursor-pointer items-center gap-[var(--sidebar-control-gap)] overflow-hidden "
           "text-left outline-hidden ring-ring transition-[width,height,padding] hover:bg-sidebar-row-hover "
           "hover:text-sidebar-foreground focus-visible:ring-2 focus-visible:ring-inset active:bg-sidebar-row-active "
           "active:text-sidebar-foreground disabled:pointer-events-none disabled:opacity-64 aria-disabled:pointer-events-none "
           "aria-disabled:opacity-64 data-[active=true]:bg-sidebar-row-selected data-[active=true]:font-medium "
           "data-[active=true]:text-sidebar-foreground [&>span:last-child]:truncate [&>svg:not([class*='size-'])]:size-4 "
           "[&>svg]:shrink-0 [&>svg]:text-[var(--sidebar-icon-color)] hover:[&>svg]:text-sidebar-foreground "
           "active:[&>svg]:text-sidebar-foreground data-[active=true]:[&>svg]:text-sidebar-foreground h-8 "
           "rounded-[var(--control-radius)] px-[var(--sidebar-row-content-inset)] py-1.5 text-sm font-medium "
           "text-sidebar-muted-foreground/80")
SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
       'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-{name}">{body}</svg>')
NAV = [("/settings/general", "General", "settings2",
        '<path d="M14 17H5"/><path d="M19 7h-9"/><circle cx="17" cy="17" r="3"/><circle cx="7" cy="7" r="3"/>'),
       ("/settings/appearance", "Appearance", "palette",
        '<circle cx="13.5" cy="6.5" r=".5" fill="currentColor"/><circle cx="17.5" cy="10.5" r=".5" fill="currentColor"/>'
        '<path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652'
        '-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.555-2.503 5.555-5.554C21.965 6.012 17.461 2 12 2z"/>'),
       ("/settings/chat", "Chat", "message-square-text",
        '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/><path d="M13 8H7"/><path d="M17 12H7"/>'),
       ("/settings/voice", "Voice", "mic",
        '<path d="M12 19v3"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><rect x="9" y="2" width="6" height="13" rx="3"/>')]
SWITCH_ROOT = ("inline-flex h-[calc(var(--thumb-size)+2px)] w-[calc(var(--thumb-size)*2-2px)] shrink-0 cursor-pointer "
               "items-center rounded-full p-[2px] outline-none transition-[background-color,box-shadow] duration-200 "
               "data-checked:bg-primary data-unchecked:bg-input [--thumb-size:--spacing(4)] sm:[--thumb-size:--spacing(3.5)]")
SWITCH_THUMB = ("pointer-events-none block size-[calc(var(--thumb-size)-2px)] shrink-0 origin-left rounded-(--thumb-size) "
                "bg-background shadow-sm/5 dark:data-unchecked:bg-muted-foreground data-checked:origin-right "
                "data-checked:translate-x-[calc(var(--thumb-size)-4px)]")


def nav_item(to, label, icon, body):
    active = to == "/settings/general"
    attrs = 'data-active="true" aria-current="page"' if active else 'data-active="false"'
    return (f'<li class="group/menu-item relative" data-sidebar="menu-item" data-slot="sidebar-menu-item">'
            f'<button type="button" data-settings-section="{to}" {attrs} data-sidebar="menu-button" data-size="default" '
            f'data-slot="sidebar-menu-button" class="{NAV_BTN}">{SVG.format(name=icon, body=body)}'
            f'<span class="truncate max-md:sr-only">{label}</span></button></li>')


def settings_row(title, desc, on):
    state = "data-checked" if on else "data-unchecked"
    return (f'<div data-slot="settings-row" tabindex="-1" class="group/settings-row @container/settings-row rounded-xl px-3 sm:px-4 py-3">'
            f'<div class="flex flex-col gap-3 @min-[32rem]/settings-row:grid @min-[32rem]/settings-row:grid-cols-[minmax(0,1fr)_auto] '
            f'@min-[32rem]/settings-row:items-center @min-[32rem]/settings-row:gap-8"><div class="min-w-0 flex-1 space-y-1">'
            f'<div class="flex min-h-5 items-center gap-1.5"><h3 class="text-sm font-medium text-foreground">{title}</h3></div>'
            f'<p class="max-w-2xl text-pretty text-xs leading-normal text-muted-foreground">{desc}</p></div>'
            f'<div class="flex w-full min-w-0 shrink-0 items-center gap-2 @min-[32rem]/settings-row:w-auto @min-[32rem]/settings-row:justify-end">'
            f'<button type="button" role="switch" aria-checked="{str(on).lower()}" {state}="" class="{SWITCH_ROOT}">'
            f'<span {state}="" class="{SWITCH_THUMB}"></span></button></div></div></div>')


def settings_section(sid, title, rows):
    return (f'<section id="{sid}" tabindex="-1" class="space-y-2.5"><div data-settings-scroll-target '
            f'class="flex min-h-7 flex-wrap items-start justify-between gap-x-4 gap-y-1 px-3 sm:px-4">'
            f'<div class="flex min-w-0 items-center gap-1"><h2 class="flex min-h-7 items-center gap-2 text-base font-semibold '
            f'text-foreground">{title}</h2></div></div><div data-slot="settings-group" class="relative overflow-visible '
            f'text-foreground rounded-xl border border-border/60 bg-card/40 shadow-xs/5 *:not-first:border-t '
            f'*:not-first:border-border/50 [&>[data-slot=settings-row]]:rounded-none">{"".join(rows)}</div></section>')


general_sections = "".join([
    settings_section("notifications", "Notifications", [
        settings_row("Desktop notifications", "Notify when a thread finishes or needs input while the app is in the background.", True),
        settings_row("In-app notifications", "Show a toast when another thread finishes, fails, or needs input or approval while this app has focus.", True)]),
    settings_section("sidebar", "Sidebar", [
        settings_row("Project grouping", "Combine matching repositories across environments.", True),
        settings_row("Show archived threads", "List archived threads under each project.", False)]),
    settings_section("composer", "Composer", [
        settings_row("Send on Enter", "Enter sends the message. Shift+Enter adds a new line.", True),
        settings_row("Spell check", "Underline misspelled words while you type.", False),
        settings_row("Keep draft per thread", "Each thread keeps its own unsent draft.", True)]),
])
general = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>T3 general settings harness</title>
<link rel="stylesheet" href="{css}">
<style>body{{background:var(--background);color:var(--foreground);font-family:system-ui,sans-serif;margin:0}}</style>
<script>window.__TSR_ROUTER__={{state:{{location:{{pathname:"/settings/general"}}}},
  navigate(o){{this.state.location.pathname=o.to;return Promise.resolve();}}}};</script>
</head><body>
<div class="relative flex min-h-0 flex-1" style="height:100vh">
  <nav role="navigation" class="flex w-60 shrink-0 flex-col border-e bg-sidebar text-sidebar-foreground max-md:w-14">
    <div class="flex w-full min-w-0 flex-col" data-sidebar="content" data-slot="sidebar-content">
      <div class="relative flex w-full min-w-0 flex-col p-[var(--sidebar-content-inset)]" data-sidebar="group" data-slot="sidebar-group">
        <div class="flex flex-col gap-4"><div class="flex flex-col gap-1">
          <h3 id="settings-nav-group-0" class="px-2 text-xs font-medium text-sidebar-muted-foreground/80 sr-only">Preferences</h3>
          <div role="group" aria-labelledby="settings-nav-group-0">
            <ul class="flex w-full min-w-0 flex-col gap-1" data-sidebar="menu" data-slot="sidebar-menu">
              {"".join(nav_item(*item) for item in NAV)}
            </ul>
          </div>
        </div></div>
      </div>
    </div>
  </nav>
  <div class="flex min-h-0 min-w-0 flex-1 flex-col">
    <header class="flex h-12 shrink-0 items-center gap-3 border-b ps-5 pe-2 text-sm">Settings <span class="text-muted-foreground">/</span> General</header>
    <div class="min-h-0 flex flex-1 flex-col">
      <div class="topbar-scroll-fade scrollbar-gutter-both flex-1 overflow-y-auto" data-settings-page-scroll>
        <div class="mx-auto flex w-full flex-col px-5 pt-6 pb-12 sm:px-6 max-w-4xl gap-8">{general_sections}</div>
      </div>
    </div>
  </div>
</div>
{LOADER}
</body></html>"""
(out.parent / "settings-general.html").write_text(general, encoding="utf-8")
print(out.parent / "settings-general.html")
