"""
A2A (Agent-to-Agent) protocol for langchain-agent-server.

Provides:
- Task data model  (TaskState, TaskStatus, TaskMessage, TaskEvent, Task)
- Budget models    (TaskBudgets, AutonomousConfig)
- TaskManager ABC  (send_message, get_task, cancel_task, submit_autonomous)
- LocalTaskManager – in-process execution with OTel instrumentation
- NullTaskManager  – no-op (for disabling A2A without code changes)
- JSON-RPC 2.0 dispatcher  (SendMessage / GetTask / CancelTask)
- setup_a2a_routes()  – mounts the A2A endpoint on a FastAPI app

A2A spec compatibility
----------------------
Implements A2A RC v1.0 PascalCase methods plus legacy snake_case aliases:
  SendMessage / tasks/send
  GetTask     / tasks/get
  CancelTask  / tasks/cancel

The ``process_fn`` signature expected by LocalTaskManager::

    async def process_fn(text: str, session_id: str) -> tuple[str, int]:
        # returns (response_text, tool_call_count)
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, Union

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry import metrics, trace as trace_api
from pydantic import BaseModel

from fast_langchain_server.telemetry import SERVICE_NAME, get_current_trace_context, is_otel_enabled

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Task data model
# ---------------------------------------------------------------------------


class TaskState(str, Enum):
    """A2A task lifecycle states."""

    SUBMITTED = "submitted"
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    INPUT_REQUIRED = "input-required"


VALID_TRANSITIONS: Dict[TaskState, set] = {
    TaskState.SUBMITTED: {TaskState.WORKING, TaskState.CANCELED, TaskState.FAILED},
    TaskState.WORKING: {
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CANCELED,
        TaskState.INPUT_REQUIRED,
    },
    TaskState.INPUT_REQUIRED: {TaskState.WORKING, TaskState.CANCELED, TaskState.FAILED},
    TaskState.COMPLETED: set(),
    TaskState.FAILED: set(),
    TaskState.CANCELED: set(),
}

TERMINAL_STATES = {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED}

# Event type constants
EVENT_TASK_SUBMITTED = "task.submitted"
EVENT_TASK_WORKING = "task.working"
EVENT_TASK_COMPLETED = "task.completed"
EVENT_TASK_FAILED = "task.failed"
EVENT_TASK_CANCELED = "task.canceled"
EVENT_AUTONOMOUS_BUDGET_EXHAUSTED = "autonomous.budget.exhausted"


@dataclass
class TaskStatus:
    state: TaskState
    message: Optional[str] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "state": self.state.value,
            "timestamp": self.timestamp.isoformat(),
        }
        if self.message is not None:
            d["message"] = self.message
        return d


@dataclass
class TaskMessage:
    role: str  # "user" | "agent"
    text: str

    def to_dict(self) -> Dict[str, Any]:
        return {"role": self.role, "parts": [{"type": "text", "text": self.text}]}


@dataclass
class TaskEvent:
    id: str
    type: str
    timestamp: str  # ISO-8601 UTC
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "type": self.type, "timestamp": self.timestamp, "data": self.data}


@dataclass
class Task:
    id: str
    session_id: str
    status: TaskStatus
    history: List[TaskMessage] = field(default_factory=list)
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    events: List[TaskEvent] = field(default_factory=list)
    autonomous: bool = False
    output: str = ""

    def add_event(self, event_type: str, data: Optional[Dict[str, Any]] = None) -> TaskEvent:
        event = TaskEvent(
            id=uuid.uuid4().hex[:12],
            type=event_type,
            timestamp=datetime.now(timezone.utc).isoformat(),
            data=data or {},
        )
        self.events.append(event)
        return event

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "sessionId": self.session_id,
            "status": self.status.to_dict(),
            "history": [m.to_dict() for m in self.history],
            "artifacts": self.artifacts,
            "metadata": self.metadata,
            "events": [e.to_dict() for e in self.events],
            "autonomous": self.autonomous,
            "output": self.output,
        }


# ---------------------------------------------------------------------------
# Budget / autonomous config
# ---------------------------------------------------------------------------


@dataclass
class TaskBudgets:
    """Overall budget limits for async A2A task execution.  0 = unlimited."""

    max_iterations: int = 10
    max_runtime_seconds: int = 300
    max_tool_calls: int = 50
    interval_seconds: int = 0


@dataclass
class AutonomousConfig:
    """Per-iteration config for CRD-triggered autonomous loops.  0 = unlimited."""

    goal: str = ""
    interval_seconds: int = 0
    max_iter_runtime_seconds: int = 60


# ---------------------------------------------------------------------------
# OTel metrics (lazy init)
# ---------------------------------------------------------------------------

_task_counter: Optional[metrics.Counter] = None
_task_duration: Optional[metrics.Histogram] = None


def _get_task_metrics() -> Tuple[Optional[metrics.Counter], Optional[metrics.Histogram]]:
    global _task_counter, _task_duration
    if not is_otel_enabled():
        return None, None
    if _task_counter is None:
        meter = metrics.get_meter(SERVICE_NAME)
        _task_counter = meter.create_counter("fls.tasks", description="Task lifecycle events", unit="1")
        _task_duration = meter.create_histogram("fls.task.duration", description="Task execution duration", unit="ms")
    return _task_counter, _task_duration


# ---------------------------------------------------------------------------
# ProcessFn type alias
# ---------------------------------------------------------------------------

ProcessFn = Callable[[str, str], Awaitable[Tuple[str, int]]]


# ---------------------------------------------------------------------------
# TaskManager ABC
# ---------------------------------------------------------------------------


class TaskManager(ABC):
    """Abstract base for A2A task lifecycle management."""

    @abstractmethod
    async def send_message(
        self,
        text: str,
        session_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Task: ...

    @abstractmethod
    async def submit_autonomous(
        self,
        goal: str,
        session_id: Optional[str] = None,
        budgets: Optional[TaskBudgets] = None,
        autonomous_config: Optional[AutonomousConfig] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Task: ...

    @abstractmethod
    async def get_task(self, task_id: str) -> Optional[Task]: ...

    @abstractmethod
    async def cancel_task(self, task_id: str) -> bool: ...

    async def wait_for_completion(
        self, task_id: str, timeout: float = 60.0, poll_interval: float = 0.1
    ) -> Optional[Task]:
        """Poll until task reaches a terminal state or timeout expires."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            task = await self.get_task(task_id)
            if task and task.status.state in TERMINAL_STATES:
                return task
            await asyncio.sleep(poll_interval)
        return await self.get_task(task_id)

    async def shutdown(self) -> None:
        pass


