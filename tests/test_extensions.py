"""Integration tests for extension commands."""

from __future__ import annotations

import os
import sys
import textwrap

import pytest


@pytest.fixture
def updated(repospace, run_repospace):
    code, out, err = run_repospace(["update"], cwd=repospace.ws)
    assert code == 0, err
    return repospace


def test_extension_runs(updated, run_repospace):
    code, out, err = run_repospace(["liba-hello"], cwd=updated.ws)
    assert code == 0, err
    assert out.strip() == "hello from liba"


def test_extension_own_arguments(updated, run_repospace):
    code, out, err = run_repospace(["liba-hello", "--shout"], cwd=updated.ws)
    assert code == 0, err
    assert out.strip() == "HELLO FROM LIBA"


def test_extension_grouped_in_help(updated, run_repospace):
    code, out, err = run_repospace(["help"], cwd=updated.ws)
    assert code == 0
    assert "extension commands from member liba (path: liba):" in out
    assert "liba-hello:" in out
    assert "print a greeting from liba" in out


def test_help_for_extension(updated, run_repospace):
    code, out, err = run_repospace(["help", "liba-hello"], cwd=updated.ws)
    assert code == 0
    assert "--shout" in out


def test_builtin_name_collision_ignored(updated, run_repospace):
    spec = updated.ws / "liba" / "repospace-commands.yaml"
    spec.write_text(spec.read_text() + "      - name: update\n" "        class: LibACommand\n")
    code, out, err = run_repospace(["liba-hello"], cwd=updated.ws)
    assert code == 0, err
    assert 'ignoring member liba extension command "update"' in err
    assert "built-in" in err


def test_extension_collision_first_member_wins(updated, run_repospace):
    libb = updated.ws / "libb"
    (libb / "scripts").mkdir()
    (libb / "scripts" / "ext.py").write_text(textwrap.dedent("""\
            from repospace.commands import RepospaceCommand


            class Hello(RepospaceCommand):
                def __init__(self):
                    super().__init__(
                        "liba-hello", "impostor", "impostor",
                        requires_repospace=False,
                    )

                def do_add_parser(self, parser_adder):
                    return parser_adder.add_parser(self.name)

                def do_run(self, args, unknown):
                    print("from libb impostor")


            class Unique(RepospaceCommand):
                def __init__(self):
                    super().__init__(
                        "libb-cmd", "a libb command", "a libb command",
                        requires_repospace=False,
                    )

                def do_add_parser(self, parser_adder):
                    return parser_adder.add_parser(self.name)

                def do_run(self, args, unknown):
                    print("libb command output")
            """))
    (libb / "repospace-commands.yaml").write_text(textwrap.dedent("""\
            extension-commands:
              - file: scripts/ext.py
                commands:
                  - name: liba-hello
                    class: Hello
                  - name: libb-cmd
                    class: Unique
                    help: a libb command
            """))
    updated.rewrite_app_yaml()
    yaml_file = updated.app / "repospace.yaml"
    yaml_file.write_text(
        yaml_file.read_text().replace(
            "      revision: v1.0\n",
            "      revision: v1.0\n" "      extension-commands: repospace-commands.yaml\n",
        )
    )
    # liba comes first in resolution order, so it keeps liba-hello.
    code, out, err = run_repospace(["liba-hello"], cwd=updated.ws)
    assert code == 0, err
    assert out.strip() == "hello from liba"
    assert 'ignoring member libb extension command "liba-hello"' in err
    # libb's non-colliding command works.
    code, out, err = run_repospace(["libb-cmd"], cwd=updated.ws)
    assert code == 0, err
    assert out.strip() == "libb command output"


def test_allow_extensions_config(updated, run_repospace):
    run_repospace(["config", "commands.allow-extensions", "false"], cwd=updated.ws)
    code, out, err = run_repospace(["liba-hello"], cwd=updated.ws)
    assert code == 2
    assert "unknown command" in err


def test_invalid_allow_extensions_value_warns_and_continues(updated, run_repospace):
    # Like a malformed alias: a value getboolean rejects must not take
    # down every invocation, or the "config -d" that removes it could
    # never run.
    run_repospace(["config", "commands.allow-extensions", "nonsense"], cwd=updated.ws)
    code, out, err = run_repospace(["list"], cwd=updated.ws)
    assert code == 0, err
    assert "cannot load extension commands" in err
    assert "Traceback" not in err
    code, out, err = run_repospace(["config", "-d", "commands.allow-extensions"], cwd=updated.ws)
    assert code == 0, err
    code, out, err = run_repospace(["liba-hello"], cwd=updated.ws)
    assert code == 0, err


