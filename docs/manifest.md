# The repospace manifest format

A manifest is a YAML file, conventionally named `repospace.yaml`, with a single
top-level `manifest` key. `repospace.yaml` is the default entry point
everywhere: for the repospace itself, another file inside the manifest
repository can be selected with `init --manifest` (recorded as the
`manifest.file` configuration option), just as an importing manifest can name a
file with `import: file:`. All sections are optional:

```yaml
manifest:
  version: '0.2'
  defaults:
    remote: upstream
    revision: main
  remotes:
    - name: upstream
      url-base: https://example.com/repos
  members:
    - name: mylib
    - name: other
      url: https://elsewhere.com/other
      revision: v2.0
      path: libs/other
  self:
    name: app
    cmake-packages: [App]
  group-filter: [-optional]
```

Validation is strict, because what a manifest describes is carried out by
creating directories and moving checkouts: a defect that survives parsing does
not stay a data problem, it becomes a wrong tree on disk.

- Unknown keys are rejected everywhere, so a misspelled attribute is reported
  rather than quietly ignored.

- So are keys with an explicit null value (e.g. a `groups:` whose entries were
  all commented out) — remove the key instead; only `manifest:` and `userdata:`
  may be null. Everything else is typed non-null, and a null left in place
  would be coerced downstream instead of refused (`str(None)` is the revision
  `"None"`) or would break path derivation outright.

- Empty strings are rejected wherever a value would otherwise silently fall
  back to a default or change meaning (`name`, `remote`, `repo-path`, `url`,
  `path`, `revision`, `remotes` entries, `import` paths, `import: file:`, and
  the `path-allowlist`/`path-blocklist` patterns). An empty `path`, for one,
  reads as "the directory this is relative to", which is never what an entry
  means to say.

A machine-readable JSON Schema of this format is at
[scripts/schemas/manifest-schema.json](../scripts/schemas/manifest-schema.json).
The installed package ships the same file under the environment prefix, as
`share/repospace/schemas/manifest-schema.json` — for a virtual environment,
`<venv>/share/repospace/schemas/manifest-schema.json` — so an editor can be
pointed at the copy that belongs to the repospace it is editing.

## version

The minimum schema version required to parse the file, as a string (quote it —
YAML would otherwise parse `0.2` as a number, which is tolerated but warned
about in errors; `0.10` unquoted is the number `0.1`, a different version).

A schema version is the `major.minor` of the repospace release that last changed
the manifest format, so a release adding no manifest feature has no schema
version of its own. The current, and so far only, schema version is `0.2`, which
shipped with repospace 0.2.0.

The value must name a released schema version exactly. A newer one fails with an
error asking to upgrade repospace — that is what the key buys, and it is
reported before any other check, so a file written for a later repospace is
answered with "upgrade" rather than with a list of versions that could not
possibly contain what it asks for. Anything else is rejected as invalid with the
list of versions this repospace accepts, including a spelling that merely
compares equal such as `0.2.0`: one version has one spelling, or two manifests
declaring the same thing would not compare equal as text.

The declared version is not a claim repospace checks the file against: nothing
verifies that a file declaring an older version restricts itself to what that
version offered. It only says which repospace can read the file.

The version is checked per file, including imported ones, before anything else —
an imported file comes from another repository and may well have been written
for a repospace newer than the one reading it.

## defaults

- `remote`: remote used by members that give neither `remote` nor `url`.
- `revision`: revision for members without one (default: `main`).

