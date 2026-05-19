import os
import yaml
from pathlib import Path
from typing import List, Tuple
from harness_lite.skills.types import Skill


def _parse_skill_markdown(content: str, default_name: str) -> Tuple[str, str]:
    """
    解析 SKILL.md，提取 name 和 description
    优先尝试读取 YAML 前置元数据，失败则降级使用 Markdown 正文提取
    """
    name = default_name
    description = ""
    lines = content.splitlines()

    # 1. 尝试解析 YAML Frontmatter (--- ... ---)
    if content.startswith("---\n"):
        end_index = content.find("\n---\n", 4)
        if end_index != -1:
            try:
                metadata = yaml.safe_load(content[4:end_index])
                if isinstance(metadata, dict):
                    name = metadata.get("name", name).strip()
                    description = metadata.get("description", description).strip()
            except yaml.YAMLError:
                pass  # 解析失败则静默降级

    # 2. 降级方案：从 Markdown 标题和首段提取
    if not description:
        for line in lines:
            stripped = line.strip()
            # 提取第一个一级标题作为 name
            if stripped.startswith("# "):
                if name == default_name:
                    name = stripped[2:].strip()
                continue
            # 提取第一段非空、非元数据的正文作为 description
            if stripped and not stripped.startswith("---") and not stripped.startswith("#"):
                description = stripped[:200]  # 截取前 200 个字符
                break

    if not description:
        description = f"Skill context for {name}"

    return name, description

def load_skills_from_directory(skills_dir: str) -> List[Skill]:
    """
    遍历指定目录，扫描所有包含 SKILL.md 的子文件夹并将其转化为 Skill 对象
    """

    loaded_skills = []
    base_path = Path(skills_dir)
    if not base_path.exists() or not base_path.is_dir():
        return loaded_skills

    for entry in base_path.iterdir():
        if entry.is_dir():
            skill_file = entry / "SKILL.md"
            if skill_file.exists():
                content = skill_file.read_text(encoding="utf-8")
                default_name = entry.name

                name, description = _parse_skill_markdown(content, default_name)

                skill = Skill(
                    name=name,
                    description=description,
                    content=content,
                    path=str(skill_file)
                )
                loaded_skills.append(skill)

    return loaded_skills