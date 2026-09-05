# Extension commands

An extension command is a `repospace` subcommand provided by a member (or by the
manifest repository itself). It is implemented as a Python class in a file
inside the member (or manifest) repository; one file may provide several
commands. The entry point is declared by an accompanying YAML specification, and
it runs like a built-in: `repospace help` lists it with the member attribution,
`repospace help <name>` prints its argument help, and `repospace <name> ...`
runs it inside the repospace.

**Warning**: Extension commands execute code from cloned repositories with the
user's privileges. Only use manifests you trust, or disable extensions with
`repospace config commands.allow-extensions false`.

## Declaring extension commands

A member opts in through the `command-extensions` manifest attribute (see
[the manifest format](manifest.md)). Its value is one specification file, or a
list of them, relative to the member root:

```yaml
manifest:
  members:
    - name: tools
      url: https://example.com/tools
      path: external/tools
      command-extensions: repospace-commands.yaml
    - name: other
      url: https://example.com/other
      path: external/other
      revision: v2.0
```

The attribute may also appear under `self:`, in which case the paths are
relative to the manifest repository root and `repospace help` lists the commands
under `member manifest (path: .)`. When a manifest imported from a member
declares `command-extensions` under its own `self:` section, the commands are
attributed to that member, so a repository can ship its own commands without the
importing manifest naming them.

The specification file, conventionally named `repospace-commands.yaml`, has one
key, `command-extensions`, holding a list of entries:

```yaml
command-extensions:
  - file: scripts/revisions.py
    commands:
      - name: revisions
        class: Revisions
        help: print the declared revision of each member
```

| Key          | Required   | Meaning                                                                                                                            |
|--------------|------------|------------------------------------------------------------------------------------------------------------------------------------|
| `file`       | yes        | Python file, relative to the member root                                                                                           |
| `commands`   | yes        | List of commands the file provides                                                                                                 |
| `name`       | yes        | Command name; the class must register the same name                                                                                |
| `class`      | no         | Module attribute holding the command class; defaults to `name`, exactly as written (`name: probe` looks up `probe`, not `Probe`)   |
| `help`       | no         | One-line text shown by `repospace help`; defaults to `(no help provided; try "repospace <name> -h")`                               |

Both the specification path and each `file` must resolve inside the member
directory: a path that escapes it, through `..`, an absolute path, or a symbolic
link, is rejected. One file may provide several commands, and one specification
may list several files.

## Discovery and precedence

Extension commands are discovered on every invocation that reaches a command,
after the manifest has been resolved. Nothing is imported at that point: the
specification files are read, the Python files are not. A member whose
specification file does not exist (typically because it is not cloned yet) is
skipped silently.

When the manifest cannot be loaded, extensions are unavailable and no warning is
printed: `repospace help` says that they cannot be loaded, and invoking one
fails as an unknown command with a hint. Before the first `repospace update`, a
manifest that imports from a member cannot be resolved, so its extension
commands do not exist yet.

Extensions are also unavailable, with a warning, when:

- a specification file is unreadable, not UTF-8, not valid YAML, or does not
  have the layout above. One broken file disables all extension commands, and
  the warning names the file and the problem;
- `commands.allow-extensions` is not a boolean.

Command names are resolved in this order:

1. Built-in commands. An extension using a built-in name is ignored with a
   warning.
2. Extension commands: the manifest repository's own (`self:`) first, then the
   members in manifest resolution order (self-imports, then the top-level file,
   then member imports). When two members provide the same name, the first one
   wins and the other is ignored with a warning.
3. Aliases (`alias.<name>` configuration options). An alias never shadows a real
   command, but it may expand to an extension command.

Setting `commands.allow-extensions` to `false` disables discovery; the commands
are then unknown. The usual configuration precedence applies, so a local `true`
overrides a global `false`.

## Writing a command

An extension command is a class that:

- subclasses `repospace.commands.RepospaceCommand`;
- has a constructor that takes no arguments and calls the base constructor with
  the command's name, one-line help, and description;
- implements `do_add_parser(self, parser_adder)`, which must return the parser
  created by `parser_adder.add_parser(self.name, ...)`;
- implements `do_run(self, args, unknown)`.

The base constructor accepts:

| Argument                 | Default           | Meaning                                                                                                 |
|--------------------------|-------------------|---------------------------------------------------------------------------------------------------------|
| `name`                   | required          | Command name; must equal the `name` in the specification                                                |
| `help`                   | required          | One-line help (use it for `add_parser(help=...)`)                                                       |
| `description`            | required          | Longer text for the command's own `--help`                                                              |
| `accepts_unknown_args`   | `False`           | Accept arguments the parser does not know; otherwise they are an error                                  |
| `requires_repospace`     | `True`            | Fail when run outside a repospace. Extension commands only exist inside one, so this is always met      |
| `verbosity`              | `Verbosity.INF`   | Base verbosity before the global `-v`/`-q` flags are applied                                            |
| `forward_dashdash`       | `False`           | Hand everything after the first `--` to `do_run` unparsed; requires `accepts_unknown_args`              |

