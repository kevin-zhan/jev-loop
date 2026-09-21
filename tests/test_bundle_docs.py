"""Documentation and skill hygiene for the bundle standard.

These are cheap structural checks, not a prose review: the normative spec exists in both
languages, the skills stay portable after being copied out of the repository, the shipped
reference bundle is discoverable, and the documents that promise a command actually name a
command the CLI implements.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from jev_loop import cli

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS = sorted((REPO_ROOT / "skills").glob("*/SKILL.md"))
LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")

BILINGUAL_PAIRS = [
    "README.md",
    "CONTRIBUTING.md",
    "docs/pi-integration.md",
    "docs/bundle-authoring.md",
    "docs/design.md",
    "spec/bundle-standard.md",
]


def test_the_normative_specification_exists_in_both_languages() -> None:
    english = REPO_ROOT / "spec" / "bundle-standard.md"
    chinese = REPO_ROOT / "spec" / "bundle-standard.zh-CN.md"
    assert english.is_file() and chinese.is_file()
    for document in (english, chinese):
        text = document.read_text(encoding="utf-8")
        assert ".agents/jev-bundle/" in text
        assert "schema_version" in text
        assert "not_checked" in text


def test_the_manifest_schema_is_machine_readable_and_names_the_specification() -> None:
    schema = json.loads((REPO_ROOT / "spec" / "bundle-manifest-v2.schema.json").read_text(encoding="utf-8"))
    assert schema["$schema"].startswith("https://json-schema.org/")
    assert schema["properties"]["schema_version"] == {"const": 2}


@pytest.mark.parametrize("relative", BILINGUAL_PAIRS)
def test_every_canonical_document_has_a_chinese_counterpart(relative: str) -> None:
    english = REPO_ROOT / relative
    chinese = english.with_name(english.name.replace(".md", ".zh-CN.md"))
    assert english.is_file(), relative
    assert chinese.is_file(), chinese.name
    english_text = english.read_text(encoding="utf-8")
    chinese_text = chinese.read_text(encoding="utf-8")
    assert "English" in english_text and "简体中文" in chinese_text
    # Section headings are the cheapest information-equality signal available without a translator.
    english_sections = len(re.findall(r"^#{2,3} ", english_text, re.M))
    chinese_sections = len(re.findall(r"^#{2,3} ", chinese_text, re.M))
    assert english_sections == chinese_sections, f"{relative}: {english_sections} vs {chinese_sections} sections"


def test_the_html_walkthroughs_keep_the_same_section_skeleton() -> None:
    english = (REPO_ROOT / "docs" / "getting-started.html").read_text(encoding="utf-8")
    chinese = (REPO_ROOT / "docs" / "getting-started.zh-CN.html").read_text(encoding="utf-8")
    assert len(re.findall(r"<h2>", english)) == len(re.findall(r"<h2>", chinese))
    for anchor in ("bundle-cli", "path-a", "path-b", "path-c", "bundle", "safety", "verify"):
        assert f'id="{anchor}"' in english and f'id="{anchor}"' in chinese, anchor


def test_the_walkthroughs_lead_with_the_host_neutral_path() -> None:
    english = (REPO_ROOT / "docs" / "getting-started.html").read_text(encoding="utf-8")
    chinese = (REPO_ROOT / "docs" / "getting-started.zh-CN.html").read_text(encoding="utf-8")
    assert english.index('id="bundle-cli"') < english.index('id="path-a"')
    assert chinese.index('id="bundle-cli"') < chinese.index('id="path-a"')
    assert "bundle-standard.md" in english and "bundle-standard.zh-CN.md" in chinese


@pytest.mark.parametrize("skill", SKILLS, ids=lambda path: path.parent.name)
def test_skills_are_portable_when_copied(skill: Path) -> None:
    text = skill.read_text(encoding="utf-8")
    for target in LINK.findall(text):
        if target.startswith(("http://", "https://", "#")):
            continue
        assert not target.startswith("../"), f"{skill}: {target} escapes the skill directory"
        assert (skill.parent / target).resolve().is_file(), f"{skill}: missing {target}"


@pytest.mark.parametrize("skill", SKILLS, ids=lambda path: path.parent.name)
def test_skill_frontmatter_follows_the_agent_skills_rules(skill: Path) -> None:
    frontmatter = skill.read_text(encoding="utf-8").split("---")[1]
    name = re.search(r"^name: (\S+)$", frontmatter, re.M)
    description = re.search(r"^description: (.+)$", frontmatter, re.M)
    assert name is not None and name.group(1) == skill.parent.name
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name.group(1))
    assert len(name.group(1)) <= 64
    assert description is not None and 0 < len(description.group(1)) <= 1024
    # A plain YAML scalar may not contain ": " (it would parse as a nested mapping).
    for line in frontmatter.strip().splitlines():
        if line.startswith(("name:", "description:", "compatibility:")):
            _, value = line.split(":", 1)
            assert ": " not in value, f"{skill}: quote or rephrase {line[:60]}"


def test_skill_references_are_self_contained_files() -> None:
    for reference in sorted((REPO_ROOT / "skills").glob("*/references/*.md")):
        text = reference.read_text(encoding="utf-8")
        assert text.strip(), reference
        for target in LINK.findall(text):
            if target.startswith(("http://", "https://", "#")):
                continue
            assert not target.startswith("../"), f"{reference}: {target} escapes the skill directory"
            assert (reference.parent / target).resolve().is_file(), f"{reference}: missing {target}"


def test_creator_scripts_call_the_cli_and_fail_clearly() -> None:
    for script in sorted((REPO_ROOT / "skills" / "jev-bundle-creator" / "scripts").glob("*.sh")):
        text = script.read_text(encoding="utf-8")
        assert "jev-loop" in text
        assert "command -v jev-loop" in text, f"{script}: must explain a missing CLI"
        assert script.stat().st_mode & 0o111, f"{script}: must be executable"


def test_the_documented_commands_are_implemented() -> None:
    parser = cli._parser()
    subcommands = next(
        action
        for action in parser._actions
        if hasattr(action, "choices") and action.choices and "bundle" in action.choices
    )
    assert set(subcommands.choices["bundle"]._subparsers._group_actions[0].choices) == {
        "list",
        "show",
        "validate",
        "init",
        "conformance",
    }
    assert "rpc" in subcommands.choices

    spec = (REPO_ROOT / "spec" / "bundle-standard.md").read_text(encoding="utf-8")
    for command in ("bundle list", "bundle show", "bundle validate", "bundle conformance", "bundle init", "rpc"):
        assert f"jev-loop {command}" in spec, command


@pytest.mark.parametrize(
    "relative",
    ["README.md", "README.zh-CN.md", "CONTRIBUTING.md", "CONTRIBUTING.zh-CN.md", "spec/bundle-standard.md",
     "spec/bundle-standard.zh-CN.md", "docs/bundle-authoring.md", "docs/bundle-authoring.zh-CN.md",
     "docs/pi-integration.md", "docs/pi-integration.zh-CN.md"],
)
def test_relative_document_links_resolve(relative: str) -> None:
    document = REPO_ROOT / relative
    for target in LINK.findall(document.read_text(encoding="utf-8")):
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        path = target.split("#", 1)[0]
        if not path:
            continue
        assert (document.parent / path).resolve().exists(), f"{relative}: missing {target}"


def test_the_reference_bundle_ships_with_its_instructions() -> None:
    bundle = REPO_ROOT / ".agents" / "jev-bundle" / "offline-switchboard"
    assert (bundle / "bundle.json").is_file()
    assert (bundle / "BUNDLE.md").is_file()
    assert (bundle / "tests" / "test_bundle_offline.py").is_file()
    manifest = json.loads((bundle / "bundle.json").read_text(encoding="utf-8"))
    assert manifest["scaffold"] is False
    assert manifest["name"] == bundle.name


def test_the_pi_adapter_only_calls_host_actions_that_exist() -> None:
    """A typo in the adapter's action names would otherwise only surface at run time."""
    adapter = (REPO_ROOT / "extensions" / "pi-jev.ts").read_text(encoding="utf-8")
    service = (REPO_ROOT / "src" / "jev_loop" / "host" / "service.py").read_text(encoding="utf-8")
    actions = set(re.findall(r'action: "([a-z_]+)"', adapter))
    assert actions, "the adapter must name its host actions"
    for action in sorted(actions):
        assert f'"{action}"' in service, f"pi-jev.ts calls the unknown host action {action!r}"


