"""基于 BeautifulSoup 的网页正文抓取工具"""

from typing import Dict, Any

from harness_lite.tools.base import BaseTool


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
