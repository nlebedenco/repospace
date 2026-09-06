"""Tests for manifest parsing and validation (no imports)."""

from __future__ import annotations

import textwrap

import pytest

from repospace.manifest import (
    MalformedManifest,
    Manifest,
    ManifestMember,
    ManifestVersionError,
    Submodule,
    validate,
)


def load(text, **kwargs):
    return Manifest.from_data(textwrap.dedent(text), **kwargs)


def test_empty_manifest():
    manifest = load("manifest:")
    assert len(manifest.members) == 1
    assert isinstance(manifest.members[0], ManifestMember)
    assert manifest.members[0].name == "manifest"


def test_basic_member():
    manifest = load("""
        manifest:
          members:
            - name: lib
              url: https://example.com/lib
        """)
    (member,) = manifest.members[1:]
    assert member.name == "lib"
    assert member.url == "https://example.com/lib"
    assert member.revision == "main"
    assert member.path == "lib"
    assert member.remote_name == "origin"
    assert member.declared_by == "manifest"
    assert member.groups == []
    assert member.submodules is False


def test_defaults_and_remotes():
    manifest = load("""
        manifest:
          defaults:
            remote: origin-remote
            revision: v1.2
          remotes:
            - name: origin-remote
              url-base: https://example.com/base
          members:
            - name: alpha
            - name: beta
              revision: dev
              repo-path: other/beta
            - name: gamma
              path: nested/gamma
        """)
    alpha, beta, gamma = manifest.members[1:]
    assert alpha.url == "https://example.com/base/alpha"
    assert alpha.revision == "v1.2"
    assert beta.url == "https://example.com/base/other/beta"
    assert beta.revision == "dev"
    assert gamma.path == "nested/gamma"
    assert alpha.remote_name == "origin"


def test_duplicate_remote_name_rejected():
    with pytest.raises(MalformedManifest) as excinfo:
        load("""
            manifest:
              remotes:
                - name: origin-remote
                  url-base: https://one.example.com
                - name: origin-remote
                  url-base: https://two.example.com
            """)
    assert "remote name origin-remote used twice" in str(excinfo.value)


def test_float_revision_rejected():
    with pytest.raises(MalformedManifest) as excinfo:
        load("""
            manifest:
              members:
                - name: lib
                  url: https://example.com/lib
                  revision: 1.10
            """)
    assert "quote" in str(excinfo.value)


def test_float_default_revision_rejected():
    with pytest.raises(MalformedManifest):
        load("""
            manifest:
              defaults:
                revision: 1.10
            """)


def test_null_default_revision_rejected():
    with pytest.raises(MalformedManifest) as excinfo:
        load("""
            manifest:
              defaults:
                revision:
            """)
    assert "revision has no value" in str(excinfo.value)


def test_integer_revision_rejected():
    # Revisions are strings, never numbers; unquoted numerics are a missing quote.
    with pytest.raises(MalformedManifest) as excinfo:
        load("""
            manifest:
              members:
                - name: lib
                  url: https://example.com/lib
                  revision: 123456
            """)
    assert "quote" in str(excinfo.value)


@pytest.mark.parametrize(
    "text",
    [
        "manifest:\n  members:\n    - name: lib\n      url: u\n      revision: '-delete'\n",
        "manifest:\n  defaults:\n    revision: '-force'\n",
    ],
)
def test_leading_dash_revision_rejected(text):
    # Git refnames cannot begin with "-", and such a value would be read as an option by the git
    # commands that take a revision.
    with pytest.raises(MalformedManifest) as excinfo:
        Manifest.from_data(text)
    assert 'begins with "-"' in str(excinfo.value)


def member_with_revision(revision):
    return {"manifest": {"members": [{"name": "lib", "url": "u", "revision": revision}]}}


