"""Error handling tests: cycles, failures, propagation, cancellation."""
from __future__ import annotations

import time
from typing import Any

import pytest

from kimix.dag import (
    DAG,
    Context,
    CycleError,
    DAGValidationError,
    DependencyError,
    ExecutionError,
    Executor,
    TaskNode,
)

from tests.test_dag.conftest import failing_task, async_fail, make_adder


def make_executor(max_workers: int | None = None) -> Executor:
    return Executor(max_workers=max_workers)


# ============================================================================
# Cycle detection
# ============================================================================
class TestCycleDetection:
    def test_cycle_raises_before_execution(self) -> None:
        dag = DAG()
        dag.add_node(TaskNode("a", lambda c: 1, dependencies={"b"}))
        dag.add_node(TaskNode("b", lambda c: 1, dependencies={"a"}))
        exe = make_executor()
        with pytest.raises(CycleError):
            exe.execute(dag)

    def test_self_loop_raises(self) -> None:
        dag = DAG()
        dag.add_node(TaskNode("a", lambda c: 1, dependencies={"a"}))
        exe = make_executor()
        with pytest.raises(CycleError, match="Self-reference"):
            exe.execute(dag)

    def test_long_cycle(self) -> None:
        dag = DAG()
        dag.add_node(TaskNode("a", lambda c: 1, dependencies={"d"}))
        dag.add_node(TaskNode("b", lambda c: 1, dependencies={"a"}))
        dag.add_node(TaskNode("c", lambda c: 1, dependencies={"b"}))
        dag.add_node(TaskNode("d", lambda c: 1, dependencies={"c"}))
        with pytest.raises(CycleError):
            dag.validate()


# ============================================================================
# Task failure propagation
# ============================================================================
class TestFailurePropagation:
    def test_single_failure(self) -> None:
        dag = DAG()
        dag.add_node(TaskNode("a", failing_task))
        exe = make_executor()
        with pytest.raises(ExecutionError) as exc_info:
            exe.execute(dag)
        assert "a" in exc_info.value.errors
        assert isinstance(exc_info.value.errors["a"], ValueError)

    def test_failure_blocks_downstream(self) -> None:
        dag = DAG()
        dag.add_node(TaskNode("root", failing_task))
        dag.add_node(TaskNode("child", make_adder("x", 1), dependencies={"root"}))
        exe = make_executor()
        with pytest.raises(ExecutionError) as exc_info:
            exe.execute(dag)
        assert "root" in exc_info.value.errors
        assert "child" in exc_info.value.errors
        assert isinstance(exc_info.value.errors["child"], DependencyError)

    def test_failure_in_one_branch_does_not_kill_other(self) -> None:
        """
          root
         /    \
       fail   ok
        """
        dag = DAG()
        dag.add_node(TaskNode("root", lambda c: 1))
        dag.add_node(TaskNode("fail", failing_task, dependencies={"root"}))
        dag.add_node(TaskNode("ok", make_adder("x", 1), dependencies={"root"}))
        exe = make_executor()
        with pytest.raises(ExecutionError) as exc_info:
            exe.execute(dag)
        assert "fail" in exc_info.value.errors
        assert isinstance(exc_info.value.errors["fail"], ValueError)
        # "ok" should have succeeded, so it won't be in errors
        # (but since ExecutionError is raised we don't get normal results)
        # However, the node itself should be done with no error.
        assert dag.get_node("ok").done
        assert dag.get_node("ok").error is None

    def test_async_failure(self) -> None:
        dag = DAG()
        dag.add_node(TaskNode("a", async_fail))
        exe = make_executor()
        with pytest.raises(ExecutionError) as exc_info:
            exe.execute(dag)
        assert isinstance(exc_info.value.errors["a"], RuntimeError)

    def test_partial_failure_in_diamond(self) -> None:
        r"""
          a
         / \
        b  fail
         \ /
          d
        d should get DependencyError because 'fail' fails.
        """
        dag = DAG()
        dag.add_node(TaskNode("a", lambda c: 1))
        dag.add_node(TaskNode("b", make_adder("x", 1), dependencies={"a"}))
        dag.add_node(TaskNode("fail", failing_task, dependencies={"a"}))
        dag.add_node(TaskNode("d", make_adder("x", 10), dependencies={"b", "fail"}))
        exe = make_executor()
        with pytest.raises(ExecutionError) as exc_info:
            exe.execute(dag)
        assert "fail" in exc_info.value.errors
        assert "d" in exc_info.value.errors
        assert isinstance(exc_info.value.errors["d"], DependencyError)
        # b should have succeeded
        assert dag.get_node("b").done
        assert dag.get_node("b").error is None


# ============================================================================
# Cancellation
# ============================================================================
class TestCancellation:
    def test_pre_cancelled_context(self) -> None:
        dag = DAG()
        dag.add_node(TaskNode("a", lambda c: 1))
        ctx = Context()
        ctx.cancel()
        exe = make_executor()
        with pytest.raises(ExecutionError) as exc_info:
            exe.execute(dag, ctx)
        assert "a" in exc_info.value.errors
        assert isinstance(exc_info.value.errors["a"], RuntimeError)

    def test_cancel_does_not_hang(self) -> None:
        dag = DAG()
        dag.add_node(TaskNode("a", make_adder("x", 1)))
        ctx = Context()
        ctx.cancel()
        exe = make_executor()
        with pytest.raises(ExecutionError):
            exe.execute(dag, ctx)


# ============================================================================
# Retry exhaustion
# ============================================================================
class TestRetryExhaustion:
    def test_all_retries_fail(self) -> None:
        def always_fails(_c: Context) -> None:
            raise RuntimeError("nope")
        dag = DAG()
        dag.add_node(TaskNode("a", always_fails, retries=3))
        exe = make_executor()
        with pytest.raises(ExecutionError) as exc_info:
            exe.execute(dag)
        assert isinstance(exc_info.value.errors["a"], RuntimeError)


# ============================================================================
# ExecutionError structure
# ============================================================================
class TestExecutionErrorStructure:
    def test_errors_dict_populated(self) -> None:
        dag = DAG()
        dag.add_node(TaskNode("a", lambda c: (_ for _ in ()).throw(ValueError("v"))))
        dag.add_node(TaskNode("b", lambda c: (_ for _ in ()).throw(TypeError("t"))))
        exe = make_executor()
        with pytest.raises(ExecutionError) as exc_info:
            exe.execute(dag)
        err = exc_info.value
        assert set(err.errors.keys()) == {"a", "b"}
        assert isinstance(err.errors["a"], ValueError)
        assert isinstance(err.errors["b"], TypeError)
        assert "failed" in str(err)
