# herdr-plugins

Plugins and research notes for [Herdr](https://herdr.dev), a terminal multiplexer for
coding agents.

All three are Python 3.9+, standard library only, no build step, no dependencies. macOS and
Linux, Herdr 0.8.0 or newer.

## Plugins

| Plugin | What it does | Config needed |
| --- | --- | --- |
| [`agent-caffeinate/`](agent-caffeinate/) | Keeps your machine awake for exactly as long as your coding agents are working, and lets it sleep a minute after they stop. Optional `☕ caffeinate` tab bar indicator. | none |
| [`workspace-time-tracker/`](workspace-time-tracker/) | Records how long you actually spend in each Space. Entries close on a switch, and close backdated to your last activity after a minute of quiet — so idle time is never billed as work. `track report` reads it back. | none |
| [`vscode-workspace-sync/`](vscode-workspace-sync/) | Keeps the `folders` array of a VS Code multi-root `.code-workspace` file in sync with your Herdr Spaces — create, close, rename or reorder a Space and the VS Code explorer follows, with no window reload. `adopt` goes the other way, creating Spaces *from* a workspace file you already have. | **yes** — one JSON file naming the workspace file |

Install one:

```sh
herdr plugin install bmingles/herdr-plugins/agent-caffeinate
```

Then follow that plugin's README — each one's **Setup** section is four steps and gives
every path literally.

Two things that trip up every Herdr plugin, including these:

- **Installing does not start anything.** Startup hooks run when a Herdr *server* boots. All
  three plugins also recover on the next Space switch, which is the quickest way to start
  them without restarting Herdr.
- **Hook output is invisible** except through `herdr plugin log list --plugin <plugin-id>`,
  JSON-escaped. That is why each plugin's real diagnostic is a `doctor` command you run in a
  terminal, never a plugin action.

Each plugin publishes its terminal commands at a fixed path under
`~/.local/state/herdr/plugins/<plugin-id>/`, refreshed on every run, so the READMEs can hand
out a literal path that survives a reinstall. No setup needed — nothing here asks you to
change your `PATH`. Symlink one onto your `PATH` if you use it often; each README says
where.

## Scripts

`scripts/` holds shell glue meant to be **sourced**, not installed as a plugin.

| File | Defines |
| --- | --- |
| [`scripts/bash_aliases.sh`](scripts/bash_aliases.sh) | `herdrvs` — `vscode-workspace-sync`'s `adopt`, reachable from any project directory. |

```sh
source /path/to/herdr-plugins/scripts/bash_aliases.sh
cd ~/code/my-project && herdrvs --dry-run
```

Locators only: each finds the plugin (the fixed launcher first, then this checkout) and
passes every argument through. Every decision lives in the plugin.

Note that adopt and sync are **mutually exclusive per Herdr session**: a session either has
a configured `workspaceFile` and mirrors Herdr into it, or it has none and can import one.
Adopt refuses, exiting 2, in a session sync manages.

## Development

```sh
herdr plugin link ./agent-caffeinate                       # run a working tree
cd agent-caffeinate && python3 -m unittest discover -s test
```

`herdr plugin link` picks up source changes on the next hook invocation — there is nothing
to build. The floor is **Python 3.9**, which stock macOS ships; the tests are the only thing
that catches a 3.10+ construct.

## Docs

| Document | Contents |
| --- | --- |
| [`docs/herdr-research-notes.md`](docs/herdr-research-notes.md) | How Herdr's plugin system, socket API and agent detection actually behave, established largely by experiment. |
| [`docs/herdr-vscode-sync-facts.md`](docs/herdr-vscode-sync-facts.md) | Pasted host output from the discovery run behind `vscode-workspace-sync`: `api snapshot` shapes, which plugin event hooks fire, the server environment, and VS Code's folder live-reload behaviour. |
| [`docs/herdr-daemon-facts.md`](docs/herdr-daemon-facts.md) | Whether a plugin daemon survives its hook (yes) and its server (also yes, which is a problem), the real `events.subscribe` contract, and which signals actually reveal that a human is working. |
| [`docs/example-vscode-workspace.md`](docs/example-vscode-workspace.md) | A representative `.code-workspace` file, and the primary test fixture for `vscode-workspace-sync`. |

## Skills

`.claude/skills/` holds the agent-facing distillations of the above:
`herdr-plugin-authoring` (manifest, plugin environment, socket API) and
`herdr-devcontainer-agent-running` (surfacing a containerised agent's status via
`HERDR_AGENT`).

## Plans

`.plans/` tracks in-flight and completed work; `.plans/PLAN.md` is the index and the source
of truth for status.
