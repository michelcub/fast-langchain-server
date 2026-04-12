"""
Tests for the A2A (Agent-to-Agent) JSON-RPC 2.0 protocol.

Covers:
- Task data model serialisation
- LocalTaskManager lifecycle (submit, get, cancel)
- JSON-RPC dispatcher (SendMessage, GetTask, CancelTask, unknown method)
- A2A routes mounted on Server when a2a=True
- Autonomous submit (background task, budget-based)
- NullTaskManager no-ops
"""
from __future__ import annotations

import asyncio
import json
import pytest
from fastapi.testclient import TestClient

from fast_langchain_server.a2a import (
    AutonomousConfig,
    LocalTaskManager,
    NullTaskManager,
    Task,
    TaskBudgets,
    TaskState,
    TaskStatus,
    TERMINAL_STATES,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_METHOD_NOT_FOUND,
    JSONRPC_TASK_NOT_FOUND,
)
from fast_langchain_server.memory import LocalMemory
from fast_langchain_server.server import Server

from .conftest import _make_mock_agent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _echo_process_fn(text: str, session_id: str):
    """Minimal process_fn: echoes the input, reports 0 tool calls."""
    return f"echo: {text}", 0


async def _tool_process_fn(text: str, session_id: str):
    """process_fn that pretends to use 2 tool calls."""
    return f"result: {text}", 2


# ---------------------------------------------------------------------------
# Task data model
# ---------------------------------------------------------------------------


class TestTaskModel:
    def test_task_to_dict_structure(self):
        status = TaskStatus(state=TaskState.SUBMITTED)
        task = Task(id="t1", session_id="s1", status=status)
        d = task.to_dict()
        assert d["id"] == "t1"
        assert d["sessionId"] == "s1"
        assert d["status"]["state"] == "submitted"
        assert d["history"] == []
        assert d["autonomous"] is False

    def test_add_event(self):
        task = Task(id="t1", session_id="s1", status=TaskStatus(state=TaskState.SUBMITTED))
        ev = task.add_event("task.submitted", {"key": "value"})
        assert ev.type == "task.submitted"
        assert ev.data == {"key": "value"}
        assert len(task.events) == 1

    def test_terminal_states_set(self):
        assert TaskState.COMPLETED in TERMINAL_STATES
        assert TaskState.FAILED in TERMINAL_STATES
        assert TaskState.CANCELED in TERMINAL_STATES
        assert TaskState.WORKING not in TERMINAL_STATES


# ---------------------------------------------------------------------------
# LocalTaskManager
# ---------------------------------------------------------------------------


