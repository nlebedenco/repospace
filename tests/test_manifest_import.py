"""Tests for import resolution, precedence, filters, and provenance.

These run without git — member imports are served by an in-memory
importer stub, self-imports by real files under a temporary directory —
except the few that exercise the git-tree route directly.
"""

from __future__ import annotations

import os
import textwrap

import pytest

from repospace.manifest import (
    MANIFEST_REV,
    ImportFlag,
    MalformedManifest,
    Manifest,
    ManifestImportFailed,
    Member,
    member_manifest_content,
)


def stub(mapping):
    """Importer serving YAML text keyed by (member name, path)."""

    def importer(member, path):
        try:
            return mapping[(member.name, path)]
        except KeyError:
            raise ManifestImportFailed(member, path, "not in stub")

    return importer


def load(text, mapping=None, **kwargs):
    importer = stub(mapping) if mapping is not None else None
    return Manifest.from_data(textwrap.dedent(text), importer=importer, **kwargs)


def names(manifest):
    return [m.name for m in manifest.members]


TOP_WITH_CHILD = """
manifest:
  members:
    - name: child
      url: u/child
      import: true
"""


def test_import_true_reads_default_file():
    manifest = load(
        TOP_WITH_CHILD,
        {("child", "repospace.yaml"): ("manifest:\n  members:\n    - name: grand\n      url: u/grand\n")},
    )
    assert names(manifest) == ["manifest", "child", "grand"]
    assert manifest.has_imports
    grand = manifest.members[2]
    assert grand.declared_by == "child"
    assert manifest.members[1].declared_by == "manifest"


def test_import_empty_map_reads_default_file():
    # Every import-map key is optional, so "import: {}" equals
    # "import: true".
    manifest = load(
        """
        manifest:
          members:
            - name: child
              url: u/child
              import: {}
        """,
        {("child", "repospace.yaml"): ("manifest:\n  members:\n    - name: grand\n      url: u/grand\n")},
    )
    assert names(manifest) == ["manifest", "child", "grand"]
    assert manifest.has_imports


def test_import_false_is_explicit_no_import():
    manifest = load(
        """
        manifest:
          members:
            - name: child
              url: u/child
              import: false
        """,
        {},
    )
    assert names(manifest) == ["manifest", "child"]
    assert not manifest.has_imports


def test_groups_with_empty_map_import_rejected():
    with pytest.raises(MalformedManifest) as excinfo:
        load(
            """
            manifest:
              members:
                - name: child
                  url: u/child
                  groups: [g]
                  import: {}
            """,
            {},
        )
    assert "cannot be combined" in str(excinfo.value)


def test_import_string_path():
    manifest = load(
        """
        manifest:
          members:
            - name: child
              url: u
              import: sub/deps.yaml
        """,
        {("child", "sub/deps.yaml"): ("manifest:\n  members:\n    - name: dep\n      url: u/dep\n")},
    )
    assert names(manifest) == ["manifest", "child", "dep"]


def test_import_sequence():
    manifest = load(
        """
        manifest:
          members:
            - name: child
              url: u
              import:
                - one.yaml
                - file: two.yaml
        """,
        {
            ("child", "one.yaml"): ("manifest:\n  members:\n    - name: one\n      url: u1\n"),
            ("child", "two.yaml"): ("manifest:\n  members:\n    - name: two\n      url: u2\n"),
        },
    )
    assert names(manifest) == ["manifest", "child", "one", "two"]


CHILD_THREE = (
    "manifest:\n"
    "  members:\n"
    "    - name: x\n"
    "      url: ux\n"
    "    - name: y\n"
    "      url: uy\n"
    "      path: libs/y\n"
    "    - name: z\n"
    "      url: uz\n"
)


def test_name_allowlist():
    manifest = load(
        """
        manifest:
          members:
            - name: child
              url: u
              import:
                name-allowlist: [x, z]
        """,
        {("child", "repospace.yaml"): CHILD_THREE},
    )
    assert names(manifest) == ["manifest", "child", "x", "z"]