@pytest.mark.parametrize(
    "revision,fragment",
    [
        ("main:refs/heads/work", "contains ':'"),
        ("+main", 'begins with "+"'),
        ("refs/heads/*", "contains '*'"),
        ("main foo", "contains ' '"),
        ("main\tfoo", "contains '\\t'"),
        ("main^{}", "contains '^'"),
        ("v1~2", "contains '~'"),
        ("a?b", "contains '?'"),
        ("a[b", "contains '['"),
        ("a\\b", "contains '\\\\'"),
        ("a..b", 'contains ".."'),
        ("main@{1}", 'contains "@{"'),
        ("@", 'is "@"'),
    ],
)
def test_refspec_and_operator_syntax_in_revision_rejected(revision, fragment):
    # A revision reaches git as a fetch refspec and as a revision argument: "main:refs/heads/work"
    # would move the member's local "work" branch to the remote's main, "*" fetches a pattern, "^"
    # and "~" are revision operators. Only refname-safe strings pass.
    with pytest.raises(MalformedManifest) as excinfo:
        Manifest.from_data(member_with_revision(revision))
    assert fragment in str(excinfo.value)


@pytest.mark.parametrize(
    "revision",
    [
        "main",
        "refs/heads/main",
        "v1.0",
        "feature/x-y_z",
        "0123abcd",
        "HEAD",
        "release/2024.01",
        "a+b",
        "x@y",
    ],
)
def test_refname_safe_revisions_accepted(revision):
    manifest = Manifest.from_data(member_with_revision(revision))
    assert manifest.members[1].revision == revision


@pytest.mark.parametrize(
    "body,fragment",
    [
        ("- name: a\n      url: u\n      remote: r", "both"),
        ("- name: a\n      url: u\n      repo-path: p", "repo-path"),
        ("- name: a", "no remote or url"),
        ("- name: a\n      remote: nope", "not defined"),
        ("- name: manifest\n      url: u", "reserved"),
        ("- name: a/b\n      url: u", "separator"),
        ("- name: a\n      url: u\n      path: /abs", "absolute"),
        ("- name: a\n      url: u\n      path: 'C:evil'", "drive letter"),
        ("- name: a\n      url: u\n      path: 'C:/evil'", "drive letter"),
        ("- name: a\n      url: u\n      path: ../up", "escapes"),
        ("- name: a\n      url: u\n      path: 'sub\\dir'", "backslash"),
        ("- name: a\n      url: u\n      path: .", "topdir itself"),
        ("- name: a\n      url: u\n      path: .repospace/x", ".repospace"),
        ("- name: a\n      url: u\n      path: .git", '".git" component'),
        (
            "- name: a\n      url: u\n      path: sub/.GIT/hooks",
            '".git" component',
        ),
        (
            "- name: a\n      url: u\n      path: sub/x/../.git",
            '".git" component',
        ),
        ("- name: .git\n      url: u", '".git" component'),
        ("- name: a\n      url: u\n      path:", '"path" is not a string'),
        ("- name: a\n      url:", '"url" is not a string'),
        ("- name: ''\n      url: u", 'non-empty string "name"'),
        ("- name: a\n      url: ''", '"url" is empty'),
        ("- name: a\n      url: u\n      path: ''", '"path" is empty'),
        ("- name: a\n      remote: ''", '"remote" is empty'),
        (
            "- name: a\n      url: u\n      revision: ''",
            "revision is empty",
        ),
        (
            "- name: a\n      url: u\n      revision:",
            "revision has no value",
        ),
        (
            "- name: a\n      url: u\n      groups: [g]\n      import: true",
            "cannot be combined",
        ),
        ("- name: a\n      url: u\n      groups: [-bad]", "invalid group"),
        ("- name: a\n      url: u\n      groups: [7]", "quote"),
        ("- name: a\n      url: u\n      groups: [1.10]", "quote"),
        ("- name: a\n      url: u\n    - name: a\n      url: u", "twice"),
    ],
)
def test_member_errors(body, fragment):
    text = f"manifest:\n  members:\n    {body}\n"
    with pytest.raises(MalformedManifest) as excinfo:
        Manifest.from_data(text)
    assert fragment in str(excinfo.value)


