"""Inbound entrypoint: create Herdr Spaces from a `.code-workspace` file's `folders`.

Reached via the `bin/adopt` shim, never executed directly. This is the **opposite**
direction from `src/main.py`, and the two are deliberately not symmetric.

`main.py` is safe to run on twelve event hooks because `folders` is *regenerable*: it
can always be recomputed from Herdr and overwritten. A Space is not. It owns tabs,
panes, running agents and scrollback, so it can only ever be **added**, never
reconciled by rewriting. Adopt is therefore one-shot, additive, explicitly invoked,
and never hooked to an event.

**Adopt and sync are mutually exclusive per session.** A session either has a
configured `workspaceFile` -- sync mirrors Herdr into it -- or it does not, and then
adopt may import into it. That single rule removes what would otherwise be three
separate problems: the feedback loop (a create fires `workspace.created`, which runs
the sync hook, which rewrites the file), `mode: "active"` (the file holds one folder,
so adopting then syncing truncates it straight back), and `pinnedFolders` (pins live
in the file but must not become Spaces). See `guard`.
"""

import argparse
import glob
import os
import sys

import config
import folders as folders_mod
import herdr
import jsonc
import launchers

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_REFUSED = 2

RESULT_ADOPTED = "adopted"
RESULT_DRY_RUN = "dry-run"
RESULT_NOTHING = "nothing-to-do"
RESULT_REFUSED = "refused-managed"

ACTION_CREATE = "create"
ACTION_EXISTS = "exists"
ACTION_SKIP = "skip"

WORKSPACE_GLOB = "*.code-workspace"


class AdoptError(Exception):
    """Fatal problem with the workspace file or the invocation. User-facing message."""


def log(msg):
    sys.stdout.write("%s\n" % msg)


def err(msg):
    sys.stderr.write("%s: %s\n" % (config.PLUGIN_ID, msg))


class FolderRef(object):
    """One usable `folders[]` entry: an absolute resolved path and an optional name."""

    __slots__ = ("path", "name", "raw")

    def __init__(self, path, name=None, raw=None):
        self.path = path
        self.name = name
        self.raw = raw if raw is not None else path

    def __repr__(self):  # pragma: no cover - debugging aid
        return "FolderRef(path=%r, name=%r)" % (self.path, self.name)

    def __eq__(self, other):
        if not isinstance(other, FolderRef):
            return NotImplemented
        return (self.path, self.name) == (other.path, other.name)


class Planned(object):
    """What adopt intends to do about one folder, before anything is executed."""

    __slots__ = ("ref", "action", "note", "space_id")

    def __init__(self, ref, action, note=None, space_id=None):
        self.ref = ref
        self.action = action
        self.note = note
        self.space_id = space_id

    def __repr__(self):  # pragma: no cover - debugging aid
        return "Planned(%r, %r)" % (self.ref.path, self.action)


class Rename(object):
    """A queued relabel of an existing Space. Only ever produced under `--relabel`."""

    __slots__ = ("space_id", "old_label", "new_label", "path")

    def __init__(self, space_id, old_label, new_label, path):
        self.space_id = space_id
        self.old_label = old_label
        self.new_label = new_label
        self.path = path

    def __repr__(self):  # pragma: no cover - debugging aid
        return "Rename(%r, %r -> %r)" % (self.space_id, self.old_label, self.new_label)


class Plan(object):
    """The whole decision, computed before any Herdr state is mutated."""

    __slots__ = ("items", "renames", "extras")

    def __init__(self, items, renames, extras):
        self.items = items
        self.renames = renames
        self.extras = extras

    def of(self, action):
        return [i for i in self.items if i.action == action]


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------