def test_name_blocklist():
    manifest = load(
        """
        manifest:
          members:
            - name: child
              url: u
              import:
                name-blocklist: [y]
        """,
        {("child", "repospace.yaml"): CHILD_THREE},
    )
    assert names(manifest) == ["manifest", "child", "x", "z"]


def test_allowlist_overrides_blocklist():
    manifest = load(
        """
        manifest:
          members:
            - name: child
              url: u
              import:
                name-allowlist: [y]
                name-blocklist: [y]
        """,
        {("child", "repospace.yaml"): CHILD_THREE},
    )
    assert names(manifest) == ["manifest", "child", "y"]


def test_path_allowlist_matches_right_anchored():
    manifest = load(
        """
        manifest:
          members:
            - name: child
              url: u
              import:
                path-allowlist: [y]
        """,
        {("child", "repospace.yaml"): CHILD_THREE},
    )
    # Pattern "y" matches path "libs/y" from the right.
    assert names(manifest) == ["manifest", "child", "y"]


def test_filters_compose_down_the_tree():
    # The nested import cannot widen what the top-level allowlist permits.
    manifest = load(
        """
        manifest:
          members:
            - name: child
              url: u
              import:
                name-allowlist: [x, y]
        """,
        {
            ("child", "repospace.yaml"): (
                "manifest:\n" "  members:\n" "    - name: x\n" "      url: ux\n" "      import: true\n"
            ),
            ("x", "repospace.yaml"): (
                "manifest:\n" "  members:\n" "    - name: y\n" "      url: uy\n" "    - name: z\n" "      url: uz\n"
            ),
        },
    )
    assert names(manifest) == ["manifest", "child", "x", "y"]


def test_unknown_import_key_rejected():
    with pytest.raises(MalformedManifest):
        load(
            """
            manifest:
              members:
                - name: child
                  url: u
                  import:
                    name-whitelist: [x]
            """,
            {},
        )


def test_unknown_non_string_import_keys_reported():
    # A bare "on:" key is the boolean True after YAML resolution and
    # does not sort against the string keys.
    with pytest.raises(MalformedManifest) as excinfo:
        load(
            """
            manifest:
              members:
                - name: child
                  url: u
                  import:
                    on: true
                    bogus: 1
            """,
            {},
        )
    message = str(excinfo.value)
    assert "invalid import contents" in message
    assert "True" in message and "bogus" in message


@pytest.mark.parametrize("key", ["path-allowlist", "path-blocklist"])
@pytest.mark.parametrize("pattern", ["''", "'.'"])
def test_empty_path_pattern_rejected(key, pattern):
    # A pattern with no components matches nothing and would crash
    # PurePath.match; like other empty strings it is rejected.
    with pytest.raises(MalformedManifest) as excinfo:
        load(
            "manifest:\n"
            "  members:\n"
            "    - name: child\n"
            "      url: u\n"
            "      import:\n"
            f"        {key}: [{pattern}]\n",
            {},
        )
    assert "is not a path pattern" in str(excinfo.value)


def test_empty_member_import_rejected():
    # "" is not "no import": it would read the member's root directory,
    # not fall back to the default file. Like other empty strings, it
    # is an editing artifact and rejected.
    with pytest.raises(MalformedManifest) as excinfo:
        load(
            """
            manifest:
              members:
                - name: child
                  url: u
                  import: ""
            """,
            {},
        )
    assert '"import" is empty' in str(excinfo.value)


def test_empty_import_map_file_rejected():
    with pytest.raises(MalformedManifest) as excinfo:
        load(
            """
            manifest:
              members:
                - name: child
                  url: u
                  import:
                    file: ""
            """,
            {},
        )
    assert '"file" is empty' in str(excinfo.value)


def test_empty_self_import_rejected(tmp_path):
    top = tmp_path / "repospace.yaml"
    top.write_text('manifest:\n  self:\n    import: ""\n')
    with pytest.raises(MalformedManifest) as excinfo:
        Manifest.from_file(top)
    assert "is empty" in str(excinfo.value)


