"""Send a synthetic trace and verify it through Langfuse v4's observations API.

No model request, user content, default credential, or legacy ingestion API is used.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def verify(env_file, *, timeout=30):
    import httpx

    from app.core.config import Settings
    from app.observability import create_observer

    settings = Settings(_env_file=env_file)
    observer = create_observer(settings)
    status = observer.status()
    if not status.get("configured") or not status.get("enabled"):
        raise RuntimeError("LANGFUSE_CONFIGURATION_REQUIRED")
    run_id, draft_id = str(uuid4()), str(uuid4())
    start = datetime.now(UTC) - timedelta(minutes=1)
    observer.start()
    try:
        with observer.execution(run_id, 0, draft_id=draft_id, workflow="observability_canary") as root:
            with observer.span("canary.prepare", run_type="tool", metadata={"is_demo": True}):
                pass
            with observer.span("canary.validate", metadata={"is_demo": True}) as validation:
                validation.set(validation_outcome="passed")
            root.set(status="completed", business_outcome="completed", is_demo=True)
        trace_id = UUID(root.trace_id).hex
    finally:
        observer.close(timeout=settings.obs_shutdown_timeout_seconds)
    if observer.status().get("failed") or observer.status().get("dropped"):
        raise RuntimeError("LANGFUSE_EXPORT_FAILED")
    deadline = time.monotonic() + timeout
    expected = {"lesson.execute", "canary.prepare", "canary.validate"}
    with httpx.Client(base_url=settings.langfuse_base_url, timeout=2, follow_redirects=False,
            auth=(settings.langfuse_public_key, settings.langfuse_secret_key.get_secret_value())) as client:
        while time.monotonic() < deadline:
            response = client.get("/api/public/v2/observations", params={
                "traceId": trace_id, "fields": "core,basic,usage,metadata", "limit": 100,
                "fromStartTime": start.isoformat(),
                "toStartTime": (datetime.now(UTC) + timedelta(minutes=1)).isoformat()})
            if response.status_code != 200:
                raise RuntimeError(f"LANGFUSE_READBACK_HTTP_{response.status_code}")
            rows = response.json().get("data", [])
            by_name = {row.get("name"): row for row in rows}
            if expected.issubset(by_name):
                root_id = by_name["lesson.execute"]["id"]
                for name in expected - {"lesson.execute"}:
                    row = by_name[name]
                    if row.get("parentObservationId", row.get("parent_observation_id")) != root_id:
                        raise RuntimeError("LANGFUSE_PARENT_MISMATCH")
                return {"verified": True, "platform": "langfuse", "sdk_version": version("langfuse"),
                    "business_run_id": run_id, "trace_id": trace_id, "observations": len(rows),
                    "names": sorted(by_name), "hierarchy_verified": True, "provider_requests": 0,
                    "recorded_at": datetime.now(UTC).isoformat()}
            time.sleep(.25)
    raise RuntimeError("LANGFUSE_READBACK_TIMEOUT")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Send synthetic metadata to the configured self-hosted project")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".cache/langfuse-app.env")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--output", type=Path, default=ROOT / ".cache/langfuse-verification.json")
    options = parser.parse_args(argv)
    if not options.live:
        print("dry-run: no network requests; --live verifies synthetic observations in the configured Langfuse project")
        return 0
    try:
        evidence = verify(options.env_file, timeout=max(1, min(120, options.timeout)))
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as error:
        # Never print SDK/HTTP/validation messages containing settings or authentication details.
        code = str(error) if isinstance(error, RuntimeError) and str(error).startswith("LANGFUSE_") else "LANGFUSE_CHECK_FAILED"
        print(code)
        return 1
    print(json.dumps(evidence, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
