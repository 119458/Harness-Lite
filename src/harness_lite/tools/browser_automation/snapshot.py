"""浏览器 DOM 快照生成

策略：注入一段 JavaScript 遍历可见 DOM，挑出"可交互元素"和"语义内容元素"，
为可交互元素分配 ref 编号并暂存到 window.__harnessRefMap，
方便后续 click/fill 通过 ref 精确定位，规避脆弱的 CSS 选择器。

返回结构：
{
  "tree": <嵌套字典或字符串>,
  "ref_count": <int>
}
"""

# 可交互的原生标签
INTERACTIVE_TAGS = ["a", "button", "input", "textarea", "select", "option",
                    "label", "details", "summary"]

# 语义内容标签（无交互但有结构信息）
SEMANTIC_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6",
                 "p", "li", "td", "th", "caption", "figcaption",
                 "blockquote", "pre", "code", "nav", "main", "article",
                 "section", "header", "footer", "form", "table",
                 "img", "video", "audio"]

KEEP_TAGS = list(set(INTERACTIVE_TAGS) | set(SEMANTIC_TAGS))

# 注入到页面里的快照采集脚本
DOM_SNAPSHOT_SCRIPT = """
() => {
    const KEEP = new Set(%(keep)s);
    const INTERACTIVE = new Set(%(interactive)s);
    const SKIP = new Set(["script", "style", "noscript", "svg", "path", "meta", "link", "br", "hr"]);
    const ROLE_INTERACTIVE = new Set([
        "button", "link", "tab", "menuitem", "option", "switch",
        "checkbox", "radio", "combobox", "searchbox", "slider",
        "spinbutton", "textbox", "treeitem"
    ]);

    let refSeq = 0;
    const refTable = {};

    function isVisible(node) {
        if (!(node instanceof HTMLElement)) return true;
        const style = window.getComputedStyle(node);
        if (style.display === "none" || style.visibility === "hidden") return false;
        if (parseFloat(style.opacity) === 0) return false;
        return true;
    }

    function hasInteractiveSignal(el) {
        const role = el.getAttribute("role");
        if (role && ROLE_INTERACTIVE.has(role)) return true;
        if (el.hasAttribute("onclick") || el.hasAttribute("tabindex")) return true;
        if (el.getAttribute("contenteditable") === "true") return true;
        return false;
    }

    function plainText(el) {
        let buf = "";
        for (const child of el.childNodes) {
            if (child.nodeType === Node.TEXT_NODE) buf += child.textContent;
        }
        return buf.trim();
    }

    function walk(node) {
        if (node.nodeType === Node.TEXT_NODE) {
            const t = node.textContent.trim();
            return t ? t : null;
        }
        if (node.nodeType !== Node.ELEMENT_NODE) return null;

        const tag = node.tagName.toLowerCase();
        if (SKIP.has(tag)) return null;
        if (!isVisible(node)) return null;

        const childResults = [];
        for (const child of node.childNodes) {
            const r = walk(child);
            if (r !== null) childResults.push(r);
        }

        const isInteractive = INTERACTIVE.has(tag) || hasInteractiveSignal(node);
        const keep = KEEP.has(tag) || isInteractive;

        if (!keep) {
            if (childResults.length === 0) return null;
            if (childResults.length === 1) return childResults[0];
            return childResults;
        }

        const summary = { tag: tag };
        if (isInteractive) {
            refSeq += 1;
            summary.ref = refSeq;
            refTable[refSeq] = node;
        }

        // 抽取常用属性
        if (tag === "a" && node.href) summary.href = node.getAttribute("href");
        if (tag === "img") {
            summary.alt = node.alt || "";
            summary.src = node.getAttribute("src") || "";
        }
        if (tag === "input" || tag === "textarea" || tag === "select") {
            summary.type = node.type || "text";
            if (node.name) summary.name = node.name;
            if (node.placeholder) summary.placeholder = node.placeholder;
            if (node.value) summary.value = node.value;
            if (node.disabled) summary.disabled = true;
        }

        const aria = node.getAttribute("aria-label");
        if (aria) summary.aria = aria;

        const directText = plainText(node);
        if (directText) summary.text = directText;

        if (childResults.length > 0) summary.children = childResults;

        return summary;
    }

    const tree = walk(document.body);
    window.__harnessRefMap = refTable;
    return { tree: tree, ref_count: refSeq };
}
"""

# 通过 json.dumps 安全注入标签列表，避免 % 格式化对脚本内容产生意外解析
import json as _json

DOM_SNAPSHOT_SCRIPT = DOM_SNAPSHOT_SCRIPT.replace(
    "%(keep)s", _json.dumps(list(KEEP_TAGS))
).replace(
    "%(interactive)s", _json.dumps(list(INTERACTIVE_TAGS))
)


def flatten_snapshot(node, indent: int = 0) -> list:
    """把树状快照展平为 LLM 友好的文本行列表

    每个可交互元素以 [ref:N] 开头，便于后续工具调用精确定位。
    """
    if node is None:
        return []
    if isinstance(node, str):
        return [(" " * indent) + node]
    if isinstance(node, list):
        out = []
        for child in node:
            out.extend(flatten_snapshot(child, indent))
        return out
    if not isinstance(node, dict):
        return []

    tag = node.get("tag", "?")
    ref = node.get("ref")
    prefix = f"[ref:{ref}] <{tag}>" if ref else f"<{tag}>"

    attrs = []
    for key in ("type", "name", "href", "alt", "aria", "placeholder", "value"):
        val = node.get(key)
        if val:
            display = str(val)
            if len(display) > 80:
                display = display[:77] + "..."
            attrs.append(f'{key}="{display}"')
    if node.get("disabled"):
        attrs.append("disabled")

    header = (" " * indent) + prefix
    if attrs:
        header += " " + " ".join(attrs)

    text = node.get("text")
    if text:
        if len(text) > 120:
            text = text[:117] + "..."
        header += f" :: {text}"

    lines = [header]
    for child in node.get("children", []):
        lines.extend(flatten_snapshot(child, indent + 2))
    return lines