@pytest.mark.parametrize(
    "block,fragment",
    [
        ("      import: ../evil.yaml\n", "escapes the member repository"),
        (
            "      import: sub/../../evil.yaml\n",
            "escapes the member repository",
        ),
        ("      import: /etc/evil.yaml\n", "is an absolute path"),
        (
            "      import:\n        file: ../evil.yaml\n",
            "escapes the member repository",
        ),
    ],
)
def test_member_import_path_escape_rejected(block, fragment):
    # git refuses to read such a path, and the failure would otherwise
    # be reported as a stale repospace-rev, advising an update that
    # cannot help.
    with pytest.raises(MalformedManifest) as excinfo:
        load(
            "manifest:\n" "  members:\n" "    - name: child\n" "      url: u\n" + block,
            {},
        )
    assert fragment in str(excinfo.value)


def test_member_import_path_is_normalized_before_reading():
    # The importer (and git, on the direct route) is asked for the
    # normalized path, as the self-import route and the cycle check
    # already use: git does not normalize an inner ".." in a
    # <rev>:<path> spec, and the failure would be reported as a stale
    # repospace-rev, advising an update that cannot help.
    manifest = load(
        "manifest:\n  members:\n    - name: child\n      url: u\n      import: sub/../conf/./x.yaml\n",
        {("child", "conf/x.yaml"): "manifest:\n  members:\n    - name: dep\n      url: u/dep\n"},
    )
    assert names(manifest) == ["manifest", "child", "dep"]


def test_member_import_with_inner_dotdot_read_from_git(repos):
    repo = repos.create("child", {"conf/x.yaml": "manifest:\n  members:\n    - name: dep\n      url: u/dep\n"})
    repos.branch(repo, MANIFEST_REV)
    manifest = Manifest.from_data(
        "manifest:\n  members:\n    - name: child\n      url: u\n      import: sub/../conf/x.yaml\n",
        topdir=str(repos.base),
    )
    assert names(manifest) == ["manifest", "child", "dep"]


def test_duplicate_name_rejected_even_when_filtered():
    # A file declaring the same name twice is malformed regardless of
    # which occurrences an importing manifest's filter keeps.
    with pytest.raises(MalformedManifest) as excinfo:
        load(
            """
            manifest:
              members:
                - name: child
                  url: u
                  import:
                    name-blocklist: [x]
            """,
            {
                ("child", "repospace.yaml"): (
                    "manifest:\n" "  members:\n" "    - name: x\n" "      url: u1\n" "    - name: x\n" "      url: u2\n"
                )
            },
        )
    assert "member name x used twice" in str(excinfo.value)


def test_first_definition_wins_and_loser_import_not_processed():
    manifest = load(
        """
        manifest:
          members:
            - name: dup
              url: top-url
            - name: imp
              url: u
              import: true
        """,
        {
            ("imp", "repospace.yaml"): (
                "manifest:\n" "  members:\n" "    - name: dup\n" "      url: other-url\n" "      import: true\n"
            ),
            ("dup", "repospace.yaml"): ("manifest:\n  members:\n    - name: never\n      url: nope\n"),
        },
    )
    assert names(manifest) == ["manifest", "dup", "imp"]
    assert manifest.members[1].url == "top-url"


def test_path_prefix():
    manifest = load(
        """
        manifest:
          members:
            - name: child
              url: u
              import:
                path-prefix: ext
        """,
        {
            ("child", "repospace.yaml"): (
                "manifest:\n"
                "  members:\n"
                "    - name: lib\n"
                "      url: ul\n"
                "    - name: deep\n"
                "      url: ud\n"
                "      import:\n"
                "        path-prefix: more\n"
            ),
            ("deep", "repospace.yaml"): ("manifest:\n  members:\n    - name: leaf\n      url: uleaf\n"),
        },
    )
    by_name = {m.name: m for m in manifest.members}
    # path-prefix scopes only to imported content; the importing
    # member's own placement comes from its "path" attribute.
    assert by_name["child"].path == "child"
    assert by_name["lib"].path == "ext/lib"
    assert by_name["deep"].path == "ext/deep"
    assert by_name["leaf"].path == "ext/more/leaf"


