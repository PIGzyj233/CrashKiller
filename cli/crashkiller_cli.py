#!/usr/bin/env python3
"""CrashKiller CI/CD CLI for symbol and crash dump ingestion."""

from __future__ import annotations

import argparse
import json
import sys
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

PE_EXTENSIONS = {".exe", ".dll"}
TERMINAL_STATUSES = {"ANALYZED", "FAILED"}


@dataclass(slots=True)
class SymbolPair:
    pdb_path: Path
    pe_path: Path | None = None


def discover_symbol_pairs(scan_dir: Path) -> list[SymbolPair]:
    """Scan scan_dir recursively for .pdb files and pair with matching .exe/.dll."""
    if not scan_dir.is_dir():
        raise FileNotFoundError(f"Symbol directory not found: {scan_dir}")

    pdb_files: list[Path] = []
    pdb_dirs: set[Path] = set()
    pdb_by_stem: dict[str, list[Path]] = {}
    pe_by_dir_stem: dict[tuple[Path, str], Path] = {}
    pe_by_stem: dict[str, list[Path]] = {}

    for path in sorted(scan_dir.rglob("*")):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext == ".pdb":
            pdb_files.append(path)
            pdb_dirs.add(path.parent.resolve())
            pdb_by_stem.setdefault(path.stem.lower(), []).append(path)
        elif ext in PE_EXTENSIONS:
            stem_key = path.stem.lower()
            pe_by_dir_stem.setdefault((path.parent.resolve(), stem_key), path)
            pe_by_stem.setdefault(stem_key, []).append(path)

    pairs: list[SymbolPair] = []
    for pdb_path in pdb_files:
        stem_key = pdb_path.stem.lower()
        same_dir_pe = pe_by_dir_stem.get((pdb_path.parent.resolve(), stem_key))
        if same_dir_pe is not None:
            pairs.append(SymbolPair(pdb_path=pdb_path, pe_path=same_dir_pe))
        else:
            candidates = pe_by_stem.get(stem_key, [])
            pdbs_for_stem = pdb_by_stem.get(stem_key, [])
            pe_match: Path | None = None
            if (
                len(candidates) == 1
                and len(pdbs_for_stem) == 1
                and candidates[0].parent.resolve() not in pdb_dirs
            ):
                pe_match = candidates[0]
            pairs.append(
                SymbolPair(
                    pdb_path=pdb_path,
                    pe_path=pe_match,
                )
            )
    return pairs


def _cmd_upload_symbols(args: argparse.Namespace) -> int:
    pairs: list[SymbolPair] = []
    if args.dir:
        try:
            pairs.extend(discover_symbol_pairs(Path(args.dir)))
        except FileNotFoundError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2

    if args.pdb:
        pdb_path = Path(args.pdb)
        if not pdb_path.is_file():
            print(f"Error: PDB file not found: {pdb_path}", file=sys.stderr)
            return 2
        pe_path = Path(args.pe) if args.pe else None
        if pe_path is not None and not pe_path.is_file():
            print(f"Error: PE file not found: {pe_path}", file=sys.stderr)
            return 2
        pairs.append(SymbolPair(pdb_path=pdb_path, pe_path=pe_path))

    if not pairs:
        print("No .pdb files found to upload.")
        return 0

    if args.dry_run:
        for pair in pairs:
            pe_label = str(pair.pe_path) if pair.pe_path else "(none)"
            print(f"[DRY-RUN] pdb={pair.pdb_path} pe={pe_label}")
        return 0

    base_url = args.server.rstrip("/")
    upload_url = f"{base_url}/api/v1/symbols/upload"
    had_error = False

    with httpx.Client(timeout=120.0) as client:
        for pair in pairs:
            with ExitStack() as stack:
                pdb_fh = stack.enter_context(open(pair.pdb_path, "rb"))
                files: dict[str, Any] = {
                    "pdb_file": (pair.pdb_path.name, pdb_fh, "application/octet-stream")
                }
                if pair.pe_path is not None:
                    pe_fh = stack.enter_context(open(pair.pe_path, "rb"))
                    files["pe_file"] = (pair.pe_path.name, pe_fh, "application/octet-stream")

                try:
                    resp = client.post(
                        upload_url,
                        headers={"X-API-Key": args.api_key},
                        data={
                            "project_key": args.project,
                            "app_version": args.app_version,
                        },
                        files=files,
                    )
                except httpx.HTTPError as exc:
                    print(f"Upload failed for {pair.pdb_path}: {exc}", file=sys.stderr)
                    had_error = True
                    continue

            if resp.status_code >= 400:
                print(
                    f"Upload failed for {pair.pdb_path} ({resp.status_code}): {resp.text}",
                    file=sys.stderr,
                )
                had_error = True
            else:
                body = resp.json()
                print(
                    f"Uploaded {body.get('debug_file')} ({body.get('debug_id')}) "
                    f"has_pe_unwind={body.get('has_pe_unwind')} "
                    f"deduplicated={body.get('deduplicated')}"
                )

    if had_error and args.fail_on_error:
        return 1
    return 0


