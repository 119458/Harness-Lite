"""长期记忆系统 v2 单元测试。

覆盖：
- 索引按 type 分组 + STALE 仅作用于 project
- save_memory 强制 Why/How for feedback/project + 幂等更新
- 后台抽取 trigger_extraction 的 daemon 线程行为
- 召回：build_recall_payload 仅装指南+索引 + async_filter_recommendations 包装
"""
import asyncio
import json
import shutil
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest


# ============================================================
# Fake OpenAI client（用于 mock 小模型调用）
# ============================================================

class _FakeMessage:
    def __init__(self, content: str):
        self.content = content


class _FakeChoice:
    def __init__(self, content: str):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str):
        self.choices = [_FakeChoice(content)]


def _make_fake_openai(content_payload: str):
    """工厂：返回一个 fake OpenAI 类，其 chat.completions.create 总是返回固定 content。"""
    class FakeOpenAI:
        def __init__(self, *args, **kwargs):
            self.chat = _Chat()

        def close(self):
            pass

    class _Chat:
        def __init__(self):
            self.completions = _Completions()

    class _Completions:
        def create(self, *args, **kwargs):
            return _FakeResponse(content_payload)

    return FakeOpenAI


# ============================================================
# 公共 fixture
# ============================================================

@pytest.fixture
def ltm():
    tmpdir = tempfile.mkdtemp()
    from harness_lite.memory.long_term import LongTermMemoryManager
    manager = LongTermMemoryManager(base_dir=tmpdir)
    yield manager
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def configured_small_model(monkeypatch):
    """让 small_config 看起来已配置且与 main 不同（不触发降级）。"""
    from harness_lite.memory import long_term as ltm_module
    small = {"api_key": "small-k", "base_url": "small-url", "model_name": "small-m"}
    main = {"api_key": "main-k", "base_url": "main-url", "model_name": "main-m"}
    monkeypatch.setattr(ltm_module, "get_small_config", lambda: small)
    monkeypatch.setattr(ltm_module, "get_main_config", lambda: main)
    return small


# ============================================================
# 基础行为（保留 v1 测试）
# ============================================================