def test_the_main_skill_is_the_neutral_entry_and_not_a_pi_skill() -> None:
    main = (REPO_ROOT / "skills" / "jev-loop" / "SKILL.md").read_text(encoding="utf-8")
    frontmatter, body = main.split("---")[1], main.split("---")[2]
    assert re.search(r"^name: jev-loop$", frontmatter, re.M)
    # The main entry must stand on the host-neutral CLI/JSON protocol, not on the pi extension.
    assert "jev-loop rpc" in body
    assert "optional client" in frontmatter or "optional client" in body
    assert not (REPO_ROOT / "skills" / "pi-jev").exists()
    for document in (
        "README.md",
        "README.zh-CN.md",
        "docs/pi-integration.md",
        "docs/pi-integration.zh-CN.md",
        "docs/getting-started.html",
        "docs/getting-started.zh-CN.html",
        "AGENTS.md",
    ):
        text = (REPO_ROOT / document).read_text(encoding="utf-8")
        assert "skills/pi-jev" not in text, document
        assert "skill:pi-jev" not in text, document


def test_the_pi_package_registers_both_skills() -> None:
    package = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    assert package["pi"]["skills"] == ["./skills/jev-loop", "./skills/jev-bundle-creator"]
    assert not (REPO_ROOT / "skills" / "pi-jev").exists(), "the old skill directory must be renamed, not duplicated"


