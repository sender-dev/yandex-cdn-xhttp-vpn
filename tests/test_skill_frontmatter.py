import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n", re.S)


def frontmatter_fields(path):
    text = path.read_text(encoding="utf-8")
    match = FRONTMATTER.match(text)
    assert match, f"{path.name} has no YAML frontmatter"
    fields = {}
    for line in match.group(1).split("\n"):
        if not line.strip():
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


class FrontmatterTests(unittest.TestCase):
    def test_skill_md_declares_required_fields(self):
        fields = frontmatter_fields(ROOT / "SKILL.md")
        self.assertEqual(fields["name"], "yandex-cdn-xhttp-vpn")
        self.assertTrue(fields["description"])
        self.assertTrue(fields["license"])

    def test_unquoted_values_have_no_colon_space(self):
        for path in sorted(ROOT.rglob("SKILL.md")):
            if ".git" in path.parts:
                continue
            for key, value in frontmatter_fields(path).items():
                quoted = len(value) > 1 and value[0] == value[-1] and value[0] in "\"'"
                if quoted:
                    continue
                self.assertNotIn(
                    ": ",
                    value,
                    f"{path.name}: unquoted '{key}' contains ': ', which strict YAML"
                    " parsers read as a nested mapping",
                )

    def test_frontmatter_parses_as_yaml(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("pyyaml is not installed")
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        match = FRONTMATTER.match(text)
        self.assertIsNotNone(match)
        parsed = yaml.safe_load(match.group(1))
        self.assertEqual(parsed["name"], "yandex-cdn-xhttp-vpn")


if __name__ == "__main__":
    unittest.main()
