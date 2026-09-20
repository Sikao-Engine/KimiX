"""Unit tests for Context, TaskNode, DAG, TopologicalSorter, and utilities."""
from __future__ import annotations

import threading

import pytest
from kimix.dag import (
    DAG,
    Context,
    CycleError,
    DAGValidationError,
    TaskNode,
    TopologicalSorter,
    detect_cycle,
    validate_dag,
)


# ============================================================================
# Context
# ============================================================================
class TestContext:
    def test_init_empty(self) -> None:
        ctx = Context()
        assert ctx.get("missing") is None
        assert not ctx.cancelled

    def test_get_set(self) -> None:
        ctx = Context()
        ctx.set("k", 42)
        assert ctx.get("k") == 42
        assert ctx.get("missing", "default") == "default"

    def test_update(self) -> None:
        ctx = Context()
        ctx.update({"a": 1, "b": 2})
        assert ctx.get("a") == 1
        assert ctx.get("b") == 2

    def test_cancel(self) -> None:
        ctx = Context()
        ctx.cancel()
        assert ctx.cancelled
        with pytest.raises(RuntimeError, match="cancelled"):
            ctx.check_cancelled()

    def test_thread_safety(self) -> None:
        ctx = Context()
        errors: list[Exception] = []

        def writer() -> None:
            try:
                for i in range(1000):
                    ctx.set("counter", i)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert isinstance(ctx.get("counter"), int)


# ============================================================================
# TaskNode
# ============================================================================
class TestTaskNode:
    def test_defaults(self) -> None:
        node = TaskNode("n", lambda c: 1)
        assert node.name == "n"
        assert node.dependencies == set()
        assert node.retries == 0
        assert not node.done

    def test_dependencies_as_list(self) -> None:
        node = TaskNode("n", lambda c: 1, dependencies=["a", "b"])
        assert node.dependencies == {"a", "b"}

    def test_retries_clamped_negative(self) -> None:
        node = TaskNode("n", lambda c: 1, retries=-5)
        assert node.retries == 0

    def test_sync_execute(self, ctx: Context) -> None:
        node = TaskNode("n", lambda c: c.get("x", 0) + 1)
        result = node.execute(ctx)
        assert result == 1

    def test_execute_with_params(self, ctx: Context) -> None:
        node = TaskNode("n", lambda c: c.get("x", 0) + 5)
        result = node.execute(ctx)
        assert result == 5

    def test_async_execute(self, ctx: Context) -> None:
        async def _task(c: Context) -> int:
            return 42
        node = TaskNode("n", _task)
        result = node.execute(ctx)
        assert result == 42

    def test_retry_success(self, ctx: Context) -> None:
        calls = 0
        def _task(_c: Context) -> int:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise RuntimeError("fail")
            return calls
        node = TaskNode("n", _task, retries=3)
        result = node.execute(ctx)
        assert result == 3
        assert calls == 3

    def test_retry_exhausted(self, ctx: Context) -> None:
        def _task(_c: Context) -> None:
            raise RuntimeError("always fails")
        node = TaskNode("n", _task, retries=2)
        with pytest.raises(RuntimeError, match="always fails"):
            node.execute(ctx)

    def test_mark_done(self) -> None:
        node = TaskNode("n", lambda c: 1)
        node.mark_done(result=123)
        assert node.done
        assert node.result == 123
        assert node.error is None

    def test_mark_done_with_error(self) -> None:
        node = TaskNode("n", lambda c: 1)
        exc = ValueError("oops")
        node.mark_done(error=exc)
        assert node.done
        assert node.result is None
        assert node.error is exc

    def test_repr(self) -> None:
        node = TaskNode("foo", lambda c: 1, dependencies={"bar"})
        assert "foo" in repr(node)
        assert "bar" in repr(node)

    def test_execute_checks_cancel(self, ctx: Context) -> None:
        ctx.cancel()
        node = TaskNode("n", lambda c: 1)
        with pytest.raises(RuntimeError, match="cancelled"):
            node.execute(ctx)


