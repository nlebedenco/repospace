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

Unknown keys are rejected everywhere. So are keys with an explicit null value
(e.g. a `groups:` whose entries were all commented out) — remove the key
instead; only `manifest:` and `userdata:` may be null. Empty strings are
rejected wherever a value would otherwise silently fall back to a default or
change meaning (`name`, `remote`, `repo-path`, `url`, `path`, `revision`,
`remotes` entries, `import` paths, `import: file:`, and the
`path-allowlist`/`path-blocklist` patterns).

A machine-readable JSON Schema of this format is at
[scripts/schemas/manifest-schema.json](../scripts/schemas/manifest-schema.json).
The installed package ships the same file under the environment prefix, as
`share/repospace/schemas/manifest-schema.json` — for a virtual environment,
`<venv>/share/repospace/schemas/manifest-schema.json`.

## version

The minimum schema version required to parse the file, as a string (quote it —
YAML would otherwise parse `0.2` as a number, which is tolerated but warned
about in errors; `0.10` unquoted is the number `0.1`, a different version).

A schema version is the `major.minor` of the repospace release that last changed
the manifest format, so a release adding no manifest feature has no schema
version of its own. The current, and so far only, schema version is `0.2`, which
shipped with repospace 0.2.0.

The value must name a released schema version exactly. A newer one fails with an
error asking to upgrade repospace — that is what the key buys. Anything else,
including a spelling that merely compares equal such as `0.2.0`, is rejected as
invalid with the list of versions this repospace accepts.

The declared version is not a claim repospace checks the file against: nothing
verifies that a file declaring an older version restricts itself to what that
version offered. It only says which repospace can read the file.

The version is checked per file, including imported ones, before anything else.

## defaults

- `remote`: remote used by members that give neither `remote` nor `url`.
- `revision`: revision for members without one (default: `main`). Like every
  revision, it must be a plausible git refname or commit SHA: no leading `-`
  or `+`, and none of the characters a refname cannot contain (whitespace,
  `~`, `^`, `:`, `?`, `*`, `[`, `\`, `..`, `@{`). A revision is passed to git
  as a fetch refspec and as a revision argument, where each of those has a
  meaning of its own; a `:` would make `update` move a local branch of the
  member.

`defaults` and `remotes` apply only to the file that contains them; they are
never inherited across imports.

## remotes

Named URL prefixes. Each entry needs `name` and `url-base`; names must be unique
within the file. A member using a remote gets the fetch URL
`url-base + "/" + (repo-path or name)`; trailing slashes on `url-base` are
dropped.

Remote names are manifest-internal identifiers for URL prefixes: in a cloned
member the git remote is always named `origin`, however the URL was declared.

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

Member paths use forward slashes and are confined to the repospace: backslashes,
absolute paths, drive letters, `..` escapes, `.git` path components (in any case
— member content must never act as a git directory), and placement inside
`.repospace/` are rejected lexically. A path is normalized before it is used, so
`libs/x/../y` *is* `libs/y` — that is the placement, and the form re-emitted by
`manifest --resolve`; a path that normalizes to the repospace top itself (or, in
an imported manifest, to its `path-prefix` directory) is rejected. Confinement
is to the repospace top, not to the `path-prefix`: under a prefix of `external`,
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
`preserve` always wins
over `mirror`: a preserved ref is never pushed to, even where upstream has a ref
of the same name, so work that exists only on `origin` is never overwritten by
an upstream ref that collides with it. Mirrored refs, on the other hand, belong
to upstream: they are force-updated, and any commit `origin` alone held on one
of them is dropped.

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
selects nothing and is refused; `mirror: tags: []` is how to select nothing on
purpose.

Only the first `!` is the marker, and the pattern below it reads a `!`
literally, so `!!x` excludes the ref named `!x`. A pattern that selects has no
such escape — the language has no escape character — so a ref name beginning
with `!` is selected only by a pattern that reaches it with a wildcard.

Patterns are never handed to git, so unlike a revision they may hold glob
syntax; the remaining refname rules still apply (no whitespace, `~`, `^`, `:`,
`\`, `..` or `@{`, and `@` alone is not a pattern). A qualified pattern such as
`refs/heads/main` is well formed and matches nothing, since the name it is
matched against is `main`.

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
git-confusing directories (`.git`, `.repospace`) there. After cloning, `name` is
never consulted again.

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
  `repospace update`. During `update`, the member is cloned and fetched the
  moment its manifest data is needed, so resolution always sees fresh data. A
  `self: import:` inside a manifest read from a member is resolved the same way
  — from the member's `repospace-rev`, never from its working tree.

Manifest data is never read through a symbolic link, by either route: an
imported file (or a `.yml`/`.yaml` file inside an imported directory) that is a
link is an error, and so is a link anywhere on the path to it below the
repository root. Git stores a link as a file whose content is its target, so
following one on the filesystem would make the same repository resolve
differently depending on where it was read from.

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
nested import can only narrow what its parent allowed. `path-prefix` accumulates
by path joining and applies only to the imported content; the importing member's
own placement comes from its `path` attribute. `true` and `false` are not
allowed under `self: import:`.

### Precedence

Resolution order is: members imported from `self: import:` first, then this
file's `members:`, then member imports in declaration order. The first
definition of a member name wins; later definitions are ignored, and an ignored
definition's `import:` is never processed. So self-imports override the
top-level file, which overrides member imports — dropping an override file into
a self-imported directory (its name sorts first) overrides everything.

Import cycles are rejected with the cycle spelled out ("import cycle:
repospace.yaml -> a.yaml -> b.yaml -> a.yaml"). Importing the same file on two
sibling branches (a diamond) is legal; duplicate members are handled by
first-definition-wins. Deeply nested non-cyclic imports fail with "import level
too deep".

## group-filter

A list of `+group`/`-group` entries; `-` disables a group by default. A member
is inactive when all of its groups are disabled; inactive members are skipped by
`update` and hidden from default `list` output. Filters from imported manifests
apply with lower precedence than the importing file; self-imported filters have
the highest. The `manifest.group-filter` configuration option and
`update --group-filter` apply on top of everything.

## Extension commands

`extension-commands` names one YAML specification file, or a list of them,
relative to the member root (or to the manifest repository root under `self:`).
Each file declares Python files inside the same repository and the command
classes they provide; the Python files are imported only when a command is run
or its help is requested. Built-in names cannot be overridden; when two members
provide the same command name, the member earlier in resolution order wins.
See [extensions.md](extensions.md) for the specification format and how to
write a command.

When a manifest imported from a member declares `extension-commands` (or
`cmake-packages`) under its own `self:` section, those values are attributed to
that member.

## cmake-packages

Lists CMake package names exposed by a member (or by the manifest repository
under `self:`). Package names may only contain letters, digits, and `._+-` (and
must not start with punctuation), because they are interpolated into generated
CMake code. On every `update`, repospace writes `.repospace/packages.cmake`,
which sets `ENV{<name>_ROOT}` to the declaring member's absolute path for every
declared package (first declaration wins), plus `.repospace/members.json` with
all resolved member data. See the README for the CMake workflow.