def guard(env=None):
    """Return a refusal message when this session is sync-managed, else ``None``.

    Reuses `config.load` rather than reimplementing the four resolution rules, so the
    two directions can never disagree about which session owns which file.

    A config file that exists but does not parse raises `ConfigError` on through. That
    is deliberate: treating a typo'd sync config as "no config" would let adopt run in
    exactly the session it must not.
    """
    if env is None:
        env = os.environ

    if env.get(config.ENV_WORKSPACE_FILE):
        return (
            "%s is set, which makes the sync direction active for this run.\n"
            "  Adopt and sync are mutually exclusive: a session either mirrors Herdr "
            "into a workspace file, or imports one. Unset it to adopt."
            % config.ENV_WORKSPACE_FILE
        )

    if not os.path.exists(config.config_path()):
        return None

    cfg = config.load(env)
    if cfg.workspace_file is not None:
        return (
            "Herdr session %r is sync-managed: %s is mirrored into\n"
            "    %s\n"
            "  (resolved from %s)\n"
            "  Adopt and sync are mutually exclusive per session. This session is the "
            "\"dynamic workspace\" case -- VS Code follows what you do in Herdr, so "
            "importing the file back in would fight the sync hook. To import instead, "
            "use a session with no configured workspaceFile."
            % (cfg.session_name, config.PLUGIN_ID, cfg.workspace_file, cfg.selection)
        )
    return None


# ---------------------------------------------------------------------------
# Reading the workspace file
# ---------------------------------------------------------------------------


def discovery_dir(env=None):
    """Which directory to look for a `*.code-workspace` in.

    A plugin command's cwd is the **plugin root**, not the user's directory, so an
    action invocation must not use `os.getcwd()` -- it would search this repo. Herdr
    hands the invoking context over in `HERDR_PLUGIN_CONTEXT_JSON` instead, so an
    action searches the directory of the pane it was invoked from.

    `HERDR_PLUGIN_ACTION_ID` is set only for `[[actions]]`, which is what distinguishes
    the two cases without a flag.
    """
    if env is None:
        env = os.environ
    if not env.get("HERDR_PLUGIN_ACTION_ID"):
        return os.getcwd()
    context = herdr.plugin_context(env)
    for key in ("focused_pane_cwd", "workspace_cwd"):
        value = context.get(key)
        if value:
            return value
    raise AdoptError(
        "invoked as a plugin action, but HERDR_PLUGIN_CONTEXT_JSON carried no "
        "focused_pane_cwd or workspace_cwd, so there is no directory to search.\n"
        "  Run ./bin/adopt --file <path> directly instead."
    )


def discover_workspace_file(cwd):
    """The single `*.code-workspace` in `cwd`. Never guesses between several."""
    matches = sorted(glob.glob(os.path.join(cwd, WORKSPACE_GLOB)))
    matches = [m for m in matches if os.path.isfile(m)]
    if not matches:
        raise AdoptError(
            "no %s file in %s\n  Pass one explicitly with --file." % (WORKSPACE_GLOB, cwd)
        )
    if len(matches) > 1:
        listing = "\n".join("    %s" % os.path.basename(m) for m in matches)
        raise AdoptError(
            "%d %s files in %s:\n%s\n  Pick one with --file."
            % (len(matches), WORKSPACE_GLOB, cwd, listing)
        )
    return matches[0]


def resolve_folder_path(raw, base_dir):
    """Resolve one `folders[].path` the way VS Code does, then normalise it.

    A relative path is relative to the **workspace file's own directory**, not `$PWD`.
    Normalisation then goes through `folders.resolve_path`, so a path matches a Space
    here by exactly the rule the sync direction uses to emit one -- including its
    refusal to resolve symlinks, which on macOS would rewrite `/tmp` to `/private/tmp`.
    """
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        expanded = os.path.join(base_dir, expanded)
    return folders_mod.resolve_path(expanded)


