Yes. Release `2.4.0-beta.3` of the Lantern editor has the fix, and the stable `2.3.7` does not. The fix is in the file watcher, `src/watch/debounce.ts`, and it only changes how long the watcher waits.

## Pull requests in this release

| PR | Area | Change | Size | Tests | State |
|---|---|---|---|---|---|
| [#4821](https://example.com/lantern/pull/4821) | Watcher | Debounce from 50 ms to 120 ms | +38 / -12 | 14 new | Merged on 18 Nov |
| [#4830](https://example.com/lantern/pull/4830) | Search | Index files up to 2 MB, was 512 KB | +210 / -64 | 9 new | Merged on 20 Nov |
| [#4836](https://example.com/lantern/pull/4836) | Themes | High-contrast palette | +1,204 / -0 | none | Open, 2 reviews |
| [#4799](https://example.com/lantern/pull/4799) | Watcher | Polling fallback on network drives | | | Closed, replaced by #4821 |

#4821 is the one you asked about. Its author measured 3 save events per keystroke before the change and 1 after, on a folder of 12,400 files.

## A date to keep in mind

The `2.3.x` line gets security fixes until **31 Mar 2027**, 126 days from today. After that, only `2.4` and later get updates. The upgrade guide is at [docs/upgrade-2.4](https://example.com/lantern/docs/upgrade-2.4), and it lists 3 breaking changes.