_MEMBER_HEAD = "manifest:\n  members:\n    - name: a\n      url: u\n"


@pytest.mark.parametrize(
    "text,fragment",
    [
        ("manifest:\n  defaults:\n", '"defaults" has no value'),
        ("manifest:\n  remotes:\n", '"remotes" has no value'),
        ("manifest:\n  members:\n", '"members" has no value'),
        ("manifest:\n  self:\n", '"self" has no value'),
        ("manifest:\n  group-filter:\n", '"group-filter" has no value'),
        (_MEMBER_HEAD + "      clone-depth:\n", '"clone-depth" has no value'),
        (_MEMBER_HEAD + "      submodules:\n", '"submodules: false"'),
        (
            _MEMBER_HEAD + "      extension-commands:\n",
            '"extension-commands" has no value',
        ),
        (
            _MEMBER_HEAD + "      cmake-packages:\n",
            '"cmake-packages" has no value',
        ),
        (_MEMBER_HEAD + "      import:\n", '"import: false"'),
        (_MEMBER_HEAD + "      groups:\n", '"groups" has no value'),
        (
            _MEMBER_HEAD + "      import:\n        name-allowlist:\n",
            '"name-allowlist" has no value',
        ),
        (
            "manifest:\n  self:\n    extension-commands:\n",
            '"extension-commands" has no value',
        ),
        (
            "manifest:\n  self:\n    cmake-packages:\n",
            '"cmake-packages" has no value',
        ),
        ("manifest:\n  self:\n    import:\n", '"import" has no value'),
    ],
)
def test_explicit_null_keys_rejected(text, fragment):
    # Explicit null is invalid everywhere except "manifest" itself and "userdata"; the key belongs
    # removed instead.
    with pytest.raises(MalformedManifest) as excinfo:
        Manifest.from_data(text)
    assert fragment in str(excinfo.value)


def test_null_userdata_allowed():
    manifest = load(_MEMBER_HEAD + "      userdata:\n")
    assert manifest.members[1].userdata is None


def test_dotdot_prefixed_path_component_allowed():
    # "..foo" is a valid directory name, not an escape.
    manifest = load("""
        manifest:
          members:
            - name: a
              url: u
              path: ..foo
        """)
    assert manifest.members[1].path == "..foo"


def test_member_path_is_normalized():
    # The stored path is what as_dict() re-emits and what the placement checks run on; "libs/x/../y"
    # is the directory "libs/y".
    manifest = load("""
        manifest:
          members:
            - name: a
              url: u
              path: libs/x/../y
        """)
    assert manifest.members[1].path == "libs/y"
    assert manifest.as_dict()["manifest"]["members"][0]["path"] == "libs/y"


def test_member_path_normalizing_to_topdir_rejected():
    with pytest.raises(MalformedManifest) as excinfo:
        load("""
            manifest:
              members:
                - name: a
                  url: u
                  path: sub/..
            """)
    assert "topdir itself" in str(excinfo.value)


def test_undefined_default_remote():
    with pytest.raises(MalformedManifest):
        load("""
            manifest:
              defaults:
                remote: ghost
            """)


def test_empty_default_revision_rejected():
    with pytest.raises(MalformedManifest) as excinfo:
        load("manifest:\n  defaults:\n    revision: ''\n")
    assert "revision is empty" in str(excinfo.value)


def test_empty_default_remote_rejected():
    with pytest.raises(MalformedManifest) as excinfo:
        load("manifest:\n  defaults:\n    remote: ''\n")
    assert "non-empty string" in str(excinfo.value)


def test_empty_remotes_entry_rejected():
    with pytest.raises(MalformedManifest) as excinfo:
        load("""
            manifest:
              remotes:
                - name: r
                  url-base: ''
            """)
    assert 'non-empty string "url-base"' in str(excinfo.value)