Like every revision, the default must be a plausible git refname or commit SHA:
no leading `-` or `+`, and none of the characters a refname cannot contain
(whitespace, `~`, `^`, `:`, `?`, `*`, `[`, `\`, `..`, `@{`). The rule is not
pedantry about refname syntax; a revision reaches git twice, as a fetch refspec
and as a revision argument, and each refused character has a meaning of its own
in those positions. `:` separates a refspec's source from its destination, so it
would let a manifest move a local branch of the member; `*` makes the refspec a
pattern; `^`, `~`, `..` and `@{` are revision operators; `?`, `[` and `\` are
glob syntax; whitespace and control characters split or hide the argument. A
leading `-` would be read as a command-line option by the git commands that take
a revision, and a leading `+` marks a refspec as forced for the name that
follows it, so `update` would fetch a branch other than the one named (write
`refs/heads/<name>` instead).

For the same reason a revision must be a string and never a bare number: YAML
mangles unquoted numeric revisions (`1.10` becomes the float `1.1`, `0700` an
octal integer), and a number where a revision belongs is almost always a missing
pair of quotes.

`defaults` and `remotes` apply only to the file that contains them; they are
never inherited across imports, so a manifest resolves to the same members
whether it is read as the top-level file or through an importer that happens to
define names of its own.

## remotes

Named URL prefixes. Each entry needs `name` and `url-base`; names must be unique
within the file, since a repeated name would let the later entry win silently
and make the URL a member gets depend on declaration order. A member using a
remote gets the fetch URL `url-base + "/" + (repo-path or name)`; trailing
slashes on `url-base` are dropped.

Remote names are manifest-internal identifiers for URL prefixes: in a cloned
member the git remote is always named `origin`, however the URL was declared.
Nothing about the name a manifest chose survives into the checkout, which is
what lets an imported manifest use its own names without colliding with the
importing one.

## members

Each entry accepts:

| Attribute            | Type             | Default             | Meaning                                                                                                              |
|----------------------|------------------|---------------------|----------------------------------------------------------------------------------------------------------------------|
| `name`               | string, required | —                   | Unique identifier (`manifest` is reserved; no `/` or `\`)                                                            |
| `description`        | string           | none                | Informational                                                                                                        |
| `remote`             | string           | `defaults.remote`   | Named remote for the fetch URL                                                                                       |
| `repo-path`          | string           | `name`              | Suffix appended to the remote's `url-base`                                                                           |
| `url`                | string           | derived             | Complete fetch URL (mutually exclusive with `remote`/`repo-path`)                                                    |
| `revision`           | string           | `defaults.revision` | Branch, tag, or commit SHA (refname-safe: no leading `-`/`+`, no `~^:?*[\`, whitespace, `..`, `@{`)                  |
| `path`               | string           | `name`              | Checkout path relative to the accumulated import `path-prefix`, or to the repospace top when there is none           |
| `submodules`         | bool or list     | `false`             | `true` = update all recursively; or a list of `{path, name}`                                                         |
| `clone-depth`        | positive int     | none                | Passed to `git fetch --depth`                                                                                        |
| `extension-commands` | string or list   | none                | Extension command specification file(s), relative to the member root                                                 |
| `cmake-packages`     | string or list   | none                | CMake package names whose `<name>_ROOT` should point at this member                                                  |
| `import`             | see below        | none                | Import the member's own manifest(s)                                                                                  |
| `groups`             | list             | `[]`                | Group membership (mutually exclusive with `import`)                                                                  |
| `upstream`           | mapping          | none                | The repository this member's origin forks, and the refs `repospace mirror` keeps equal to it (see below)             |
| `userdata`           | any              | none                | Ignored by repospace                                                                                                 |

`manifest` is reserved as a name because it is what repospace calls the manifest
repository itself: it is also the value `declared-by` holds for a member the
manifest repository declares. A member of that name could not be told from it.

### Member paths

Member paths use forward slashes and are confined to the repospace. The
following are rejected lexically, before anything touches the filesystem:

- backslashes — member paths are POSIX paths and normalized by POSIX rules,
  under which a backslash is an ordinary filename character and not a
  separator, so `libs\x` would name one directory rather than two;
- absolute paths and, on Windows, drive letters: a drive-relative path such as
  `C:evil` is not "absolute", yet joining it discards the repospace top
  entirely;
- `..` escapes, checked on the first path component after normalization (`..`
  as a whole component, so a directory legitimately named `..foo` is fine);
- `.git` path components, in any case, since some filesystems are
  case-insensitive — member content must never land where git treats it as a
  git directory, hooks included, which git would execute on the next command;
- placement inside `.repospace/`, which belongs to the tool.

A path is normalized before it is used, so `libs/x/../y` *is* `libs/y` — that is
the placement, and the form re-emitted by `manifest --resolve`; normalizing
first is what keeps the checks above, the checkout location and the resolved
output all speaking of one directory. A path that normalizes to the repospace
top itself (or, in an imported manifest, to its `path-prefix` directory) is
rejected: that is a member with no directory of its own. Confinement is to the
repospace top, not to the `path-prefix`: under a prefix of `external`,
`path: ../tools/foo` places the member at `tools/foo`.

Member checkouts live in real directories inside the repospace: no existing
component of a member path below the repospace top may be a symbolic link — the
member's own directory included — so that neither a link committed in a member
nor one committed in the manifest repository can redirect a checkout elsewhere.
Components that do not exist yet are fine; `update` creates them as directories.
The repospace top itself, and anything above it, may be a symlink: that
placement is the user's own doing, not something manifest data can steer.

### upstream

A member with an `upstream` declares that its own repository — `origin`, the one
its `url` names — is a fork of that upstream. As independent maintainers work
on upstream we can use `repospace mirror` to push branches and tags from there
into `origin`.

```yaml
members:
  - name: foobar
    url: git@github.com:me/foobar
    upstream:
      url: git@github.com:joedoe/foobar
      mirror:
        heads: [main, 'release/*']
        tags: ['v[0-9]*']
      preserve:
        heads: ['forked/*']
        tags: ['forked/*']
```

The upstream repository is named exactly as a member is — `url`, or `remote`
plus `repo-path` (default: the member name), falling back to `defaults.remote` —
and must resolve to a URL different from the member's own.

`mirror` selects what is copied from upstream, `preserve` what belongs to
`origin` alone; each takes `heads` and `tags` lists of patterns, and each list
left out keeps its default. `mirror` defaults to every branch and every tag,
`preserve` to nothing.

`mirror: tags: []` is the one list that may be written empty, and it mirrors no
tag at all — the only way to say so, since leaving the key out mirrors every tag
instead. `mirror: heads` may not: upstream always has a default branch, so a
mirror is always for at least one.

A mirror run pushes to `origin` every upstream branch its `mirror: heads`
patterns select and its `preserve: heads` patterns do not, and every upstream
tag its `mirror: tags` patterns select and its `preserve: tags` patterns do not.
`preserve` always wins over `mirror`: a preserved ref is never pushed to, even
where upstream has a ref of the same name, so work that exists only on `origin`
is never overwritten by an upstream ref that collides with it. Mirrored refs, on
the other hand, belong to upstream: they are force-updated, and any commit
`origin` alone held on one of them is dropped.

Deleting is opt-in. With `repospace mirror --prune`, every `origin` branch and
tag that is neither selected by `mirror` nor selected by `preserve` is deleted,
so `origin` ends up holding exactly the selected upstream refs plus the
preserved ones; that also removes refs an earlier run mirrored under wider
patterns, and upstream refs the patterns no longer select. Without `--prune`
such refs are left alone, so a `mirror` list narrower than what `origin` holds —
including one narrowed by accident, since each of `heads` and `tags` defaults to
`**` on its own — costs nothing until the deletion is asked for.

A pattern is matched by repospace against the whole ref name below `refs/heads/`
or `refs/tags/` (`main`, `release/1.0`, `v1.2`), following gitignore's rules:

- `*` and `?` match within one `/`-separated component.

- `*` matches any run of characters, `?` exactly one.

- `**` spans components, so `**` alone is every name and `release/**` is
  everything below `release/`.

- `[abc]` is a character class and `[!abc]` its negation, which also excludes
  `/`.

- `[a-c]` is a range; a reversed one (`[c-a]`) holds nothing.

- an unclosed `[` is a literal, as in a shell glob.

A list of patterns is read as a gitignore file is: a leading `!` excludes what
the rest of the pattern matches, and the last pattern matching a name is the one
that decides. So `['**', '!wip/*']` selects every name except the ones below
`wip/`, and `['**', '!wip/*', 'wip/keep']` selects that one back. An exclusion
only narrows what another pattern selected, so a list of nothing but exclusions
selects nothing whatever refs exist, and is refused as the mistake it almost
certainly is; `mirror: tags: []` is how to select nothing on purpose.

Only the first `!` is the marker, and the pattern below it reads a `!`
literally, so `!!x` excludes the ref named `!x`. A pattern that selects has no
such escape — the language has no escape character — so a ref name beginning
with `!` is selected only by a pattern that reaches it with a wildcard.

Patterns are never handed to git, which is why, unlike a revision, they may hold
glob syntax; the remaining refname rules still apply (no whitespace, `~`, `^`,
`:`, `\`, `..` or `@{`, and `@` alone is not a pattern). A qualified pattern
such as `refs/heads/main` is well formed and matches nothing, since the name it
is matched against is `main`.

`repospace mirror` refuses a member whose `mirror: heads` matches none of the
branches upstream has, with or without `--prune`. A mirror is always for at
least one branch, since upstream always has a default one, so selecting none of
them mirrors nothing — and under `--prune` it deletes every branch `origin`
holds that `preserve` does not match, because what tells a ref upstream deleted
from a ref that only ever existed on `origin` is exclusion: whatever `preserve`
does not name is taken to be upstream's. Where upstream has no branches at all
there is nothing to check: no pattern, however wide, could have matched one.

`mirror: tags` is not checked that way, and neither is `preserve`. Upstream may
define no tag, a fork may want none of the tags it does define, and a `preserve`
pattern matching nothing is what a fork looks like before it has created the ref
it means to keep. Each is a use of its own that cannot be told from a mistyped
pattern, and none of them leaves the mirror with nothing to mirror; `--prune`
being opt-in and `--dry-run` showing the plan first are what guard them.
`mirror: tags: []` under `--prune` therefore does delete the tags `origin`
holds, as the rule above says it does; `preserve: tags` is what keeps a fork's
own tags out of it.

`repospace mirror --prune` refuses a plan that would delete the branch
`origin`'s HEAD points at, since a git server rejects that push
(`receive.denyDeleteCurrent`, which git refuses by default, on bare
repositories too). Read what it points at with
`git ls-remote --symref <origin url> HEAD`.

Listing that branch under `preserve: heads` always resolves it. Listing it under
`mirror: heads` resolves it only when upstream has a branch of that name, since
what a mirror run pushes is selected from upstream's refs: a branch that exists
only on `origin` cannot be selected however wide the `mirror` patterns are, and
is still pruned. The remaining way out is to move HEAD on `origin`, with
`git symbolic-ref HEAD refs/heads/<branch>` in it, or through the default-branch
setting of the code-hosting platform serving it.

## self

Attributes of the manifest repository itself: `name`, `extension-commands`,
`cmake-packages`, `import`, and `userdata`.

`name` is a *suggested clone-directory name*, used only by `repospace init` when
it clones a manifest repository and no DIRECTORY argument is given (like the
directory name `git clone` derives from the URL, which is also the fallback when
`name` is absent). It must be a single plain path component that does not start
with a dot — the manifest author may suggest a name, but only the caller decides
placement on their filesystem, and the author must not create hidden or
git-confusing directories (`.git`, `.repospace`) there. Refusing a leading dot
covers `.` and `..` in the same rule. After cloning, `name` is never consulted
again.

### Git hygiene

The repospace metadata and the members live inside (or beside) the manifest
repository, so its `.gitignore` should normally include `.repospace/` plus the
prefixes where members are placed. Keeping all member paths under one prefix
makes this a single entry — for example `external/` when the manifest declares
(or self-imports with `path-prefix: external`) its members there.

## Imports

A manifest can import other manifests:

- from the manifest repository itself (`self: import:`) — read from the
  filesystem, relative to the manifest repository root; paths must stay inside
  the repository (no absolute paths, no escaping via `..` or symlinks);
- from a member (`members: - import:`) — a path inside the member, with the same
  confinement (no absolute paths, no `..` escapes) — read from git at
  `refs/heads/repospace-rev`, i.e. the member's state as of the last
  `repospace update`. Reading from git rather than from the working tree keeps
  resolution reproducible: what a member contributes is the revision the
  manifest pins it to, not whatever is checked out or half-edited in it. During
  `update`, the member is cloned and fetched the moment its manifest data is
  needed, so resolution always sees fresh data. A `self: import:` inside a
  manifest read from a member is resolved the same way — from the member's
  `repospace-rev`, never from its working tree.

Manifest data is never read through a symbolic link, by either route: an
imported file (or a `.yml`/`.yaml` file inside an imported directory) that is a
link is an error, and so is a link anywhere on the path to it below the
repository root. Git stores a link as a file whose content is its target, so on
the git side a linked file's target text would be parsed as YAML and a path
through a linked directory would not resolve at all — the same repository would
then resolve differently depending on which route read it.

Accepted forms:

```yaml
import: true                  # the member's repospace.yaml
import: path/to/file.yaml     # a file, or a directory (all .yml/.yaml
                              # files in it, sorted by name)
import:                       # a mapping with filters
  file: repospace.yaml
  name-allowlist: [a, b]
  name-blocklist: [c]
  path-allowlist: [libs/*]
  path-blocklist: [vendor/*]
  path-prefix: external
import:                       # a sequence of any of the above
  - one.yaml
  - file: two.yaml
```

Filter semantics: names match exactly; path patterns match from the right (`foo`
matches `a/b/foo`) and must name at least one path component (`''` and `.` are
rejected). A member matched by an allowlist is imported even if a blocklist also
matches. With no allowlist, everything not blocklisted is imported; with an
allowlist, only listed entries are. Filters compose down the import tree — a
nested import can only narrow what its parent allowed, so an imported manifest
cannot widen its way back into members its importer excluded. `path-prefix`
accumulates by path joining and applies only to the imported content; the
importing member's own placement comes from its `path` attribute. `true` and
`false` are not allowed under `self: import:`; name the file to import instead.

### Precedence

Resolution order is: members imported from `self: import:` first, then this
file's `members:`, then member imports in declaration order. The first
definition of a member name wins; later definitions are ignored, and an ignored
definition's `import:` is never processed. So self-imports override the
top-level file, which overrides member imports — dropping an override file into
a self-imported directory (its name sorts first) overrides everything. Read the
other way round, the order says that an imported manifest can add members but
never replace one the importing repository has already defined.

Import cycles are rejected with the cycle spelled out ("import cycle:
repospace.yaml -> a.yaml -> b.yaml -> a.yaml"). Only the ancestor chain counts,
so importing the same file on two sibling branches (a diamond) is legal — it
terminates, and duplicate members are settled by first-definition-wins. Deeply
nested non-cyclic imports fail with "import level too deep".

## group-filter

A list of `+group`/`-group` entries; `-` disables a group by default. A member
is inactive when all of its groups are disabled, so a member belonging to
several groups survives as long as one of them is enabled; inactive members are
skipped by `update` and hidden from default `list` output. Filters from imported
manifests apply with lower precedence than the importing file; self-imported
filters have the highest. The `manifest.group-filter` configuration option and
`update --group-filter` apply on top of everything, in that order — the file
states the project's default, the configuration adapts it to a machine, and the
command line to a single run.

## Extension commands

`extension-commands` names one YAML specification file, or a list of them,
relative to the member root (or to the manifest repository root under `self:`).
Each file declares Python files inside the same repository and the command
classes they provide; the Python files are imported only when a command is run
or its help is requested, so discovering extensions never executes member code.
Built-in names cannot be overridden; when two members provide the
same command name, the member earlier in resolution order wins.
See [extensions.md](extensions.md) for the specification format and how to
write a command.

When a manifest imported from a member declares `extension-commands` (or
`cmake-packages`) under its own `self:` section, those values are attributed to
that member, so a repository can describe what it provides without the importing
manifest repeating it.

## cmake-packages

Lists CMake package names exposed by a member (or by the manifest repository
under `self:`). Package names may only contain letters, digits, and `._+-` (and
must not start with punctuation), because they are interpolated into generated
CMake code, in a position (`ENV{<name>_ROOT}`) that cannot be quoted — a name
free of that restriction could break the generated file or smuggle code into it.
On every `update`, repospace writes `.repospace/packages.cmake`, which sets
`ENV{<name>_ROOT}` to the declaring member's absolute path for every declared
package (first declaration wins), plus `.repospace/members.json` with all
resolved member data. See the README for the CMake workflow.
