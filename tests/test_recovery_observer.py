import pytest

from scripts.check_worker_recovery import require_process_and_model_evidence


def state(pid, instance="same"):
    return {"instance": instance, "processes": {"one-click": pid}}


def test_lease_requeue_without_os_process_replacement_cannot_pass_observer():
    with pytest.raises(RuntimeError, match="replace"):
        require_process_and_model_evidence(state(1), state(1), [{"provider": "openai_compatible", "status": "success"}])


@pytest.mark.parametrize("calls", [[], [{"provider": "mock", "status": "success"}],
                                  [{"provider": "unknown", "status": "interrupted"}]])
def test_real_configuration_without_real_call_receipt_cannot_pass_observer(calls):
    with pytest.raises(RuntimeError, match="real-model receipt"):
        require_process_and_model_evidence(state(1), state(2), calls)


def test_observer_accepts_real_replacement_and_successful_model_receipt():
    require_process_and_model_evidence(state(1), state(2), [{"provider": "unknown", "status": "interrupted"},
        {"provider": "openai_compatible", "status": "success"}])
