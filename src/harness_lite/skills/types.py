from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class Skill:
    """
    技能的数据载体，纯粹表示一段带有元数据的 SOP 上下文
    """
    name: str
    description: str
    content: str
    path: Optional[str] = None
