"""
Symphony-Lite 记忆管理器
/workspace/symphony/memory_manager.py

职责：
- 任务完成后生成记忆记录（markdown，按 topic 归档）
- 追加 topic 的 .index.md 索引条目
- 更新 constitution.md 中的 topic 索引行
- 提供索引合并接口
"""
import datetime
import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config_loader import Config
    from tasks_db import Task


TOPIC_LABEL_MAP = {
    "iran": "伊朗新闻与地缘",
    "daily-report": "日报",
    "project": "项目设计与决策",
}


class MemoryManager:
    def __init__(self, config: "Config"):
        self.config = config
        self.constitution_path = config.memory.constitution_file
        self.index_root = config.memory.index_root
        self.index_filename = config.memory.index_filename
        self.index_max_entries = getattr(config.memory, "index_max_entries", 50)
        self.index_min_entries = getattr(config.memory, "index_min_entries", 10)
        self.keyword_filter = getattr(config.memory, "keyword_filter", True)

    def on_task_complete(self, task: "Task", stdout: str = "", stderr: str = "", exit_code: int = 0) -> None:
        """
        任务完成时调用（在 after_run 钩子之后）。
        1. 生成记忆记录文件
        2. 追加 topic .index.md 条目
        3. 更新 constitution.md 对应 topic 索引行
        """
        if not task.topic:
            print(f"[MemoryManager] {task.id} has no topic, skipping memory generation")
            return

        topic = task.topic
        date_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        slug = self._make_slug(task.title)
        record_filename = f"{date_str}-{slug}.md"
        record_path = self._render_path(topic, record_filename)

        # 1. 生成记忆记录
        self._write_record(record_path, task, stdout, stderr, exit_code)

        # 2. 追加 topic .index.md 条目
        index_path = self._index_path(topic)
        one_liner = self._summarize_one_liner(task, stdout)
        self._append_index_entry(index_path, date_str, record_filename, one_liner, topic)

        # 3. 更新 constitution.md 的 topic 索引行
        self._update_constitution_topic_line(topic)

        # 4. 滑动窗口裁剪（防止 .index.md 无限膨胀）
        self._trim_index(topic)

        print(f"[MemoryManager] Memory record written: {record_path}")

    def _write_record(self, path: str, task: "Task", stdout: str, stderr: str, exit_code: int,
                      discoveries: list = None, decisions: list = None, risks: list = None) -> None:
        """写单条记忆记录，包含结构化知识字段"""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        date_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")

        # 从 stdout 解析 discoveries/decisions/risks（如果未传入）
        if discoveries is None or decisions is None or risks is None:
            d, dc, r = self._extract_structured_fields(stdout)
            discoveries = discoveries if discoveries is not None else d
            decisions = decisions if decisions is not None else dc
            risks = risks if risks is not None else r

        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# 记忆记录：{task.topic} / {date_str}\n\n")
            f.write(f"**任务**：{task.id} — {task.title}\n")
            f.write(f"**执行时间**：{datetime.datetime.utcnow().isoformat()}Z\n")
            f.write(f"**一句话摘要**：{self._summarize_one_liner(task, stdout)}\n\n")

            # ── 结构化知识字段（核心新增）────────────────────────────
            if discoveries:
                f.write("## 发现（Discoveries）\n\n")
                for item in discoveries:
                    f.write(f"- {item}\n")
                f.write("\n")

            if decisions:
                f.write("## 决策（Decisions）\n\n")
                for item in decisions:
                    f.write(f"- {item}\n")
                f.write("\n")

            if risks:
                f.write("## 风险与坑点（Risks）\n\n")
                for item in risks:
                    f.write(f"- {item}\n")
                f.write("\n")
            # ── 原有内容保留 ───────────────────────────────────────

            f.write("## 关键内容\n\n")
            f.write(f"- exit_code：{exit_code}\n")
            if task.metadata:
                for k, v in task.metadata.items():
                    f.write(f"- {k}：{v}\n")
            f.write("\n## 产物路径\n\n")
            f.write(f"`{path}`\n")

    def _append_index_entry(self, index_path: str, date_str: str,
                            record_filename: str, one_liner: str, topic: str) -> None:
        """追加一行到 topic 的 .index.md"""
        os.makedirs(os.path.dirname(index_path), exist_ok=True)

        # 如果文件不存在，先写表头
        if not os.path.exists(index_path):
            topic_label = TOPIC_LABEL_MAP.get(topic, topic)
            with open(index_path, "w", encoding="utf-8") as f:
                f.write(f"# {topic_label} 话题记忆索引\n\n")
                f.write("## 条目\n\n")
                f.write("| 日期 | 记录文件 | 一句话摘要 |\n")
                f.write("|------|---------|-----------|\n")

        # 追加一行
        with open(index_path, "a", encoding="utf-8") as f:
            safe_name = record_filename.replace("|", "\\|")
            f.write(f"| {date_str} | `{safe_name}` | {one_liner} |\n")

    def _update_constitution_topic_line(self, topic: str) -> None:
        """
        读取 constitution.md，找到 topic 对应的索引行，
        更新 `→ 见 memory_index/{topic}/.index.md` 这一行（只更新行，不重写文件）。
        """
        if not os.path.exists(self.constitution_path):
            return

        topic_label = TOPIC_LABEL_MAP.get(topic, topic)
        marker = f"### {topic_label}"
        new_line = f"→ 见 `memory_index/{topic}/.index.md`"

        with open(self.constitution_path, encoding="utf-8") as f:
            lines = f.readlines()

        for i, line in enumerate(lines):
            if marker in line:
                next_idx = i + 1
                if next_idx < len(lines):
                    next_line = lines[next_idx]
                    if new_line.strip() not in next_line.strip():
                        lines[next_idx] = new_line + "\n"
                        with open(self.constitution_path, "w", encoding="utf-8") as f:
                            f.writelines(lines)
                break

    def _summarize_one_liner(self, task: "Task", stdout: str) -> str:
        """生成一句话摘要"""
        snippet = ""
        if stdout:
            snippet = stdout.strip()[-100:]
        elif task.metadata:
            snippet = str(list(task.metadata.values())[0])
        snippet = snippet.replace("\n", " ").strip()
        if len(snippet) > 80:
            snippet = snippet[:77] + "..."
        return snippet or f"{task.title}（无输出摘要）"

    def _extract_structured_fields(self, stdout: str) -> tuple[list, list, list]:
        """
        从 stdout 中解析 STRUCTURED_OUTPUT JSON 块，
        提取 discoveries / decisions / risks 字段。
        解析失败时返回空列表，不影响记录写入。
        """
        if not stdout:
            return [], [], []

        # 找 STRUCTURED_OUTPUT JSON 块
        import json
        # 匹配 ```json ... STRUCTURED_OUTPUT { ... } ``` 或直接 { ... }
        match = re.search(
            r'(?:STRUCTURED_OUTPUT\s*\n?\s*)?(\{.*\})',
            stdout,
            re.DOTALL,
        )
        if not match:
            return [], [], []

        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            # 尝试找最外层 JSON 对象
            try:
                # 截取到最后一个 } 为止
                candidate = match.group(1)
                data = json.loads(candidate.rsplit("}", 1)[0] + "}")
            except json.JSONDecodeError:
                return [], [], []

        discoveries = data.get("discoveries") or []
        decisions = data.get("decisions") or []
        risks = data.get("risks") or []

        # 确保是 list
        if not isinstance(discoveries, list):
            discoveries = [str(discoveries)]
        if not isinstance(decisions, list):
            decisions = [str(decisions)]
        if not isinstance(risks, list):
            risks = [str(risks)]

        return discoveries, decisions, risks

    def _make_slug(self, title: str) -> str:
        """把标题转成 safe filename slug"""
        slug = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]", "-", title)
        slug = re.sub(r"-+", "-", slug).strip("-")
        return slug[:50] or "untitled"

    def _render_path(self, topic: str, filename: str) -> str:
        return os.path.join(self.index_root, topic, filename)

    def _index_path(self, topic: str) -> str:
        return os.path.join(self.index_root, topic, self.index_filename)

    # ─── I7: 滑动窗口裁剪 ────────────────────────────────────────────

    def _trim_index(self, topic: str) -> None:
        """
        滑动窗口裁剪 .index.md：只保留最新 index_max_entries 条，
        同时至少保留 index_min_entries 条（防止 topic 条目过少）。
        """
        index_path = self._index_path(topic)
        if not os.path.exists(index_path):
            return

        with open(index_path, encoding="utf-8") as f:
            lines = f.readlines()

        # 解析 markdown table body（跳过 header + 表头分隔行）
        table_start = 0
        for i, line in enumerate(lines):
            if line.startswith("| 日期 |"):
                table_start = i + 1  # 下一行是分隔行，跳过
                break

        # 跳过 "|--" 分隔行，取实际数据行
        data_lines = []
        for line in lines[table_start:]:
            stripped = line.strip()
            if stripped.startswith("|") and "---" not in stripped:
                data_lines.append(line)

        if not data_lines:
            return

        # 计算裁剪后保留数：至少 min_entries，至多 max_entries
        keep_count = max(self.index_min_entries,
                         min(self.index_max_entries, len(data_lines)))
        trimmed = data_lines[-keep_count:]

        # 重写文件（保留 header + 分隔行）
        with open(index_path, "w", encoding="utf-8") as f:
            for line in lines[:table_start]:
                f.write(line)
            for row in trimmed:
                f.write(row)

    # ─── I7: 关键词过滤上下文 ──────────────────────────────────────

    def build_filtered_context(self, topic: str, keywords: list[str] = None) -> str:
        """
        根据 topic（+ 可选 keywords）在记忆索引中检索，返回过滤后的上下文片段。
        keywords 为空时返回该 topic 下全部最新条目（受 index_max_entries 限制）。
        """
        if not self.keyword_filter and not keywords:
            return ""

        index_path = self._index_path(topic)
        if not os.path.exists(index_path):
            return ""

        # 1. 读取 .index.md，取最新 index_max_entries 条
        with open(index_path, encoding="utf-8") as f:
            all_lines = f.readlines()

        table_start = 0
        for i, line in enumerate(all_lines):
            if line.startswith("| 日期 |"):
                table_start = i + 1
                break

        data_lines = []
        for line in all_lines[table_start:]:
            stripped = line.strip()
            if stripped.startswith("|") and "---" not in stripped:
                data_lines.append(line)

        # 取最新 index_max_entries 条
        entries = data_lines[-self.index_max_entries:]

        if not keywords:
            # 无关键词：只返回条目列表
            return self._format_entries_as_context(entries, topic)

        # 2. 关键词过滤：读取对应记录文件，在正文中搜索
        filtered = []
        topic_dir = os.path.dirname(index_path)
        kw_lower = [k.lower() for k in keywords]

        for entry_line in entries:
            # 提取文件名（第二个 | 列）
            cols = entry_line.strip("| ").split("|")
            if len(cols) < 2:
                continue
            record_name = cols[1].strip().strip("`").strip()
            record_path = os.path.join(topic_dir, record_name)
            if not os.path.exists(record_path):
                continue
            with open(record_path, encoding="utf-8") as f:
                content = f.read().lower()
            if any(kw in content for kw in kw_lower):
                filtered.append(entry_line)

        return self._format_entries_as_context(filtered, topic)

    def _format_entries_as_context(self, entries: list[str], topic: str) -> str:
        """
        把 markdown table 行格式化为上下文片段。
        对每条记录，读取对应文件，提取 discoveries/decisions/risks 正文，
        注入到新任务 prompt 中。
        """
        if not entries:
            return ""
        topic_label = TOPIC_LABEL_MAP.get(topic, topic)
        header = f"【记忆上下文 · {topic_label}】\n"
        lines = [header]

        topic_dir = os.path.dirname(self._index_path(topic))

        for entry in entries:
            cols = [c.strip() for c in entry.strip("| ").split("|")]
            if len(cols) < 2:
                continue
            date = cols[0]
            summary = cols[2] if len(cols) >= 3 else ""

            record_name = cols[1].strip().strip("`").strip()
            record_path = os.path.join(topic_dir, record_name)

            lines.append(f"  [{date}] {summary}")

            # 读取记录文件，提取知识正文
            if os.path.exists(record_path):
                kd = self._read_knowledge_from_record(record_path)
                if kd:
                    lines.append(kd)

        return "\n".join(lines) + "\n"

    def _read_knowledge_from_record(self, record_path: str) -> str:
        """
        从单条记忆记录文件中提取 discoveries/decisions/risks 正文，
        返回格式化的字符串，无内容时返回空字符串。
        """
        try:
            with open(record_path, encoding="utf-8") as f:
                content = f.read()
        except Exception:
            return ""

        parts = []
        # 提取 ## 发现（Discoveries） 到下一个 ## 之间的内容
        for section_title, field_key in [
            ("发现", "discoveries"),
            ("决策", "decisions"),
            ("风险", "risks"),
        ]:
            # 匹配 ## 发现（Discoveries） 这样的标题
            import re as _re
            m = _re.search(
                rf"##\s+[^#]*【?{section_title}【?.*?\n(.*?)(?=##|\Z)",
                content,
                _re.DOTALL,
            )
            if m:
                body = m.group(1).strip()
                if body:
                    # 提取所有 list 条目（- 开头）
                    items = _re.findall(r"^\s*-\s+(.+)$", body, _re.MULTILINE)
                    for item in items:
                        parts.append(f"    - {item}")

        if not parts:
            return ""
        return "\n".join(parts) + "\n"

    def merge_topic_index(self, topic: str) -> None:
        """
        GRaffe 触发：合并某 topic 的索引条目。
        读取该 topic 下所有记忆记录，生成合并摘要，重写索引。
        """
        index_path = self._index_path(topic)
        topic_dir = os.path.dirname(index_path)

        records = []
        if os.path.exists(topic_dir):
            for fname in os.listdir(topic_dir):
                if fname.endswith(".md") and fname != ".index.md":
                    with open(os.path.join(topic_dir, fname), encoding="utf-8") as f:
                        records.append((fname, f.read()))

        if not records:
            print(f"[MemoryManager] No records to merge for topic {topic}")
            return

        merged_filename = f"merged-{datetime.datetime.utcnow().strftime('%Y-%m-%d')}.md"
        merged_path = os.path.join(topic_dir, merged_filename)

        with open(merged_path, "w", encoding="utf-8") as f:
            f.write(f"# {topic} 话题记忆合并摘要（合并 {len(records)} 条）\n\n")
            for fname, content in records:
                m = re.search(r"\*\*一句话摘要\*\*：(.+)", content)
                summary = m.group(1) if m else fname
                f.write(f"- {summary}（来源：{fname}）\n")

        date_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        with open(index_path, "w", encoding="utf-8") as f:
            topic_label = TOPIC_LABEL_MAP.get(topic, topic)
            f.write(f"# {topic_label} 话题记忆索引\n\n")
            f.write("## 条目\n\n")
            f.write("| 日期 | 记录文件 | 一句话摘要 |\n")
            f.write("|------|---------|-----------|\n")
            f.write(f"| {date_str} | `{merged_filename}` | {topic} 话题合并摘要（合并 {len(records)} 条记录） |\n")

        print(f"[MemoryManager] Merged {len(records)} records into {merged_path}")
