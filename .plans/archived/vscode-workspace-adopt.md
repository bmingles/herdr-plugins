# vscode-workspace-adopt

Add the **inbound** direction to `vscode-workspace-sync`: read a `.code-workspace`
file's `folders` array and create the Herdr Spaces it describes. Plus a
`scripts/herdrvs` shell function as the low-ceremony entry point.

## Why this is not a two-way sync

The two directions are not symmetric, and the asymmetry drives every decision below.

- **Herdr → file** (the existing `bin/sync`) is *regenerable*. `folders` can always be
  recomputed from scratch and overwritten, which is why it is safe on 12 event hooks.
- **file → Herdr** (this plan) is *not*. A Space owns tabs, panes, running agents and
  scrollback. It can only ever be **added**, never reconciled by rewriting.

So adopt is one-shot, additive, and explicitly invoked. It is **never** event-hooked.

## Mutual exclusivity

A Herdr session is either sync-managed **or** adoptable, never both. This is the
design decision that removes the feedback loop (`workspace.created` → sync hook →
rewrite), the `mode: "active"` conflict (the file holds one folder, so adopting then
syncing truncates it back), and the `pinnedFolders` problem (pins are in the file but
must not become Spaces).

The two are a choice of tool:

- *"I have a workspace already. I want to initialize a copy in Herdr."* → **adopt**
- *"This is a dynamic workspace. I want VS Code to mirror what I do in Herdr."* → **sync**

`adopt` refuses to run in a session that resolves to a `workspaceFile`. The check
reuses `config.load()` exactly:

| Situation | Adopt |
| --- | --- |
| `$HERDR_VSCODE_SYNC_WORKSPACE_FILE` set | **refuse** — the override makes sync active |
| `config.json` absent | allow |
| `config.json` present but invalid | **hard fail** — do not silently allow a typo'd sync config |
| `cfg.workspace_file` is not `None` (rules 2 or 3 matched) | **refuse**, naming the session and the file |
| `cfg.skip_reason` is set (rule 4, `unmapped`) | allow |

Refusal exits **2**, distinct from a failure's 1, so the shell function can tell them apart.

## Measured Herdr behaviour this depends on

Probed on **herdr 0.8.2** in the devcontainer against a throwaway session
(2026-08-27). All four are silent failure modes or footguns, and each one dictates a
specific defence:

| Behaviour | Evidence | Consequence for this plan |
| --- | --- | --- |
| **No dedupe by path.** `workspace create` with a `--cwd` that already backs a Space creates a *second* Space and does **not** update the label. | Same `--cwd` twice → `w2` "alpha" and `w3` "beta", both listed. | Adopt **must** dedupe itself, by resolved path, against a live snapshot. |
| **A nonexistent `--cwd` silently succeeds and falls back to `$HOME`.** No error. | `--cwd /workspaces/does-not-exist-xyz` → `w4` with `cwd: /home/vscode`. | Every folder must be `os.path.isdir`-checked **before** the call. |
| **A relative `--cwd` is not resolved against the caller's cwd** — it also lands at `$HOME`. | `--cwd ./docs` → `w5` with `cwd: /home/vscode`. | Always pass an absolute, resolved path. |
| `workspace rename <WORKSPACE_ID> <LABEL>` works and returns the updated record. | `rename w2 renamed-alpha` → `label: "renamed-alpha"`. | Makes `--relabel` possible. |

Also confirmed: `herdr plugin action invoke` accepts only `--plugin` — **plugin actions
take no arguments**. That is why the direct `bin/adopt` invocation is the primary
interface and the action is a convenience, mirroring how `--doctor` is a flag and
deliberately not an action.

## Contract

### `bin/adopt`

```
adopt [--file PATH] [--dry-run] [--relabel] [--reason R]
```

| Flag | Meaning |
| --- | --- |
| `--file PATH` | The `.code-workspace` to read. Default: discover in `$PWD`. |
| `--dry-run` | Print the plan; create and rename nothing. |
| `--relabel` | Also rename an existing Space whose label differs from the file's `name`. **Default off** — adopt is otherwise strictly non-mutating. |
| `--reason R` | Logged, never branched on. Matches `bin/sync`. Default `manual`. |