def _wait_for_crash(
    client: httpx.Client,
    base_url: str,
    crash_id: str,
    timeout_sec: float,
) -> tuple[str, str | None, str | None]:
    events_url = f"{base_url}/api/v1/crashes/{crash_id}/events"
    status_url = f"{base_url}/api/v1/crashes/{crash_id}/status"
    deadline = time.monotonic() + timeout_sec

    try:
        with client.stream(
            "GET",
            events_url,
            headers={"Accept": "text/event-stream"},
            timeout=max(0.5, min(5.0, timeout_sec)),
        ) as resp:
            if resp.status_code == 200:
                current_data: list[str] = []
                for raw_line in resp.iter_lines():
                    if time.monotonic() > deadline:
                        break
                    line = raw_line.rstrip("\r\n")
                    if line == "":
                        if current_data:
                            payload_str = "\n".join(current_data)
                            current_data = []
                            try:
                                evt = json.loads(payload_str)
                            except json.JSONDecodeError:
                                continue
                            st = str(evt.get("status") or "")
                            if st in TERMINAL_STATUSES:
                                return st, evt.get("ai_verdict"), evt.get("error")
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        current_data.append(line[5:].lstrip())
    except httpx.HTTPError:
        pass

    while time.monotonic() < deadline:
        resp = client.get(status_url, timeout=15.0)
        if resp.is_client_error:
            resp.raise_for_status()
        if resp.status_code == 200:
            data = resp.json()
            st = str(data.get("status") or "")
            if st in TERMINAL_STATUSES:
                return st, data.get("ai_verdict"), data.get("error")
        time.sleep(0.2)

    raise TimeoutError(f"Timed out waiting for crash {crash_id} to reach terminal status")


