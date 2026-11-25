"""System prompts, constants, and OpenAI-compatible tool definitions for AI triage."""

from __future__ import annotations

from typing import Any

VALID_VERDICTS: frozenset[str] = frozenset(
    {"KNOWN_ISSUE", "NEW_ISSUE", "REGRESSION", "NEEDS_REVIEW"}
)
CRASH_EVIDENCE_TOOLS: frozenset[str] = frozenset(
    {
        "get_crash_overview",
        "get_other_threads",
        "get_log_summary",
        "read_log_lines",
        "read_log_around_time",
        "search_log",
    }
)
TRUNCATION_MARKER = "\n...[TRUNCATED]"
_INT32_MAX = 2_147_483_647

_AI_SYSTEM_PROMPT_BASE = """You are a senior C++ Windows/Native Crash Triage Engineer.
You investigate a crash dump and its diagnostic logs using ONLY the provided read-only tools.

Investigation rules:
1. ALWAYS start by calling get_crash_overview to inspect crashing stack frames, missing symbols,
   session_id (if present), related_session_crashes, candidate known issues, and attached logs.
2. Inspect attached logs (get_log_summary, search_log, read_log_around_time, read_log_lines),
   other threads (get_other_threads), or known issues (search_known_issues, get_issue_detail).
3. CRITICAL session & mid-file crash log rule:
   - Processes often auto-restart after crashing, and logs may be collected manually hours later.
     Therefore, the crashing session often lives in the MIDDLE of a log file rather than the end.
   - When session_id is present (especially if trailing_lines_after_session > 0), DO NOT rely on
     global tail_lines or global recent_errors from post-restart sessions at the end of the file.
   - Prioritize session_recent_errors, session_tail_lines, and post_session_lines (untagged
     crash-moment lines and restart boundary right after session_last_line) from get_log_summary.
   - Use read_log_lines around session_last_line (e.g. [session_last_line - 80, +40]) to inspect
     untagged fatal/exception lines around the crash boundary, and pass only_session=true to
     search_log or read_log_around_time to filter out other sessions.
   - On delayed manual uploads, crash_time may reflect upload time rather than actual crash time;
     anchor to session_last_line / session_last_ts, and check related_session_crashes too.
4. Never invent log line numbers, file contents, or issue_id values. Cite only observed tool data.
   Use function_preview only as a hint from a truncated function name.
   Never return an empty NEEDS_REVIEW without first calling tools to inspect the crash.
5. Only AFTER completing your tool-based investigation, output ONLY a single valid JSON object
   (no surrounding prose) with non-empty analysis fields (including for NEEDS_REVIEW):
{
  "verdict": "KNOWN_ISSUE | NEW_ISSUE | REGRESSION | NEEDS_REVIEW",
  "matched_issue_id": 42,
  "confidence": 0.91,
  "suggested_title": "concise plain-text issue title in the required output language",
  "root_cause_analysis": "structured Markdown analysis (headings, lists, inline code)",
  "reasoning_summary": "structured Markdown numbered reasoning steps and conclusion",
  "evidence_log_snippets": [
    {"log_id": "uuid", "start_line": 1, "end_line": 5, "relevance": "why this matters"}
  ],
  "key_log_patterns": ["distinctive literal error substring from logs or stack"],
  "suggested_component": "subsystem name or null"
}
"""


