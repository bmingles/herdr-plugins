# vscode-workspace-sync: map Herdr sessions to workspace files

**Speculative — do not implement on sight.** This is only worth building if the user
actually runs more than one Herdr session at a time and wants each to drive its own VS
Code window. Today a named session syncs nothing at all (`result=skipped-session`), which
is correct and safe for the single-session case. If that has not become a real annoyance,
close this plan rather than implementing it.

## Problem

Plugin registration lives in `~/.config/herdr/plugins.json`, which is **not**
session-scoped: one linked plugin runs in every session's server (probe 11, "Unplanned
finding: plugins are global across sessions" in `docs/herdr-vscode-sync-facts.md`). With a
single configured `workspaceFile`, two servers would each compute `folders` from their own
Space list and overwrite the other's.

The shipped guard resolves this bluntly: `config.named_session()` skips unless
`$HERDR_SOCKET_PATH` sits outside `.../sessions/<name>/`. Only the default session ever
syncs; named sessions are inert whether or not the default session is running.

The generalisation is one workspace file **per session**, keyed by session name.

## Decisions

**Config shape.** One new optional top-level key, `sessions`: an object mapping session
name to a per-session config object.

```jsonc
{
  "sessions": {
    "default": { "workspaceFile": "~/code/main.code-workspace" },
    "work":    { "workspaceFile": "~/code/work.code-workspace",
                 "pinnedFolders": ["~/code/work"] },
    "oss":     { "workspaceFile": "~/code/oss.code-workspace", "mode": "active" }
  }
}
```

`"default"` is a legal key and names the default session, so a multi-session config is
symmetric — the default session is not special-cased into the top level. The existing
flat form stays valid and unchanged.

**Resolution order.** Exactly these four rules, in order:

1. Resolve the session name (below).
2. If `sessions[name]` exists → use it. `workspaceFile` is **required** in the entry and
   is **never** inherited from the top level. `mode` and `pinnedFolders` fall back to the
   top-level values, then to their existing defaults (`"mirror"`, `[]`).
3. Else if `name == "default"` and a top-level `workspaceFile` is set → use the top-level
   config. This is today's behaviour, byte for byte.
4. Else → skip: log `result=skipped-session` and exit 0.

With no `sessions` key, rules 3 and 4 reproduce current behaviour exactly, so every
existing config keeps working and `test_named_session_socket_skips_without_writing` must
still pass untouched.

**Session-name derivation.** From `$HERDR_SOCKET_PATH`, verified against probe 11:

| socket | name |
| --- | --- |
| unset | `default` |
| `/Users/x/.config/herdr/herdr.sock` | `default` |
| `/Users/x/.config/herdr/sessions/probe/herdr.sock` | `probe` |

```
parts = socket_path.replace(os.sep, "/").split("/")
if "sessions" in parts and a component follows it:
    name = parts[parts.index("sessions") + 1]   # first occurrence
else:
    name = "default"
```

Use the **first** occurrence of `sessions`, which yields the right answer even for a
session literally named `sessions` (`.../sessions/sessions/herdr.sock` → `sessions`). Do
not shell out to `herdr session list` — the socket path is authoritative, already in the
hook environment, and needs no subprocess.

**`workspaceFile` uniqueness is a hard error.** If two resolvable configs (two `sessions`
entries, or one entry and the top-level file reachable via rule 3) point at the same
`os.path.realpath`, raise `ConfigError` naming both session names and the shared path.
Two sessions writing one file is the exact failure the session guard exists to prevent;
silently allowing it back in through the new key would defeat the feature.

**`workspaceFile` becomes optional at the top level** when `sessions` is present and
non-empty. Absent both, keep today's error text. This lets a user drive only named
sessions and leave the default session inert.

**`HERDR_VSCODE_SYNC_WORKSPACE_FILE` keeps winning over everything**, for every session,
including one that rule 4 would skip. It is a one-run debugging override and overriding
the skip is what makes it useful for testing a named session by hand.

**The summary line gains a `session=` field**, before `mode=`:

```
vscode-workspace-sync: reason=event session=work mode=mirror target=/… folders=3 result=wrote
```

Existing tests assert on `result=…` and `reason=…` as substrings, so adding a field is
safe.

## Checklist

- [x] `src/config.py`: add `resolve_session_name(env=None)` returning the derived name,
      per the table above.
- [x] `src/config.py`: `Config` gains a `session_name` slot; `load()` implements the
      four-rule resolution and populates `workspace_file`, `mode`, `pinned_folders` from
      the selected entry.
- [x] `src/config.py`: replace `named_session()` with the rule-4 outcome surfaced from
      `load()` — a resolved-but-unmapped session must be representable without raising
      (e.g. `Config.skip_reason` set and `workspace_file` left `None`). Delete
      `named_session()` and its import site; do not leave both mechanisms live.
- [x] `src/config.py`: validate `sessions` — object of objects, string `workspaceFile`
      required per entry, unknown keys inside an entry warn (matching top-level
      behaviour), non-object entry is a `ConfigError`.
