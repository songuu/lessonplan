import json
import subprocess
from unittest.mock import Mock

from scripts.demo_arq_sse import redis_identity, stop_owned_child


def test_demo_evidence_does_not_include_redis_credentials():
    recorded = redis_identity("redis://review-user:known-review-secret@127.0.0.1:16379/6")
    encoded = json.dumps(recorded)
    assert "review-user" not in encoded and "known-review-secret" not in encoded
    assert recorded["host"] == "127.0.0.1" and recorded["database"] == "/6"


def test_unresponsive_owned_child_is_terminated_then_killed():
    process = Mock()
    process.wait.side_effect = [subprocess.TimeoutExpired("owned-demo", 15),
                               subprocess.TimeoutExpired("owned-demo", 5), None]
    process.returncode = -9
    state = stop_owned_child(process)
    process.terminate.assert_called_once_with()
    process.kill.assert_called_once_with()
    assert state == {"exited": True, "exit_code": -9, "shutdown": "kill"}
