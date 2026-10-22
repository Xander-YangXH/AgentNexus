"""Skills（阶段 4.2，decisions/0021）：按需加载的“操作说明 + 工具许可”。

每个技能是一个 Markdown 文件：开头的 YAML 写名称、一句话描述、允许使用的工具，正文是完整的操作说明。
- 系统提示里只放技能列表（名称 + 描述）；模型调用 load_skill(name) 才拿到正文，并在本次回答里启用它列出的工具。
  这样说明不会一直占着上下文，工具列表也只在需要时变长（工具定义本身也算输入 token）。
- 技能列出的工具当前不可用（例如 MCP 网关没起来）时，这个技能不出现在列表里，免得模型加载了却用不了。
- 技能文件随代码发布（构建进镜像），不是用户上传的内容：正文会进系统级上下文，不能来自不可信的来源。
"""

import dataclasses
import re
from pathlib import Path

import yaml

LOAD_SKILL = "load_skill"
DEFAULT_DIR = Path(__file__).resolve().parents[3] / "skills"  # backend/skills
_NAME = re.compile(r"^[a-z][a-z0-9-]{0,40}$")


@dataclasses.dataclass(frozen=True)
class Skill:
    name: str
    description: str
    tools: tuple[str, ...]
    body: str


def parse_skill(text: str) -> Skill:
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not match:
        raise ValueError("skill file must start with a YAML header between --- lines")
    meta = yaml.safe_load(match.group(1)) or {}
    name, description, tools = meta.get("name"), meta.get("description"), meta.get("tools") or []
    if not isinstance(name, str) or not _NAME.match(name):
        raise ValueError(f"invalid skill name: {name!r}")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"skill {name} needs a description")
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        raise ValueError(f"skill {name}: tools must be a list of names")
    return Skill(name, description.strip(), tuple(tools), match.group(2).strip())


def load_skills(directory: Path = DEFAULT_DIR) -> dict[str, Skill]:
    skills = {}
    for path in sorted(directory.glob("*.md")):
        skill = parse_skill(path.read_text(encoding="utf-8"))
        if skill.name in skills:
            raise ValueError(f"duplicate skill name {skill.name} ({path.name})")
        skills[skill.name] = skill
    return skills


def load_skill_spec(names: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": LOAD_SKILL,
            "description": "加载一个技能：返回它的完整操作说明，并启用它需要的工具。",
            "parameters": {"type": "object", "properties": {"name": {"type": "string", "enum": names}},
                           "required": ["name"]},
        },
    }