def test_path_prefix_dict_equals_single_item_sequence():
    for import_block in (
        "      import:\n        path-prefix: ext\n",
        "      import:\n        - path-prefix: ext\n",
    ):
        manifest = load(
            "manifest:\n" "  members:\n" "    - name: child\n" "      url: u\n" + import_block,
            {("child", "repospace.yaml"): ("manifest:\n  members:\n    - name: lib\n      url: ul\n")},
        )
        by_name = {m.name: m for m in manifest.members}
        assert by_name["child"].path == "child"
        assert by_name["lib"].path == "ext/lib"


TOP_WITH_PREFIX = """
manifest:
  members:
    - name: child
      url: u
      import:
        path-prefix: ext
"""


def test_imported_path_is_normalized_under_prefix():
    manifest = load(
        TOP_WITH_PREFIX,
        {
            ("child", "repospace.yaml"): (
                "manifest:\n" "  members:\n" "    - name: lib\n" "      url: ul\n" "      path: sub/../lib\n"
            )
        },
    )
    by_name = {m.name: m for m in manifest.members}
    assert by_name["lib"].path == "ext/lib"
    assert by_name["lib"].as_dict()["path"] == "ext/lib"


def test_imported_path_of_prefix_directory_rejected():
    # "path: ." names the prefix directory, not a member of its own —
    # the same mistake as "path: ." at the top level.
    with pytest.raises(MalformedManifest) as excinfo:
        load(
            TOP_WITH_PREFIX,
            {
                ("child", "repospace.yaml"): (
                    "manifest:\n" "  members:\n" "    - name: lib\n" "      url: ul\n" "      path: .\n"
                )
            },
        )
    assert "path-prefix directory ext itself" in str(excinfo.value)


def test_member_attribution_of_extensions_and_packages():
    manifest = load(
        """
        manifest:
          members:
            - name: child
              url: u
              extension-commands: own.yaml
              import: true
        """,
        {
            ("child", "repospace.yaml"): (
                "manifest:\n" "  self:\n" "    extension-commands: imported.yaml\n" "    cmake-packages: [ChildPkg]\n"
            )
        },
    )
    child = manifest.members[1]
    assert child.extension_commands == ["own.yaml", "imported.yaml"]
    assert child.cmake_packages == ["ChildPkg"]


def test_group_filter_precedence_across_imports(tmp_path):
    top = tmp_path / "repospace.yaml"
    sub = tmp_path / "sub.yaml"
    top.write_text(textwrap.dedent("""
            manifest:
              group-filter: [-a]
              self:
                import: sub.yaml
              members:
                - name: imp
                  url: u
                  import: true
            """))
    sub.write_text("manifest:\n  group-filter: [+b, -c]\n")
    manifest = Manifest.from_file(
        top,
        importer=stub({("imp", "repospace.yaml"): ("manifest:\n  group-filter: [+a, -b]\n")}),
    )
    # Precedence: self-import (-c, +b) > top (-a) > member import (-b, +a).
    assert manifest.group_filter == ["-a", "-c"]


def test_self_import_wins_over_top_level(tmp_path):
    top = tmp_path / "repospace.yaml"
    subdir = tmp_path / "submanifests"
    subdir.mkdir()
    (subdir / "00-override.yaml").write_text(
        "manifest:\n" "  members:\n" "    - name: common\n" "      url: from-self-import\n"
    )
    (subdir / "10-more.yml").write_text("manifest:\n  members:\n    - name: extra\n      url: ue\n")
    top.write_text(textwrap.dedent("""
            manifest:
              self:
                import: submanifests
              members:
                - name: common
                  url: from-top
            """))
    manifest = Manifest.from_file(top)
    assert names(manifest) == ["manifest", "common", "extra"]
    assert manifest.members[1].url == "from-self-import"
    assert manifest.members[1].declared_by == "manifest"