def _build_output_language_instructions(output_language: str) -> str:
    """Return strict language, brevity, and impact-focused Markdown rules."""
    if output_language == "en-US":
        return (
            "\n6. Output language, brevity & impact-focused Markdown rules (STRICT — optimize "
            "for 10-second human scanning, NOT an essay):\n"
            "   - Write ALL human-readable fields (`suggested_title`, `root_cause_analysis`, "
            "`reasoning_summary`, and `evidence_log_snippets[].relevance`) in clear, concise "
            "English.\n"
            "   - `suggested_title`: one short plain-text title (<= 14 words, no Markdown). "
            "State scenario + core error cleanly.\n"
            "   - `suggested_component`: short subsystem name (<= 4 words, e.g. "
            "`DXGI / WGC Capture`), never a full sentence.\n"
            "   - `root_cause_analysis`: rendered as Markdown in the main card. Keep total length "
            "tight (~130-230 words, ~10-15 lines). DO NOT recite the full call stack or paste raw "
            "log lines (the UI already displays the stack and clickable log cards). Answer what "
            "engineers care about most in exactly 3 concise sections separated by `\\n\\n`:\n"
            "     `### Root Cause`\\n(2-3 direct sentences explaining why it crashed, the root "
            "cause, and the single decisive stack/log fact)\\n\\n"
            "`### Session & Subsequent Impact`\\n"
            "- **Current Session**: (Did this interrupt/hang/kill the active session, or was it "
            "recovered? 1 concise sentence grounded in logs/dump.)\\n"
            "- **Subsequent Sessions**: (Based on post-crash restart/trailing logs, did the "
            "process restart cleanly and resume service, or is there lingering GPU/driver/state "
            "risk? State explicitly if log evidence is insufficient.)\\n\\n"
            "`### Recommended Actions`\\n- (2-3 concrete, 1-sentence engineering fixes)\n"
            "   - Use inline backticks sparingly (1-2 key symbols/error codes per sentence). Do "
            "NOT wrap entire log lines, timestamps, or internal JSON field names in backticks.\n"
            "   - `reasoning_summary`: shown in a secondary collapsible panel. Keep to 3-4 brief "
            "bullets (<= 100 words total) explaining ONLY why the verdict/issue match was chosen "
            "and cross-checks performed. DO NOT repeat `root_cause_analysis`.\n"
            "   - `evidence_log_snippets[].relevance`: 1 concise sentence (<= 20 words) per item.\n"
            "   - `key_log_patterns`: keep strictly to 2-5 high-signal searchable error substrings "
            "(never dump 10+ generic stack frame names).\n"
        )
    return (
        "\n6. 输出语言、精炼度与影响评估规范（必须严格遵守，让工程师 10 秒内看懂，"
        "严禁写成高密度长文）：\n"
        "   - 所有面向人类阅读的字段（`suggested_title`、`root_cause_analysis`、"
        "`reasoning_summary`、`evidence_log_snippets[].relevance`）必须使用**简体中文**撰写。\n"
        "   - `suggested_title`：一行精炼中文标题（不超过 40 字，无 Markdown 标记），"
        "省去冗长命名空间，直接说明“触发场景 + 核心错误”。\n"
        "   - `suggested_component`：简短子系统名（不超过 20 字，如 `DXGI/WGC 显存采集`），"
        "不要写整句带括号和箭头的长描述。\n"
        "   - `root_cause_analysis`：前端主卡片按 Markdown 渲染。**严格控制总篇幅在 260~450 字"
        "（约 10~15 行）**，**禁止复述完整调用栈或抄写整行原始日志**（页面下方已有完整调用栈和"
        "可点击的证据日志卡片）。围绕工程师最关心的两个核心问题，固定使用以下 3 个精炼小节"
        "（小节间用 `\\n\\n` 分隔）：\n"
        "     `### 根因与触发原因`\\n（2~3 句直奔主题：为什么崩溃、根本原因是什么，带出 1 个最核心"
        "业务入口与 1 个决定性错误码/日志事实即可）\\n\\n"
        "`### 影响评估`\\n"
        "- **当次会话影响**：（明确结论：是否中断/卡死/强杀了当次会话，还是容错继续运行，1 句话给出"
        "日志或堆栈依据）\\n"
        "- **后续会话影响**：（结合 `post_session_lines` 与崩溃后日志，明确说明进程是否正常自动"
        "重启并恢复服务、是否存在 GPU/驱动异常或脏状态延续到下一次会话的风险；若无后续日志则如实"
        "注明“无崩溃后日志，暂无法确认”）\\n\\n"
        "`### 修复建议`\\n- （2~3 条可直接落地的短动作，每条 1 句话，不展开长篇说教）\n"
        "   - **克制使用行内代码反引号**：每句仅对 1~2 个最关键的函数名/错误码/文件名加反引号，"
        "**禁止把整条日志、时间戳、内部字段名或所有参数都包成代码胶囊**，避免满屏灰块干扰阅读。\n"
        "   - `reasoning_summary`：在前端默认折叠的「推理依据」区域展示。仅用 **3~4 条极简短句"
        "列表（总字数不超过 200 字）**说明为何做出该 `verdict` 判定（如命中哪个已知问题、关键"
        "交叉验证结果），**严禁把 `root_cause_analysis` 的内容再重复写一遍**。\n"
        "   - `evidence_log_snippets[].relevance`：每条限 1 句精炼中文（不超过 45 字），直接"
        "说明该段日志证明了什么。\n"
        "   - `key_log_patterns`：严格精选 **2~5 条**最具检索价值的核心错误特征子串，"
        "不要塞入十余个普通函数名或泛用词。\n"
    )