# ============================================================================
# DAG
# ============================================================================
class TestDAG:
    def test_empty(self, empty_dag: DAG) -> None:
        assert len(empty_dag) == 0
        assert empty_dag.nodes == {}
        assert empty_dag.edges == {}

    def test_add_node(self, empty_dag: DAG) -> None:
        node = TaskNode("a", lambda c: 1)
        empty_dag.add_node(node)
        assert len(empty_dag) == 1
        assert "a" in empty_dag
        assert empty_dag.get_node("a") is node

    def test_add_duplicate_raises(self, empty_dag: DAG) -> None:
        empty_dag.add_node(TaskNode("a", lambda c: 1))
        with pytest.raises(DAGValidationError, match="already exists"):
            empty_dag.add_node(TaskNode("a", lambda c: 2))

    def test_add_edge(self, empty_dag: DAG) -> None:
        empty_dag.add_node(TaskNode("a", lambda c: 1))
        empty_dag.add_node(TaskNode("b", lambda c: 2))
        empty_dag.add_edge("a", "b")
        assert empty_dag.edges["b"] == {"a"}
        assert empty_dag.get_node("b").dependencies == {"a"}

    def test_add_edge_missing_upstream(self, empty_dag: DAG) -> None:
        empty_dag.add_node(TaskNode("b", lambda c: 1))
        with pytest.raises(KeyError, match="Upstream"):
            empty_dag.add_edge("a", "b")

    def test_add_edge_missing_downstream(self, empty_dag: DAG) -> None:
        empty_dag.add_node(TaskNode("a", lambda c: 1))
        with pytest.raises(KeyError, match="Downstream"):
            empty_dag.add_edge("a", "b")

    def test_get_node_missing(self, empty_dag: DAG) -> None:
        with pytest.raises(KeyError, match="not found"):
            empty_dag.get_node("x")

    def test_validate_empty(self, empty_dag: DAG) -> None:
        empty_dag.validate()  # should not raise

    def test_validate_cycle(self, empty_dag: DAG) -> None:
        empty_dag.add_node(TaskNode("a", lambda c: 1, dependencies={"b"}))
        empty_dag.add_node(TaskNode("b", lambda c: 1, dependencies={"a"}))
        with pytest.raises(CycleError):
            empty_dag.validate()

    def test_validate_self_reference(self, empty_dag: DAG) -> None:
        empty_dag.add_node(TaskNode("a", lambda c: 1, dependencies={"a"}))
        with pytest.raises(CycleError, match="Self-reference"):
            empty_dag.validate()

    def test_repr(self, empty_dag: DAG) -> None:
        empty_dag.add_node(TaskNode("a", lambda c: 1))
        r = repr(empty_dag)
        assert "a" in r


# ============================================================================
# TopologicalSorter
# ============================================================================
class TestTopologicalSorter:
    def test_empty(self) -> None:
        ts = TopologicalSorter({})
        assert ts.sort() == []

    def test_linear(self) -> None:
        ts = TopologicalSorter({"a": set(), "b": {"a"}, "c": {"b"}})
        order = ts.sort()
        assert order.index("a") < order.index("b") < order.index("c")

    def test_diamond(self) -> None:
        ts = TopologicalSorter({"a": set(), "b": {"a"}, "c": {"a"}, "d": {"b", "c"}})
        order = ts.sort()
        assert order.index("a") < order.index("b")
        assert order.index("a") < order.index("c")
        assert order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")

    def test_cycle_detection(self) -> None:
        ts = TopologicalSorter({"a": {"b"}, "b": {"a"}})
        with pytest.raises(CycleError):
            ts.sort()

    def test_isolated_nodes(self) -> None:
        ts = TopologicalSorter({"a": set(), "b": set(), "c": set()})
        order = ts.sort()
        assert set(order) == {"a", "b", "c"}

    def test_complex_graph(self) -> None:
        edges = {
            "a": set(),
            "b": {"a"},
            "c": {"a"},
            "d": {"b", "c"},
            "e": {"d"},
            "f": set(),
        }
        ts = TopologicalSorter(edges)
        order = ts.sort()
        assert order.index("a") < order.index("b")
        assert order.index("a") < order.index("c")
        assert order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")
        assert order.index("d") < order.index("e")
        assert "f" in order


# ============================================================================
# Utilities
# ============================================================================
class TestDetectCycle:
    def test_no_cycle(self) -> None:
        graph = {"a": set(), "b": {"a"}}
        assert detect_cycle(graph) is None

    def test_simple_cycle(self) -> None:
        graph = {"a": {"b"}, "b": {"a"}}
        cycle = detect_cycle(graph)
        assert cycle is not None
        assert cycle[0] == cycle[-1]

    def test_self_loop(self) -> None:
        graph = {"a": {"a"}}
        cycle = detect_cycle(graph)
        assert cycle is not None
        assert "a" in cycle

    def test_no_nodes(self) -> None:
        assert detect_cycle({}) is None

    def test_cycle_with_extra_nodes(self) -> None:
        graph = {"a": {"b"}, "b": {"c"}, "c": {"b"}, "d": set()}
        cycle = detect_cycle(graph)
        assert cycle is not None
        assert "b" in cycle and "c" in cycle


class TestValidateDag:
    def test_valid(self) -> None:
        nodes = {"a": None, "b": None}
        edges = {"a": set(), "b": {"a"}}
        validate_dag(nodes, edges)  # no raise

    def test_unknown_node_in_edge(self) -> None:
        nodes = {"a": None}
        edges = {"a": {"b"}}
        with pytest.raises(DAGValidationError, match="unknown node"):
            validate_dag(nodes, edges)

    def test_self_reference(self) -> None:
        nodes = {"a": None}
        edges = {"a": {"a"}}
        with pytest.raises(CycleError, match="Self-reference"):
            validate_dag(nodes, edges)
