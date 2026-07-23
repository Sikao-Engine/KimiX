"""Edge-case tests for DAG execution."""
from __future__ import annotations

from typing import Any

import pytest

from kimix.dag import DAG, Context, Executor, TaskNode

from tests.test_dag.conftest import make_adder


def make_executor(max_workers: int | None = None) -> Executor:
    return Executor(max_workers=max_workers)


# ============================================================================
# Empty / trivial
# ============================================================================
class TestEmptyAndTrivial:
    def test_empty_dag(self) -> None:
        dag = DAG()
        results = make_executor().execute(dag)
        assert results == {}

    def test_single_node_no_deps(self) -> None:
        dag = DAG()
        dag.add_node(TaskNode("lonely", lambda c: "hello"))
        results = make_executor().execute(dag)
        assert results == {"lonely": "hello"}

    def test_two_isolated_nodes(self) -> None:
        dag = DAG()
        dag.add_node(TaskNode("a", lambda c: 1))
        dag.add_node(TaskNode("b", lambda c: 2))
        results = make_executor().execute(dag)
        assert results == {"a": 1, "b": 2}


# ============================================================================
# Diamond variations
# ============================================================================
class TestDiamondVariations:
    def test_classic_diamond(self) -> None:
        dag = DAG()
        dag.add_node(TaskNode("a", make_adder("x", 1)))
        dag.add_node(TaskNode("b", make_adder("x", 10), dependencies={"a"}))
        dag.add_node(TaskNode("c", make_adder("x", 100), dependencies={"a"}))
        dag.add_node(TaskNode("d", make_adder("x", 1000), dependencies={"b", "c"}))
        results = make_executor().execute(dag)
        assert results["a"] == 1
        assert results["b"] in (11, 111)   # race between b and c
        assert results["c"] in (101, 111)  # race between b and c
        assert results["d"] in (1011, 1111)

    def test_diamond_with_one_broken_branch(self) -> None:
        from kimix.dag import ExecutionError, DependencyError
        dag = DAG()
        dag.add_node(TaskNode("a", make_adder("x", 1)))
        dag.add_node(TaskNode("b", lambda c: (_ for _ in ()).throw(ValueError("bad")), dependencies={"a"}))
        dag.add_node(TaskNode("c", make_adder("x", 100), dependencies={"a"}))
        dag.add_node(TaskNode("d", make_adder("x", 1000), dependencies={"b", "c"}))
        exe = make_executor()
        with pytest.raises(ExecutionError) as exc_info:
            exe.execute(dag)
        assert "b" in exc_info.value.errors
        assert "d" in exc_info.value.errors
        assert isinstance(exc_info.value.errors["d"], DependencyError)
        assert dag.get_node("c").done
        assert dag.get_node("c").error is None

    def test_double_diamond(self) -> None:
        """Two diamonds stacked."""
        dag = DAG()
        dag.add_node(TaskNode("a", make_adder("x", 1)))
        dag.add_node(TaskNode("b1", make_adder("x", 10), dependencies={"a"}))
        dag.add_node(TaskNode("c1", make_adder("x", 100), dependencies={"a"}))
        dag.add_node(TaskNode("d1", make_adder("x", 1000), dependencies={"b1", "c1"}))
        dag.add_node(TaskNode("b2", make_adder("x", 10000), dependencies={"d1"}))
        dag.add_node(TaskNode("c2", make_adder("x", 100000), dependencies={"d1"}))
        dag.add_node(TaskNode("d2", make_adder("x", 1000000), dependencies={"b2", "c2"}))
        results = make_executor().execute(dag)
        assert results["d2"] == 1111111


# ============================================================================
# Deep chains
# ============================================================================
class TestDeepChains:
    def test_chain_of_50(self) -> None:
        dag = DAG()
        prev: str | None = None
        for i in range(50):
            name = f"n{i}"
            deps = {prev} if prev else set()
            dag.add_node(TaskNode(name, lambda c, i=i: i, dependencies=deps))
            prev = name
        results = make_executor().execute(dag)
        assert results["n49"] == 49

    def test_chain_with_fan_out_at_end(self) -> None:
        dag = DAG()
        dag.add_node(TaskNode("root", lambda c: 1))
        prev = "root"
        for i in range(10):
            name = f"link{i}"
            dag.add_node(TaskNode(name, lambda c, i=i: i, dependencies={prev}))
            prev = name
        for i in range(5):
            dag.add_node(TaskNode(f"leaf{i}", lambda c, i=i: i + 100, dependencies={prev}))
        results = make_executor().execute(dag)
        assert results["leaf4"] == 104


# ============================================================================
# Node returning None / exceptions as results
# ============================================================================
class TestResultShapes:
    def test_none_result(self) -> None:
        dag = DAG()
        dag.add_node(TaskNode("a", lambda c: None))
        results = make_executor().execute(dag)
        assert results["a"] is None

    def test_result_is_exception_instance(self) -> None:
        """A task that returns an Exception object (not raises) is fine."""
        dag = DAG()
        dag.add_node(TaskNode("a", lambda c: ValueError("i am a result")))
        results = make_executor().execute(dag)
        assert isinstance(results["a"], ValueError)

    def test_result_is_dict(self) -> None:
        dag = DAG()
        dag.add_node(TaskNode("a", lambda c: {"key": [1, 2, 3]}))
        results = make_executor().execute(dag)
        assert results["a"] == {"key": [1, 2, 3]}

    def test_result_is_callable(self) -> None:
        dag = DAG()
        dag.add_node(TaskNode("a", lambda c: lambda x: x * 2))
        results = make_executor().execute(dag)
        assert callable(results["a"])
        assert results["a"](5) == 10


# ============================================================================
# Context mutation edge cases
# ============================================================================
class TestContextEdgeCases:
    def test_context_none_default(self) -> None:
        """Executor creates a fresh Context if none provided."""
        dag = DAG()
        dag.add_node(TaskNode("a", lambda c: c.get("missing", "fallback")))
        results = make_executor().execute(dag)
        assert results["a"] == "fallback"

    def test_context_key_overwrite(self) -> None:
        ctx = Context()
        ctx.set("x", 99)
        dag = DAG()
        dag.add_node(TaskNode("a", lambda c: c.set("x", 1)))
        dag.add_node(TaskNode("b", lambda c: c.set("x", 2), dependencies={"a"}))
        make_executor().execute(dag, ctx)
        assert ctx.get("x") == 2

    def test_context_update_with_empty_dict(self) -> None:
        ctx = Context()
        ctx.set("x", 1)
        ctx.update({})
        assert ctx.get("x") == 1