def test_url_base_trailing_slash_dropped():
    manifest = load("""
        manifest:
          remotes:
            - name: r
              url-base: https://example.com/base/
          members:
            - name: lib
              remote: r
        """)
    assert manifest.members[1].url == "https://example.com/base/lib"


def test_duplicate_paths_rejected():
    with pytest.raises(MalformedManifest) as excinfo:
        load("""
            manifest:
              members:
                - name: a
                  url: u
                  path: same
                - name: b
                  url: u
                  path: same
            """)
    assert "is taken by" in str(excinfo.value)


def test_submodules_forms():
    manifest = load("""
        manifest:
          members:
            - name: a
              url: u
              submodules: true
            - name: b
              url: u
              submodules:
                - path: modules/x
                - path: modules/y
                  name: why
        """)
    a, b = manifest.members[1:]
    assert a.submodules is True
    assert b.submodules == [
        Submodule("modules/x"),
        Submodule("modules/y", "why"),
    ]


def test_submodules_invalid():
    with pytest.raises(MalformedManifest):
        load("""
            manifest:
              members:
                - name: a
                  url: u
                  submodules:
                    - name: missing-path
            """)


def test_new_attributes():
    manifest = load("""
        manifest:
          members:
            - name: a
              url: u
              extension-commands: exts.yaml
              cmake-packages: [Foo, Bar]
          self:
            name: app
            extension-commands: [one.yaml, two.yaml]
            cmake-packages: App
        """)
    member = manifest.members[1]
    assert member.extension_commands == ["exts.yaml"]
    assert member.cmake_packages == ["Foo", "Bar"]
    mm = manifest.members[0]
    assert mm.extension_commands == ["one.yaml", "two.yaml"]
    assert mm.cmake_packages == ["App"]
    assert manifest.yaml_name == "app"


# The parameters are YAML scalars, quoted here rather than through repr(), so that '"a\\b"' reaches
# the parser as a name holding one backslash instead of a literal backslash-b.
@pytest.mark.parametrize("name", ["''", "'.'", "'..'", "a/b", '"a\\\\b"', ".repospace"])
def test_self_invalid_name_rejected(name):
    with pytest.raises(MalformedManifest):
        load(f"""
            manifest:
              self:
                name: {name}
            """)


def test_self_path_no_longer_accepted():
    with pytest.raises(MalformedManifest):
        load("""
            manifest:
              self:
                path: app
            """)


@pytest.mark.parametrize(
    "text,exc",
    [
        ("manifest:\n  version: '1.0'\n", None),
        ("manifest:\n  version: 1.0\n", None),
        ("manifest:\n  version: '1.0.0'\n", None),
        ("manifest:\n  version: '1'\n", None),
        ("manifest:\n  version: '2.0'\n", ManifestVersionError),
        ("manifest:\n  version: '1.5'\n", ManifestVersionError),
        ("manifest:\n  version: '1.0.1'\n", ManifestVersionError),
        ("manifest:\n  version: '0.9'\n", MalformedManifest),
        ("manifest:\n  version: 'banana'\n", MalformedManifest),
    ],
)
def test_versions(text, exc):
    if exc is None:
        Manifest.from_data(text)
    else:
        with pytest.raises(exc):
            Manifest.from_data(text)


def test_version_checked_before_structure():
    # A newer manifest may be structurally incompatible; the version error must win over the
    # unknown-key error.
    with pytest.raises(ManifestVersionError):
        load("""
            manifest:
              version: '2.0'
              shiny-new-section: {}
            """)


def test_null_version_rejected():
    # An explicit null is an editing artifact like everywhere else; the quoting hint for an
    # unquotable value would be wrong advice.
    with pytest.raises(MalformedManifest) as excinfo:
        Manifest.from_data("manifest:\n  version:\n")
    assert '"version" has no value; remove the key' in str(excinfo.value)
    assert "quote" not in str(excinfo.value)


