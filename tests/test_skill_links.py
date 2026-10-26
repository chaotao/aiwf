"""Guard the shipped skill docs: reference set, relative links and anchors.

The skill is the agent's only entry point; a broken link or a stale anchor is a
silent dead end for a weak model. These checks are cheap and run on every test.
"""
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills/aiwf"
REF_DIR = SKILL_DIR / "references"

EXPECTED_REFERENCES = {
    "advanced.md",   # 批量 / 队列 / 修订 / 集成
    "audit.md",      # 运行记录 + 业务进展视图
    "create.md",     # 创建 / 字段 / 产物 / 路由
    "examples.md",   # 范例目录（入口）
    "run.md",        # 执行循环 + 验收
    "templates.md",  # 模板库（入口）
}

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
HEADING_RE = re.compile(r"^(#{1,6}) (.+)$", re.MULTILINE)


def prose(path: Path) -> str:
    """File text with fenced code blocks removed (samples are not real links)."""
    kept, fence = [], False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if not fence:
            kept.append(line)
    return "\n".join(kept)


def slug(text: str) -> str:
    """GitHub-flavoured heading anchor, ASCII punctuation stripped."""
    text = text.replace("`", "").strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.strip().replace(" ", "-")


def anchors(path: Path) -> set:
    return {slug(m.group(2)) for m in HEADING_RE.finditer(prose(path))}


def markdown_files():
    return sorted(SKILL_DIR.rglob("*.md"))


class SkillLinksTest(unittest.TestCase):
    def test_reference_set_is_the_merged_six(self):
        actual = {p.name for p in REF_DIR.glob("*.md")}
        self.assertEqual(EXPECTED_REFERENCES, actual)

    def test_relative_links_and_anchors_resolve(self):
        problems = []
        for doc in markdown_files():
            for target in LINK_RE.findall(prose(doc)):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                path_part, _, anchor = target.partition("#")
                dest = doc if not path_part else (doc.parent / path_part).resolve()
                rel = "%s -> %s" % (doc.relative_to(ROOT), target)
                if not dest.exists():
                    problems.append("missing file: " + rel)
                    continue
                if anchor and dest.suffix == ".md":
                    if anchor not in anchors(dest):
                        problems.append("missing anchor: " + rel)
        self.assertEqual([], problems)

    def test_every_reference_is_reachable_from_skill_md(self):
        linked = set()
        for target in LINK_RE.findall(prose(SKILL_DIR / "SKILL.md")):
            path_part = target.partition("#")[0]
            if path_part.startswith("references/"):
                linked.add(path_part.split("/", 1)[1])
        self.assertEqual(EXPECTED_REFERENCES, linked)


if __name__ == "__main__":
    unittest.main()
