"""Entrypoint. Reached via the `bin/sync` shim, never executed directly.

Every invocation -- startup hook, event hook, or action -- does the same three things:
read the authoritative Herdr state, compute the desired `folders` array, and rewrite the
file only if the result differs. **The event payload is never read to decide what
changed**; state is recomputed from scratch. That makes runs idempotent, makes the
redundant invocations Herdr emits (a move re-emits `workspace.focused`; `worktree create`
emits `workspace.updated` + `workspace.created`) free, and means a missed or unknown
event cannot leave stale state behind.
"""

import argparse
import os
import sys

import config
import folders as folders_mod
import herdr
import write as write_mod

EXIT_OK = 0
EXIT_FAIL = 1

RESULT_WROTE = "wrote"
RESULT_UNCHANGED = "unchanged"
RESULT_SKIPPED_EMPTY = "skipped-empty"
RESULT_SKIPPED_SESSION = "skipped-session"


def log(msg):
    sys.stdout.write("%s\n" % msg)


def err(msg):
    sys.stderr.write("%s: %s\n" % (config.PLUGIN_ID, msg))


def summary(reason, session, mode, target, count, result):
    """The one line every run prints.

    Hook stdout is invisible except through `herdr plugin log list`, so this line is the
    plugin's entire operational record. `session=` is what tells two sessions' lines
    apart in one interleaved log.
    """
    log(
        "%s: reason=%s session=%s mode=%s target=%s folders=%d result=%s"
        % (config.PLUGIN_ID, reason, session, mode, target or "-", count, result)
    )


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="sync",
        description="Sync a VS Code multi-root workspace file's folders with Herdr Spaces.",
    )
    parser.add_argument(
        "--reason",
        default="manual",
        help="why this run happened (startup|event|action|manual); logged, never branched on",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="print diagnostics and a preview of the splice, then exit without writing",
    )
    return parser.parse_args(argv)


def warn_unpinned_active(cfg):
    """Warn, loudly, about unpinned `active` mode -- then proceed normally.

    Measured on VS Code 1.134.0: the window's extension host restarts if and only if the
    path at `folders[0]` changes. In `active` mode the single Space *is* `folders[0]`, so
    an unpinned config reactivates every language server and the Git extension on every
    Space switch. Nothing breaks -- no window reload, no lost editors, no lost terminals
    -- so this is never a validation error.
    """
    if cfg.mode != config.MODE_ACTIVE or cfg.pinned_folders:
        return
    err(
        "WARNING: mode=\"active\" with no \"pinnedFolders\". The only folder is "
        "folders[0], so every Space switch will restart the VS Code window's extension "
        "host (all language servers and the Git extension reactivate). Pin at least one "
        "folder to hold index 0 and this becomes free."
    )


def describe_entries(entries):
    return [
        (e.path if not e.name else "%s  (name: %s)" % (e.path, e.name)) for e in entries
    ]


