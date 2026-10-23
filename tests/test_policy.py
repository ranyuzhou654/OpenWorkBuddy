import os
from pathlib import Path

from nano_workbuddy.policy import PRESETS, Rule, check

HOME = Path("/Users/me/.nwb")
ROOT = Path("/work/proj")
PLACES = {"root": str(ROOT), "home": str(HOME), "spill": str(HOME / "spill" / "s1")}


def effect(preset, action, resource, extra=()):
    return check(action, resource, [*PRESETS[preset].rules, *extra], PLACES).effect


def test_workspace_preset():
    assert effect("workspace", "read", "/etc/hosts") == "allow"
    assert effect("workspace", "write", "/work/proj/src/a.py") == "allow"
    assert effect("workspace", "edit", "/work/proj/README.md") == "allow"
    assert effect("workspace", "write", "/work/other/a.py") == "ask"  # outside the project
    assert effect("workspace", "write", "/work/proj/.git/hooks/pre-commit") == "ask"  # protected
    assert effect("workspace", "bash", "rm -rf build") == "allow"  # the sandbox confines it
    assert effect("workspace", "bash.unsandboxed", "curl example.com") == "ask"
    assert effect("workspace", "mcp__x__y", "") == "ask"  # unknown tools default to asking


def test_secrets_are_denied_but_own_spill_files_are_readable():
    ssh = os.path.expanduser("~/.ssh/id_ed25519")
    assert effect("workspace", "read", ssh) == "deny"
    assert effect("workspace", "read", str(HOME / "token")) == "deny"
    assert effect("workspace", "read", str(HOME / "spill" / "s1" / "call_1.txt")) == "allow"
    assert effect("workspace", "read", str(HOME / "spill" / "s2" / "call_1.txt")) == "deny"  # other sessions


def test_readonly_and_trust_presets():
    assert effect("readonly", "read", "/work/proj/a") == "allow"
    assert effect("readonly", "write", "/work/proj/a") == "ask"
    assert effect("readonly", "bash", "ls") == "ask"
    assert effect("trust", "write", "/anywhere") == "allow"
    assert effect("trust", "bash.unsandboxed", "anything") == "allow"


def test_last_matching_rule_wins_and_config_rules_override_presets():
    extra = [Rule("bash", "git push*", "deny")]
    assert effect("workspace", "bash", "git push origin main", extra) == "deny"
    assert effect("workspace", "bash", "git status", extra) == "allow"


def test_always_allow_suggestion_never_widens_into_a_wildcard():
    c = check("bash.unsandboxed", "rm *", [], PLACES)
    rule = c.suggestion()
    assert rule.matches("bash.unsandboxed", "rm *", PLACES)
    assert not rule.matches("bash.unsandboxed", "rm -rf /", PLACES)
    path_rule = check("write", "/work/other/a.py", [], PLACES).suggestion()
    assert path_rule.resource == "/work/other/*" and path_rule.matches("write", "/work/other/b.py", PLACES)
    assert check("write", "/work/[x]/a.py", [], PLACES).suggestion().resource == "/work/[[]x]/*"
