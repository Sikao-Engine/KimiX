# Computer and browser automation tools

Kimi CLI exposes two consolidated tools, `computer_use` and `browser_use`, in the
base agent and the KimiX worker agent. Both use `kosong.tooling.CallableTool2`,
return bounded JSON, and route side-effecting actions through Kimi's approval
runtime. The tools are blocked as a whole in read-only mode.

## `computer_use` (Windows)

The native Windows backend uses pywinauto/UI Automation for window discovery,
fresh element snapshots, invoke/select/toggle actions, and value input. Pillow
captures screenshots; pywinauto's Win32 mouse/keyboard implementation handles
explicitly approved foreground input.

Typical sequence:

1. `capture(mode="som")` to receive a numbered-overlay screenshot and elements.
2. Use an `element` and the returned `snapshot_id` instead of coordinates.
3. Re-capture after an action, or set `capture_after=true`.
4. If a result says `verify_fresh_state`, inspect the new state before deciding
   whether another action is needed. The tool never automatically replays input.

Background actions are deliberately limited to UIA operations that do not
intentionally raise a window. Coordinate clicks, key combinations, wheel input,
and drag require `delivery_mode="foreground"`; these move the real cursor or
focus. A background failure does not silently escalate to foreground delivery.
`set_value` is generally more reliable than synthetic typing for edit controls.

Screenshots are saved below the current Kimi session's
`.kimix_cache/<session>/automation/computer_use/` directory. Models with
`image_in` also receive a budget-compressed inline image. Other models receive
the absolute screenshot path and can use `read_image` when available.

Windows limitations:

- UIPI can prevent a non-elevated process from controlling elevated apps.
- SSH/Session 0 and locked sessions do not provide a normal interactive desktop.
- `WindowsApps` programs such as Notepad may have a launcher PID and a distinct
  interactive child PID. Use `list_windows` or title/process matching when the
  initially launched PID has no visible window.
- Custom-drawn or inaccessible applications may expose few UIA elements.
- Background delivery support varies by control pattern; it is not guaranteed
  merely because an element appears in a snapshot.

## `browser_use`

The browser backend uses Playwright and Chromium/CDP. It launches an isolated,
non-persistent browser context for each validated session name. Cookies, tabs,
and page state persist between calls in that context; separate session names do
not share a cookie jar. No model-written host Python is executed.

Supported actions cover navigation, DOM snapshots, click/fill/type/key input,
text/attribute extraction, approved JavaScript evaluation, screenshots, tabs,
waiting, scrolling, history, and explicit session close. Mutations invalidate
element handles so subsequent actions should use a fresh `snapshot`.

Chrome and Edge are auto-detected on Windows. Otherwise install Playwright's
Chromium in the project environment:

```bash
uv run playwright install chromium
```

Optional session `custom_config["config_json"]["browser_use"]` keys:

| Key | Default | Description |
|---|---:|---|
| `headed` | `false` | Show the automated browser window. |
| `executable_path` | unset | Explicit Chromium-family executable. |
| `cdp_url` | unset | Attach to an operator-supplied CDP endpoint instead of launching. |
| `timeout_ms` | `30000` | Playwright operation timeout, clamped to 1–120 seconds. |
| `inactivity_timeout` | `300` | Idle-context cleanup threshold in seconds, checked on later calls. |
| `max_elements` | `100` | Maximum interactive elements retained in one snapshot. |

`KIMI_BROWSER_EXECUTABLE` is an alternative executable override and
`KIMI_BROWSER_CDP_URL` an alternative CDP endpoint setting. Explicit config
takes precedence. Browser screenshots are saved under the session's
`automation/browser_use/<session>/` directory. Use `action="close"` to release a
named context; closing the last context also releases a locally owned browser.

### URL and script boundaries

- HTTP(S) navigation uses the shared URL safety policy, including DNS checks,
  credential-bearing query rejection, and an always-blocked cloud metadata
  floor. Redirected page URLs have sensitive query data redacted from results.
- `KIMI_ALLOW_PRIVATE_URLS=true` permits intentional loopback/LAN development
  sites. It does not permit cloud metadata endpoints.
- Local `file:`, `javascript:`, and browser-internal navigation schemes are
  refused. Self-contained `about:blank`, `data:text/html`, and `data:text/plain`
  pages are supported for fixtures and generated content.
- URL checks apply to the requested top-level navigation. They are not a
  network monitor for redirects, subresources, or JavaScript-initiated traffic.
  Use OS/container/proxy controls when a hard SSRF boundary is required.
- `evaluate` runs JavaScript in the active page, not host Python, but still has
  that page's origin/network authority and therefore requires approval.
- Do not place credentials in URLs or extract password values into model
  context. Password inputs are masked in DOM snapshots.

## Tests

Contract tests run normally:

```bash
uv run pytest kimi-cli/tests/tools/test_computer_use.py \
  kimi-cli/tests/tools/test_browser_use.py -q
```

Live tests are opt-in because they launch real local applications. They control
only a uniquely named Notepad fixture and a self-contained Chromium page:

```bash
KIMI_RUN_WINDOWS_UI_TESTS=1 uv run pytest \
  kimi-cli/tests/tools/test_automation_live.py -q
```


## Performance benchmark

The opt-in live benchmark owns a temporary Notepad process and an incognito
Chromium session. It measures steady-state browser snapshot/extraction/screenshot
and desktop AX/vision/SOM capture latency without injecting foreground input:

```bash
uv run python tools/bench_automation.py --repeat 7 --elements 80 \
  --output .kimix_cache/automation-benchmark.json
```

Use the same settings before and after changes. The JSON includes median, mean,
minimum, p95, and maximum wall-clock milliseconds. `--suite browser` and
`--suite computer` run either side independently.