class TestBasic:
    def test_ensure_dir_creates_memory_md(self, ltm):
        assert (ltm.base_dir / "MEMORY.md").exists()
        assert ltm.get_entrypoint_text() == ""
        payload = ltm.build_recall_payload("q", "sid")
        assert payload.count("## 记忆索引") == 1
        assert "（当前未沉淀任何记忆。）" in payload

    def test_scan_empty_returns_empty(self, ltm):
        assert ltm.scan_memory_files() == []

    def test_scan_skips_memory_md(self, ltm):
        (ltm.base_dir / "user-test.md").write_text(
            "---\nname: user-test\ndescription: test user\ntype: user\n---\n\ntest\n",
            encoding="utf-8",
        )
        headers = ltm.scan_memory_files()
        assert len(headers) == 1
        assert headers[0].filename == "user-test.md"
        assert headers[0].type == "user"

    def test_corrupt_frontmatter_graceful(self, ltm):
        (ltm.base_dir / "bad.md").write_text(
            "---\n: broken yaml :\n---\n", encoding="utf-8",
        )
        headers = ltm.scan_memory_files()
        assert headers == []

    def test_complete_frontmatter_enters_manifest_unchanged(self, ltm):
        (ltm.base_dir / "complete.md").write_text(
            "---\n"
            "name: complete-name\n"
            "description: complete desc\n"
            "type: user\n"
            "updated_at: '2026-01-02'\n"
            "---\n\n"
            "完整正文不应进入 manifest\n",
            encoding="utf-8",
        )
        headers = ltm.scan_memory_files()
        assert len(headers) == 1
        header = headers[0]
        assert header.filename == "complete.md"
        assert header.type == "user"
        assert header.name == "complete-name"
        assert header.description == "complete desc"
        assert header.updated_at == "2026-01-02"
        assert ltm.format_manifest(headers) == "- [user] complete.md — complete desc"

    def test_missing_name_falls_back_to_filename_stem(self, ltm):
        (ltm.base_dir / "stem-name.md").write_text(
            "---\ndescription: desc\ntype: reference\n---\n\n正文\n",
            encoding="utf-8",
        )
        headers = ltm.scan_memory_files()
        assert len(headers) == 1
        assert headers[0].name == "stem-name"

    def test_missing_description_falls_back_to_body_excerpt(self, ltm):
        (ltm.base_dir / "body-desc.md").write_text(
            "---\nname: body-desc\ntype: user\n---\n\n"
            "\n# 用户是数据科学家，关注可观测性与日志。\n"
            "后续完整正文不应进入 manifest。\n",
            encoding="utf-8",
        )
        headers = ltm.scan_memory_files()
        assert len(headers) == 1
        assert headers[0].description == "用户是数据科学家，关注可观测性与日志。"

    def test_missing_or_invalid_type_excluded_from_scan_and_manifest(self, ltm):
        (ltm.base_dir / "missing-type.md").write_text(
            "---\nname: missing\ndescription: desc\n---\n\n正文\n",
            encoding="utf-8",
        )
        (ltm.base_dir / "invalid-type.md").write_text(
            "---\nname: invalid\ndescription: desc\ntype: other\n---\n\n正文\n",
            encoding="utf-8",
        )
        (ltm.base_dir / "ok.md").write_text(
            "---\nname: ok\ndescription: ok desc\ntype: user\n---\n\n正文\n",
            encoding="utf-8",
        )
        headers = ltm.scan_memory_files()
        assert [h.filename for h in headers] == ["ok.md"]
        manifest = ltm.format_manifest(headers)
        assert "missing-type.md" not in manifest
        assert "invalid-type.md" not in manifest
        assert "ok.md" in manifest

    def test_list_memory_files(self, ltm):
        (ltm.base_dir / "a.md").write_text(
            "---\nname: a\ndescription: a\ntype: reference\n---\n", encoding="utf-8",
        )
        files = ltm.list_memory_files()
        assert "a.md" in files

    def test_get_memory_content_rejects_parent_traversal(self, ltm):
        outside = ltm.base_dir.parent / f"{ltm.base_dir.name}-outside.md"
        outside.write_text("outside secret", encoding="utf-8")
        try:
            assert ltm.get_memory_content(f"../{outside.name}") == ""
        finally:
            outside.unlink(missing_ok=True)

    def test_get_memory_content_rejects_absolute_outside_path(self, ltm, tmp_path):
        outside = tmp_path / "outside.md"
        outside.write_text("outside secret", encoding="utf-8")
        assert ltm.get_memory_content(str(outside)) == ""

    def test_get_memory_content_allows_base_dir_file(self, ltm):
        inside = ltm.base_dir / "inside.md"
        inside.write_text(
            "---\nname: inside\ndescription: inside\ntype: user\n---\n\ninside body\n",
            encoding="utf-8",
        )
        assert "inside body" in ltm.get_memory_content("inside.md")

    def test_scan_skips_symlink_files(self, ltm, tmp_path):
        outside = tmp_path / "outside.md"
        outside.write_text(
            "---\nname: outside\ndescription: leaked desc\ntype: user\n---\n\n外部正文\n",
            encoding="utf-8",
        )
        link = ltm.base_dir / "linked.md"
        link.symlink_to(outside)

        headers = ltm.scan_memory_files()
        assert headers == []
        assert "leaked desc" not in ltm.get_entrypoint_text()
        assert "leaked desc" not in ltm.format_manifest(headers)

    def test_scan_memory_files_limits_to_max_files(self, ltm):
        for i in range(205):
            (ltm.base_dir / f"m-{i:03d}.md").write_text(
                f"---\nname: m-{i}\ndescription: desc {i}\ntype: user\n---\n\nbody\n",
                encoding="utf-8",
            )
        assert len(ltm.scan_memory_files()) == 200


# ============================================================
# save_memory 写入流程
# ============================================================

