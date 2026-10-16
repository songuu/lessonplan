import asyncio

import pytest

from scripts.check_local_flow import require_changed_notification, require_live_prefix


class Subscription:
    def __init__(self, messages):
        self.messages = iter(messages)

    async def get_message(self, **kwargs):
        await asyncio.sleep(.005)
        return next(self.messages, None)


async def test_canary_observes_real_changed_notification_after_subscription_ack():
    assert await require_changed_notification(Subscription([None, {"data": b"changed"}]), timeout=.1)


async def test_canary_rejects_missing_notification_even_when_database_polling_could_work():
    with pytest.raises(RuntimeError, match="no changed notification"):
        await require_changed_notification(Subscription([]), timeout=.02)


async def test_canary_rejects_unexpected_channel_payload():
    with pytest.raises(RuntimeError, match="unexpected notification"):
        await require_changed_notification(Subscription([{"data": b"different"}]), timeout=.1)


@pytest.mark.parametrize("state", [{"status": "completed", "version_count": 7},
                                 {"status": "running", "version_count": 7}])
def test_canary_rejects_buffered_or_already_committed_sse(state):
    with pytest.raises(RuntimeError, match="while running"):
        require_live_prefix(state)


def test_canary_accepts_deltas_before_formal_versions_exist():
    require_live_prefix({"status": "running", "version_count": 0})