# ---------------------------------------------------------------------------
# LocalTaskManager
# ---------------------------------------------------------------------------


class LocalTaskManager(TaskManager):
    """In-process task manager.

    Executes tasks via *process_fn* (the server's own agent run function) and
    tracks them in an in-process dict.  Includes OTel instrumentation for task
    and autonomous-loop spans/metrics.
    """

    def __init__(
        self,
        process_fn: ProcessFn,
        max_tasks: int = 10_000,
    ) -> None:
        self._process_fn = process_fn
        self._tasks: Dict[str, Task] = {}
        self._running: Dict[str, asyncio.Task] = {}
        self.max_tasks = max_tasks
        logger.info("LocalTaskManager ready (max_tasks=%d)", max_tasks)

    # ── Public interface ──────────────────────────────────────────────────────

    async def send_message(
        self,
        text: str,
        session_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Task:
        tracer = trace_api.get_tracer(SERVICE_NAME)
        task_counter, _ = _get_task_metrics()

        with tracer.start_as_current_span("fls.task.submit", attributes={"task.session_id": session_id or ""}):
            task = self._create_task(session_id, text, metadata)
            if task_counter:
                task_counter.add(1, {"state": "submitted"})

        await self._execute_task(task.id, text)
        return task

    async def submit_autonomous(
        self,
        goal: str,
        session_id: Optional[str] = None,
        budgets: Optional[TaskBudgets] = None,
        autonomous_config: Optional[AutonomousConfig] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Task:
        tracer = trace_api.get_tracer(SERVICE_NAME)
        task_counter, _ = _get_task_metrics()

        with tracer.start_as_current_span("fls.task.submit_autonomous", attributes={"task.autonomous": True}):
            task = self._create_task(session_id, goal, metadata)
            task.autonomous = True
            task.add_event(EVENT_TASK_SUBMITTED, {"goal_preview": goal[:200]})
            if task_counter:
                task_counter.add(1, {"state": "submitted", "autonomous": "true"})

        self._transition(task.id, TaskState.WORKING, "Autonomous execution started")
        task.add_event(EVENT_TASK_WORKING, {})

        bg = asyncio.create_task(
            self._execute_autonomous(
                task.id, goal, task.session_id,
                budgets=budgets or TaskBudgets(),
                autonomous_config=autonomous_config,
            )
        )
        self._running[task.id] = bg
        return task

    async def get_task(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    async def cancel_task(self, task_id: str) -> bool:
        tracer = trace_api.get_tracer(SERVICE_NAME)
        with tracer.start_as_current_span("fls.task.cancel", attributes={"task.id": task_id}):
            task = self._tasks.get(task_id)
            if not task or task.status.state in TERMINAL_STATES:
                return False
            if not self._transition(task_id, TaskState.CANCELED, "Canceled by request"):
                return False
            bg = self._running.pop(task_id, None)
            if bg and not bg.done():
                bg.cancel()
            return True

    async def shutdown(self) -> None:
        for bg in list(self._running.values()):
            if not bg.done():
                bg.cancel()
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
            self._running.clear()
        logger.debug("LocalTaskManager shutdown complete")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _create_task(
        self, session_id: Optional[str], input_message: Optional[str], metadata: Optional[Dict[str, Any]]
    ) -> Task:
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        session_id = session_id or f"session_{uuid.uuid4().hex[:12]}"
        task = Task(
            id=task_id,
            session_id=session_id,
            status=TaskStatus(state=TaskState.SUBMITTED),
            history=[TaskMessage(role="user", text=input_message)] if input_message else [],
            metadata=metadata or {},
        )
        self._evict_if_needed()
        self._tasks[task_id] = task
        return task

    def _transition(self, task_id: str, state: TaskState, message: Optional[str] = None) -> bool:
        task = self._tasks.get(task_id)
        if not task:
            return False
        current = task.status.state
        if state not in VALID_TRANSITIONS.get(current, set()):
            logger.warning("Invalid transition %s → %s for task %s", current.value, state.value, task_id)
            return False
        task.status = TaskStatus(state=state, message=message)
        return True

    async def _execute_task(self, task_id: str, text: str) -> None:
        tracer = trace_api.get_tracer(SERVICE_NAME)
        task_counter, task_duration = _get_task_metrics()
        t0 = time.perf_counter()

        with tracer.start_as_current_span("fls.task.execute", attributes={"task.id": task_id}) as span:
            task = self._tasks.get(task_id)
            if not task or not self._transition(task_id, TaskState.WORKING, "Processing"):
                return
            try:
                response_text, _ = await self._process_fn(text, task.session_id)
                task.history.append(TaskMessage(role="agent", text=response_text))
                self._transition(task_id, TaskState.COMPLETED, "Done")
                span.set_attribute("task.state", "completed")
                if task_counter:
                    task_counter.add(1, {"state": "completed"})
            except Exception as exc:
                logger.error("Task %s failed: %s", task_id, exc)
                self._transition(task_id, TaskState.FAILED, str(exc))
                span.set_attribute("task.state", "failed")
                span.record_exception(exc)
                if task_counter:
                    task_counter.add(1, {"state": "failed"})
            finally:
                if task_duration:
                    task_duration.record((time.perf_counter() - t0) * 1000, {"task.id": task_id})

    async def _execute_autonomous(
        self,
        task_id: str,
        goal: str,
        session_id: str,
        budgets: TaskBudgets,
        autonomous_config: Optional[AutonomousConfig],
    ) -> None:
        tracer = trace_api.get_tracer(SERVICE_NAME)
        task_counter, task_duration = _get_task_metrics()
        t0 = time.perf_counter()
        is_auto = autonomous_config is not None
        interval = autonomous_config.interval_seconds if autonomous_config else budgets.interval_seconds

        attrs = {"autonomous.task_id": task_id, "autonomous.is_autonomous": is_auto}
        if not is_auto:
            attrs.update({
                "autonomous.max_iterations": budgets.max_iterations,
                "autonomous.max_runtime_seconds": budgets.max_runtime_seconds,
                "autonomous.max_tool_calls": budgets.max_tool_calls,
            })

        with tracer.start_as_current_span("fls.autonomous.run", attributes=attrs) as span:
            task = self._tasks.get(task_id)
            if not task:
                return
            try:
                iteration = 0
                total_tool_calls = 0
                loop_start = time.monotonic()
                last_response = ""

                while True:
                    # Stop if task was externally canceled
                    current = self._tasks.get(task_id)
                    if current and current.status.state in TERMINAL_STATES:
                        break

                    # Budget checks (only in async-task mode, not autonomous)
                    if not is_auto:
                        if budgets.max_iterations > 0 and iteration >= budgets.max_iterations:
                            msg = f"Budget exhausted: max_iterations ({budgets.max_iterations})"
                            task.add_event(EVENT_AUTONOMOUS_BUDGET_EXHAUSTED, {"reason": "max_iterations"})
                            last_response = msg
                            break
                        elapsed = time.monotonic() - loop_start
                        if budgets.max_runtime_seconds > 0 and elapsed >= budgets.max_runtime_seconds:
                            msg = f"Budget exhausted: max_runtime_seconds ({budgets.max_runtime_seconds}s)"
                            task.add_event(EVENT_AUTONOMOUS_BUDGET_EXHAUSTED, {"reason": "max_runtime_seconds"})
                            last_response = msg
                            break
                        if budgets.max_tool_calls > 0 and total_tool_calls >= budgets.max_tool_calls:
                            msg = f"Budget exhausted: max_tool_calls ({budgets.max_tool_calls})"
                            task.add_event(EVENT_AUTONOMOUS_BUDGET_EXHAUSTED, {"reason": "max_tool_calls"})
                            last_response = msg
                            break

                    # Build iteration message
                    if iteration == 0:
                        message = goal
                    elif is_auto:
                        message = (
                            f"Continue working toward the goal. Iteration {iteration + 1}. "
                            "Review progress and decide next steps."
                        )
                    else:
                        message = (
                            f"Continue working toward the goal. Iteration {iteration + 1}. "
                            "If the goal is fully achieved, respond without making tool calls."
                        )

                    with tracer.start_as_current_span("fls.autonomous.iteration", attributes={"iteration": iteration}):
                        try:
                            iter_timeout = (
                                autonomous_config.max_iter_runtime_seconds
                                if is_auto and autonomous_config and autonomous_config.max_iter_runtime_seconds > 0
                                else 0
                            )
                            if iter_timeout > 0:
                                last_response, tool_call_count = await asyncio.wait_for(
                                    self._process_fn(message, session_id), timeout=iter_timeout
                                )
                            else:
                                last_response, tool_call_count = await self._process_fn(message, session_id)
                        except Exception as iter_err:
                            if is_auto:
                                logger.warning("Autonomous iteration %d failed: %s — continuing", iteration, iter_err)
                                iteration += 1
                                if interval > 0:
                                    await asyncio.sleep(interval)
                                continue
                            raise

                    if tool_call_count > 0:
                        total_tool_calls += tool_call_count
                    iteration += 1

                    # Completion for async-task mode: no tool calls = done
                    if not is_auto and tool_call_count == 0:
                        break

                    if interval > 0:
                        await asyncio.sleep(interval)

                task.output = last_response
                task.history.append(TaskMessage(role="agent", text=last_response))
                cur = self._tasks.get(task_id)
                if cur and cur.status.state not in TERMINAL_STATES:
                    self._transition(task_id, TaskState.COMPLETED, "Done")
                    task.add_event(EVENT_TASK_COMPLETED, {"output_preview": last_response[:200]})
                    span.set_attribute("task.state", "completed")
                    if task_counter:
                        task_counter.add(1, {"state": "completed", "autonomous": "true"})

            except asyncio.CancelledError:
                self._transition(task_id, TaskState.CANCELED, "Canceled")
                task.add_event(EVENT_TASK_CANCELED, {})
                span.set_attribute("task.state", "canceled")

            except Exception as exc:
                logger.error("Autonomous task %s failed: %s", task_id, exc)
                self._transition(task_id, TaskState.FAILED, str(exc))
                task.add_event(EVENT_TASK_FAILED, {"error": str(exc)})
                span.set_attribute("task.state", "failed")
                span.record_exception(exc)
                if task_counter:
                    task_counter.add(1, {"state": "failed", "autonomous": "true"})

            finally:
                self._running.pop(task_id, None)
                if task_duration:
                    task_duration.record(
                        (time.perf_counter() - t0) * 1000,
                        {"task.id": task_id, "autonomous": str(is_auto)},
                    )

    def _evict_if_needed(self) -> None:
        if len(self._tasks) < self.max_tasks:
            return
        terminal = [
            (tid, t) for tid, t in self._tasks.items() if t.status.state in TERMINAL_STATES
        ]
        terminal.sort(key=lambda x: x[1].status.timestamp)
        for tid, _ in terminal[: max(1, self.max_tasks // 10)]:
            del self._tasks[tid]


# ---------------------------------------------------------------------------
# NullTaskManager
# ---------------------------------------------------------------------------


class NullTaskManager(TaskManager):
    """No-op task manager — A2A endpoints respond successfully but do nothing."""

    async def send_message(self, text: str, session_id: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> Task:
        return Task(id=f"null_{uuid.uuid4().hex[:8]}", session_id=session_id or "null", status=TaskStatus(state=TaskState.COMPLETED))

    async def submit_autonomous(self, goal: str, session_id: Optional[str] = None, budgets: Optional[TaskBudgets] = None, autonomous_config: Optional[AutonomousConfig] = None, metadata: Optional[Dict[str, Any]] = None) -> Task:
        return Task(id=f"null_{uuid.uuid4().hex[:8]}", session_id=session_id or "null", status=TaskStatus(state=TaskState.COMPLETED), autonomous=True)

    async def get_task(self, task_id: str) -> Optional[Task]:
        return None

    async def cancel_task(self, task_id: str) -> bool:
        return False


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 models & error codes
# ---------------------------------------------------------------------------


class JsonRpcRequest(BaseModel):
    jsonrpc: str = "2.0"
    method: str
    params: Optional[Dict[str, Any]] = None
    id: Optional[Union[str, int]] = None


class JsonRpcError(BaseModel):
    code: int
    message: str
    data: Optional[Any] = None


class JsonRpcResponse(BaseModel):
    jsonrpc: str = "2.0"
    result: Optional[Any] = None
    error: Optional[JsonRpcError] = None
    id: Optional[Union[str, int]] = None

    def to_dict(self) -> dict:
        d: dict = {"jsonrpc": self.jsonrpc, "id": self.id}
        if self.error is not None:
            d["error"] = self.error.model_dump()
        else:
            d["result"] = self.result
        return d


JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603
JSONRPC_TASK_NOT_FOUND = -32001


# ---------------------------------------------------------------------------
# JSON-RPC dispatcher
# ---------------------------------------------------------------------------


async def _handle_jsonrpc(request: Request, task_manager: TaskManager) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(JsonRpcResponse(error=JsonRpcError(code=JSONRPC_PARSE_ERROR, message="Parse error")).to_dict())

    try:
        rpc = JsonRpcRequest(**body)
    except Exception:
        return JSONResponse(JsonRpcResponse(error=JsonRpcError(code=JSONRPC_INVALID_REQUEST, message="Invalid JSON-RPC request")).to_dict())

    params = rpc.params or {}
    rpc_id = rpc.id

    if rpc.method in ("SendMessage", "tasks/send"):
        return await _rpc_send_message(task_manager, params, rpc_id)
    elif rpc.method in ("GetTask", "tasks/get"):
        return await _rpc_get_task(task_manager, params, rpc_id)
    elif rpc.method in ("CancelTask", "tasks/cancel"):
        return await _rpc_cancel_task(task_manager, params, rpc_id)
    else:
        return JSONResponse(JsonRpcResponse(id=rpc_id, error=JsonRpcError(code=JSONRPC_METHOD_NOT_FOUND, message=f"Method not found: {rpc.method}")).to_dict())


async def _rpc_send_message(task_manager: TaskManager, params: Dict[str, Any], rpc_id) -> JSONResponse:
    message = params.get("message")
    if not message:
        return JSONResponse(JsonRpcResponse(id=rpc_id, error=JsonRpcError(code=JSONRPC_INVALID_PARAMS, message="Missing 'message'")).to_dict())

    parts = message.get("parts", [])
    text_parts = [p.get("text", "") for p in parts if p.get("type") == "text"]
    input_text = " ".join(text_parts) if text_parts else message.get("text", "")
    if not input_text:
        return JSONResponse(JsonRpcResponse(id=rpc_id, error=JsonRpcError(code=JSONRPC_INVALID_PARAMS, message="Message must contain text")).to_dict())

    session_id = params.get("contextId") or params.get("sessionId")
    msg_meta = message.get("metadata")
    task_meta = dict(msg_meta) if isinstance(msg_meta, dict) else None

    config = params.get("configuration", {})
    mode = config.get("mode", "interactive")

    if mode == "autonomous":
        br = config.get("budgets", {})
        budgets = TaskBudgets(
            max_iterations=br.get("maxIterations", 10),
            max_runtime_seconds=br.get("maxRuntimeSeconds", 300),
            max_tool_calls=br.get("maxToolCalls", 50),
            interval_seconds=br.get("intervalSeconds", 0),
        )
        task = await task_manager.submit_autonomous(goal=input_text, session_id=session_id, budgets=budgets, metadata=task_meta)
    else:
        task = await task_manager.send_message(input_text, session_id=session_id, metadata=task_meta)

    return JSONResponse(JsonRpcResponse(id=rpc_id, result=task.to_dict()).to_dict())


async def _rpc_get_task(task_manager: TaskManager, params: Dict[str, Any], rpc_id) -> JSONResponse:
    task_id = params.get("id")
    if not task_id:
        return JSONResponse(JsonRpcResponse(id=rpc_id, error=JsonRpcError(code=JSONRPC_INVALID_PARAMS, message="Missing 'id'")).to_dict())
    task = await task_manager.get_task(task_id)
    if not task:
        return JSONResponse(JsonRpcResponse(id=rpc_id, error=JsonRpcError(code=JSONRPC_TASK_NOT_FOUND, message=f"Task not found: {task_id}")).to_dict())
    return JSONResponse(JsonRpcResponse(id=rpc_id, result=task.to_dict()).to_dict())


async def _rpc_cancel_task(task_manager: TaskManager, params: Dict[str, Any], rpc_id) -> JSONResponse:
    task_id = params.get("id")
    if not task_id:
        return JSONResponse(JsonRpcResponse(id=rpc_id, error=JsonRpcError(code=JSONRPC_INVALID_PARAMS, message="Missing 'id'")).to_dict())
    canceled = await task_manager.cancel_task(task_id)
    task = await task_manager.get_task(task_id)
    if not canceled and not task:
        return JSONResponse(JsonRpcResponse(id=rpc_id, error=JsonRpcError(code=JSONRPC_TASK_NOT_FOUND, message=f"Task not found: {task_id}")).to_dict())
    return JSONResponse(JsonRpcResponse(id=rpc_id, result=task.to_dict() if task else None).to_dict())


# ---------------------------------------------------------------------------
# Route setup
# ---------------------------------------------------------------------------


def setup_a2a_routes(app: FastAPI, task_manager: TaskManager) -> None:
    """Mount the A2A JSON-RPC 2.0 endpoint on *app* at ``POST /``."""

    @app.post("/")
    async def a2a_jsonrpc(request: Request):
        """A2A JSON-RPC 2.0 — SendMessage / GetTask / CancelTask."""
        return await _handle_jsonrpc(request, task_manager)
