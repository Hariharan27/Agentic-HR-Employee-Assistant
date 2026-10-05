"""Langfuse tracing is optional: a no-op without keys, nested observations with a client."""

from contextlib import contextmanager

from app.core import tracing
from test_chat_orchestration import FakeLLM, orchestrator, route


class FakeObservation:
    def __init__(self, log, name):
        self.log, self.name = log, name

    def update(self, **fields):
        self.log.append(("update", self.name, sorted(fields)))
        return self


class FakeLangfuse:
    def __init__(self):
        self.log = []

    def start_as_current_observation(self, *, name, as_type="span", **fields):
        log = self.log

        @contextmanager
        def manager():
            log.append(("start", name, as_type))
            yield FakeObservation(log, name)
            log.append(("end", name, as_type))

        return manager()


def test_tracing_is_a_no_op_without_keys():
    tracing.client.cache_clear()
    assert tracing.enabled() is False
    with tracing.observe("anything", as_type="tool", input={"a": 1}) as observation:
        observation.update(output="ignored")


def test_a_chat_produces_nested_route_agent_and_tool_observations(db_session, monkeypatch):
    fake = FakeLangfuse()
    monkeypatch.setattr(tracing, "client", lambda: fake)

    orchestrator(db_session, FakeLLM([route(leave_type="CASUAL")])).chat("traced", "What is my casual leave balance?")

    starts = [(name, kind) for event, name, kind in fake.log if event == "start"]
    assert ("route", "chain") in starts
    assert ("leave_agent", "agent") in starts
    assert ("tool.get_leave_balance", "tool") in starts
    assert [event for event, *_ in fake.log].count("start") == [event for event, *_ in fake.log].count("end")


def test_secrets_are_masked_before_export():
    masked = tracing._mask(data={"text": "Username: I26010\nTemporary password: Xy9-secret", "auth": "Bearer abc.def"})

    assert "Xy9-secret" not in str(masked) and "abc.def" not in str(masked)
    assert "Temporary password: [redacted]" in masked["text"]