class TestLocalTaskManager:
    @pytest.mark.asyncio
    async def test_send_message_returns_completed_task(self):
        tm = LocalTaskManager(process_fn=_echo_process_fn)
        task = await tm.send_message("hello", session_id="s1")
        assert task.status.state == TaskState.COMPLETED
        assert any(m.role == "agent" for m in task.history)
        agent_reply = next(m.text for m in task.history if m.role == "agent")
        assert "echo: hello" in agent_reply

    @pytest.mark.asyncio
    async def test_get_task_returns_task(self):
        tm = LocalTaskManager(process_fn=_echo_process_fn)
        task = await tm.send_message("hi")
        fetched = await tm.get_task(task.id)
        assert fetched is not None
        assert fetched.id == task.id

    @pytest.mark.asyncio
    async def test_get_task_unknown_returns_none(self):
        tm = LocalTaskManager(process_fn=_echo_process_fn)
        assert await tm.get_task("nonexistent") is None

    @pytest.mark.asyncio
    async def test_cancel_running_task(self):
        """Cancel a task that is in SUBMITTED state (before execution starts)."""
        tm = LocalTaskManager(process_fn=_echo_process_fn)
        # Manually create a task without executing it so we can cancel it
        task = tm._create_task("s1", "test", None)
        assert task.status.state == TaskState.SUBMITTED
        # Transition to WORKING so cancel is valid
        tm._transition(task.id, TaskState.WORKING)
        canceled = await tm.cancel_task(task.id)
        assert canceled is True
        fetched = await tm.get_task(task.id)
        assert fetched.status.state == TaskState.CANCELED

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_task_returns_false(self):
        tm = LocalTaskManager(process_fn=_echo_process_fn)
        assert await tm.cancel_task("ghost") is False

    @pytest.mark.asyncio
    async def test_cancel_terminal_task_returns_false(self):
        tm = LocalTaskManager(process_fn=_echo_process_fn)
        task = await tm.send_message("done")
        assert task.status.state == TaskState.COMPLETED
        # Already terminal — cancel should fail
        assert await tm.cancel_task(task.id) is False

    @pytest.mark.asyncio
    async def test_failed_task_when_process_fn_raises(self):
        async def failing_fn(text, session_id):
            raise RuntimeError("boom")

        tm = LocalTaskManager(process_fn=failing_fn)
        task = await tm.send_message("oops")
        assert task.status.state == TaskState.FAILED
        assert "boom" in (task.status.message or "")

    @pytest.mark.asyncio
    async def test_eviction_when_max_tasks_reached(self):
        tm = LocalTaskManager(process_fn=_echo_process_fn, max_tasks=5)
        for _ in range(5):
            await tm.send_message("fill")
        # All 5 slots used with completed tasks
        assert len(tm._tasks) == 5
        # One more should trigger eviction and still succeed
        task = await tm.send_message("overflow")
        assert task.status.state == TaskState.COMPLETED
        assert len(tm._tasks) <= 5

    @pytest.mark.asyncio
    async def test_autonomous_submit_returns_immediately(self):
        """submit_autonomous should return before the loop finishes."""
        call_count = 0

        async def counting_fn(text, session_id):
            nonlocal call_count
            call_count += 1
            return f"iter {call_count}", 1 if call_count < 2 else 0  # stop after 2 iters

        tm = LocalTaskManager(process_fn=counting_fn)
        task = await tm.submit_autonomous(
            goal="do something",
            budgets=TaskBudgets(max_iterations=3, max_runtime_seconds=10),
        )
        # Returns immediately with WORKING state (background loop running)
        assert task.autonomous is True
        assert task.status.state == TaskState.WORKING

        # Wait for completion
        completed = await tm.wait_for_completion(task.id, timeout=5.0)
        assert completed is not None
        assert completed.status.state == TaskState.COMPLETED

    @pytest.mark.asyncio
    async def test_shutdown_cancels_running_tasks(self):
        async def slow_fn(text, session_id):
            await asyncio.sleep(60)
            return "done", 0

        tm = LocalTaskManager(process_fn=slow_fn)
        task = await tm.submit_autonomous(goal="slow goal")
        assert task.status.state == TaskState.WORKING
        # Shutdown should cancel cleanly without hanging
        await asyncio.wait_for(tm.shutdown(), timeout=2.0)


# ---------------------------------------------------------------------------
# NullTaskManager
# ---------------------------------------------------------------------------


class TestNullTaskManager:
    @pytest.mark.asyncio
    async def test_send_message_returns_completed(self):
        tm = NullTaskManager()
        task = await tm.send_message("hi")
        assert task.status.state == TaskState.COMPLETED

    @pytest.mark.asyncio
    async def test_get_task_returns_none(self):
        tm = NullTaskManager()
        assert await tm.get_task("any") is None

    @pytest.mark.asyncio
    async def test_cancel_returns_false(self):
        tm = NullTaskManager()
        assert await tm.cancel_task("any") is False

    @pytest.mark.asyncio
    async def test_submit_autonomous_returns_completed(self):
        tm = NullTaskManager()
        task = await tm.submit_autonomous(goal="go")
        assert task.status.state == TaskState.COMPLETED
        assert task.autonomous is True


# ---------------------------------------------------------------------------
# JSON-RPC endpoint via Server
# ---------------------------------------------------------------------------

_A2A_KWARGS = dict(
    agent_name="a2a-test",
    model_api_url="http://localhost:11434/v1",
    model_name="test",
    agent_port=8766,
    a2a=True,
)


def _a2a_server(response: str = "a2a response") -> tuple[Server, TestClient]:
    """Build a Server with A2A enabled and return (server, client)."""
    server = Server(agent=_make_mock_agent(response), memory=LocalMemory(), **_A2A_KWARGS)
    return server, TestClient(server.app)


def _rpc(client: TestClient, method: str, params: dict, rpc_id=1) -> dict:
    resp = client.post("/", json={"jsonrpc": "2.0", "method": method, "params": params, "id": rpc_id})
    assert resp.status_code == 200
    return resp.json()