def test_self_import_extension_precedence(tmp_path):
    top = tmp_path / "repospace.yaml"
    (tmp_path / "extra.yaml").write_text(
        "manifest:\n" "  self:\n" "    extension-commands: imported.yaml\n" "    cmake-packages: [Imported]\n"
    )
    top.write_text(textwrap.dedent("""
            manifest:
              self:
                import: extra.yaml
                extension-commands: own.yaml
                cmake-packages: [Own]
            """))
    manifest = Manifest.from_file(top)
    mm = manifest.members[0]
    # Self-imported values come first (higher precedence).
    assert mm.extension_commands == ["imported.yaml", "own.yaml"]
    assert mm.cmake_packages == ["Imported", "Own"]


def test_member_self_import_uses_member_data():
    # A "self: import:" inside a manifest imported from a member is
    # resolved from the member's own data (here: the importer), never
    # from a filesystem working tree.
    manifest = load(
        TOP_WITH_CHILD,
        {
            ("child", "repospace.yaml"): ("manifest:\n  self:\n    import: extra.yaml\n"),
            ("child", "extra.yaml"): ("manifest:\n  members:\n    - name: dep\n      url: u/dep\n"),
        },
    )
    assert names(manifest) == ["manifest", "child", "dep"]
    assert manifest.members[2].declared_by == "child"


def test_member_self_import_escape_rejected():
    with pytest.raises(MalformedManifest) as excinfo:
        load(
            TOP_WITH_CHILD,
            {("child", "repospace.yaml"): ("manifest:\n  self:\n    import: ../evil.yaml\n")},
        )
    assert "escapes the member repository" in str(excinfo.value)


def test_self_import_bool_rejected():
    with pytest.raises(MalformedManifest):
        load("manifest:\n  self:\n    import: true\n")


def test_self_import_escape_rejected(tmp_path):
    (tmp_path / "evil.yaml").write_text("manifest:\n  members:\n    - name: leaked\n      url: u\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "repospace.yaml").write_text("manifest:\n  self:\n    import: ../evil.yaml\n")
    with pytest.raises(MalformedManifest) as excinfo:
        Manifest.from_file(repo / "repospace.yaml")
    assert "escapes the manifest repository" in str(excinfo.value)


def test_self_import_symlinked_file_in_directory_rejected(tmp_path):
    # Manifest data is never read through a symlink, wherever the link
    # points: the git route cannot follow one, so following it here
    # would make the same repository resolve differently by route.
    (tmp_path / "evil.yaml").write_text("manifest:\n")
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    (repo / "repospace.yaml").write_text("manifest:\n  self:\n    import: sub\n")
    (repo / "sub" / "inn.yaml").symlink_to(tmp_path / "evil.yaml")
    with pytest.raises(MalformedManifest) as excinfo:
        Manifest.from_file(repo / "repospace.yaml")
    assert "symlinked manifest data is not allowed" in str(excinfo.value)


def test_self_import_broken_symlink_in_directory_rejected(tmp_path):
    # A link with a missing target is not a file, so it would be
    # skipped silently while the git route reports it as a symlink.
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "gone.yaml").symlink_to(tmp_path / "missing.yaml")
    top = tmp_path / "repospace.yaml"
    top.write_text("manifest:\n  self:\n    import: sub\n")
    with pytest.raises(MalformedManifest) as excinfo:
        Manifest.from_file(top)
    assert "symlinked manifest data is not allowed" in str(excinfo.value)


@pytest.mark.parametrize("target", ["real.yaml", "sub"])
def test_self_import_symlink_inside_repository_rejected(tmp_path, target):
    # Not an escape: the link stays inside the manifest repository and
    # was accepted before, file or directory alike.
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "in.yaml").write_text("manifest:\n")
    (tmp_path / "real.yaml").write_text("manifest:\n")
    (tmp_path / "link").symlink_to(tmp_path / target)
    top = tmp_path / "repospace.yaml"
    top.write_text("manifest:\n  self:\n    import: link\n")
    with pytest.raises(MalformedManifest) as excinfo:
        Manifest.from_file(top)
    assert "symlinked manifest data is not allowed" in str(excinfo.value)