def _build_system_prompt(output_language: str = "zh-CN") -> str:
    return _AI_SYSTEM_PROMPT_BASE + _build_output_language_instructions(output_language)


AI_SYSTEM_PROMPT = _build_system_prompt("zh-CN")

AI_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_crash_overview",
            "description": (
                "Get crash metadata (including custom session_id if present), top frames, "
                "missing symbols, cluster info, related crashes in the same session, recalled "
                "candidate issues, and attached log metadata (including mid-file session spans)."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_other_threads",
            "description": (
                "Inspect non-crashing threads from the minidump, optionally filtered by keyword."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": (
                            "Optional case-insensitive filter matching thread_name, "
                            "module, or function."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_log_summary",
            "description": (
                "Get precomputed log digest (error/warn counts, tail lines, recent errors, plus "
                "session errors/tail and post_session_lines boundary context if session_id is set)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "log_id": {
                        "type": "string",
                        "description": "UUID of the CrashLog belonging to this crash.",
                    },
                },
                "required": ["log_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_log_lines",
            "description": (
                "Read a specific 1-based line range from an attached log "
                "(hard capped at 200 lines)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "log_id": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["log_id", "start_line", "end_line"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_log_around_time",
            "description": (
                "Read log lines within a time window around a target timestamp "
                "(defaults to crash_time; when only_session=true and default crash_time misses "
                "on delayed uploads, automatically falls back to session_last_ts)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "log_id": {"type": "string"},
                    "timestamp": {
                        "type": "string",
                        "description": (
                            "ISO-8601 or YYYY-MM-DD HH:MM:SS timestamp; "
                            "defaults to crash_time if omitted."
                        ),
                    },
                    "window_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 120,
                        "default": 10,
                    },
                    "max_lines": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "default": 200,
                    },
                    "only_session": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "If true, restrict results to lines containing the crash's session_id."
                        ),
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional explicit session_id filter override.",
                    },
                },
                "required": ["log_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_log",
            "description": (
                "Search an attached compressed log for a substring or regex pattern, "
                "optionally restricted to lines belonging to the crash's session_id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "log_id": {"type": "string"},
                    "pattern": {"type": "string"},
                    "is_regex": {"type": "boolean", "default": False},
                    "max_matches": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 50,
                    },
                    "case_sensitive": {"type": "boolean", "default": False},
                    "only_session": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "If true, only return matches on lines containing this crash's "
                            "session_id."
                        ),
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional explicit session_id filter override.",
                    },
                },
                "required": ["log_id", "pattern"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_known_issues",
            "description": (
                "Search project knowledge base issues using stack similarity and "
                "optional text/module/function query."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "default": ""},
                    "module": {"type": "string"},
                    "function": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_issue_detail",
            "description": (
                "Fetch full details and linked stack signatures of a project issue by ID."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_id": {"type": "integer"},
                },
                "required": ["issue_id"],
                "additionalProperties": False,
            },
        },
    },
]