`do_add_parser` receives the `argparse` subparsers object. Add positional
arguments and options to the parser it returns; `-h`/`--help` is added by
`argparse`. `repospace.commands.HelpFormatter` may be passed as
`formatter_class` to re-wrap a multi-paragraph description and a hand-written
argument list to the terminal width.

`do_run` receives the parsed `argparse` namespace and the list of unknown
arguments (empty unless `accepts_unknown_args` is set). Its return value is
ignored; see [Errors and exit codes](#errors-and-exit-codes) for how to fail.

### Example

The `tools` member declared above ships a command that prints each active
member's revision. The example consists of the specification file shown under
[Declaring extension commands](#declaring-extension-commands),
`scripts/revisions.py`:

```python
"""Extension commands provided by the tools member."""

from repospace.commands import RepospaceCommand


class Revisions(RepospaceCommand):
    def __init__(self):
        super().__init__(
            "revisions",
            "print the declared revision of each member",
            "Print the name and the declared revision of each active "
            "member, one per line.",
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(
            self.name, help=self.help, description=self.description
        )
        parser.add_argument(
            "--sha",
            action="store_true",
            help="print the commit recorded by the last update instead",
        )
        return parser

    def do_run(self, args, unknown):
        manifest = self.manifest
        # members[0] is the manifest repository itself.
        for member in manifest.members[1:]:
            if not manifest.is_active(member):
                continue
            revision = member.revision
            if args.sha:
                if not member.is_cloned():
                    self.wrn(f"{member.name_and_path} is not cloned")
                    continue
                revision = member.sha()
            print(f"{member.name:20} {revision}")
```

and the manifest entry with `command-extensions: repospace-commands.yaml`. After
`repospace update` has cloned the members:

```console
$ repospace help
...
extension commands from member tools (path: external/tools):
    revisions:        print the declared revision of each member
...
$ repospace revisions
tools                main
other                v2.0
$ repospace help revisions
usage: repospace revisions [-h] [--sha]

Print the name and the declared revision of each active member, one per line.

options:
  -h, --help  show this help message and exit
  --sha       print the commit recorded by the last update instead
```

## What a command can use

The dispatcher fills these in before `do_run`:

- `self.topdir`: absolute path of the repospace top-level directory.
- `self.manifest`: the resolved `repospace.manifest.Manifest`.
- `self.config`: the `repospace.configuration.Configuration` (system, global,
  and local files merged).
- `self.verbosity`: a `repospace.commands.Verbosity` value, already adjusted by
  the global `-v`/`-q` flags.
- `self.parser`: the parser returned by `do_add_parser`, for
  `self.parser.error(...)` and `self.parser.print_help()`.

### Manifest and members

`Manifest.members` lists the resolved members in resolution order; the first
entry is the manifest repository itself (a `ManifestMember`). Useful attributes
and methods:

- `get_members(ids, allow_paths=True)`: members for a list of names or paths
  (all when the list is empty); raises `ValueError` with the unknown ids.
- `is_active(member, extra_filter=None)`: whether the member survives the group
  filter; inactive members are what `update` skips.
- `group_filter`, `topdir`, `repo_abspath`, `abspath`, `yaml_name`, `userdata`:
  resolved filter, paths, and the top-level `self:` values.
- `as_dict()`, `as_yaml()`, `as_frozen_dict()`, `as_frozen_yaml()`: the resolved
  manifest as data or text, optionally with revisions frozen to commit SHAs.

Each `Member` exposes the manifest attributes (`name`, `path`, `url`,
`revision`, `groups`, `userdata`, `description`, `submodules`, `clone_depth`,
`cmake_packages`, `command_extensions`), `declared_by` (`manifest` when the
manifest repository's files declared it, otherwise the name of the member whose
import did), plus:

- `abspath`, `posixpath`: the absolute checkout path (or `None` without a
  topdir); `name_and_path` is `"name (path)"` for messages.
- `is_cloned()`: whether the directory is a git repository root.
- `git(args, check=True, capture_stdout=False, capture_stderr=False, cwd=None)`:
  run git in the member; returns a `subprocess.CompletedProcess`.
- `sha(rev="refs/heads/repospace-rev", capture_stderr=False)`: the commit a
  revision peels to; by default the one recorded by the last `update`.
- `is_ancestor_of(rev1, rev2)` and `read_at(path, rev=...)`: ancestry check and
  blob contents from the member's git object store.

### Configuration

`Configuration.get(option, default=None, configfile=ConfigFile.ALL)` reads a
`section.key` option as a string; `getboolean`, `getint`, and `getfloat` convert
it and raise `MalformedConfig` on a bad value.
`set(option, value, configfile=ConfigFile.LOCAL)`,
`delete(option, configfile=None)` (by default from the highest-precedence file
where it is set), and `items(configfile=ConfigFile.ALL)` complete the API.
`ConfigFile` selects `ALL`, `SYSTEM`, `GLOBAL`, or `LOCAL`. Extension commands
may define their own sections; a section name may contain letters, digits, `-`,
and `_`.

### Output

Use `print` for the command's data. The helpers below honor verbosity and
`color.ui`, and go to stderr where noted:

| Helper                          | Shown when verbosity is   | Notes                                                      |
|---------------------------------|---------------------------|------------------------------------------------------------|
| `dbg(*args, level=DBG)`         | at least `level`          | `-v` reaches `DBG`, `-vv` `DBG_MORE`, `-vvv` `DBG_EXTREME` |
| `inf(*args, colorize=False)`    | at least `INF`            | the default level; silenced by `-q`                        |
| `banner(*args)`                 | at least `INF`            | `===` prefix, bold green                                   |
| `small_banner(*args)`           | at least `INF`            | `---` prefix                                               |
| `wrn(*args)`                    | at least `WRN`            | `WARNING:` prefix, yellow, stderr                          |
| `err(*args, fatal=False)`       | at least `ERR`            | `ERROR:` or `FATAL ERROR:` prefix, red, stderr             |
| `die(*args, exit_code=1)`       | at least `ERR`            | `err(..., fatal=True)` then exit                           |

### Subprocesses

`check_call`, `check_output`, and `run_subprocess` wrap the `subprocess`
functions of the same names and log the command line at `-vv`. `die_if_no_git()`
exits with a clear message when git is not installed.

### Errors and exit codes

- `self.die(message)` prints `FATAL ERROR: message` and exits with status 1 (or
  `exit_code`).
- `self.parser.error(message)` prints usage and the message and exits with
  status 2, the `argparse` convention for bad arguments.
- Raising `repospace.commands.CommandError(returncode)` exits with that status
  and prints nothing; print the diagnostic first.
- A `subprocess.CalledProcessError` that escapes `do_run` is reported as
  `FATAL ERROR: command exited with status N: <command line>` and repospace
  exits with the same status. A child killed by signal S is reported as
  `command died on signal S` instead, and the exit status is 128+S.
- A `KeyboardInterrupt` exits with status 130.
- A `repospace.git.GitNotFound` or `repospace.configuration.MalformedConfig` is
  reported as `FATAL ERROR: <message>` with status 1.
- A `BrokenPipeError` exits with status 0.

Any other exception produces a Python traceback.

## Arguments and verbosity

The global flags `-h`, `-V`, `-v`, and `-q` are recognized before the command
name only. Everything after the command name belongs to the command: with
`accepts_unknown_args`, an option the parser does not know (including a `-v`
placed after the command name) is passed to `do_run` untouched, so a command
wrapping another tool can relay it.

With `forward_dashdash` (which requires `accepts_unknown_args`), the command
line is split at the first `--`: `argparse` only sees what comes before it, and
everything after it (a later `--` included) is appended to `unknown`. Without
it, `--` has its usual `argparse` meaning.

`repospace help <name>` and `repospace <name> -h` print the command's own help;
both import the module. `repospace help` on its own does not import anything and
shows the `help` text from the specification.

## Module loading

The Python file is imported the first time the command is run or its help is
requested in a process, not at discovery, so a module that fails to import still
shows up in `repospace help`. It is executed from its path under a generated
module name (`repospace.commands.ext.cmd_1`, ...), so `__name__` is not the
file's stem while `__file__` is the file's path.

The file's directory is appended to `sys.path` (after the standard library and
installed packages) before the import, and removed again if the import fails, so
sibling helper modules can be imported by name (`import helper`). Relative
imports (`from . import helper`) do not work. Sibling modules share one global
namespace: when two extensions each ship a `helper.py`, both see whichever was
imported first.

A file is imported once per process, keyed by its resolved path. Any failure is
reported as
`FATAL ERROR: extension command <name> could not be created: <reason>` with exit
status 1 and no traceback. The reasons are: the module could not be imported (an
exception or `sys.exit()` at import time), the module has no attribute named by
`class`, the constructor raised, the object is not a `RepospaceCommand`, or the
object registered a name other than the one in the specification.