def test_unknown_non_string_keys_reported():
    # YAML resolves a bare "on:" key to the boolean True, which does not sort against string keys;
    # the key list must still be reportable.
    with pytest.raises(MalformedManifest) as excinfo:
        validate("manifest:\n  on: 1\n  bogus: 2\n")
    message = str(excinfo.value)
    assert "unknown key(s)" in message
    assert "True" in message and "bogus" in message


@pytest.mark.parametrize(
    "text",
    [
        "not-manifest: {}\n",
        "manifest: {}\nextra: 1\n",
        "manifest:\n  bogus: 1\n",
        "manifest:\n  defaults: {bogus: 1}\n",
        "manifest:\n  remotes:\n    - name: a\n",
        "manifest:\n  self: {bogus: 1}\n",
        "manifest:\n  members:\n    - name: a\n      url: u\n      bogus: 1\n",
        "manifest:\n  members:\n    - url: u\n",
        "manifest:\n  members:\n    - name: a\n      url: u\n      clone-depth: deep\n",
        "manifest:\n  members:\n    - name: a\n      url: u\n      clone-depth: 0\n",
        "manifest:\n  members:\n    - name: a\n      url: u\n      clone-depth: -1\n",
    ],
)
def test_structural_errors(text):
    with pytest.raises(MalformedManifest):
        validate(text)


@pytest.mark.parametrize("name", ["", ".", "..", ".git", ".repospace", ".hidden", "a/b"])
def test_invalid_self_names(name):
    # A suggested clone-directory name only: no paths, and no hidden or git-confusing directories
    # (".git", ".repospace") in the caller's filesystem.
    with pytest.raises(MalformedManifest) as excinfo:
        validate(f"manifest:\n  self:\n    name: {name!r}\n")
    assert "path component" in str(excinfo.value)


def test_invalid_self_name_backslash():
    with pytest.raises(MalformedManifest):
        validate('manifest:\n  self:\n    name: "a\\\\b"\n')


@pytest.mark.parametrize("name", ["app", "my-app", "v1.0", "has.dots"])
def test_valid_self_names(name):
    validate(f"manifest:\n  self:\n    name: {name!r}\n")


# YAML scalars, quoted here rather than through repr(): repr() of a name holding a newline yields a
# single-quoted scalar, in which YAML reads "\n" as a literal backslash-n and the newline is never
# tested.
@pytest.mark.parametrize("name", ["'P\"kg'", "'P;kg'", "'Pk g'", '"P\\nkg"', "'-Pkg'"])
def test_invalid_cmake_package_names(name):
    # Package names are interpolated into generated CMake code and are restricted to characters that
    # cannot break or extend it.
    for text in (
        f"manifest:\n  members:\n    - name: a\n      url: u\n" f"      cmake-packages: [{name}]\n",
        f"manifest:\n  self:\n    cmake-packages: [{name}]\n",
    ):
        with pytest.raises(MalformedManifest) as excinfo:
            validate(text)
        assert "CMake package name" in str(excinfo.value)


def test_valid_cmake_package_names():
    validate(
        "manifest:\n  members:\n    - name: a\n      url: u\n"
        "      cmake-packages: [Zephyr, foo_bar, a.b+c-d, 7zip]\n"
    )


def test_group_filter_validation():
    with pytest.raises(MalformedManifest):
        load("manifest:\n  group-filter: [nosign]\n")
    with pytest.raises(MalformedManifest):
        load("manifest:\n  group-filter: ['-bad name']\n")


def test_group_filter_numeric_item_hints_quoting():
    # YAML eats the sign of an unquoted -1; the error must suggest quoting instead of just demanding
    # a "+" or "-" the user already typed.
    with pytest.raises(MalformedManifest) as excinfo:
        load("manifest:\n  group-filter: [-1]\n")
    assert "quote" in str(excinfo.value)


def test_group_filter_resolution():
    manifest = load("""
        manifest:
          group-filter: [-foo, +foo, -bar]
        """)
    assert manifest.group_filter == ["-bar"]


