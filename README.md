# herdr-plugins

Plugins and research notes for [Herdr](https://herdr.dev), a terminal multiplexer for
coding agents.

## Plugins

| Plugin | What it does |
| --- | --- |
| [`vscode-workspace-sync/`](vscode-workspace-sync/) | Keeps the `folders` array of a VS Code multi-root `.code-workspace` file in sync with your Herdr Spaces — create, close, rename or reorder a Space and the VS Code explorer follows, with no window reload. Python 3.9+, standard library only, no build step. |
| [`agent-caffeinate/`](agent-caffeinate/) | Keeps your machine awake for exactly as long as your coding agents are working, and lets it sleep a minute after they stop. No config required. Python 3.9+, standard library only, no build step. |

Install a plugin straight from this repo:

```sh
herdr plugin install bmingles/herdr-plugins/vscode-workspace-sync
```

Or link a working tree while developing:

```sh
herdr plugin link ./vscode-workspace-sync
```

**Installing is not enough on its own.** Every plugin here also needs a `config.json` in
the directory printed by `herdr plugin config-dir <plugin-id>`, and does nothing until it
exists. Follow the plugin's own README for that — for this one,
[**vscode-workspace-sync/README.md → Install**](vscode-workspace-sync/README.md#install)
walks through the config file, a first `--doctor` run, and the initial sync.

Hook output is invisible except through `herdr plugin log list --plugin <plugin-id>`;
check it after any trigger rather than guessing.

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

`.plans/` tracks in-flight and completed work; `.plans/PLAN.md` is the index and the
source of truth for status.
