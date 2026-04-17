from __future__ import annotations

import re
from pathlib import Path


class SkillLoader:
    def __init__(self, skills_dir: Path):
        # 扫描技能目录，并缓存每个技能的元信息和正文内容。
        self.skills_dir = skills_dir
        self.skills: dict[str, dict[str, str]] = {}
        self._load_all()

    def _load_all(self) -> None:
        # 遍历 skills/*/SKILL.md，读取并解析所有技能文件。
        if not self.skills_dir.exists():
            return
        for skill_file in sorted(self.skills_dir.rglob("SKILL.md")):
            text = skill_file.read_text(encoding="utf-8")
            meta, body = self._parse_frontmatter(text)
            name = meta.get("name", skill_file.parent.name)
            self.skills[name] = {
                "body": body,
                "description": meta.get("description", "No description."),
                "path": str(skill_file),
                "tags": meta.get("tags", ""),
            }

    def _parse_frontmatter(self, text: str) -> tuple[dict[str, str], str]:
        # 解析 --- 包裹的简易 frontmatter；未命中时整段文本都视为正文。
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text.strip()

        meta: dict[str, str] = {}
        for line in match.group(1).splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
        return meta, match.group(2).strip()

    def get_descriptions(self) -> str:
        # 返回 system prompt 使用的轻量技能列表，只包含名称和简介。
        if not self.skills:
            return "(no skills available)"

        lines: list[str] = []
        for name, skill in self.skills.items():
            line = f"  - {name}: {skill['description']}"
            if skill["tags"]:
                line += f" [{skill['tags']}]"
            lines.append(line)
        return "\n".join(lines)

    def get_content(self, name: str) -> str:
        # 返回指定技能的完整正文，供模型通过 load_skill 按需注入上下文。
        skill = self.skills.get(name)
        if not skill:
            available = ", ".join(self.skills.keys()) or "(none)"
            return f"Error: Unknown skill '{name}'. Available: {available}"
        return f"<skill name=\"{name}\">\n{skill['body']}\n</skill>"