@pytest.mark.parametrize("imp", ["link/inner.yaml", "link/conf", "link/./inner.yaml"])
def test_self_import_through_symlinked_directory_rejected(tmp_path, imp):
    # Only the imported file used to be checked; a linked directory on
    # the way to it was followed, while the git route cannot resolve
    # such a path at all. Every component below the repository root
    # counts, so both routes agree about the same repository.
    (tmp_path / "real" / "conf").mkdir(parents=True)
    (tmp_path / "real" / "inner.yaml").write_text("manifest:\n  members:\n    - name: x\n      url: u\n")
    (tmp_path / "real" / "conf" / "a.yaml").write_text("manifest:\n")
    os.symlink("real", tmp_path / "link")
    top = tmp_path / "repospace.yaml"
    top.write_text(f"manifest:\n  self:\n    import: {imp}\n")
    with pytest.raises(MalformedManifest) as excinfo:
        Manifest.from_file(top)
    assert "link is a symbolic link" in str(excinfo.value)
    assert "symlinked manifest data is not allowed" in str(excinfo.value)


def test_self_import_missing_file(tmp_path):
    top = tmp_path / "repospace.yaml"
    top.write_text("manifest:\n  self:\n    import: ghost.yaml\n")
    with pytest.raises(MalformedManifest) as excinfo:
        Manifest.from_file(top)
    assert "file not found" in str(excinfo.value)


def test_self_import_without_repo_fails():
    with pytest.raises(ManifestImportFailed):
        load("manifest:\n  self:\n    import: sub.yaml\n")


def test_self_import_direct_cycle(tmp_path):
    top = tmp_path / "repospace.yaml"
    top.write_text("manifest:\n  self:\n    import: repospace.yaml\n")
    with pytest.raises(MalformedManifest) as excinfo:
        Manifest.from_file(top)
    assert "import cycle" in str(excinfo.value)


def test_self_import_default_file_cycle(tmp_path):
    # A self-import map without "file" defaults to repospace.yaml; at
    # the top level that is the document itself.
    top = tmp_path / "repospace.yaml"
    top.write_text("manifest:\n  self:\n    import:\n      path-prefix: p\n")
    with pytest.raises(MalformedManifest) as excinfo:
        Manifest.from_file(top)
    assert "import cycle" in str(excinfo.value)


def test_self_import_indirect_cycle(tmp_path):
    (tmp_path / "repospace.yaml").write_text("manifest:\n  self:\n    import: a.yaml\n")
    (tmp_path / "a.yaml").write_text("manifest:\n  self:\n    import: b.yaml\n")
    (tmp_path / "b.yaml").write_text("manifest:\n  self:\n    import: a.yaml\n")
    with pytest.raises(MalformedManifest) as excinfo:
        Manifest.from_file(tmp_path / "repospace.yaml")
    assert "import cycle" in str(excinfo.value)
    assert "a.yaml" in str(excinfo.value)


def test_self_import_diamond_is_legal(tmp_path):
    (tmp_path / "repospace.yaml").write_text("manifest:\n  self:\n    import: [a.yaml, b.yaml]\n")
    (tmp_path / "a.yaml").write_text("manifest:\n  self:\n    import: common.yaml\n")
    (tmp_path / "b.yaml").write_text("manifest:\n  self:\n    import: common.yaml\n")
    (tmp_path / "common.yaml").write_text("manifest:\n  members:\n    - name: c\n      url: u\n")
    manifest = Manifest.from_file(tmp_path / "repospace.yaml")
    assert names(manifest) == ["manifest", "c"]


def test_member_self_import_cycle():
    with pytest.raises(MalformedManifest) as excinfo:
        load(
            TOP_WITH_CHILD,
            {
                ("child", "repospace.yaml"): ("manifest:\n  self:\n    import: hw.yaml\n"),
                ("child", "hw.yaml"): ("manifest:\n  self:\n    import: repospace.yaml\n"),
            },
        )
    assert "import cycle" in str(excinfo.value)


