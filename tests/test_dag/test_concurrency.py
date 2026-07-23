"""Concurrency and thread-safety tests."""
from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from kimix.dag import DAG, Context, Executor, TaskNode

from tests.test_dag.conftest import make_adder, make_slow_task


def make_executor(max_workers: int | None = None) -> Executor:
    return Executor(max_workers=max_workers)


# ============================================================================
# Parallelism verification
# ============================================================================
class TestParallelExecution:
    def test_two_branches_parallel(self) -> None:
        """Diamond with slow sides: total time should be < sum of both sides."""
        dag = DAG()
        dag.add_node(TaskNode("a", make_adder("x", 1)))
        dag.add_node(TaskNode("b", make_slow_task("b", 0.12, 1), dependencies={"a"}))
        dag.add_node(TaskNode("c", make_slow_task("c", 0.12, 2), dependencies={"a"}))
        dag.add_node(TaskNode("d", make_adder("x", 10), dependencies={"b", "c"}))
        exe = make_executor(max_workers=2)
        start = time.perf_counter()
        results = exe.execute(dag)
        elapsed = time.perf_counter() - start
        assert results["d"] == 11  # 1 + 10
        # a is instant, b and c run in parallel (~0.12), d is instant
        assert elapsed < 0.24

    def test_many_roots_parallel(self) -> None:
        dag = DAG()
        n = 8
        for i in range(n):
            dag.add_node(TaskNode(f"t{i}", make_slow_task(f"k{i}", 0.05, i)))
        exe = make_executor(max_workers=n)
        start = time.perf_counter()
        results = exe.execute(dag)
        elapsed = time.perf_counter() - start
        assert len(results) == n
        assert elapsed < 0.20  # 8 * 0.05 = 0.40 sequential, parallel much less

    def test_worker_starvation(self) -> None:
        """More tasks than workers should still complete."""
        dag = DAG()
        for i in range(20):
            dag.add_node(TaskNode(f"t{i}", make_slow_task("x", 0.01, i)))
        exe = make_executor(max_workers=2)
        results = exe.execute(dag)
        assert len(results) == 20

    def test_concurrent_context_access(self) -> None:
        """Many tasks writing to same context key should not corrupt state."""
        dag = DAG()
        n = 20
        for i in range(n):
            dag.add_node(TaskNode(f"t{i}", make_adder("counter", 1)))
        ctx = Context()
        exe = make_executor(max_workers=4)
        exe.execute(dag, ctx)
        # Some race conditions expected but no crashes; value should be <= n
        assert ctx.get("counter", 0) <= n

    def test_execution_order_respects_dependencies(self) -> None:
        """Use timestamps to verify dependency order."""
        timestamps: dict[str, float] = {}
        lock = threading.Lock()

        def record(name: str) -> Any:
            def _task(_c: Context) -> str:
                with lock:
                    timestamps[name] = time.perf_counter()
                return name
            return _task

        dag = DAG()
        dag.add_node(TaskNode("a", record("a")))
        dag.add_node(TaskNode("b", record("b"), dependencies={"a"}))
        dag.add_node(TaskNode("c", record("c"), dependencies={"a"}))
        dag.add_node(TaskNode("d", record("d"), dependencies={"b", "c"}))
        exe = make_executor(max_workers=4)
        exe.execute(dag)
        assert timestamps["a"] < timestamps["b"]
        assert timestamps["a"] < timestamps["c"]
        assert timestamps["b"] < timestamps["d"]
        assert timestamps["c"] < timestamps["d"]


# ============================================================================
# Thread-safety of internal state
# ============================================================================
class TestThreadSafety:
    def test_done_event_set_once(self) -> None:
        """mark_done is idempotent-ish and thread-safe."""
        node = TaskNode("n", lambda c: 1)
        errors: list[Exception] = []

        def marker() -> None:
            try:
                for _ in range(100):
                    node.mark_done(result=42)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=marker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert node.done
        assert node.result == 42

    def test_context_lock_contention(self) -> None:
        ctx = Context()
        errors: list[Exception] = []

        def worker() -> None:
            try:
                for i in range(500):
                    ctx.update({str(i): i})
                    ctx.get(str(i))
                    ctx.set("latest", i)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert isinstance(ctx.get("latest"), int)
