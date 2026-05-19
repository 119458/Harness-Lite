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


class WebScraperTool(BaseTool):
    """
    网页正文提取工具，用于深入阅读单个网页
    """
    @property
    def name(self) -> str:
        return "web_scraper"
    @property
    def description(self) -> str:
        return "抓取指定 URL 的网页内容，并自动剔除广告、导航栏和脚本，提取纯净的 Markdown/纯文本正文。通常在搜索工具返回了某个高价值 URL 时配合使用。"

    def get_schema(self) -> Dict[str, Any]:
        schema = super().get_schema()
        schema["function"]["parameters"]["properties"] = {
            "url": {
                "type": "string",
                "description": "需要抓取正文的网页完整 URL (必须以 http:// 或 https:// 开头)"
            }
        }
        schema["function"]["parameters"]["required"] = ["url"]
        return schema

    def execute(self, url: str) -> str:
        if not url.startswith(("http://", "https://")):
            return "Error: 无效的 URL 格式。必须以 http:// 或 https:// 开头。"

        try:
            import requests
            from bs4 import BeautifulSoup
        except ImportError:
            return "Error: 缺少解析依赖。请告诉用户在终端执行 `pip install requests beautifulsoup4` 来启用网页抓取能力。"

        try:
            # 伪装请求头，防止被常见防火墙拦截
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()

            # 使用 bs4 解析 DOM
            soup = BeautifulSoup(response.text, 'html.parser')

            # 移除所有脚本、样式和隐藏元素
            for element in soup(["script", "style", "noscript", "header", "footer", "nav"]):
                element.decompose()

            # 提取文本并清理多余的空行
            text = soup.get_text(separator='\n')
            lines = (line.strip() for line in text.splitlines())
            chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
            clean_text = '\n'.join(chunk for chunk in chunks if chunk)

            # 截断超长网页，防止撑爆上下文 (保留前 4000 个字符)
            if len(clean_text) > 4000:
                clean_text = clean_text[:4000] + "\n\n...[网页内容过长，已被截断以保护上下文]..."

            return f"--- URL: {url} ---\n{clean_text}"

        except requests.Timeout:
            return f"Error: 请求网页 {url} 超时 (10秒)。可能目标网站响应过慢或禁止抓取。"
        except requests.RequestException as e:
            return f"Error: 请求网页失败 ({str(e)})。"
        except Exception as e:
            return f"System Error: 网页解析异常 ({str(e)})。"