def test_lazy_loading_survives_broken_module(updated, run_repospace):
    (updated.ws / "liba" / "scripts" / "liba_ext.py").write_text("raise RuntimeError('boom at import time')\n")
    # Discovery does not import the module: help still lists the command.
    code, out, err = run_repospace(["help"], cwd=updated.ws)
    assert code == 0
    assert "liba-hello:" in out
    # Invoking it fails cleanly.
    code, out, err = run_repospace(["liba-hello"], cwd=updated.ws)
    assert code == 1
    assert "could not be created" in err
    assert "could not import" in err


def test_extensions_unavailable_before_update(repospace, run_repospace):
    # liba is not cloned yet, so the manifest (which imports from liba)
    # cannot resolve and extensions are unavailable.
    code, out, err = run_repospace(["liba-hello"], cwd=repospace.ws)
    assert code == 2
    assert "manifest could not be loaded" in err
    code, out, err = run_repospace(["help"], cwd=repospace.ws)
    assert code == 0
    assert "Cannot load extension commands" in out


def test_extension_missing_class_attribute(updated, run_repospace):
    spec = updated.ws / "liba" / "repospace-commands.yaml"
    spec.write_text(spec.read_text().replace("LibACommand", "Nonexistent"))
    code, out, err = run_repospace(["liba-hello"], cwd=updated.ws)
    assert code == 1
    assert "no attribute Nonexistent" in err


def test_extension_constructor_failure(updated, run_repospace):
    (updated.ws / "liba" / "scripts" / "liba_ext.py").write_text(textwrap.dedent("""\
            class LibACommand:
                def __init__(self):
                    raise RuntimeError("boom in constructor")
            """))
    code, out, err = run_repospace(["liba-hello"], cwd=updated.ws)
    assert code == 1
    assert "constructor threw an exception" in err


def test_extension_class_not_a_command(updated, run_repospace):
    (updated.ws / "liba" / "scripts" / "liba_ext.py").write_text("class LibACommand:\n    pass\n")
    code, out, err = run_repospace(["liba-hello"], cwd=updated.ws)
    assert code == 1
    assert "could not be created" in err
    assert "not a RepospaceCommand" in err
    assert "Traceback" not in err


def test_extension_name_mismatch_with_spec(updated, run_repospace):
    # A class registering a name other than the specification's would
    # only fail later with a bare argparse "invalid choice" error;
    # the mismatch must be diagnosed instead.
    spec = updated.ws / "liba" / "repospace-commands.yaml"
    spec.write_text(spec.read_text().replace("name: liba-hello", "name: other-name"))
    code, out, err = run_repospace(["other-name"], cwd=updated.ws)
    assert code == 1
    assert "could not be created" in err
    assert 'names itself "liba-hello"' in err
    assert '"other-name"' in err
    assert "Traceback" not in err


def test_extension_non_string_help_does_not_crash_help(updated, run_repospace):
    (updated.ws / "liba" / "repospace-commands.yaml").write_text(
        "extension-commands:\n"
        "  - file: scripts/liba_ext.py\n"
        "    commands:\n"
        "      - name: liba-hello\n"
        "        help: 1.0\n"
    )
    code, out, err = run_repospace(["help"], cwd=updated.ws)
    assert code == 0
    assert "cannot load extension commands" in err
    assert "Traceback" not in err


def test_extension_non_python_file(updated, run_repospace):
    spec = updated.ws / "liba" / "repospace-commands.yaml"
    spec.write_text(spec.read_text().replace("scripts/liba_ext.py", "liba.txt"))
    code, out, err = run_repospace(["liba-hello"], cwd=updated.ws)
    assert code == 1
    assert "could not import" in err


def test_extension_module_imported_once(updated, run_repospace):
    # The module import cache must keep side effects from repeating when
    # the same extension file is loaded again in this process.
    counter = updated.ws / "liba" / "scripts" / "import-count.txt"
    ext = updated.ws / "liba" / "scripts" / "liba_ext.py"
    ext.write_text(
        "with open(__file__.replace('liba_ext.py', 'import-count.txt'),"
        " 'a') as f:\n"
        "    f.write('x')\n" + ext.read_text()
    )
    for _ in range(2):
        code, out, err = run_repospace(["liba-hello"], cwd=updated.ws)
        assert code == 0, err
    assert counter.read_text() == "x"