def _cmd_upload_crash(args: argparse.Namespace) -> int:
    dump_path = Path(args.dump)
    if not dump_path.is_file():
        print(f"Error: Dump file not found: {dump_path}", file=sys.stderr)
        return 2

    log_paths: list[Path] = []
    for raw_log in args.log or []:
        lp = Path(raw_log)
        if not lp.is_file():
            print(f"Error: Log file not found: {lp}", file=sys.stderr)
            return 2
        log_paths.append(lp)

    base_url = args.server.rstrip("/")
    upload_url = f"{base_url}/api/v1/crashes/upload"

    data: dict[str, str] = {
        "project_key": args.project,
        "app_version": args.app_version,
    }
    if args.build_id:
        data["build_id"] = args.build_id
    if args.os_version:
        data["os_version"] = args.os_version
    if args.crash_time:
        data["crash_time"] = args.crash_time

    try:
        with ExitStack() as stack:
            dump_fh = stack.enter_context(open(dump_path, "rb"))
            multipart_files: list[tuple[str, tuple[str, Any, str]]] = [
                ("dump_file", (dump_path.name, dump_fh, "application/octet-stream"))
            ]
            for lp in log_paths:
                lfh = stack.enter_context(open(lp, "rb"))
                multipart_files.append(("log_files", (lp.name, lfh, "text/plain")))

            with httpx.Client(
                timeout=120.0,
                headers={"X-API-Key": args.api_key},
            ) as client:
                resp = client.post(
                    upload_url,
                    data=data,
                    files=multipart_files,
                )
                if resp.status_code >= 400:
                    print(
                        f"Crash upload failed ({resp.status_code}): {resp.text}",
                        file=sys.stderr,
                    )
                    return 1

                payload = resp.json()
                crash_id = str(payload["crash_id"])
                print(f"Uploaded crash_id={crash_id} status={payload.get('status')}")

                if not args.wait:
                    return 0

                try:
                    final_status, verdict, err = _wait_for_crash(
                        client, base_url, crash_id, args.wait_timeout
                    )
                except TimeoutError as exc:
                    print(f"Error: {exc}", file=sys.stderr)
                    return 1

                if verdict is None and final_status == "ANALYZED":
                    status_resp = client.get(
                        f"{base_url}/api/v1/crashes/{crash_id}/status",
                        timeout=15.0,
                    )
                    if status_resp.status_code == 200:
                        verdict = status_resp.json().get("ai_verdict")

                print(f"crash_id={crash_id} status={final_status} ai_verdict={verdict}")
                if final_status == "FAILED":
                    if err:
                        print(f"Analysis failed: {err}", file=sys.stderr)
                    return 1
                return 0
    except httpx.HTTPError as exc:
        print(f"HTTP error during crash upload: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crashkiller_cli",
        description="CrashKiller CI/CD symbol and crash upload CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sym_p = sub.add_parser("upload-symbols", help="Upload PDB/PE symbol files")
    sym_p.add_argument("--server", required=True, help="CrashKiller server base URL")
    sym_p.add_argument("--project", required=True, help="Project key")
    sym_p.add_argument("--api-key", required=True, help="Project API key")
    sym_p.add_argument(
        "--app-version",
        "--version",
        dest="app_version",
        required=True,
        help="Application version string",
    )
    sym_p.add_argument("--dir", help="Directory to scan recursively for .pdb/.exe/.dll pairs")
    sym_p.add_argument("--pdb", help="Single PDB file path")
    sym_p.add_argument("--pe", help="Optional matching PE (.exe/.dll) file path")
    sym_p.add_argument("--dry-run", action="store_true", help="Print planned uploads only")
    sym_p.add_argument(
        "--fail-on-error",
        action="store_true",
        help="Exit non-zero if any symbol upload fails",
    )
    sym_p.set_defaults(func=_cmd_upload_symbols)

    crash_p = sub.add_parser("upload-crash", help="Upload a minidump and optional logs")
    crash_p.add_argument("--server", required=True, help="CrashKiller server base URL")
    crash_p.add_argument("--project", required=True, help="Project key")
    crash_p.add_argument("--api-key", required=True, help="Project API key")
    crash_p.add_argument(
        "--app-version",
        "--version",
        dest="app_version",
        required=True,
        help="Application version string",
    )
    crash_p.add_argument("--dump", required=True, help="Path to .dmp minidump file")
    crash_p.add_argument(
        "--log",
        action="append",
        default=[],
        help="Optional path to crash log file (repeatable)",
    )
    crash_p.add_argument("--build-id", help="Optional build identifier")
    crash_p.add_argument("--os-version", help="Optional OS version")
    crash_p.add_argument("--crash-time", help="Optional ISO-8601 crash timestamp")
    crash_p.add_argument(
        "--wait",
        action="store_true",
        help="Wait for SSE terminal status and print ai_verdict",
    )
    crash_p.add_argument(
        "--wait-timeout",
        type=float,
        default=120.0,
        help="Timeout in seconds when --wait is specified",
    )
    crash_p.set_defaults(func=_cmd_upload_crash)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "upload-symbols" and not args.dir and not args.pdb:
        parser.error("upload-symbols requires --dir and/or --pdb")
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
