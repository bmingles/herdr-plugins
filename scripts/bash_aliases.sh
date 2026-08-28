# shellcheck shell=bash
# scripts/bash_aliases.sh -- source this from your bash/zsh config:
#
#     source /path/to/herdr-plugins/scripts/bash_aliases.sh
#
# Defines `herdrvs`: create Herdr Spaces from the .code-workspace file in the current
# directory. All arguments pass straight through to vscode-workspace-sync's `bin/adopt`,
# so `herdrvs --dry-run`, `herdrvs --file x.code-workspace` and `herdrvs --relabel` work.
#
#     herdrvs [--dry-run|--file X|--relabel]
#
# Only `adopt` gets a function. It is the one command here run from arbitrary project
# directories, so a short name earns its keep; the rest are reached by the literal launcher
# path their README gives, or a symlink of your choosing.
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