**Discovery** (no `--file`): glob `*.code-workspace` in `$PWD`, non-recursive.
Exactly 0 → error, exit 1. Exactly 1 → use it. More than 1 → error listing all of
them and requiring `--file`, exit 1. Never guess.

**Reading `folders`**: `jsonc.loads`, then for each entry:

- Not an object, or no string `path` → warn, skip.
- `path` containing `${` (VS Code variable substitution) → warn, skip. Mis-resolving
  is worse than declining.
- Relative `path` → resolved against **the workspace file's own directory**, which is
  what VS Code does, then through `folders.resolve_path()` (expanduser → abspath →
  normpath, **no** symlink resolution) so matching is identical to the sync direction.
- Duplicate resolved path within the file → first occurrence wins, matching
  `compute_folders`.

**Desired label**: the entry's `name` if present, else nothing — Herdr derives the
label from the basename on its own, so `--label` is passed **only** when the file
supplies a `name`. Under `--relabel`, an entry with no `name` therefore never triggers
a rename.

**Planning**, per resolved folder in file order:

| Condition | Action |
| --- | --- |
| not `os.path.isdir` | `skip` + warning |
| resolved path matches an existing Space's resolved path | `exists`; with `--relabel` and a differing `name`, queue a rename |
| otherwise | `create` |

Existing Space paths come from `herdr.load_spaces()` — the snapshot's pane join, which
is a Space's *current* cwd.

**Execution**: `herdr workspace create --cwd <abs> [--label <name>] --no-focus`,
sequentially in file order, so sidebar order matches the file when starting from
empty. `--no-focus` throughout, so adopting never yanks focus. A failed create is
reported and the run **continues**; the process exits 1 at the end if any failed.

**Extra Spaces** — Spaces present in Herdr but absent from the file — are left alone
and listed in the summary. There is no `--close-extra`: closing a Space kills its
tabs, panes and any running agent, and mutual exclusivity means nothing writes them
into the file anyway. A fresh session's initial Space is the common case here.

**Output**: one line per planned action, then a summary line in `bin/sync`'s style:

```
vscode-workspace-sync: reason=manual session=default source=/p/x.code-workspace folders=4 created=2 existing=1 skipped=1 result=adopted
```

`result` is one of `adopted`, `dry-run`, `nothing-to-do`, `refused-managed`.

### `scripts/herdrvs`

A sourceable bash file defining one function, `herdrvs`, that locates the plugin root
and execs `bin/adopt "$@"`. **No logic lives in bash** — it is a locator and nothing
more, so `herdrvs --dry-run` and `herdrvs --file x` work by pass-through.

Root resolution, in order:

1. `$HERDR_VSCODE_SYNC_ROOT` if set.
2. `herdr plugin list --json` piped through `python3` for the `vscode-workspace-sync`
   entry's `plugin_root` — the exact incantation the plugin README already documents
   (`python3`, not `jq`, which macOS does not ship).
3. Otherwise a clear error naming both `herdr plugin link` and `$HERDR_VSCODE_SYNC_ROOT`.

## Checklist

- [x] `src/herdr.py`: add `create_workspace(cwd, label=None, env=None)` and
      `rename_workspace(workspace_id, label, env=None)`, reusing `herdr_bin()` and
      raising `HerdrError` on a non-zero exit or an `{"error": ...}` body.
- [x] `src/adopt.py`: new entrypoint module. Not named after any stdlib module.
- [x] `src/adopt.py`: `discover_workspace_file(cwd)` implementing the 0/1/many rule.
- [x] `src/adopt.py`: `read_folders(path)` → list of `(resolved_path, name_or_None)`,
      applying the skip/warn rules and the file-relative resolution.
- [x] `src/adopt.py`: `guard(env)` implementing the mutual-exclusivity table.
- [x] `src/adopt.py`: `plan_adoption(entries, spaces, relabel)` → a pure, testable plan
      of `create` / `exists` / `skip` / `rename` decisions plus the extra-Space list.