class TestA2ARoutes:
    def test_a2a_endpoint_not_mounted_when_null_manager(self, client):
        """POST / should 404 when task_manager_type=none (NullTaskManager)."""
        resp = client.post("/", json={"jsonrpc": "2.0", "method": "SendMessage", "params": {}, "id": 1})
        assert resp.status_code == 404

    def test_agent_card_shows_a2a_false_when_null(self, client):
        card = client.get("/.well-known/agent.json").json()
        assert card["capabilities"]["a2a"] is False
        assert "jsonrpc" not in card["supportedProtocols"]

    def test_agent_card_shows_a2a_true_when_local(self):
        server = Server(
            agent=_make_mock_agent(),
            memory=LocalMemory(),
            agent_name="a2a-card-test",
            model_api_url="http://x",
            model_name="m",
            a2a=True,
        )
        c = TestClient(server.app)
        card = c.get("/.well-known/agent.json").json()
        assert card["capabilities"]["a2a"] is True
        assert "jsonrpc" in card["supportedProtocols"]

    def test_unknown_method_returns_method_not_found(self):
        server = Server(agent=_make_mock_agent(), memory=LocalMemory(), **_A2A_KWARGS)
        c = TestClient(server.app)
        body = _rpc(c, "UnknownMethod", {})
        assert body["error"]["code"] == JSONRPC_METHOD_NOT_FOUND

    def test_send_message_missing_message_returns_invalid_params(self):
        server = Server(agent=_make_mock_agent(), memory=LocalMemory(), **_A2A_KWARGS)
        c = TestClient(server.app)
        body = _rpc(c, "SendMessage", {})
        assert body["error"]["code"] == JSONRPC_INVALID_PARAMS

    def test_get_task_missing_id_returns_invalid_params(self):
        server = Server(agent=_make_mock_agent(), memory=LocalMemory(), **_A2A_KWARGS)
        c = TestClient(server.app)
        body = _rpc(c, "GetTask", {})
        assert body["error"]["code"] == JSONRPC_INVALID_PARAMS

    def test_get_task_unknown_id_returns_task_not_found(self):
        server = Server(agent=_make_mock_agent(), memory=LocalMemory(), **_A2A_KWARGS)
        c = TestClient(server.app)
        body = _rpc(c, "GetTask", {"id": "ghost_task"})
        assert body["error"]["code"] == JSONRPC_TASK_NOT_FOUND

    def test_send_message_and_get_task_full_flow(self):
        """SendMessage → GetTask round trip with A2A text message format."""
        server = Server(agent=_make_mock_agent(), memory=LocalMemory(), **_A2A_KWARGS)
        c = TestClient(server.app)

        send_resp = _rpc(c, "SendMessage", {
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": "What is 2+2?"}],
            },
            "contextId": "session-abc",
        })
        assert "error" not in send_resp
        task = send_resp["result"]
        assert task["status"]["state"] == "completed"
        assert task["sessionId"] == "session-abc"

        get_resp = _rpc(c, "GetTask", {"id": task["id"]})
        assert "error" not in get_resp
        assert get_resp["result"]["id"] == task["id"]

    def test_cancel_task_full_flow(self):
        """CancelTask on a terminal task should return the task dict (not error)."""
        server = Server(agent=_make_mock_agent(), memory=LocalMemory(), **_A2A_KWARGS)
        c = TestClient(server.app)

        send_resp = _rpc(c, "SendMessage", {
            "message": {"role": "user", "parts": [{"type": "text", "text": "hi"}]}
        })
        task_id = send_resp["result"]["id"]

        cancel_resp = _rpc(c, "CancelTask", {"id": task_id})
        assert "error" not in cancel_resp
        assert cancel_resp["result"]["id"] == task_id

    def test_parse_error_on_invalid_json_body(self):
        server = Server(agent=_make_mock_agent(), memory=LocalMemory(), **_A2A_KWARGS)
        c = TestClient(server.app)
        resp = c.post("/", content=b"not json", headers={"content-type": "application/json"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["error"]["code"] == -32700  # JSONRPC_PARSE_ERROR

    @pytest.mark.asyncio
    async def test_send_message_legacy_tasks_send_alias(self):
        """tasks/send should work as alias for SendMessage."""
        server = Server(agent=_make_mock_agent(), memory=LocalMemory(), **_A2A_KWARGS)
        c = TestClient(server.app)
        resp = _rpc(c, "tasks/send", {
            "message": {"role": "user", "parts": [{"type": "text", "text": "hello"}]}
        })
        assert "error" not in resp
        assert resp["result"]["status"]["state"] == "completed"
