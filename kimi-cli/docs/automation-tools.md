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
dropdown selection, file upload, text/attribute/record extraction, approved
JavaScript evaluation, screenshots, tabs, waiting, scrolling, history, and
explicit session close. Mutations invalidate element handles so subsequent
actions should use a fresh `snapshot`.

Addressing is three-tiered. Element indices from `snapshot` are primary and are
guarded by `snapshot_id`; a stale index fails with `stale_element` instead of
clicking the wrong thing. `selector` (CSS/text/xpath) is the second tier. When
neither reaches the target - canvas widgets, closed shadow roots, cross-origin
frames - `click` accepts viewport `x`/`y` in CSS pixels, the same coordinate
system `snapshot` reports, with modifiers held and released around the click.

`wait` sleeps for `seconds` when given nothing else, waits for `selector` to
reach `wait_state` (attached/visible/hidden/detached), waits for `text` to become
visible, or polls a JavaScript `condition` until it is truthy. A predicate runs
page script, so it requires approval like `evaluate`.

`extract` returns `values` (text or one attribute), `records` shaped by `fields`
(`{"title": "h2", "href": "a|href"}`, with `@self` for the record element) so one
call yields schema-shaped rows, `main_content` (largest link-sparse text block,
for article reading), or raw HTML with `format="html"`.

Every result is a state receipt and, when the page produced any, includes the
recent console errors/warnings and failed requests for the active page (bounded
to `console_events` entries, URLs redacted). Errors carry a machine-readable
`error_code` plus `retryable: true|false` so a model can tell "take a fresh
snapshot and retry" from "this will never work"; timeouts deliberately omit the
flag because their outcome is unknown.

Chrome, Edge, Chrome Beta/Dev/Canary, Brave, and Chromium are auto-detected on
Windows, macOS, and Linux. Discovery is ordered and every hit is a launch
candidate:

1. `browser_use.executable_path` / `KIMI_BROWSER_EXECUTABLE` (a *preference*: if it
   fails to launch, detection continues instead of disabling the tool);
2. `browser_use.channel` / `KIMI_BROWSER_CHANNEL` (Playwright `channel`, e.g.
   `chrome`, `msedge`, `chrome-beta`);
3. the `App Paths` registry entries (`chrome.exe`, `msedge.exe`, `brave.exe`), which
   are the only source that survives a non-default install directory or drive;
4. `%ProgramFiles%`, `%ProgramW6432%`, `%ProgramFiles(x86)%`, `%LocalAppData%`
   (macOS `/Applications`; Linux well-known paths);
5. `PATH` (Scoop/Chocolatey/portable installs);
6. the Playwright-managed Chromium (`uv run playwright install chromium`).


Launch attempts share a bounded budget (`launch_budget`, 40 s by default) with a
per-candidate timeout (`launch_timeout_ms`), so a stale or hung executable cannot
stall the tool call. Every attempt is reported in the aggregate error when all
candidates fail, including the remedy for a missing Playwright browser or a host
whose kernel rejects the Chromium sandbox. Each result reports the `browser`,
`browser_version`, and `executable` actually driving the session.

Optional session `custom_config["config_json"]["browser_use"]` keys:

| Key | Default | Description |
|---|---:|---|
| `headed` | `false` | Show the automated browser window. |
| `executable_path` | unset | Preferred Chromium-family executable; detection continues if it fails. |
| `channel` | unset | Playwright channel name (`chrome`, `msedge`, `chrome-beta`, ...). |
| `cdp_url` | unset | Attach to an operator-supplied CDP endpoint instead of launching. |
| `timeout_ms` | `30000` | Playwright operation timeout, clamped to 1–120 seconds. |
| `inactivity_timeout` | `300` | Idle-context cleanup threshold in seconds, checked on later calls. |
| `max_elements` | `100` | Maximum interactive elements retained in one snapshot. |
| `launch_timeout_ms` | `15000` | Per-candidate launch timeout, clamped to 3–120 seconds. |
| `launch_budget` | `40` | Total discovery/launch budget in seconds, clamped to 5–300. |
| `max_launch_attempts` | `6` | Maximum candidates launched before the aggregate error. |
| `sandbox` | `true` | Keep the Chromium renderer sandbox; disable only for containers without user namespaces. |
| `viewport_width` / `viewport_height` | `1280` / `900` | Context viewport and `--window-size`, clamped to 320-3840 x 240-2160. |
| `extra_args` | none | Extra Chromium switches (max 20), e.g. fingerprint-reduction flags. Off by default. |
| `console_events` | `5` | Recent console errors and failed requests included per result; `0` disables them. |
| `allow_remote_cdp` | `false` | Permit a non-loopback `cdp_url`; the debugging port has no authentication. |
| `reuse_default_context` | `false` | Attach into the browser's existing context so its login state is visible. |
| `storage_state` | unset | Cookie/localStorage JSON loaded on create, saved on close (owned browsers only). |
| `allowed_origins` | none | Origin allow list for top-level navigation (`*`, `example.com`, `*.example.com`, `scheme://host:port`). |
| `blocked_origins` | none | Origin deny list, evaluated before the allow list. |

`KIMI_BROWSER_EXECUTABLE` is an alternative executable override,
`KIMI_BROWSER_CHANNEL` a channel override, `KIMI_BROWSER_CDP_URL` an alternative
CDP endpoint, `KIMI_BROWSER_STORAGE_STATE` a storage-state file,
`KIMI_BROWSER_ALLOW_REMOTE_CDP=1` accepts a remote CDP host, and
`KIMI_BROWSER_NO_SANDBOX=1` disables the sandbox for hosts that cannot provide
one. Explicit config takes precedence. Browser screenshots are
saved under the session's `automation/browser_use/<session>/` directory. Use
`action="close"` to release a named context; closing the last context also
releases a locally owned browser.

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
- `cdp_url` is loopback-only unless `allow_remote_cdp` is set: a debugging port
  authenticates nobody, so anyone who can reach it can drive the browser and read
  every logged-in cookie in that profile. Publish ports as `127.0.0.1:9222:9222`
  and use an SSH tunnel for remote hosts.
- `allowed_origins`/`blocked_origins` filter requested top-level navigations.
  They are convenience filters, not a boundary: redirects, popups and
  subresources are unaffected. Use network egress policy for a hard limit.
- Uploads resolve inside the workspace root only (absolute paths included),
  accept at most 10 regular files, and require approval. `..` and symlink escapes
  are refused with `upload_outside_workspace`.
- `storage_state` writes cookies to a plain-text JSON file. It is saved only for
  browsers this tool launched, never for an attached browser, and should point at
  a private directory.
- `reuse_default_context` writes into an attached browser's real profile. Enable
  it only for browsers you are willing to modify.

## Tests

Contract tests run normally:

```bash
uv run pytest kimi-cli/tests/tools/test_computer_use.py \
  kimi-cli/tests/tools/test_browser_use.py \
  kimi-cli/tests/tools/test_browser_use_features.py \
  kimi-cli/tests/tools/test_browser_discovery.py -q
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