class TestSaveMemory:
    def test_save_user_memory_creates_file_and_index(self, ltm):
        path = ltm.save_memory("user", "u-test", "用户测试", "用户是 QA 工程师")
        assert path is not None
        assert path.exists()
        text = path.read_text(encoding="utf-8")
        assert "type: user" in text
        assert "created_at:" in text
        index = (ltm.base_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert "u-test.md" in index
        assert "[user]" in index
        assert "用户画像" not in index
        assert "长期记忆索引" not in index

    def test_save_feedback_requires_why_and_how(self, ltm):
        # 缺 Why
        p = ltm.save_memory("feedback", "fb-1", "禁用 mock", "禁止用 mock。\n**How to apply:** 测试时")
        assert p is None
        # 缺 How
        p = ltm.save_memory("feedback", "fb-1", "禁用 mock", "**Why:** 上季度踩过坑")
        assert p is None
        # 完整
        p = ltm.save_memory(
            "feedback", "fb-1", "禁用 mock",
            "禁止 mock。\n\n**Why:** 上季度踩过坑\n**How to apply:** 测试时",
        )
        assert p is not None

    def test_save_project_requires_why_and_how(self, ltm):
        p = ltm.save_memory("project", "p-1", "重构", "进行中")
        assert p is None
        p = ltm.save_memory(
            "project", "p-1", "重构",
            "长期记忆 v2 重构进行中。\n\n**Why:** 一期暴露问题\n**How to apply:** 评审时参考",
        )
        assert p is not None

    def test_save_user_no_why_required(self, ltm):
        p = ltm.save_memory("user", "u-2", "数据科学家", "纯文本，无 Why/How")
        assert p is not None

    def test_save_reference_no_why_required(self, ltm):
        p = ltm.save_memory("reference", "r-1", "Linear 链接", "https://linear.app/...")
        assert p is not None

    def test_save_unknown_type_rejected(self, ltm):
        p = ltm.save_memory("unknown", "x", "x", "x")
        assert p is None

    def test_save_idempotent_update_preserves_created_at(self, ltm):
        p1 = ltm.save_memory("user", "u-idem", "v1", "正文 v1")
        assert p1 is not None
        first_text = p1.read_text(encoding="utf-8")
        # 强制 created_at 设回老日期（兼容 yaml 单引号包裹的字符串日期）
        import re
        modified = re.sub(
            r"created_at:\s*'?\d{4}-\d{2}-\d{2}'?",
            "created_at: '2025-01-01'",
            first_text,
            count=1,
        )
        assert modified != first_text, "regex 未匹配，请检查 frontmatter 格式"
        p1.write_text(modified, encoding="utf-8")

        # 第二次写：应保留 created_at=2025-01-01，updated_at=今天
        p2 = ltm.save_memory("user", "u-idem", "v2", "正文 v2")
        assert p2 == p1
        new_text = p2.read_text(encoding="utf-8")
        assert "2025-01-01" in new_text  # created_at 保留
        today = datetime.now().strftime("%Y-%m-%d")
        assert f"updated_at: '{today}'" in new_text or f"updated_at: {today}" in new_text
        assert "正文 v2" in new_text
        assert "正文 v1" not in new_text

        # 索引中只有一条
        index = (ltm.base_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert index.count("u-idem.md") == 1

    def test_save_empty_sanitized_name_uses_unique_memory_prefix(self, ltm):
        first = ltm._sanitize_name("!!!")
        second = ltm._sanitize_name("   ")
        assert first.startswith("memory-")
        assert second.startswith("memory-")
        assert first != "memory"
        assert second != "memory"
        assert first != second


# ============================================================
# 索引渲染：按 type 分组 + STALE 仅 project
# ============================================================

class TestIndexRendering:
    def test_index_is_flat_lines_without_headings(self, ltm):
        ltm.save_memory("user", "u-x", "用户", "正文")
        ltm.save_memory(
            "feedback", "fb-x", "偏好",
            "**Why:** r\n**How to apply:** w",
        )
        ltm.save_memory(
            "project", "p-x", "项目",
            "**Why:** r\n**How to apply:** w",
        )
        ltm.save_memory("reference", "r-x", "外部", "链接")
        text = ltm.get_entrypoint_text()
        lines = [line for line in text.splitlines() if line]
        assert all(line.startswith("- [") for line in lines)
        assert "# 长期记忆索引" not in text
        assert "## 用户画像" not in text
        assert "## 偏好反馈" not in text
        assert "## 项目动态" not in text
        assert "## 外部指针" not in text
        assert "- [user] u-x.md — 用户" in text
        assert "- [feedback] fb-x.md — 偏好" in text
        assert "- [project] p-x.md — 项目" in text
        assert "- [reference] r-x.md — 外部" in text

    def test_memory_md_is_empty_when_no_memories(self, ltm):
        assert (ltm.base_dir / "MEMORY.md").read_text(encoding="utf-8") == ""
        assert ltm.get_entrypoint_text() == ""

    def test_build_recall_payload_has_single_system_index_heading(self, ltm):
        ltm.save_memory("user", "u-rec", "rec desc", "正文")
        payload = ltm.build_recall_payload("q", "sid")
        assert payload.count("## 记忆索引") == 1
        assert "# 长期记忆索引" not in payload
        assert "- [user] u-rec.md — rec desc" in payload

    def test_project_line_marks_date_and_stale(self, ltm):
        ltm.save_memory(
            "project", "p-y", "项目",
            "**Why:** r\n**How to apply:** w",
        )
        text = ltm.get_entrypoint_text()
        assert "p-y.md" in text
        assert "项目动态" not in text
        assert "易过期" not in text

    def test_entrypoint_text_truncates_long_index_by_lines(self, monkeypatch, ltm):
        from harness_lite.memory import long_term as ltm_module
        monkeypatch.setattr(ltm_module, "MAX_ENTRYPOINT_LINES", 6)
        for i in range(5):
            ltm.save_memory("user", f"u-line-{i}", f"desc {i}", "正文")
        text = ltm.get_entrypoint_text()
        assert "索引过长，已按行数截断" in text

    def test_stale_only_for_project(self, ltm):
        # 写一条 user + 一条 project，然后手动改 frontmatter 把日期改到 5 天前
        ltm.save_memory("user", "u-old", "老用户记忆", "正文")
        ltm.save_memory(
            "project", "p-old", "老项目",
            "**Why:** r\n**How to apply:** w",
        )
        old = (datetime.now().date() - timedelta(days=5)).strftime("%Y-%m-%d")
        for name in ("u-old.md", "p-old.md"):
            fpath = ltm.base_dir / name
            text = fpath.read_text(encoding="utf-8")
            import re
            text = re.sub(r"created_at: \S+", f"created_at: {old}", text)
            text = re.sub(r"updated_at: \S+", f"updated_at: {old}", text)
            fpath.write_text(text, encoding="utf-8")

        rendered = ltm.get_entrypoint_text()
        # 只有 project 行有 STALE 标记
        lines = rendered.splitlines()
        user_line = next((l for l in lines if "u-old.md" in l), "")
        project_line = next((l for l in lines if "p-old.md" in l), "")
        assert "[STALE]" not in user_line
        assert "[STALE]" in project_line
        # 也只有 project 行带日期
        assert old not in user_line
        assert old in project_line


# ============================================================
# 召回：按需筛选 + 已读过滤 + 严苛宁缺勿滥
# ============================================================

class TestRecall:
    def test_find_relevant_no_config_returns_empty(self, monkeypatch, ltm):
        from harness_lite.memory import long_term as ltm_module
        monkeypatch.setattr(ltm_module, "get_small_config", lambda: {})
        ltm.save_memory("user", "u-z", "测试", "正文")
        assert ltm.find_relevant("test", "sid") == []

    def test_find_relevant_excludes_session_read(self, monkeypatch, configured_small_model, ltm):
        ltm.save_memory("user", "foo", "foo desc", "正文")
        ltm.save_memory("user", "bar", "bar desc", "正文")
        # 让 mock 召回返回 foo + bar
        import openai
        FakeOpenAI = _make_fake_openai(json.dumps({"selected": ["foo.md", "bar.md"]}))
        monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)

        ltm.mark_read("foo.md", "sid")
        result = ltm.find_relevant("test", "sid")
        names = [h.filename for h in result]
        assert "foo.md" not in names
        assert "bar.md" in names

    def test_find_relevant_returns_at_most_top5(self, monkeypatch, configured_small_model, ltm):
        # 写 8 条
        for i in range(8):
            ltm.save_memory("user", f"u-{i}", f"desc {i}", "正文")
        selected = [f"u-{i}.md" for i in range(8)]
        import openai
        FakeOpenAI = _make_fake_openai(json.dumps({"selected": selected}))
        monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)

        result = ltm.find_relevant("query", "sid")
        assert len(result) <= 5

    def test_find_relevant_empty_when_uncertain(self, monkeypatch, configured_small_model, ltm):
        ltm.save_memory("user", "u-uncertain", "desc", "正文")
        import openai
        FakeOpenAI = _make_fake_openai(json.dumps({"selected": []}))
        monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)

        result = ltm.find_relevant("query", "sid")
        assert result == []

    def test_build_recall_payload_returns_guide_and_index_only(self, ltm):
        """v2 改造后 payload 只包含指南 + 记忆索引，不再注入推荐清单。"""
        ltm.save_memory("user", "u-rec", "rec desc", "正文")
        payload = ltm.build_recall_payload("第一次查询", "sid")
        assert "长期记忆系统" in payload
        assert "记忆索引" in payload
        # 关键断言：不再注入「本轮可能相关」推荐段（标题是 `## 本轮可能相关`）
        assert "## 本轮可能相关" not in payload
        # 也不应再出现「本轮无明显相关记忆」占位文案
        assert "本轮无明显相关记忆" not in payload
        # 索引正常渲染
        assert "u-rec.md" in payload

    def test_build_recall_payload_accepts_legacy_signature(self, ltm):
        """旧调用点 (task, session_id, recent_tools=[...]) 仍能 work。"""
        payload = ltm.build_recall_payload(
            "q", session_id="sid", recent_tools=["read_file"],
        )
        assert "记忆索引" in payload

    def test_async_filter_recommendations_delegates_to_find_relevant(
        self, monkeypatch, configured_small_model, ltm,
    ):
        ltm.save_memory("user", "u-async", "async desc", "正文")
        import openai
        FakeOpenAI = _make_fake_openai(json.dumps({"selected": ["u-async.md"]}))
        monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)

        result = asyncio.run(
            ltm.async_filter_recommendations(
                query="anything", session_id="sid", recent_tools=["read_file"],
            )
        )
        names = [h.filename for h in result]
        assert "u-async.md" in names

    def test_async_filter_recommendations_swallows_errors(self, monkeypatch, ltm):
        """find_relevant 抛错时 async 包装必须返回 []，不向上传播。"""
        def boom(*a, **kw):
            raise RuntimeError("boom")
        monkeypatch.setattr(ltm, "find_relevant", boom)

        result = asyncio.run(
            ltm.async_filter_recommendations(query="q", session_id="sid")
        )
        assert result == []

    def test_build_recommendation_section_empty_returns_empty_string(self, ltm):
        """无候选时返回空串，调用方据此决定是否 append。"""
        assert ltm.build_recommendation_section([]) == ""

    def test_build_recommendation_section_injects_full_memory_content(self, ltm):
        path = ltm.save_memory("user", "user-x", "desc x", "正文机密内容")
        assert path is not None
        headers = ltm.scan_memory_files()
        text = ltm.build_recommendation_section(headers)
        assert "## 本轮可能相关的长期记忆" in text
        assert "### [user] user-x.md (user-x)" in text
        assert "- 描述：desc x" in text
        assert "```markdown" in text
        assert "type: user" in text
        assert "正文机密内容" in text
        assert "read_file" not in text

    def test_format_manifest_never_includes_full_body(self, ltm):
        (ltm.base_dir / "manifest-safe.md").write_text(
            "---\nname: manifest-safe\ndescription: short desc\ntype: user\n---\n\n"
            "FULL BODY SECRET SHOULD NOT APPEAR\n",
            encoding="utf-8",
        )
        headers = ltm.scan_memory_files()
        manifest = ltm.format_manifest(headers)
        recommendation = ltm.build_recommendation_section(headers)
        assert "FULL BODY SECRET" not in manifest
        assert "FULL BODY SECRET" in recommendation

    def test_recommendation_truncates_single_memory_body(self, monkeypatch, ltm):
        from harness_lite.memory import long_term as ltm_module
        monkeypatch.setattr(ltm_module, "RECALL_MAX_CHARS_PER_MEMORY", 20)
        path = ltm.save_memory("user", "long-one", "long desc", "A" * 80)
        assert path is not None

        text = ltm.build_recommendation_section(ltm.scan_memory_files())

        assert "内容过长，已省略" in text
        assert "个字符" in text
        assert "A" * 80 not in text

    def test_recommendation_omits_remaining_when_total_budget_exhausted(self, monkeypatch, ltm):
        from harness_lite.memory import long_term as ltm_module
        monkeypatch.setattr(ltm_module, "RECALL_MAX_TOTAL_CHARS", 260)
        ltm.save_memory("user", "first", "first desc", "A" * 300)
        ltm.save_memory("user", "second", "second desc", "B" * 300)
        headers = sorted(ltm.scan_memory_files(), key=lambda h: h.filename)

        text = ltm.build_recommendation_section(headers)

        assert "召回总预算已耗尽" in text
        assert "已省略" in text


