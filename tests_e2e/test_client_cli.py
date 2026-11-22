from pathlib import Path

import httpx
import pytest

from cli import crashkiller_cli


@pytest.mark.parametrize("sse_available", [True, False])
def test_crash_wait_authenticates_status_requests(
    sse_available: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dump_path = tmp_path / "crash.dmp"
    dump_path.write_bytes(b"synthetic dump")
    requests: list[str] = []
    api_key = "test-project-key"
    upload_path = "/api/v1/crashes/upload"
    events_path = "/api/v1/crashes/test-crash/events"
    status_path = "/api/v1/crashes/test-crash/status"

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == api_key
        requests.append(request.url.path)
        if request.url.path == upload_path:
            return httpx.Response(202, json={"crash_id": "test-crash", "status": "PENDING"})
        if request.url.path == events_path:
            if not sse_available:
                return httpx.Response(503)
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                text='data: {"status":"ANALYZED","ai_verdict":"NEEDS_REVIEW"}\n\n',
            )
        assert request.url.path == status_path
        return httpx.Response(200, json={"status": "FAILED", "error": "Parser failed"})

    real_client = httpx.Client

    def mock_client(**kwargs: object) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(crashkiller_cli.httpx, "Client", mock_client)
    exit_code = crashkiller_cli.main(
        [
            "upload-crash",
            "--server",
            "http://testserver",
            "--project",
            "test-project",
            "--api-key",
            api_key,
            "--app-version",
            "1.0.0",
            "--dump",
            str(dump_path),
            "--wait",
        ]
    )

    output = capsys.readouterr()
    if sse_available:
        assert exit_code == 0
        assert "status=ANALYZED ai_verdict=NEEDS_REVIEW" in output.out
        assert requests == [upload_path, events_path]
    else:
        assert exit_code == 1
        assert "Analysis failed: Parser failed" in output.err
        assert requests == [upload_path, events_path, status_path]
