from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.run_local import restart_exited_workers


def process(exit_code=None):
    return SimpleNamespace(poll=lambda: exit_code)


def test_crashed_worker_is_replaced_while_api_and_healthy_workers_stay_running():
    api, live, crashed, replacement = process(), process(), process(137), process()
    processes = {"api": api, "one-click": crashed, "structured": live}
    launch = Mock(return_value=replacement)
    counts = {}
    assert restart_exited_workers(processes, launch, counts) == ["one-click"]
    assert processes == {"api": api, "one-click": replacement, "structured": live}
    assert counts == {"one-click": 1}


def test_api_exit_stops_group_and_does_not_restart_workers():
    launch = Mock()
    with pytest.raises(RuntimeError, match="API exited"):
        restart_exited_workers({"one-click": process(1), "api": process(1)}, launch, {})
    launch.assert_not_called()


def test_crashing_worker_cannot_hot_loop_forever():
    launch = Mock()
    with pytest.raises(RuntimeError, match="restart budget"):
        restart_exited_workers({"api": process(), "one-click": process(1)}, launch, {"one-click": 3})
    launch.assert_not_called()