# ============================================================
# 已读 set
# ============================================================

class TestReadSet:
    def test_mark_and_get(self, ltm):
        ltm.mark_read("a.md", "sid1")
        ltm.mark_read("b.md", "sid1")
        ltm.mark_read("c.md", "sid2")
        assert ltm.get_read_set("sid1") == {"a.md", "b.md"}
        assert ltm.get_read_set("sid2") == {"c.md"}

    def test_clear_read_set(self, ltm):
        ltm.mark_read("a.md", "sid1")
        ltm.clear_read_set("sid1")
        assert ltm.get_read_set("sid1") == set()

    def test_reset_all_session_state(self, ltm):
        ltm.mark_read("a.md", "sid1")
        ltm.mark_read("b.md", "sid2")
        ltm.reset_all_session_state()
        assert ltm.get_read_set("sid1") == set()
        assert ltm.get_read_set("sid2") == set()


# ============================================================
# 后台抽取
# ============================================================

class TestExtraction:
    def test_trigger_extraction_below_threshold_does_nothing(self, ltm):
        """单条 user message 不应触发抽取。"""
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]
        ltm.trigger_extraction(messages, "sid")
        # 文件系统不应被写入新文件
        time.sleep(0.1)
        assert ltm.list_memory_files() == []

    def test_trigger_extraction_spawns_thread_and_persists(
        self, monkeypatch, configured_small_model, ltm,
    ):
        """达到阈值后，daemon 线程会落地 save_memory。"""
        extracted_json = json.dumps({
            "memories": [
                {
                    "type": "user",
                    "name": "extracted-1",
                    "description": "自动抽取的用户记忆",
                    "content": "用户是数据科学家",
                }
            ]
        })
        import openai
        FakeOpenAI = _make_fake_openai(extracted_json)
        monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)

        # 构造 3 条 user 消息以达到阈值
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "1"},
            {"role": "assistant", "content": "回答1"},
            {"role": "user", "content": "2"},
            {"role": "assistant", "content": "回答2"},
            {"role": "user", "content": "3"},
            {"role": "assistant", "content": "回答3"},
        ]
        ltm.trigger_extraction(messages, "sid")

        # 等抽取线程结束
        deadline = time.time() + 5
        while time.time() < deadline:
            if "extracted-1.md" in ltm.list_memory_files():
                break
            time.sleep(0.05)
        assert "extracted-1.md" in ltm.list_memory_files()

    def test_persist_extracted_memories_fills_missing_description_and_name(self, ltm):
        payload = json.dumps({
            "memories": [
                {
                    "type": "user",
                    "name": "",
                    "description": "",
                    "content": "用户长期关注日志可观测性。\n第二行不作为文件名优先来源。",
                }
            ]
        })
        ltm._persist_extracted_memories(payload, "sid", "")
        headers = ltm.scan_memory_files()
        assert len(headers) == 1
        assert headers[0].description == "用户长期关注日志可观测性。"
        assert headers[0].name == "用户长期关注日志可观测性"
        assert "用户长期关注日志可观测性.md" in ltm.list_memory_files()

    def test_persist_extracted_memories_rejects_invalid_type_and_empty_content(self, ltm):
        payload = json.dumps({
            "memories": [
                {
                    "type": "unknown",
                    "name": "bad-type",
                    "description": "bad",
                    "content": "正文",
                },
                {
                    "type": "user",
                    "name": "empty-content",
                    "description": "empty",
                    "content": "  ",
                },
            ]
        })
        ltm._persist_extracted_memories(payload, "sid", "")
        assert ltm.list_memory_files() == []

    def test_persist_extracted_memories_keeps_feedback_why_how_enforcement(self, ltm):
        payload = json.dumps({
            "memories": [
                {
                    "type": "feedback",
                    "name": "bad-feedback",
                    "description": "缺 Why/How",
                    "content": "用户不想要某种做法。",
                }
            ]
        })
        ltm._persist_extracted_memories(payload, "sid", "")
        assert "bad-feedback.md" not in ltm.list_memory_files()


    def test_extraction_enforces_why_how_for_feedback(
        self, monkeypatch, configured_small_model, ltm,
    ):
        """抽取出的 feedback 类记忆若缺 Why/How，应被 save_memory 拒绝。"""
        bad_json = json.dumps({
            "memories": [
                {
                    "type": "feedback",
                    "name": "bad-fb",
                    "description": "缺少 Why/How",
                    "content": "只有规则，没有 Why/How",
                }
            ]
        })
        import openai
        FakeOpenAI = _make_fake_openai(bad_json)
        monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)

        messages = [{"role": "user", "content": f"q{i}"} for i in range(5)]
        ltm.trigger_extraction(messages, "sid")
        time.sleep(0.5)
        # bad-fb 不应被落地
        assert "bad-fb.md" not in ltm.list_memory_files()

    def test_extraction_allows_free_form_for_user(
        self, monkeypatch, configured_small_model, ltm,
    ):
        """user 类记忆缺 Why/How 仍能落地。"""
        good_json = json.dumps({
            "memories": [
                {
                    "type": "user",
                    "name": "ok-user",
                    "description": "用户角色",
                    "content": "用户是 DBA",
                }
            ]
        })
        import openai
        FakeOpenAI = _make_fake_openai(good_json)
        monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)

        messages = [{"role": "user", "content": f"q{i}"} for i in range(5)]
        ltm.trigger_extraction(messages, "sid")
        deadline = time.time() + 3
        while time.time() < deadline:
            if "ok-user.md" in ltm.list_memory_files():
                break
            time.sleep(0.05)
        assert "ok-user.md" in ltm.list_memory_files()

    def test_extraction_swallows_errors(self, monkeypatch, configured_small_model, ltm):
        """抽取 agent 抛异常时主流程不崩，不写文件。"""
        class BoomOpenAI:
            def __init__(self, *a, **k):
                raise RuntimeError("boom")

        import openai
        monkeypatch.setattr(openai, "OpenAI", BoomOpenAI)

        messages = [{"role": "user", "content": f"q{i}"} for i in range(5)]
        ltm.trigger_extraction(messages, "sid")
        time.sleep(0.3)
        # 没有任何文件被写入
        assert ltm.list_memory_files() == []

    def test_extraction_skips_when_small_model_degraded(self, monkeypatch, ltm):
        """小模型降级到主模型时不应触发抽取。"""
        from harness_lite.memory import long_term as ltm_module
        same = {"api_key": "k", "base_url": "u", "model_name": "m"}
        monkeypatch.setattr(ltm_module, "get_small_config", lambda: same)
        monkeypatch.setattr(ltm_module, "get_main_config", lambda: same)

        called = {"flag": False}

        class TrapOpenAI:
            def __init__(self, *a, **k):
                called["flag"] = True

        import openai
        monkeypatch.setattr(openai, "OpenAI", TrapOpenAI)

        messages = [{"role": "user", "content": f"q{i}"} for i in range(5)]
        ltm.trigger_extraction(messages, "sid")
        time.sleep(0.3)
        assert called["flag"] is False

    def test_heavy_turn_spawns_two_extraction_agents(self, tmp_path, monkeypatch):
        """heavy turn (pending_tool_calls >= 20) 应启动 2 个抽取 agent (label A/B)。"""
        from harness_lite.memory import long_term as ltm_mod

        spawned_labels = []

        def fake_spawn(self, messages_copy, session_id, label=""):
            spawned_labels.append(label)

        monkeypatch.setattr(
            ltm_mod.LongTermMemoryManager, "_spawn_extraction_thread", fake_spawn
        )
        ltm = ltm_mod.LongTermMemoryManager(base_dir=str(tmp_path))

        # 构造一个含 20 个 tool_call 的 assistant 消息；
        # 同时保证总消息数 >= 4 才能命中分批分支
        heavy_assistant = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": f"c{i}",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
                for i in range(20)
            ],
        }
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "test"},
            heavy_assistant,
            {"role": "tool", "content": "result", "tool_call_id": "c0"},
        ]
        ltm.trigger_extraction(messages, session_id="sid-heavy")
        # 应该启动了 2 个 agent（label A 与 B），不关心顺序
        assert sorted(spawned_labels) == ["A", "B"]

    def test_deepcopy_isolation_between_main_and_extraction(self, tmp_path, monkeypatch):
        """trigger_extraction 后修改原 messages 不应影响后台线程持有的副本。"""
        from harness_lite.memory import long_term as ltm_mod

        received_snapshots = []

        def capture_thread(self, messages_copy, session_id, label=""):
            # 模拟后台线程：保存对副本的引用
            received_snapshots.append(messages_copy)

        monkeypatch.setattr(
            ltm_mod.LongTermMemoryManager, "_spawn_extraction_thread", capture_thread
        )
        ltm = ltm_mod.LongTermMemoryManager(base_dir=str(tmp_path))
        messages = [
            {"role": "user", "content": "msg1"},
            {"role": "assistant", "content": "a" * 9000},  # 触发 chars 阈值
        ]
        ltm.trigger_extraction(messages, session_id="sid-iso")
        assert len(received_snapshots) == 1
        snapshot = received_snapshots[0]
        # 在主流程修改 messages
        messages.append({"role": "user", "content": "later"})
        messages[0]["content"] = "MUTATED"
        # snapshot 不应被影响
        assert len(snapshot) == 2
        assert snapshot[0]["content"] == "msg1"