def test_is_active():
    manifest = load("""
        manifest:
          group-filter: [-optional]
          members:
            - name: core
              url: u
            - name: extra
              url: u
              groups: [optional]
            - name: mixed
              url: u
              groups: [optional, always]
        """)
    core, extra, mixed = manifest.members[1:]
    assert manifest.is_active(core)
    assert not manifest.is_active(extra)
    assert manifest.is_active(mixed)
    assert manifest.is_active(extra, extra_filter=["+optional"])
    assert not manifest.is_active(mixed, extra_filter=["-always"])


@pytest.mark.parametrize("entry", ["", "optional"])
def test_is_active_rejects_invalid_extra_filter(entry):
    # A filter entry must say which way it goes; an empty one used to index out of range.
    manifest = load("""
        manifest:
          members:
            - name: core
              url: u
        """)
    with pytest.raises(ValueError):
        manifest.is_active(manifest.members[1], extra_filter=[entry])


def test_get_members():
    manifest = load("""
        manifest:
          members:
            - name: a
              url: u
            - name: b
              url: u
        """)
    assert [m.name for m in manifest.get_members([])] == [
        "manifest",
        "a",
        "b",
    ]
    assert [m.name for m in manifest.get_members(["b"])] == ["b"]
    with pytest.raises(ValueError):
        manifest.get_members(["ghost"])


def test_as_dict_resolved_output():
    manifest = load("""
        manifest:
          defaults:
            remote: r
          remotes:
            - name: r
              url-base: https://example.com
          group-filter: [-opt]
          members:
            - name: a
              description: The A member
              revision: v1
            - name: b
              path: libs/b
              groups: [opt]
              cmake-packages: [B]
          self:
            name: app
            cmake-packages: [App]
        """)
    data = manifest.as_dict()
    mdata = data["manifest"]
    assert mdata["version"] == "1.0"
    assert mdata["group-filter"] == ["-opt"]
    a, b = mdata["members"]
    assert a == {
        "name": "a",
        "description": "The A member",
        "url": "https://example.com/a",
        "revision": "v1",
    }
    assert b["path"] == "libs/b"
    assert b["cmake-packages"] == ["B"]
    assert mdata["self"] == {"name": "app", "cmake-packages": ["App"]}
    # Resolved output must itself be a valid manifest.
    validate(data)
    validate(manifest.as_yaml())


def test_as_dict_active_only():
    manifest = load("""
        manifest:
          group-filter: [-opt]
          members:
            - name: a
              url: u
            - name: b
              url: u
              groups: [opt]
        """)
    names = [m["name"] for m in manifest.as_dict(active_only=True)["manifest"]["members"]]
    assert names == ["a"]


def test_path_through_member_symlink_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    top = tmp_path / "ws"
    (top / "a").mkdir(parents=True)
    (top / "a" / "link").symlink_to(outside)
    with pytest.raises(MalformedManifest) as excinfo:
        load(
            """
            manifest:
              members:
                - name: a
                  url: u
                - name: b
                  url: u
                  path: a/link/b
            """,
            topdir=str(top),
        )
    assert "is a symbolic link" in str(excinfo.value)
    assert str(top / "a" / "link") in str(excinfo.value)


def test_path_into_repospace_dir_via_symlink_rejected(tmp_path):
    top = tmp_path / "ws"
    (top / ".repospace").mkdir(parents=True)
    (top / "a").mkdir()
    (top / "a" / "link").symlink_to(top / ".repospace")
    with pytest.raises(MalformedManifest):
        load(
            """
            manifest:
              members:
                - name: a
                  url: u
                - name: b
                  url: u
                  path: a/link/b
            """,
            topdir=str(top),
        )


def test_member_directory_symlink_rejected(tmp_path):
    # Symlinks are valid only above the repospace directory: a member's own directory being a link
    # would let a link committed in the manifest repository send the checkout anywhere.
    outside = tmp_path / "outside"
    outside.mkdir()
    top = tmp_path / "ws"
    top.mkdir()
    (top / "m").symlink_to(outside)
    with pytest.raises(MalformedManifest) as excinfo:
        load(
            """
            manifest:
              members:
                - name: m
                  url: u
            """,
            topdir=str(top),
        )
    assert "is a symbolic link" in str(excinfo.value)
    assert str(top / "m") in str(excinfo.value)


