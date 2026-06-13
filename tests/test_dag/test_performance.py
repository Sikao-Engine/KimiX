"""Performance tests for large graph execution."""
from __future__ import annotations

import time
from typing import Any

import pytest

from kimix.dag import DAG, Executor, TaskNode

pytestmark = pytest.mark.slow


def make_executor(max_workers: int | None = None) -> Executor:
    return Executor(max_workers=max_workers)


# ============================================================================
# Large graph benchmarks
# ============================================================================
class TestLargeGraphs:
    def test_100_nodes_linear(self) -> None:
        """Linear chain of 100 nodes should complete quickly (no I/O)."""
        dag = DAG()
        prev: str | None = None
        for i in range(100):
            name = f"n{i}"
            deps = {prev} if prev else set()
            dag.add_node(TaskNode(name, lambda c, i=i: i, dependencies=deps))
            prev = name
        exe = make_executor()
        start = time.perf_counter()
        results = exe.execute(dag)
        elapsed = time.perf_counter() - start
        assert results["n99"] == 99
        assert elapsed < 2.0  # generous; mostly thread overhead

    def test_100_nodes_parallel_roots(self) -> None:
        """100 independent nodes should execute in parallel."""
        dag = DAG()
        for i in range(100):
            dag.add_node(TaskNode(f"n{i}", lambda c, i=i: i))
        exe = make_executor()
        start = time.perf_counter()
        results = exe.execute(dag)
        elapsed = time.perf_counter() - start
        assert len(results) == 100
        assert elapsed < 2.0

    def test_wide_fan_out(self) -> None:
        """One root, 50 children."""
        dag = DAG()
        dag.add_node(TaskNode("root", lambda c: 1))
        for i in range(50):
            dag.add_node(TaskNode(f"c{i}", lambda c, i=i: i + 10, dependencies={"root"}))
        exe = make_executor(max_workers=10)
        start = time.perf_counter()
        results = exe.execute(dag)
        elapsed = time.perf_counter() - start
        assert len(results) == 51
        assert elapsed < 2.0

    def test_deep_binary_tree(self) -> None:
        """Binary tree of depth 5 = 63 nodes (levels 0..5)."""
        dag = DAG()
        depth = 5
        dag.add_node(TaskNode("0", lambda c: 1))
        node_idx = 1
        for d in range(depth):
            level_size = 2 ** d
            for _ in range(level_size):
                parent = str(node_idx - level_size)
                left = str(node_idx)
                right = str(node_idx + 1)
                dag.add_node(TaskNode(left, lambda c: 1, dependencies={parent}))
                dag.add_node(TaskNode(right, lambda c: 1, dependencies={parent}))
                node_idx += 2

        exe = make_executor()
        start = time.perf_counter()
        results = exe.execute(dag)
        elapsed = time.perf_counter() - start
        assert len(results) == 63
        assert elapsed < 2.0

    def test_500_nodes_random_deps(self) -> None:
        """500 nodes with randomized but valid dependencies."""
        import random
        rng = random.Random(42)
        dag = DAG()
        for i in range(500):
            name = f"n{i}"
            # each node depends on up to 3 earlier nodes
            max_deps = min(3, i)
            if max_deps == 0:
                deps: set[str] = set()
            else:
                n_deps = rng.randint(1, max_deps)
                deps = {f"n{rng.randint(0, i - 1)}" for _ in range(n_deps)}
            dag.add_node(TaskNode(name, lambda c, i=i: i, dependencies=deps))
        exe = make_executor()
        start = time.perf_counter()
        results = exe.execute(dag)
        elapsed = time.perf_counter() - start
        assert len(results) == 500
        assert elapsed < 5.0

    @pytest.mark.slow
    def test_1000_nodes_parallel_roots(self) -> None:
        """1000 independent nodes."""
        dag = DAG()
        for i in range(1000):
            dag.add_node(TaskNode(f"n{i}", lambda c, i=i: i))
        exe = make_executor()
        start = time.perf_counter()
        results = exe.execute(dag)
        elapsed = time.perf_counter() - start
        assert len(results) == 1000
        assert elapsed < 5.0