- [x] `src/adopt.py`: execution, summary line, and exit codes (0 ok, 1 fail, 2 refused).
- [x] `bin/adopt`: POSIX-sh shim, a copy of `bin/sync` pointing at `src/adopt.py`.
      Executable; `src/adopt.py` is **not** executable and carries no shebang.
- [x] `herdr-plugin.toml`: one `[[actions]]` entry `id = "adopt"`, contexts
      `["global", "workspace"]`. **No `[[events]]` entry** — adopt is never event-driven.
- [x] `scripts/herdrvs`: the sourceable locator function.
- [x] `test/test_adopt.py`: guard table, discovery 0/1/many, folder reading
      (relative, `~`, `${}`, dupes, malformed), planning (create/exists/skip/relabel),
      and the subprocess layer against a fake `HERDR_BIN_PATH` script.
- [x] `test/fixtures/adopt-*.code-workspace` fixtures for the reading tests.
- [x] `vscode-workspace-sync/README.md`: document adopt, the mutual-exclusivity rule,
      and the four measured Herdr behaviours.
- [x] Top-level `README.md`: mention `scripts/herdrvs`.
- [x] `docs/herdr-vscode-sync-facts.md`: record the four probed behaviours with their
      evidence, dated and version-stamped.

## Validation

- [x] `cd vscode-workspace-sync && /usr/bin/python3 -m py_compile src/*.py` exits 0.
- [x] `cd vscode-workspace-sync && /usr/bin/python3 -m unittest discover -s test` — all
      139 pre-existing tests still pass, plus the new ones.
- [x] `bin/adopt` is executable; `src/adopt.py` is not and has no shebang:
      `test -x bin/adopt && ! test -x src/adopt.py && ! head -1 src/adopt.py | grep -q '^#!'`
- [x] No module in `src/` shadows a stdlib module:
      `python3 -c "import sys,os; [sys.exit('shadow: '+f) for f in os.listdir('src') if f.endswith('.py') and f[:-3] in sys.stdlib_module_names]"` (3.10+ only; eyeball on 3.9).
- [x] **Guard, refuse**: with a `config.json` holding a top-level `workspaceFile` and
      the default session, `bin/adopt --dry-run` exits **2** and names the session and file.
- [x] **Guard, allow**: with `HERDR_PLUGIN_CONFIG_DIR` pointed at an empty directory,
      `bin/adopt --dry-run` proceeds to a plan.
- [x] **Guard, override**: with `HERDR_VSCODE_SYNC_WORKSPACE_FILE` set, exits **2**.
- [x] **Guard, broken config**: with a `config.json` containing invalid JSON, exits **1**
      with the parse error — not 2, and not a plan.
- [x] **Discovery**: in a dir with 0 `*.code-workspace` → exit 1; with 2 → exit 1 listing
      both; with 1 → proceeds.
- [x] **Dry run is inert**: against a live session,
      `bin/adopt --file <f> --dry-run` prints a plan and `herdr workspace list` is
      byte-identical before and after.
- [x] **Nonexistent folder is skipped**: a workspace file naming a directory that does
      not exist reports `skip` and creates **no** Space at `$HOME`.
- [x] **Relative paths resolve against the file's directory**, not `$PWD`: a fixture with
      `{"path": "sub"}` plans a create for `<dir-of-file>/sub`.
- [x] **End-to-end, live session**: from empty, adopt a 3-folder file → 3 Spaces in file
      order; re-run → `created=0 existing=3 result=nothing-to-do` and still 3 Spaces
      (this is the no-dedupe defence).
- [x] **`--relabel`**: with a file entry carrying a differing `name`, a second run with
      `--relabel` renames and without it does not.
- [x] `herdrvs` sourced into a bash shell resolves the plugin root and passes `--dry-run`
      through.

## Relevant Files