@pytest.mark.parametrize(
    "content, hint",
    [
        ("{invalid yaml", "cannot parse YAML"),
        ("- 1\n", 'expected an "extension-commands" list'),
        ("extension-commands:\n  - 5\n", 'needs "file" and "commands"'),
        (
            "extension-commands:\n" "  - file: scripts/liba_ext.py\n" "    commands:\n" "      - 5\n",
            'needs a non-empty string "name"',
        ),
        (
            "extension-commands:\n" "  - file: scripts/liba_ext.py\n" "    commands:\n" "      - name: ''\n",
            'needs a non-empty string "name"',
        ),
        (
            "extension-commands:\n"
            "  - file: scripts/liba_ext.py\n"
            "    commands:\n"
            "      - name: liba-hello\n"
            "        class: 123\n",
            '"class" is not a non-empty string',
        ),
        (
            "extension-commands:\n"
            "  - file: scripts/liba_ext.py\n"
            "    commands:\n"
            "      - name: liba-hello\n"
            "        help: 1.0\n",
            '"help" is not a string',
        ),
    ],
)
def test_extension_spec_schema_errors(updated, run_repospace, content, hint):
    (updated.ws / "liba" / "repospace-commands.yaml").write_text(content)
    code, out, err = run_repospace(["topdir"], cwd=updated.ws)
    assert code == 0
    assert "cannot load extension commands" in err
    assert hint in err


def _declare_app_extensions(updated, spec_rel):
    yaml_file = updated.app / "repospace.yaml"
    yaml_file.write_text(
        yaml_file.read_text().replace(
            "    cmake-packages: [App]\n",
            "    cmake-packages: [App]\n" f"    extension-commands: {spec_rel}\n",
        )
    )


def test_extension_spec_path_escape_blocked(updated, run_repospace):
    _declare_app_extensions(updated, "../evil.yaml")
    code, out, err = run_repospace(["topdir"], cwd=updated.ws)
    assert code == 0
    assert "escapes the member directory" in err


def test_extension_py_file_escape_blocked(updated, run_repospace):
    _declare_app_extensions(updated, "app-commands.yaml")
    (updated.app / "app-commands.yaml").write_text(
        "extension-commands:\n" "  - file: ../outside.py\n" "    commands:\n" "      - name: app-cmd\n"
    )
    code, out, err = run_repospace(["topdir"], cwd=updated.ws)
    assert code == 0
    assert "escapes the member directory" in err


def test_extension_missing_spec_ignored(updated, run_repospace):
    # The member may not be cloned yet; a missing spec file is not an
    # error.
    _declare_app_extensions(updated, "nonexistent.yaml")
    code, out, err = run_repospace(["topdir"], cwd=updated.ws)
    assert code == 0, err
    assert "cannot load extension commands" not in err


def test_extension_member_without_abspath_has_no_specs():
    from types import SimpleNamespace

    from repospace.commands import _member_specs

    member = SimpleNamespace(abspath=None, extension_commands=["x.yaml"], name="ghost")
    assert _member_specs(member) == []


def test_extension_exiting_at_import_fails_cleanly(updated, run_repospace):
    # sys.exit() at import time must not end repospace with the
    # extension's own code and nothing said about why.
    (updated.ws / "liba" / "scripts" / "liba_ext.py").write_text("import sys\n\nsys.exit(42)\n")
    code, out, err = run_repospace(["liba-hello"], cwd=updated.ws)
    assert code == 1
    assert "could not be created" in err
    assert "sys.exit(42)" in err
    assert "Traceback" not in err


def test_failed_extension_import_leaves_sys_path_unchanged(updated, run_repospace, monkeypatch):
    # A directory appended for an import that then failed would stay on
    # the path for the rest of the process.
    monkeypatch.setattr(sys, "path", list(sys.path))
    before = list(sys.path)
    (updated.ws / "liba" / "scripts" / "liba_ext.py").write_text("raise RuntimeError('boom at import time')\n")
    code, out, err = run_repospace(["liba-hello"], cwd=updated.ws)
    assert code == 1
    assert sys.path == before


def test_extension_directory_joins_sys_path_once(updated, run_repospace, monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    scripts = updated.ws / "liba" / "scripts"
    for _ in range(2):
        code, out, err = run_repospace(["liba-hello"], cwd=updated.ws)
        assert code == 0, err
    assert sys.path.count(str(scripts)) == 1


def test_non_utf8_spec_file_warns_cleanly(updated, run_repospace):
    # One member's undecodable specification must not crash every
    # invocation, down to "repospace topdir".
    spec = updated.ws / "liba" / "repospace-commands.yaml"
    spec.write_bytes(b"extension-commands:\n  - file: \xff\xfe.py\n")
    code, out, err = run_repospace(["topdir"], cwd=updated.ws)
    assert code == 0
    assert "cannot load extension commands" in err
    assert "not valid UTF-8" in err
    assert "Traceback" not in err


def test_unreadable_spec_file_warns_cleanly(updated, run_repospace):
    spec = updated.ws / "liba" / "repospace-commands.yaml"
    spec.chmod(0)
    try:
        if os.access(spec, os.R_OK):
            pytest.skip("cannot make the file unreadable (root?)")
        code, out, err = run_repospace(["topdir"], cwd=updated.ws)
    finally:
        spec.chmod(0o644)
    assert code == 0
    assert "cannot load extension commands" in err
    assert "cannot read" in err
    assert "Traceback" not in err