def read_folders(path):
    """Parse a `.code-workspace` into `(refs, warnings)`.

    Unusable entries are warned about and dropped rather than being fatal: a workspace
    file is a hand-edited document and one bad entry should not block importing the
    rest.
    """
    try:
        with open(path, "r") as fh:
            text = fh.read()
    except (IOError, OSError) as exc:
        raise AdoptError("cannot read %s: %s" % (path, exc))

    try:
        doc = jsonc.loads(text)
    except ValueError as exc:
        raise AdoptError("%s is not valid JSON/JSONC: %s" % (path, exc))
    if not isinstance(doc, dict):
        raise AdoptError("%s must contain a JSON object" % path)

    raw_folders = doc.get("folders")
    if raw_folders is None:
        raise AdoptError("%s has no top-level \"folders\" array" % path)
    if not isinstance(raw_folders, list):
        raise AdoptError("%s: \"folders\" must be an array" % path)

    base_dir = os.path.dirname(os.path.abspath(path))
    refs = []
    warnings = []
    seen = set()

    for index, item in enumerate(raw_folders):
        where = "folders[%d]" % index
        if not isinstance(item, dict):
            warnings.append("%s is not an object; ignored" % where)
            continue
        raw_path = item.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            warnings.append("%s has no string \"path\"; ignored" % where)
            continue
        if "${" in raw_path:
            # VS Code substitutes ${workspaceFolder} and friends at load time. Guessing
            # at the expansion would silently root a Space in the wrong place, and the
            # measured cost of a wrong --cwd is a Space at $HOME with no error.
            warnings.append(
                "%s path %r uses ${...} variable substitution, which cannot be "
                "resolved outside VS Code; ignored" % (where, raw_path)
            )
            continue

        resolved = resolve_folder_path(raw_path, base_dir)
        if resolved in seen:
            warnings.append("%s path %r duplicates an earlier folder; ignored"
                            % (where, raw_path))
            continue
        seen.add(resolved)

        name = item.get("name")
        if not isinstance(name, str) or not name:
            name = None
        refs.append(FolderRef(resolved, name, raw_path))

    return refs, warnings


# ---------------------------------------------------------------------------
# Planning -- pure, and the part worth testing hardest
# ---------------------------------------------------------------------------


def plan_adoption(refs, spaces, relabel=False, isdir=None):
    """Decide what to do about every folder, without touching Herdr.

    `spaces` comes from `herdr.load_spaces()`; each `path` is the Space's *current*
    cwd from the snapshot's pane join.

    The `isdir` check is not defensive tidiness. Measured on herdr 0.8.2: a
    `workspace create --cwd` naming a directory that does not exist **succeeds**, and
    silently roots the Space at `$HOME`. Nothing downstream would catch it.
    """
    if isdir is None:
        isdir = os.path.isdir

    by_path = {}
    for space in spaces:
        if not space.path:
            continue
        resolved = folders_mod.resolve_path(space.path)
        # First Space wins: Herdr does not dedupe by path, so two Spaces may legitimately
        # share one, and adopt must treat that as "already covered" either way.
        if resolved not in by_path:
            by_path[resolved] = space

    items = []
    renames = []
    wanted = set()

    for ref in refs:
        if not isdir(ref.path):
            items.append(Planned(ref, ACTION_SKIP, "not an existing directory"))
            continue
        wanted.add(ref.path)
        space = by_path.get(ref.path)
        if space is None:
            items.append(Planned(ref, ACTION_CREATE))
            continue
        items.append(Planned(ref, ACTION_EXISTS, "already Space %s" % space.id, space.id))
        if relabel and ref.name and (space.label or "") != ref.name:
            renames.append(Rename(space.id, space.label or "", ref.name, ref.path))

    extras = []
    for space in spaces:
        resolved = folders_mod.resolve_path(space.path) if space.path else None
        if resolved is None or resolved not in wanted:
            extras.append(space)

    return Plan(items, renames, extras)


# ---------------------------------------------------------------------------
# Reporting and execution
# ---------------------------------------------------------------------------


def describe_plan(plan, source, dry_run):
    log("%s adopt" % config.PLUGIN_ID)
    log("")
    log("source           : %s" % source)
    log("")
    for item in plan.items:
        label = "" if not item.ref.name else "  (name: %s)" % item.ref.name
        note = "" if not item.note else "  -- %s" % item.note
        log("  %-7s %s%s%s" % (item.action, item.ref.path, label, note))
    if not plan.items:
        log("  (no usable folders)")

    for rename in plan.renames:
        log("  %-7s %s: %r -> %r"
            % ("rename", rename.space_id, rename.old_label, rename.new_label))

    if plan.extras:
        log("")
        log("Spaces not in the workspace file (left alone):")
        for space in plan.extras:
            log("  %-6s %-24s %s" % (space.id, space.label, space.path or "<no cwd>"))

    if dry_run:
        log("")
        log("--dry-run: nothing was created or renamed.")


