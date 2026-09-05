# repospace

A command line tool for managing multi-repository workspaces.

A repospace is a directory tree of git repositories ("members") whose
composition is declared in a YAML manifest kept in a main project (the "manifest
repository"). The `repospace` tool clones and updates members to their declared
revisions, resolves manifests recursively (a member may declare its own
dependencies in its own manifest), loads extension commands provided by members,
and generates files that let CMake builds consume the repospace layout without
running any tool at configure time.

## Installation

```sh
pip install repospace
```

**Host system requirements:**

- Python >= 3.9
- git

## Quick start

Add a `repospace.yaml` to the root of your project (the manifest repository):

```yaml
manifest:
  version: '1.0'
  remotes:
    - name: upstream
      url-base: https://example.com/repos
  defaults:
    remote: upstream
  members:
    - name: mylib
      path: external/mylib
    - name: other
      url: https://elsewhere.com/other
      revision: v2.0
      path: external/other
```

Clone your manifest repository and initialize the repospace in one step:

```sh
repospace init git@githost:myorg/app
```

Or create the repospace inside an existing checkout:

```sh
cd app
repospace init
```

Then, from anywhere inside the repospace, fetch the members:

```sh
repospace update
repospace list
```

A repospace is always colocated with its manifest repository: `init` creates a
`.repospace/` directory, and the directory containing it is the repospace root.
The manifest is `<root>/repospace.yaml` by default; `init --manifest FILE`
selects another file inside the manifest repository and records it as the
`manifest.file` configuration option, the same choice an importing manifest has
with `import: file:`.

`update` clones every active member (and everything their own manifests declare,
recursively, according to the import rules) into its declared subdirectory,
points it at the manifest revision with a detached `HEAD`, and records that
revision in the repospace-owned branch `repospace-rev` of each member. Members
are cloned with `git init` plus `git remote add`, never `git clone`, and
repospace claims only the `repospace-rev` branch and the `origin` remote name.

Never commit `.repospace/`; `init` refuses a clone that contains it. Add it and
the member locations (`external/` in the example above) to the manifest
repository's `.gitignore`.

## Commands

| Command    | Purpose                                                            |
|------------|--------------------------------------------------------------------|
| `init`     | create a repospace in place or by cloning a manifest repository    |
| `update`   | update members to their manifest revisions                         |
| `list`     | print information about members, including staleness flags         |
| `manifest` | `--resolve`, `--freeze`, `--validate`, or `--path` of the manifest |
| `compare`  | compare member checkouts against the manifest                      |
| `diff`     | run `git diff` on members                                          |
| `status`   | run `git status` on members                                        |
| `forall`   | run a shell command in each member's directory                     |
| `grep`     | search members with `git grep`, ripgrep, or grep                   |
| `config`   | get or set configuration options                                   |
| `topdir`   | print the repospace top-level directory                            |
| `help`     | get help for repospace or a command                                |

Run `repospace help <command>` for details. The global flags `-v`, `-q`, and
`-V` are recognized before the command name only; everything after the command
name belongs to the command, so `diff`, `status`, and `grep` pass unknown
arguments through to the underlying tool untouched.

Members can provide additional commands through the `command-extensions`
manifest attribute; `repospace help` lists the extensions available in a
repospace, grouped by the member providing them. Command aliases are defined
with the `alias.<name>` configuration option.

## Manifest

See [docs/manifest.md](docs/manifest.md) for the complete manifest format:
sections (`version`, `defaults`, `remotes`, `members`, `self`, `group-filter`),
recursive imports with allowlist/blocklist filters, member groups, extension
commands, and CMake packages. A JSON Schema is at
[scripts/schemas/repospace-manifest-schema.json](scripts/schemas/repospace-manifest-schema.json).

Notes:

- The default revision is `main`. Members whose upstream default branch is
  `master` need an explicit `revision: master`.

- `repospace update` never deletes directories. A member removed from the
  manifest stays on disk; `repospace list` warns about it.

- Extension commands execute code from cloned repositories. Only use manifests
  you trust, or disable extensions with `repospace config
  commands.allow-extensions false`.

## CMake integration

Every `repospace update` regenerates two files in `<topdir>/.repospace/`, even
when some members failed, so they always describe what is on disk:

- `packages.cmake` sets `ENV{<name>_ROOT}` to the declaring member's absolute
  path for every name listed in a `cmake-packages` attribute, unless the
  variable is already set, so that `find_package()` finds packages provided by
  members. The first declaration of a name wins, and inactive members are
  skipped. The file starts with a guard: the cache variable
  `REPOSPACE_UPDATE_HASH` records a hash of the repospace state at the first
  configure, and a later configure with a different hash fails with a message
  asking for `cmake --fresh`. The hash changes whenever a member is added,
  removed, moved, activated or deactivated by a group filter, or checked out at
  a different commit.

- `members.json` holds every resolved member as a flat array in resolution order
  (`name`, `path`, `abspath`, `url`, `revision`, `sha`, `groups`,
  `cmake-packages`, `command-extensions`, `declared-by`), beside `topdir` and a
  format `version`, for consumption with `string(JSON)`. `sha` is the commit
  recorded in `repospace-rev` at the last update and `declared-by` the manifest
  that declared the member.

Use them from a top-level `CMakeLists.txt`:

```cmake
cmake_minimum_required(VERSION 3.24)
# Colocated layout: .repospace/ sits next to this CMakeLists.txt.
include(${CMAKE_CURRENT_LIST_DIR}/.repospace/packages.cmake)
project(myapp)
```

Both files are written only when their content changes, so they are safe to list
in `CMAKE_CONFIGURE_DEPENDS`. Only `repospace update` refreshes them; git
operations done by hand inside members do not.

## Configuration

Git-style INI configuration, with options named `section.key`, at three levels.
Local wins over global, and global over system.

| Level  | File                                                                                                                     | Override                  |
|--------|--------------------------------------------------------------------------------------------------------------------------|---------------------------|
| system | `/etc/repospace-config` (`%PROGRAMDATA%\repospace\config` on Windows)                                                    | `REPOSPACE_CONFIG_SYSTEM` |
| global | `~/.repospace-config` if it exists, else `$XDG_CONFIG_HOME/repospace/config` (`XDG_CONFIG_HOME` defaults to `~/.config`) | `REPOSPACE_CONFIG_GLOBAL` |
| local  | `<topdir>/.repospace/config`                                                                                             | `REPOSPACE_CONFIG_LOCAL`  |

Like git config, section and key names are case-insensitive and stored in
lowercase. Recognized options:

| Option                      | Meaning                                                                                                       | Default          |
|-----------------------------|---------------------------------------------------------------------------------------------------------------|------------------|
| `manifest.file`             | top-level manifest file, relative to the root; written by `init --manifest`                                   | `repospace.yaml` |
| `manifest.group-filter`     | comma-separated `+group`/`-group` entries applied on top of the manifest's own filters                        | none             |
| `commands.allow-extensions` | load extension commands from members                                                                          | `true`           |
| `color.ui`                  | colorize output                                                                                               | `true`           |
| `update.fetch`              | fetch strategy: `smart` (no fetch when the revision is a tag or commit already available locally) or `always` | `smart`          |
| `update.narrow`             | fetch only the manifest revision, not all branches and tags                                                   | `false`          |
| `update.sync-submodules`    | run `git submodule sync` before updating submodules                                                           | `true`           |
| `grep.tool`                 | `git-grep`, `ripgrep`, or `grep`                                                                              | `git-grep`       |
| `grep.<tool>-args`          | extra arguments for that tool, shell-quoted                                                                   | none             |
| `grep.<tool>-path`          | path to that tool's executable                                                                                | none             |
| `alias.<name>`              | command alias; the value is a repospace command line, shell-quoted                                            | none             |

## Development

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pre-commit install
pytest
```

`./bootstrap` (`bootstrap.cmd` on Windows) creates the venv, installs the
project with its dev dependencies and the pre-commit hooks, provisions Node.js
inside the venv for `markdownlint-cli2` (which the pre-commit hooks run on
Markdown files), and configures the repository's commit message template.

### VS Code

The task "Project: Reload" in `.vscode/tasks.json` runs on folder open, invokes
the bootstrap script and then selects `.venv` as the project interpreter. The
second step is there because the Python Environments extension picks an
interpreter only at its own startup, which on a fresh clone happens before
bootstrap has created `.venv`. The selection goes through the
`pythonVenvSelect.select` command registered by the startup macro
`.vscode/macros/select-venv.js`, loaded by the damolinx-macros extension listed
in `.vscode/extensions.json`. Only terminals opened after the task has finished
have the venv activated. To repeat the sequence later, run "Project: Reload"
from Terminal > Run Task.