def test_import_depth_guard():
    def importer(member, path):
        index = int(member.name[1:])
        return "manifest:\n" "  members:\n" f"    - name: m{index + 1}\n" "      url: u\n" "      import: true\n"

    with pytest.raises(ManifestImportFailed) as excinfo:
        Manifest.from_data(
            "manifest:\n" "  members:\n" "    - name: m1\n" "      url: u\n" "      import: true\n",
            importer=importer,
        )
    assert "too deep" in str(excinfo.value)
    # The failure is attributed to the member whose data was being
    # resolved, not to "self".
    assert "from self" not in str(excinfo.value)
    assert "from m" in str(excinfo.value)


def test_ignore_flag_skips_all_imports(tmp_path):
    top = tmp_path / "repospace.yaml"
    top.write_text(textwrap.dedent("""
            manifest:
              self:
                import: ghost-dir
              members:
                - name: child
                  url: u
                  import: true
            """))
    manifest = Manifest.from_file(top, import_flags=ImportFlag.IGNORE)
    assert names(manifest) == ["manifest", "child"]
    assert not manifest.has_imports


@pytest.mark.parametrize(
    "text,fragment",
    [
        ("manifest:\n  members:\n    - name: a\n      url: u\n      import: 5\n", "invalid import 5 of type int"),
        (
            "manifest:\n  members:\n    - name: a\n      url: u\n      import: [false]\n",
            'falsy "import" inside a sequence',
        ),
        ("manifest:\n  members:\n    - name: a\n      url: u\n      import: ''\n", "is empty"),
        (
            "manifest:\n  members:\n    - name: a\n      url: u\n      import:\n        file: 5\n",
            '"file" is not a string',
        ),
        ("manifest:\n  self:\n    import: 5\n", "has invalid type int"),
        ("manifest:\n  self:\n    import: true\n", "of boolean"),
        ("manifest:\n  self:\n    import: ['']\n", "is empty"),
        ("manifest:\n  self:\n    import:\n      bogus: 1\n", "invalid import contents"),
    ],
)
def test_import_values_are_validated_when_imports_are_ignored(text, fragment):
    # "manifest --validate" does not follow imports; an import value of
    # the wrong shape must still fail there, not only on the next real
    # load.
    with pytest.raises(MalformedManifest) as excinfo:
        Manifest.from_data(text, import_flags=ImportFlag.IGNORE)
    assert fragment in str(excinfo.value)


def test_ignore_members_still_resolves_self_imports(tmp_path):
    top = tmp_path / "repospace.yaml"
    (tmp_path / "extra.yaml").write_text("manifest:\n  members:\n    - name: fromself\n      url: u\n")
    top.write_text(textwrap.dedent("""
            manifest:
              self:
                import: extra.yaml
              members:
                - name: child
                  url: u
                  import: true
            """))
    manifest = Manifest.from_file(top, import_flags=ImportFlag.IGNORE_MEMBERS)
    assert names(manifest) == ["manifest", "fromself", "child"]


def test_unreadable_self_import_rejected(tmp_path):
    # A permission problem inside import resolution must surface as
    # MalformedManifest, like the top-level manifest read, not as a
    # traceback from a raw OSError.
    top = tmp_path / "repospace.yaml"
    top.write_text("manifest:\n  self:\n    import: extra.yaml\n")
    imported = tmp_path / "extra.yaml"
    imported.write_text("manifest:\n")
    imported.chmod(0)
    try:
        if os.access(imported, os.R_OK):
            pytest.skip("cannot make the file unreadable (root?)")
        with pytest.raises(MalformedManifest) as excinfo:
            Manifest.from_file(top)
        assert "cannot read" in str(excinfo.value)
    finally:
        imported.chmod(0o644)


def test_member_import_without_importer_fails():
    with pytest.raises(ManifestImportFailed):
        Manifest.from_data(textwrap.dedent(TOP_WITH_CHILD))


def test_importer_returning_none_skips():
    manifest = Manifest.from_data(textwrap.dedent(TOP_WITH_CHILD), importer=lambda member, path: None)
    assert names(manifest) == ["manifest", "child"]