def run_doctor(cfg, reason):
    log("%s doctor" % config.PLUGIN_ID)
    log("")
    log("config file      : %s" % cfg.source_path)
    log("  exists         : %s" % os.path.exists(cfg.source_path))
    log("  workspaceFile  : %s" % (cfg.workspace_file or "<none for this session>"))
    log("  mode           : %s" % cfg.mode)
    log("  pinnedFolders  : %s" % (cfg.pinned_folders or "[]"))
    for warning in cfg.warnings:
        log("  warning        : %s" % warning)

    log("")
    log("herdr socket     : %s" % (os.environ.get(config.ENV_SOCKET_PATH) or "<unset>"))
    log("  session        : %s" % cfg.session_name)
    log("  resolved from  : %s" % cfg.selection)
    if cfg.sessions:
        log("  session map    :")
        for name in sorted(cfg.sessions):
            log(
                "    %-14s %s%s"
                % (
                    name,
                    cfg.sessions[name].workspace_file,
                    "   <- this session" if name == cfg.session_name else "",
                )
            )
    if cfg.skip_reason is not None:
        log("  guard          : SKIP - %s" % cfg.skip_reason)
        log("")
        log("a real run would write nothing and exit 0.")
        summary(reason, cfg.session_name, cfg.mode, None, 0, RESULT_SKIPPED_SESSION)
        return EXIT_OK

    real = os.path.realpath(cfg.workspace_file)
    exists = os.path.isfile(real)
    log("")
    log("target file      : %s" % cfg.workspace_file)
    log("  realpath       : %s" % real)
    log("  exists         : %s" % exists)

    log("")
    log("snapshot source  : %s" % herdr.snapshot_source())

    try:
        snap = herdr.read_snapshot()
    except herdr.HerdrError as exc:
        err("cannot read Herdr state: %s" % exc)
        return EXIT_FAIL

    context = herdr.plugin_context()
    spaces, focused_id = herdr.reduce_snapshot(snap, context)
    log("  herdr version  : %s (protocol %s)" % (snap.get("version"), snap.get("protocol")))
    log("  focused space  : %s" % focused_id)
    log("  spaces         : %d" % len(spaces))
    for i, space in enumerate(spaces):
        log("    [%d] %-6s %-24s %s" % (i, space.id, space.label, space.path))
    if context:
        log("  context ws     : %s (cwd %s)"
            % (context.get("workspace_id"), context.get("workspace_cwd")))

    entries = folders_mod.compute_folders(spaces, focused_id, cfg)
    log("")
    log("computed folders : %d" % len(entries))
    for line in describe_entries(entries):
        log("    %s" % line)
    if not entries:
        log("    (empty -- a real run would log skipped-empty and write nothing)")

    warn_unpinned_active(cfg)

    if not exists:
        log("")
        log("no preview: target file does not exist. A real run would exit non-zero.")
        return EXIT_OK

    # Whether a real run would write is worth stating; rendering the diff is not --
    # the folders array is regenerable from Herdr, so just run `sync` and read the file.
    text = write_mod.read_text(real)
    workspace_dir = os.path.dirname(os.path.abspath(cfg.workspace_file))
    log("")
    if write_mod.is_unchanged(text, entries, workspace_dir):
        log("would write      : no (a real run would log unchanged)")
    else:
        log("would write      : yes")
    summary(reason, cfg.session_name, cfg.mode, cfg.workspace_file, len(entries), "doctor")
    return EXIT_OK


def run_sync(cfg, reason):
    real = os.path.realpath(cfg.workspace_file)
    if not os.path.isfile(real):
        err(
            "target workspace file does not exist: %s\n  (configured as %s in %s)\n"
            "  Refusing to create it -- a typo must not produce a stray workspace file."
            % (real, cfg.workspace_file, cfg.source_path)
        )
        return EXIT_FAIL

    try:
        spaces, focused_id = herdr.load_spaces()
        entries = folders_mod.compute_folders(spaces, focused_id, cfg)
        if not entries:
            err(
                "computed folder list is empty; writing nothing. Emitting "
                "\"folders\": [] would blank the VS Code explorer, and an empty "
                "result far more likely means Herdr returned something unexpected "
                "than that no folders are wanted."
            )
            summary(
                reason, cfg.session_name, cfg.mode, cfg.workspace_file, 0,
                RESULT_SKIPPED_EMPTY,
            )
            return EXIT_OK
        # Pass the *configured* path: `sync_file` realpaths it internally for the
        # write, but resolves relative `folders` entries against the configured
        # directory -- which is what VS Code resolves against too.
        result = write_mod.sync_file(cfg.workspace_file, entries)
        summary(
            reason, cfg.session_name, cfg.mode, cfg.workspace_file, len(entries), result
        )
        return EXIT_OK
    except herdr.HerdrError as exc:
        err("cannot read Herdr state: %s" % exc)
        return EXIT_FAIL


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])

    try:
        cfg = config.load()
    except config.ConfigError as exc:
        err("%s" % exc)
        return EXIT_FAIL

    for warning in cfg.warnings:
        err("warning: %s" % warning)

    if args.doctor:
        return run_doctor(cfg, args.reason)

    if cfg.skip_reason is not None:
        log("%s: %s" % (config.PLUGIN_ID, cfg.skip_reason))
        summary(args.reason, cfg.session_name, cfg.mode, None, 0, RESULT_SKIPPED_SESSION)
        return EXIT_OK

    warn_unpinned_active(cfg)
    return run_sync(cfg, args.reason)


if __name__ == "__main__":
    sys.exit(main())
