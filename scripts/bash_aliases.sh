# shellcheck shell=bash
# scripts/bash_aliases.sh -- source this from your bash/zsh config:
#
#     source /path/to/herdr-plugins/scripts/bash_aliases.sh
#
# Defines:
#
#   `herdrvs`  create Herdr Spaces from the .code-workspace file in the current
#              directory. All arguments pass straight through to vscode-workspace-sync's
#              `bin/adopt`, so `herdrvs --dry-run`, `herdrvs --file x.code-workspace` and
#              `herdrvs --relabel` work.
#
#                  herdrvs [--dry-run|--file X|--relabel]
#
#   `herdrs`   run `herdr` with `--session` named after the current directory: its path
#              relative to $HOME (or its absolute path, outside it or for $HOME itself),
#              one part per folder joined with `.`. Each folder name is lowercased, `.`
#              in it made `_`, each run of other characters outside [a-z0-9_-] made one
#              `-`, and `-` trimmed from both ends; folders left empty are dropped. herdr
#              sockets must fit in ~104 bytes, so a name over 40 characters keeps the whole
#              folders at its end that fit and gains a hash of the whole name. ~/code/tools/devc-vscode gives
#              `code.tools.devc-vscode`. devc-vscode's sessionNameForDir applies the same
#              rule to find a window's session, so keep the two in step. Additional
#              arguments pass through, e.g. `herdrs space`.
#
#                  herdrs [args...]
#
# These are the commands here run from arbitrary project directories, so short names
# earn their keep; the rest are reached by the literal launcher path their README gives,
# or a symlink of your choosing.
#
# This is a locator and nothing else -- every decision (path resolution, deduping against
# live Spaces, the sync/adopt mutual-exclusivity guard) lives in
# vscode-workspace-sync/src/adopt.py. It looks first at the fixed launcher the plugin
# maintains in its state directory (which points at the installed *or* linked plugin, and
# follows it across reinstalls), then falls back to the checkout this file was sourced from.
#
# Entirely optional: nothing in the plugins depends on this file being sourced.

# bash sets BASH_SOURCE when sourcing; zsh leaves it unset and puts the path in $0.
if [ -n "${BASH_SOURCE[0]-}" ]; then
    _herdr_plugins_src="${BASH_SOURCE[0]}"
else
    _herdr_plugins_src="$0"
fi
_herdr_plugins_root=$(cd "$(dirname "$_herdr_plugins_src")/.." && pwd)

# _herdr_plugin_cmd <plugin-id> <command-name> -> prints an executable path, or nothing.
_herdr_plugin_cmd() {
    local launcher="$HOME/.local/state/herdr/plugins/$1/$2"
    if [ -x "$launcher" ]; then
        echo "$launcher"
        return 0
    fi
    local fallback="$_herdr_plugins_root/$1/bin/$2"
    if [ -x "$fallback" ]; then
        echo "$fallback"
        return 0
    fi
    echo "herdr-plugins: cannot find '$2' for plugin '$1'." >&2
    echo "  Install it:  herdr plugin install bmingles/herdr-plugins/$1" >&2
    echo "  Or link it:  herdr plugin link $_herdr_plugins_root/$1" >&2
    echo "  The launcher appears once the plugin has run at least once." >&2
    return 127
}

herdrvs() {
    local cmd
    cmd=$(_herdr_plugin_cmd vscode-workspace-sync adopt) || return 127
    "$cmd" "$@"
}

# _herdr_session_name <dir> -> prints the session name `herdrs` uses for <dir>.
_herdr_session_name() {
    local dir="${1%/}" home="${HOME%/}" rel name hash
    case "$dir" in
        "$home"/?*) rel="${dir#"$home"/}" ;;
        *) rel="${dir#/}" ;;
    esac
    name=$(printf '%s' "$rel" | LC_ALL=C tr '[:upper:].' '[:lower:]_' |
        LC_ALL=C sed -E 's#[^a-z0-9_/-]+#-#g; s#-*/-*#/#g; s#^-+##; s#-+$##; s#/+#/#g; s#^/##; s#/$##' |
        tr '/' '.')
    if [ "${#name}" -gt 40 ]; then
        if command -v sha1sum >/dev/null 2>&1; then
            hash=$(printf '%s' "$name" | sha1sum)
        else
            hash=$(printf '%s' "$name" | shasum)
        fi
        hash="${hash:0:6}"
        local tail="${name: -33}"
        # Start at a folder boundary rather than partway through a folder's name.
        if [ "${name: -34:1}" != . ] && [[ "$tail" == *.* ]]; then
            tail="${tail#*.}"
        fi
        tail=$(printf '%s' "$tail" | sed -E 's/^[^a-z0-9]+//')
        name="$tail-$hash"
    fi
    printf '%s' "$name"
}

herdrs() {
    local name
    name=$(_herdr_session_name "$PWD")
    if [ -z "$name" ]; then
        echo "herdrs: cannot name a session after '$PWD'." >&2
        return 2
    fi
    herdr --session "$name" "$@"
}