def test_importer_returning_list():
    manifest = Manifest.from_data(
        textwrap.dedent(TOP_WITH_CHILD),
        importer=lambda member, path: [
            "manifest:\n  members:\n    - name: one\n      url: u1\n",
            "manifest:\n  members:\n    - name: two\n      url: u2\n",
        ],
    )
    assert names(manifest) == ["manifest", "child", "one", "two"]


def test_declared_by_chain():
    manifest = load(
        TOP_WITH_CHILD,
        {
            ("child", "repospace.yaml"): (
                "manifest:\n" "  members:\n" "    - name: grand\n" "      url: u\n" "      import: true\n"
            ),
            ("grand", "repospace.yaml"): ("manifest:\n  members:\n    - name: leaf\n      url: u\n"),
        },
    )
    by_name = {m.name: m for m in manifest.members}
    assert by_name["child"].declared_by == "manifest"
    assert by_name["grand"].declared_by == "child"
    assert by_name["leaf"].declared_by == "grand"


def test_recursive_alias_in_self_import_rejected(tmp_path):
    # A YAML alias can make the import sequence contain itself; that
    # must be a clean error, not a RecursionError traceback.
    top = tmp_path / "repospace.yaml"
    top.write_text("manifest:\n  self:\n    import: &a [*a]\n")
    with pytest.raises(MalformedManifest) as excinfo:
        Manifest.from_file(top)
    assert "recursive YAML alias" in str(excinfo.value)


def test_recursive_alias_in_member_import_rejected():
    with pytest.raises(MalformedManifest) as excinfo:
        load("""
            manifest:
              members:
                - name: child
                  url: u
                  import: &a [*a]
            """)
    assert "recursive YAML alias" in str(excinfo.value)


def test_sibling_alias_in_import_stays_legal(tmp_path):
    # A diamond — the same anchored list aliased twice as siblings —
    # is not a cycle.
    top = tmp_path / "repospace.yaml"
    (tmp_path / "one.yaml").write_text("manifest:\n  members:\n    - name: one\n      url: u1\n")
    top.write_text("manifest:\n  self:\n    import:\n      - &a [one.yaml]\n      - *a\n")
    manifest = Manifest.from_file(top)
    assert names(manifest) == ["manifest", "one"]


@pytest.mark.parametrize("path", ["link.yaml", "conf"])
def test_member_import_of_symlink_rejected(repos, path):
    # git stores a symlink as a blob with mode 120000, so the object
    # type alone would let the link target text be parsed as manifest
    # data — as a single-file import or inside a directory import.
    repo = repos.create(
        "child",
        {"real.yaml": "manifest:\n", "conf/real.yaml": "manifest:\n"},
    )
    os.symlink("real.yaml", repo / "link.yaml")
    os.symlink("real.yaml", repo / "conf" / "link.yaml")
    repos.commit(repo, {}, "add symlinks")
    repos.branch(repo, MANIFEST_REV)
    member = Member("child", url="unused", topdir=str(repos.base))
    with pytest.raises(MalformedManifest) as excinfo:
        member_manifest_content(member, path)
    assert "symlinked manifest data is not allowed" in str(excinfo.value)


def test_member_import_of_regular_file_still_read(repos):
    repo = repos.create("child", {"conf/real.yaml": "manifest: # real\n"})
    os.symlink("real.yaml", repo / "conf" / "link.txt")
    repos.commit(repo, {}, "add symlink")
    repos.branch(repo, MANIFEST_REV)
    member = Member("child", url="unused", topdir=str(repos.base))
    # The symlink is not manifest data by name, so the directory still
    # imports cleanly.
    assert member_manifest_content(member, "conf") == ["manifest: # real\n"]
    assert member_manifest_content(member, "conf/real.yaml") == ["manifest: # real\n"]


def test_non_utf8_self_import_rejected(tmp_path):
    top = tmp_path / "repospace.yaml"
    top.write_text("manifest:\n  self:\n    import: bad.yaml\n")
    (tmp_path / "bad.yaml").write_bytes(b"manifest: \xff\n")
    with pytest.raises(MalformedManifest) as excinfo:
        Manifest.from_file(top)
    assert "cannot read" in str(excinfo.value)
