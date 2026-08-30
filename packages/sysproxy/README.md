# sysproxy

Ownership-aware system proxy attachment service, extracted from
[ferret](../..) as a standalone, dependency-free package.

- `SystemProxyService` — attach/detach/recover the system proxy with a
  snapshot-and-journal protocol: before overwriting the system settings it
  snapshots the current values and writes a journal file, so a crashed
  process can restore the user's original proxy on the next start
  (`recover()`).
- Backends: `WindowsSystemProxyBackend` (registry via `winreg`, WinINet
  refresh, loopback exemption) and an `UnsupportedSystemProxyBackend`
  fallback for other platforms.

Design rules:

- **Zero dependencies** — standard library only (`winreg` is imported lazily
  inside functions).
- **No default directories** — the journal path must be injected by the host
  (usually a fixed file name inside the host's config directory); `None`
  disables journaling. A journal in the wrong place is as good as none.
- **No translation** — exceptions carry English literals defined as module
  constants (`ERR_SET_FAILED` …); the host translates them at the display
  boundary against those constants.