# ============================================================
# clear_all 清理
# ============================================================

class TestClearAll:
    def test_clear_all_removes_files_and_state(self, ltm):
        ltm.save_memory("user", "x", "x", "x")
        ltm.mark_read("a.md", "sid")
        count = ltm.clear_all()
        assert count >= 1
        assert ltm.list_memory_files() == []
        assert ltm.get_read_set("sid") == set()

    def test_clear_all_keeps_memory_md_and_resets_index(self, ltm):
        ltm.save_memory("user", "x", "x", "x")
        count = ltm.clear_all()
        assert count == 1
        entrypoint = ltm.base_dir / "MEMORY.md"
        assert entrypoint.exists()
        assert entrypoint.read_text(encoding="utf-8") == ""

    def test_clear_all_logs_per_file_delete_failures(self, monkeypatch, caplog, ltm):
        ltm.save_memory("user", "x", "x", "x")
        target = ltm.base_dir / "x.md"

        def fail_unlink(self):
            if self == target:
                raise OSError("cannot delete")
            return original_unlink(self)

        original_unlink = Path.unlink
        monkeypatch.setattr(Path, "unlink", fail_unlink)
        caplog.set_level("WARNING", logger="harness_lite.memory.long_term")

        count = ltm.clear_all()

        assert count == 0
        assert target.exists()
        assert "删除长期记忆文件失败" in caplog.text
        assert (ltm.base_dir / "MEMORY.md").read_text(encoding="utf-8") == "# 长期记忆索引\n\n"

# ============================================================
# 抽取消息净化：跳过 is_meta 临时推荐
# ============================================================

class TestExtractionSanitize:
    def test_sanitize_skips_is_meta_messages(self, ltm):
        """临时推荐 system 消息标记 is_meta=True 时必须被抽取 agent 跳过。"""
        messages = [
            {"role": "system", "content": "guide"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "ok"},
            {"role": "system", "content": "## 本轮可能相关的长期记忆 ...", "is_meta": True},
        ]
        cleaned = ltm._sanitize_messages_for_extraction(messages)
        # 仅保留前 3 条；is_meta 系统消息被丢弃
        assert len(cleaned) == 3
        joined = "".join((m.get("content") or "") for m in cleaned)
        assert "本轮可能相关" not in joined

    def test_sanitize_strips_internal_fields(self, ltm):
        """普通消息的内部字段（_meta_id 等）应被剥离，role/content 保留。"""
        messages = [
            {"role": "user", "content": "hi", "_meta_id": "abc"},
        ]
        cleaned = ltm._sanitize_messages_for_extraction(messages)
        assert cleaned == [{"role": "user", "content": "hi"}]
