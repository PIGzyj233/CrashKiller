from __future__ import annotations

import collections
import http.server
import json
import threading
import time
import uuid
from typing import Any


class MockLLMServer:
    """Local OpenAI-compatible mock HTTP server for E2E AI triage tests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: collections.deque[dict[str, Any]] = collections.deque()
        self.requests: list[dict[str, Any]] = []
        self._handler: Any = None
        self._delay_s: float = 0.0
        self._error_status: int | None = None
        self._error_message: str = "Mock LLM error"
        self._loop_forever_tool: tuple[str, dict[str, Any]] | None = None
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int = 0

    def start(self) -> None:
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _send_json(self, status: int, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                if self.path.rstrip("/").endswith("/models"):
                    self._send_json(
                        200,
                        {
                            "object": "list",
                            "data": [{"id": "mock-gpt-4o-mini", "object": "model"}],
                        },
                    )
                    return
                self._send_json(404, {"error": {"message": "Not found"}})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    req_json = json.loads(raw_body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    req_json = {"raw": raw_body.decode("utf-8", errors="replace")}

                req_record = {
                    "method": "POST",
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": req_json,
                }
                with outer._lock:
                    outer.requests.append(req_record)
                    req_idx = len(outer.requests) - 1
                    delay_s = outer._delay_s
                    err_status = outer._error_status
                    err_msg = outer._error_message
                    handler_fn = outer._handler
                    loop_tool = outer._loop_forever_tool

                if delay_s > 0:
                    time.sleep(delay_s)

                if err_status is not None:
                    self._send_json(err_status, {"error": {"message": err_msg, "code": err_status}})
                    return

                if handler_fn is not None:
                    res = handler_fn(req_json, req_record["headers"], req_idx)
                    if isinstance(res, tuple):
                        status_code, body = res
                    else:
                        status_code, body = 200, res
                    self._send_json(status_code, body)
                    return

                has_tool_result = any(
                    isinstance(m, dict) and m.get("role") == "tool"
                    for m in (req_json.get("messages") or [])
                )
                with outer._lock:
                    if outer._queue:
                        spec = outer._queue.popleft()
                    elif loop_tool is not None:
                        t_name, t_args = loop_tool
                        spec = outer._make_tool_call_spec([(t_name, t_args)])
                    else:
                        spec = outer._make_verdict_spec()

                    if spec.get("_requires_tool_context") and not has_tool_result:
                        outer._queue.appendleft(spec)
                        spec = outer._make_tool_call_spec([("get_crash_overview", {})])

                spec_delay = float(spec.get("delay_s", 0.0))
                if spec_delay > 0:
                    time.sleep(spec_delay)

                status = int(spec.get("status", 200))
                body = spec.get("body")
                if body is None:
                    body = outer._build_completion_payload(
                        model=req_json.get("model", "mock-gpt-4o-mini"),
                        content=spec.get("content"),
                        tool_calls=spec.get("tool_calls"),
                        finish_reason=spec.get("finish_reason", "stop"),
                    )
                self._send_json(status, body)

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def _build_completion_payload(
        self,
        model: str,
        content: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        finish_reason: str = "stop",
    ) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": 110,
                "completion_tokens": 42,
                "total_tokens": 152,
            },
        }

    def _make_tool_call_spec(self, calls: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
        tc_list = []
        for idx, (name, args) in enumerate(calls):
            tc_list.append(
                {
                    "id": f"call_{uuid.uuid4().hex[:10]}_{idx}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            )
        return {
            "status": 200,
            "content": None,
            "tool_calls": tc_list,
            "finish_reason": "tool_calls",
        }

    def _make_verdict_spec(
        self,
        verdict: str = "NEW_ISSUE",
        matched_issue_id: int | None = None,
        confidence: float = 0.91,
        suggested_title: str = "Renderer::SubmitDrawCall null pointer dereference",
        root_cause_analysis: str = (
            "Null texture pointer passed to SubmitDrawCall after async load failure."
        ),
        reasoning_summary: str = (
            "Stack trace and error log match null pointer dereference pattern."
        ),
        evidence_log_snippets: list[dict[str, Any]] | None = None,
        key_log_patterns: list[str] | None = None,
        suggested_component: str = "Rendering",
    ) -> dict[str, Any]:
        payload = {
            "verdict": verdict,
            "matched_issue_id": matched_issue_id,
            "confidence": confidence,
            "suggested_title": suggested_title,
            "root_cause_analysis": root_cause_analysis,
            "reasoning_summary": reasoning_summary,
            "evidence_log_snippets": (
                evidence_log_snippets if evidence_log_snippets is not None else []
            ),
            "key_log_patterns": (key_log_patterns if key_log_patterns is not None else ["FATAL"]),
            "suggested_component": suggested_component,
        }
        return {
            "status": 200,
            "content": json.dumps(payload),
            "finish_reason": "stop",
            "_requires_tool_context": True,
        }

    def queue_tool_call(self, tool_name: str, arguments: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._queue.append(self._make_tool_call_spec([(tool_name, arguments or {})]))

    def queue_tool_calls(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        with self._lock:
            self._queue.append(self._make_tool_call_spec(calls))

    def queue_verdict(
        self,
        verdict: str = "NEW_ISSUE",
        matched_issue_id: int | None = None,
        confidence: float = 0.91,
        suggested_title: str = "Renderer::SubmitDrawCall null pointer dereference",
        root_cause_analysis: str = "Null texture pointer passed to SubmitDrawCall.",
        reasoning_summary: str = "Crash stack and logs indicate null pointer dereference.",
        evidence_log_snippets: list[dict[str, Any]] | None = None,
        key_log_patterns: list[str] | None = None,
        suggested_component: str = "Rendering",
    ) -> None:
        with self._lock:
            self._queue.append(
                self._make_verdict_spec(
                    verdict=verdict,
                    matched_issue_id=matched_issue_id,
                    confidence=confidence,
                    suggested_title=suggested_title,
                    root_cause_analysis=root_cause_analysis,
                    reasoning_summary=reasoning_summary,
                    evidence_log_snippets=evidence_log_snippets,
                    key_log_patterns=key_log_patterns,
                    suggested_component=suggested_component,
                )
            )

    def queue_response(
        self,
        status: int = 200,
        body: dict[str, Any] | None = None,
        content: str | None = None,
        delay_s: float = 0.0,
    ) -> None:
        with self._lock:
            self._queue.append(
                {"status": status, "body": body, "content": content, "delay_s": delay_s}
            )

    def set_handler(self, fn: Any) -> None:
        with self._lock:
            self._handler = fn

    def set_delay(self, seconds: float) -> None:
        with self._lock:
            self._delay_s = seconds

    def set_error_status(self, status: int | None, message: str = "Mock LLM error") -> None:
        with self._lock:
            self._error_status = status
            self._error_message = message

    def set_loop_forever(
        self,
        tool_name: str = "get_crash_overview",
        arguments: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._loop_forever_tool = (tool_name, arguments or {})

    def reset(self) -> None:
        with self._lock:
            self.requests.clear()
            self._handler = None
            self._delay_s = 0.0
            self._error_status = None
            self._error_message = "Mock LLM error"
            self._loop_forever_tool = None
            self._queue.clear()