def summary(reason, session, source, plan, created, renamed, result):
    """The one line every run prints, in `bin/sync`'s format."""
    log(
        "%s: reason=%s session=%s source=%s folders=%d created=%d existing=%d "
        "skipped=%d renamed=%d result=%s"
        % (
            config.PLUGIN_ID,
            reason,
            session,
            source or "-",
            len(plan.items),
            created,
            len(plan.of(ACTION_EXISTS)),
            len(plan.of(ACTION_SKIP)),
            renamed,
            result,
        )
    )


def execute(plan, env=None):
    """Create and rename. Returns `(created, renamed, failures)`.

    A single failure does not abort the run -- one unusable folder should not block
    importing the rest -- but any failure makes the process exit non-zero.
    """
    if env is None:
        env = os.environ
    created = 0
    renamed = 0
    failures = []

    for item in plan.of(ACTION_CREATE):
        try:
            record = herdr.create_workspace(item.ref.path, item.ref.name, env)
        except herdr.HerdrError as exc:
            failures.append("create %s: %s" % (item.ref.path, exc))
            err("failed to create Space for %s: %s" % (item.ref.path, exc))
            continue
        created += 1
        log("  created %s  %s  (%s)"
            % (record.get("workspace_id"), item.ref.path, record.get("label")))

    for rename in plan.renames:
        try:
            herdr.rename_workspace(rename.space_id, rename.new_label, env)
        except herdr.HerdrError as exc:
            failures.append("rename %s: %s" % (rename.space_id, exc))
            err("failed to rename %s: %s" % (rename.space_id, exc))
            continue
        renamed += 1
        log("  renamed %s  %r -> %r"
            % (rename.space_id, rename.old_label, rename.new_label))

    return created, renamed, failures


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="adopt",
        description=(
            "Create Herdr Spaces from a VS Code multi-root workspace file's folders. "
            "Additive and one-shot; refuses to run in a session the sync direction "
            "manages."
        ),
    )
    parser.add_argument(
        "--file",
        default=None,
        help="the .code-workspace to read (default: the single one in the current directory)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan and exit without creating or renaming anything",
    )
    parser.add_argument(
        "--relabel",
        action="store_true",
        help="also rename an existing Space whose label differs from the file's \"name\"",
    )
    parser.add_argument(
        "--reason",
        default="manual",
        help="why this run happened (action|manual); logged, never branched on",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    env = os.environ
    launchers.refresh(env)
    session = config.resolve_session_name(env)

    try:
        refusal = guard(env)
    except config.ConfigError as exc:
        err("%s" % exc)
        return EXIT_FAIL
    if refusal is not None:
        err(refusal)
        return EXIT_REFUSED

    try:
        source = args.file if args.file else discover_workspace_file(discovery_dir(env))
        source = os.path.abspath(os.path.expanduser(source))
        if not os.path.isfile(source):
            raise AdoptError("no such workspace file: %s" % source)
        refs, warnings = read_folders(source)
    except AdoptError as exc:
        err("%s" % exc)
        return EXIT_FAIL

    for warning in warnings:
        err("warning: %s" % warning)

    try:
        spaces, _focused_id = herdr.load_spaces(env)
    except herdr.HerdrError as exc:
        err("cannot read Herdr state: %s" % exc)
        return EXIT_FAIL

    plan = plan_adoption(refs, spaces, args.relabel)
    describe_plan(plan, source, args.dry_run)

    if args.dry_run:
        summary(args.reason, session, source, plan, 0, 0, RESULT_DRY_RUN)
        return EXIT_OK

    todo = len(plan.of(ACTION_CREATE)) + len(plan.renames)
    if not todo:
        summary(args.reason, session, source, plan, 0, 0, RESULT_NOTHING)
        return EXIT_OK

    log("")
    created, renamed, failures = execute(plan, env)
    summary(args.reason, session, source, plan, created, renamed, RESULT_ADOPTED)
    return EXIT_FAIL if failures else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
