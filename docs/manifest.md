# The repospace manifest format

A manifest is a YAML file, conventionally named `repospace.yaml`, with a single
top-level `manifest` key. `repospace.yaml` is the default entry point
everywhere: for the repospace itself, another file inside the manifest
repository can be selected with `init --manifest` (recorded as the
`manifest.file` configuration option), just as an importing manifest can name a
file with `import: file:`. All sections are optional:

```yaml
manifest:
  version: '1.0'
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
[scripts/schemas/repospace-manifest-schema.json](../scripts/schemas/repospace-manifest-schema.json).

## version

The minimum schema version required to parse the file, as a string (quote it —
YAML would otherwise parse `1.0` as a number, which is tolerated but warned
about in errors). The current schema version is `1.0`. A manifest declaring a
newer version than the running repospace supports fails with an error asking to
upgrade repospace. The version is checked per file, including imported ones,
before anything else.

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
| `path`               | string           | `name`              | Checkout path relative to the repospace top (POSIX separators; must stay inside the repospace; no `.git` components) |
| `submodules`         | bool or list     | `false`             | `true` = update all recursively; or a list of `{path, name}`                                                         |
| `clone-depth`        | positive int     | none                | Passed to `git fetch --depth`                                                                                        |
| `extension-commands` | string or list   | none                | Extension command specification file(s), relative to the member root                                                 |
| `cmake-packages`     | string or list   | none                | CMake package names whose `<name>_ROOT` should point at this member                                                  |
| `import`             | see below        | none                | Import the member's own manifest(s)                                                                                  |
| `groups`             | list             | `[]`                | Group membership (mutually exclusive with `import`)                                                                  |
| `userdata`           | any              | none                | Ignored by repospace                                                                                                 |

Member paths are confined to the repospace: absolute paths, drive letters, `..`
escapes, `.git` path components (in any case — member content must never act as
a git directory), and placement inside `.repospace/` are rejected lexically. A
path is normalized before it is used, so `libs/x/../y` *is* `libs/y` — that is
the placement, and the form re-emitted by `manifest --resolve`; a path that
normalizes to the repospace top itself (or, in an imported manifest, to its
`path-prefix` directory) is rejected.

Member checkouts live in real directories inside the repospace: no existing
component of a member path below the repospace top may be a symbolic link — the
member's own directory included — so that neither a link committed in a member
nor one committed in the manifest repository can redirect a checkout elsewhere.
Components that do not exist yet are fine; `update` creates them as directories.
The repospace top itself, and anything above it, may be a symlink: that
placement is the user's own doing, not something manifest data can steer.

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