| File | Change |
| --- | --- |
| `vscode-workspace-sync/src/adopt.py` | **new** — the entrypoint |
| `vscode-workspace-sync/src/herdr.py` | add `create_workspace`, `rename_workspace` |
| `vscode-workspace-sync/bin/adopt` | **new** — POSIX-sh shim |
| `vscode-workspace-sync/herdr-plugin.toml` | one `[[actions]]` entry |
| `vscode-workspace-sync/README.md` | adopt section, mutual exclusivity, measured behaviours |
| `vscode-workspace-sync/test/test_adopt.py` | **new** |
| `vscode-workspace-sync/test/fixtures/adopt-*.code-workspace` | **new** fixtures |
| `vscode-workspace-sync/test/_support.py` | extend `WORKSPACE_FIXTURES` if the new fixtures should join the round-trip sweep |
| `scripts/herdrvs` | **new** — sourceable locator function |
| `README.md` | mention `scripts/herdrvs` |
| `docs/herdr-vscode-sync-facts.md` | record the four probed behaviours |
| `.plans/PLAN.md` | status + phase row |

Not touched: `src/main.py`, `src/write.py`, `src/rewrite.py`, `src/folders.py`,
`src/config.py`, `src/jsonc.py` — adopt reads through their existing public surface and
the sync direction's behaviour is unchanged.

## Implementation corrections

Five things the plan did not anticipate. All are folded into the code and docs.

1. **A plugin action's cwd is the plugin root**, so the `[[actions]]` entry could not use
   `os.getcwd()` for discovery — it would have searched this repo. Added
   `adopt.discovery_dir(env)`: when `HERDR_PLUGIN_ACTION_ID` is set it reads
   `focused_pane_cwd` (then `workspace_cwd`) out of `HERDR_PLUGIN_CONTEXT_JSON`,
   otherwise it returns the process cwd. Verified live — with the focused pane in
   `.../scratchpad`, the action found `.../scratchpad/from-action.code-workspace` while
   the plugin root had no workspace file at all.

2. **`os.access(path, os.X_OK)` is unreliable on this bind-mounted working tree** — it
   returns `True` for a 0644 file, so the planned executable-bit assertion passed
   vacuously in one direction and failed in the other. `ShimTest` reads `os.stat`
   mode bits instead, which is also what actually gets committed.

3. **The probe harness needed an explicit pty size.** Under
   `script -qc "herdr --session probe" /dev/null` the client got a 2-row viewport and
   *every* pane spawn failed with `ghostty error -2` — `tab create` too, so it was not a
   `workspace create` bug. A `pty.fork()` harness with `TIOCSWINSZ` at 220x60 fixed it.
   Recorded in the facts doc, since anyone reproducing §19 in a container will hit it.

4. **The summary line gained `renamed=`**, which the plan's example omitted. `--relabel`
   is otherwise invisible in the one-line record.

5. **Three adopt fixtures joined `WORKSPACE_FIXTURES`** rather than staying private to
   `test_adopt`. `adopt-basic`, `adopt-relative` and `adopt-messy` all carry a valid
   top-level `folders` member, so they are free extra shapes for the existing
   tokenizer/rewrite round-trip sweeps — a mixed-type array in particular. The two
   fixtures with a deliberately invalid `folders` were left out, since the sweep requires
   a spliceable member.

## Result

**199 tests pass** (baseline was 139), and the whole thing was verified against a **real
Herdr 0.8.2 server** in the devcontainer, not just fakes:

- dry run printed the plan and left `workspace list` untouched;
- a 4-folder file created exactly 3 Spaces in file order, honoured the one explicit
  `name`, and **skipped** the nonexistent folder — no stray Space at `$HOME`;
- a re-run reported `existing=3 result=nothing-to-do` and created nothing, which is the
  no-dedupe defence working against the real server;
- `--relabel` renamed `w3` only when asked, and was a no-op without the flag;
- the guard refused a session mapped in `sessions` with exit 2;
- `herdrvs` resolved the plugin root both ways (`$HERDR_VSCODE_SYNC_ROOT` and
  `herdr plugin list --json`) and exited 127 with guidance when neither worked;
- `herdr plugin action invoke adopt` ran clean at exit 0 through the real plugin loader.

The probe session was deleted and the plugin unlinked afterwards; `plugins.json` is back
to `[]`.