- [x] `src/config.py`: implement the realpath uniqueness check as a `ConfigError`.
- [x] `src/main.py`: `main()` consults the new skip reason instead of
      `config.named_session()`; keep `result=skipped-session` and exit 0.
- [x] `src/main.py`: add `session=<name>` to `summary()`.
- [x] `src/main.py`: `run_doctor()` prints the resolved session name, which rule matched
      (`sessions[<name>]`, top-level, or unmapped), and every configured session with its
      target so a user can see the whole map at once.
- [x] `config.example.json`: document `sessions` with the multi-session example above.
- [x] `README.md`: add `sessions` to the Configuration table; rewrite
      `## One Herdr session at a time` to describe the mapping, retitled (e.g.
      `## One workspace file per Herdr session`). Keep the explanation of *why* the guard
      exists — the global-registration finding is the reason the feature has this shape.
- [x] `test/test_config.py`: cover name derivation (all four rows), the four resolution
      rules, inheritance of `mode`/`pinnedFolders`, the uniqueness error, missing
      per-entry `workspaceFile`, and top-level `workspaceFile` absent with `sessions`
      present.
- [x] `test/test_cli.py`: a named session **with** a mapping writes its own file; a named
      session **without** one still logs `skipped-session` and writes nothing; the env
      override beats an unmapped session.

## Validation

Run from `vscode-workspace-sync/`. `/usr/bin/python3` specifically — the floor is 3.9.

- [x] `/usr/bin/python3 -m unittest discover -s test -v` — all pass, count strictly above
      the current 147.
- [x] `/usr/bin/python3 -m py_compile src/*.py` — clean.
- [x] Existing `test_named_session_socket_skips_without_writing` passes **unmodified**,
      proving backward compatibility.
- [x] A pre-existing flat config (`workspaceFile` only, no `sessions`) with
      `HERDR_SOCKET_PATH` unset still writes, and with
      `HERDR_SOCKET_PATH=/x/.config/herdr/sessions/other/herdr.sock` still skips.
- [x] With a two-entry `sessions` map and `HERDR_VSCODE_SYNC_FAKE_SNAPSHOT` pointed at
      `test/fixtures/snapshot.json`, running once per session socket writes **two
      different** files and neither run touches the other's.
- [x] `./bin/sync --doctor` under a named session socket names the resolved session and
      prints the full map.
- [x] A config whose two entries share one `workspaceFile` exits non-zero and names both
      sessions and the path.

## Relevant Files

- `vscode-workspace-sync/src/config.py` — resolution, validation, name derivation.
- `vscode-workspace-sync/src/main.py` — skip path, `summary()`, `run_doctor()`.
- `vscode-workspace-sync/config.example.json` — documents every key.
- `vscode-workspace-sync/README.md` — Configuration table (~line 76), session section
  (~line 286).
- `vscode-workspace-sync/test/test_config.py`
- `vscode-workspace-sync/test/test_cli.py`
- `vscode-workspace-sync/test/_support.py` — `FakeConfig` needs the new `session_name`
  slot if the folder tests touch it.
- `.plans/PLAN.md` — status and phase row.

Not touched: `src/folders.py`, `src/write.py`, `src/rewrite.py`, `src/jsonc.py`,
`src/herdr.py`. Folder computation is per-config and does not change; the snapshot is
read from whichever socket invoked the hook, which already scopes it to one session.

## Notes

- `herdr-plugin.toml` needs no change — hooks already fire in every session's server.
  That is the premise of the whole feature, not a gap.
- A session's Spaces are visible only through its own server's `herdr api snapshot`, so
  there is no cross-session leakage to defend against beyond the shared config file.

## Implementation corrections

Two things the plan got wrong, recorded so the next reader trusts the numbers:

- **The test baseline was 106, not 147.** `README.md` claimed 147 and this plan repeated
  it; `unittest discover` actually ran 106 before any change. The claim was stale, not a
  regression — no tests were removed. After this work: **139 tests**. Both the README and
  the validation item now say 139.
- **One existing test did need editing**, contrary to "adding a field is safe."
  `test_cli.TestSyncRun.test_rewrites_then_reports_unchanged` asserted
  `"reason=action mode=mirror"` as one adjacent substring, which the new `session=` field
  splits. Updated to `"reason=action session=default mode=mirror"`. The important one —
  `test_named_session_socket_skips_without_writing`, the backward-compatibility proof —
  did pass unmodified, as predicted.

`test/_support.py` was **not** touched. `FakeConfig` only needs `mode` and
`pinned_folders`, which `compute_folders` is still the sole consumer of; the folder tests
never see a session. The Relevant Files entry was conditional and the condition did not
hold.

Verified beyond the automated suite, against `test/fixtures/snapshot-portable.json` with
two mapped sessions: `work` (inheriting `mode=mirror`) wrote 3 folders and left `oss`
byte-identical; `oss` (entry-level `mode=active`) then wrote 2 folders to its own file;
the unmapped `default` session logged `skipped-session` and exited 0; a duplicated
`workspaceFile` exited 1 naming both sessions and the shared path.
