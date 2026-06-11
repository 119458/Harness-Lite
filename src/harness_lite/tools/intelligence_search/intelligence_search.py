"""基于 Tavily API 的网络搜索工具"""

import os
import json
import urllib.request
import urllib.error
from typing import Dict, Any

from harness_lite.tools.base import BaseTool


class IntelligenceSearchTool(BaseTool):
    """
    基于大模型优化的搜索引擎工具
    """
    @property
    def name(self) -> str:
        return "intelligence_search"
    @property
    def description(self) -> str:
        return "使用搜索引擎查找互联网上的实时信息。当本地知识库或记忆中缺乏最新数据、事实或报错解决方案时调用。返回高度浓缩的摘要信息。"

    def __init__(self):
        super().__init__()
        self.api_key = os.environ.get("TAVILY_API_KEY")

    def get_schema(self) -> Dict[str, Any]:
        schema = super().get_schema()
        schema["function"]["parameters"]["properties"] = {
            "query": {
                "type": "string",
                "description": "要搜索的查询词。请尽量使用精确的自然语言或关键字组合。"
            }
        }
        schema["function"]["parameters"]["required"] = ["query"]
        return schema

    def execute(self, query: str) -> str:
        if not self.api_key:
            return "Error: 系统未配置 TAVILY_API_KEY 环境变量，无法执行互联网搜索。请提醒用户在 .env 中进行配置。"

        try:
            url = "https://api.tavily.com/search"
            payload = {
                "api_key": self.api_key,
                "query": query,
                "search_depth": "basic",
                "include_answer": True,
                "max_results": 5
            }

            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

            with urllib.request.urlopen(req, timeout=10) as response:
                result_data = json.loads(response.read().decode("utf-8"))

            output = []
            if result_data.get("answers"):
                output.append(f"【AI 总结结论】:\n{result_data['answer']}\n")
            output.append("【核心来源参考】:")
            for idx, res in enumerate(result_data.get("results", [])):
                output.append(
                    f"{idx + 1}. [Title]: {res.get('title')}\n   [URL]: {res.get('url')}\n   [Content]: {res.get('content')}\n")

            return "\n".join(output) if output else "未找到与该查询相关的有效信息。"

        except urllib.error.URLError as e:
            return f"Network Error: 无法连接到搜索服务 ({str(e)})。"
        except Exception as e:
            return f"Search Error: 执行检索时发生异常 ({str(e)})。"