def test_member_path_components_may_not_exist(tmp_path):
    # Nothing is cloned yet: components that do not exist are created as directories by "update", so
    # they are not symlinks.
    top = tmp_path / "ws"
    top.mkdir()
    manifest = load(
        """
        manifest:
          members:
            - name: m
              url: u
              path: libs/deep/m
        """,
        topdir=str(top),
    )
    assert manifest.members[1].abspath == str(top / "libs" / "deep" / "m")


def test_symlink_above_topdir_allowed(tmp_path):
    # The repospace itself may be reached through a symlink; that placement is the user's own doing,
    # not manifest data's.
    real = tmp_path / "real"
    (real / "m").mkdir(parents=True)
    top = tmp_path / "ws"
    top.symlink_to(real)
    manifest = load(
        """
        manifest:
          members:
            - name: m
              url: u
        """,
        topdir=str(top),
    )
    assert manifest.members[1].abspath == str(real / "m")


def test_from_file_and_from_topdir(tmp_path):
    (tmp_path / ".repospace").mkdir()
    (tmp_path / "repospace.yaml").write_text(
        "manifest:\n  members:\n    - name: lib\n      url: u\n"
    )

    by_file = Manifest.from_file(tmp_path / "repospace.yaml")
    assert by_file.topdir == str(tmp_path)
    assert by_file.members[0].path == "."
    assert by_file.members[1].abspath == str(tmp_path / "lib")

    by_topdir = Manifest.from_topdir(str(tmp_path))
    assert by_topdir.members[0].path == "."
    assert by_topdir.members[0].abspath == str(tmp_path)
    assert [m.name for m in by_topdir.members] == ["manifest", "lib"]


def test_from_file_in_subdirectory_anchors_at_topdir(tmp_path):
    # manifest.file may live in a subdirectory; the manifest repository is still the topdir, so the
    # manifest member's path is "." (as in from_topdir) and a member path equal to the subdirectory
    # name is not a path collision.
    (tmp_path / ".repospace").mkdir()
    sub = tmp_path / "manifests"
    sub.mkdir()
    (sub / "m.yaml").write_text(
        "manifest:\n  members:\n    - name: lib\n      url: u\n      path: manifests\n"
    )
    manifest = Manifest.from_file(sub / "m.yaml")
    assert manifest.topdir == str(tmp_path)
    assert manifest.repo_abspath == str(tmp_path)
    assert manifest.members[0].path == "."
    assert manifest.members[0].abspath == str(tmp_path)


@pytest.mark.parametrize(
    "make,fragment",
    [
        (lambda p: p / "ghost.yaml", "manifest file not found"),
        (lambda p: p, "cannot read manifest file"),
    ],
)
def test_from_file_read_errors_raise_malformed(tmp_path, make, fragment):
    # from_file wraps read failures like from_topdir: a missing file or a directory must not escape
    # as a raw OSError.
    with pytest.raises(MalformedManifest) as excinfo:
        Manifest.from_file(make(tmp_path))
    assert fragment in str(excinfo.value)


def test_from_file_non_utf8_raises_malformed(tmp_path):
    source = tmp_path / "m.yaml"
    source.write_bytes(b"manifest: \xff\n")
    with pytest.raises(MalformedManifest) as excinfo:
        Manifest.from_file(source)
    assert "not valid UTF-8" in str(excinfo.value)


def test_non_utf8_manifest_raises_malformed(tmp_path):
    (tmp_path / ".repospace").mkdir()
    (tmp_path / "repospace.yaml").write_bytes(b"manifest: \xff\n")
    with pytest.raises(MalformedManifest) as excinfo:
        Manifest.from_topdir(str(tmp_path))
    assert "not valid UTF-8" in str(excinfo.value)