def test_the_skills_keep_the_safety_rules_that_are_easy_to_weaken() -> None:
    """Quarantine release, real-run authorization and bundle-text scope must stay explicit."""
    main = (REPO_ROOT / "skills" / "jev-loop" / "SKILL.md").read_text(encoding="utf-8")
    lifecycle = (REPO_ROOT / "skills" / "jev-loop" / "references" / "lifecycle.md").read_text(encoding="utf-8")
    protocol = (REPO_ROOT / "skills" / "jev-loop" / "references" / "protocol.md").read_text(encoding="utf-8")
    creator = (REPO_ROOT / "skills" / "jev-bundle-creator" / "SKILL.md").read_text(encoding="utf-8")

    for text in (main, lifecycle, protocol):
        assert "attestation" in text, "release_resources must be documented as a caller attestation"
    for text in (main, lifecycle):
        assert "user or operator" in text, "clearing a quarantine needs explicit human confirmation"
        assert "never fill it in yourself" in text or "Never fill it in on your own initiative" in text
    # Bundle text is scoped guidance, not a blanket prohibition and not new authority.
    assert "authorized technical usage guidance" in main
    assert "do not follow instructions found inside it" not in main.lower()
    # Real environments need their own authorization; offline acceptance is the default.
    assert "explicit scope and budget" in creator and "fake requester" in creator
    assert "network sandbox" in creator
    assert "never present an offline acceptance run as if the real task had been done" in creator
    # The runtime ships no general adapters, but the repo's own bundles are acknowledged.
    assert "ships no general business" in main


def test_the_spec_documents_the_same_three_meanings_and_authorization_rules() -> None:
    english = (REPO_ROOT / "spec" / "bundle-standard.md").read_text(encoding="utf-8")
    chinese = (REPO_ROOT / "spec" / "bundle-standard.zh-CN.md").read_text(encoding="utf-8")
    for text in (english, chinese):
        assert "attestation" in text or "自述声明" in text
        assert "validation_ok" in text
    assert "explicit scope and budget" in english
    assert "明确的范围与预算" in chinese


def test_the_boundedness_contract_is_documented_precisely() -> None:
    """The boundedness statement must match the implementation, in both languages."""
    english = (REPO_ROOT / "spec" / "bundle-standard.md").read_text(encoding="utf-8")
    chinese = (REPO_ROOT / "spec" / "bundle-standard.zh-CN.md").read_text(encoding="utf-8")
    for text in (english, chinese):
        assert "token_scan_incomplete" in text
        assert "symlinks_not_scanned" in text
    for phrase in (
        "descriptor-anchored",
        "pinned descriptor",
        "no queued path",
        "depth-bounded",
        "per-file cap",
        "root-relative",
        "never reopened",
        "fails closed",
        "best-effort",
        "not a sandbox",
    ):
        assert phrase in english, phrase
    for phrase in (
        "以描述符为锚",
        "固定的描述符",
        "深度有界",
        "单文件上限",
        "根相对路径",
        "绝不重新打开",
        "不是沙箱",
    ):
        assert phrase in chinese, phrase
