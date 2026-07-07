"""Performance tests for large graph execution."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from kimix.dag import DAG, Executor, TaskNode, Context, TopologicalSorter

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


# ============================================================================
# Extended benchmarks: async tasks, topological sorter, retry, contention
# ============================================================================


class TestExecutorExtendedBenchmark:
    """Extended executor benchmarks."""

    def test_async_tasks_mixed_with_sync(self) -> None:
        """Executor with async tasks mixed with sync (handled internally by execute)."""
        dag = DAG()

        async def async_fn(ctx: Context) -> str:
            await asyncio.sleep(0.001)
            return "async"

        def sync_fn(ctx: Context) -> str:
            return "sync"

        dag.add_node(TaskNode("async1", async_fn))
        dag.add_node(TaskNode("sync1", sync_fn))
        dag.add_node(TaskNode("async2", async_fn, dependencies={"sync1"}))
        dag.add_node(TaskNode("sync2", sync_fn, dependencies={"async1"}))
        dag.add_node(TaskNode("async3", async_fn, dependencies={"sync2", "async2"}))

        exe = make_executor(max_workers=4)
        start = time.perf_counter()
        results = exe.execute(dag)
        elapsed = time.perf_counter() - start
        assert len(results) == 5
        assert results["async1"] == "async"
        assert results["sync1"] == "sync"
        assert elapsed < 5.0

    def test_1000_node_topological_sorter(self) -> None:
        """TopologicalSorter isolated: 1000-node DAG."""
        import random
        rng = random.Random(42)
        dag = DAG()
        for i in range(1000):
            name = f"n{i}"
            max_deps = min(3, i)
            if max_deps == 0:
                deps = set()
            else:
                n_deps = rng.randint(1, max_deps)
                deps = {f"n{rng.randint(0, i - 1)}" for _ in range(n_deps)}
            dag.add_node(TaskNode(name, lambda c, i=i: i, dependencies=deps))
        # Convert DAG to edges dict for TopologicalSorter
        edges = {name: node.dependencies for name, node in dag.nodes.items()}
        sorter = TopologicalSorter(edges)
        start = time.perf_counter()
        order = sorter.sort()
        elapsed = time.perf_counter() - start
        assert len(order) == 1000
        assert elapsed < 2.0

    def test_5000_node_topological_sorter(self) -> None:
        """TopologicalSorter isolated: 5000-node DAG."""
        import random
        rng = random.Random(42)
        dag = DAG()
        for i in range(5000):
            name = f"n{i}"
            max_deps = min(2, i)
            if max_deps == 0:
                deps = set()
            else:
                n_deps = rng.randint(1, max_deps)
                deps = {f"n{rng.randint(0, i - 1)}" for _ in range(n_deps)}
            dag.add_node(TaskNode(name, lambda c, i=i: i, dependencies=deps))
        edges = {name: node.dependencies for name, node in dag.nodes.items()}
        sorter = TopologicalSorter(edges)
        start = time.perf_counter()
        order = sorter.sort()
        elapsed = time.perf_counter() - start
        assert len(order) == 5000
        assert elapsed < 5.0

    def test_retry_path_100_nodes(self) -> None:
        """Retry path: 100 nodes where 20% fail on first attempt (uses retries= param)."""
        import random
        rng = random.Random(42)
        attempt_counts: dict[str, int] = {}

        def flaky_fn(ctx: Context) -> str:
            name = ctx.get("name", "")
            attempt_counts[name] = attempt_counts.get(name, 0) + 1
            if attempt_counts[name] == 1 and rng.random() < 0.2:
                raise ValueError("Transient error")
            return f"done-{name}"

        dag = DAG()
        for i in range(100):
            dag.add_node(TaskNode(f"n{i}", flaky_fn, retries=1))
        exe = make_executor(max_workers=8)
        start = time.perf_counter()
        results = exe.execute(dag)
        elapsed = time.perf_counter() - start
        assert len(results) == 100
        assert elapsed < 10.0

    def test_context_contention(self) -> None:
        """Context.set()/get() contention: 100 tasks x 100 writes each."""
        dag = DAG()

        def writer_fn(ctx: Context) -> int:
            total = 0
            for j in range(100):
                ctx.set("counter", ctx.get("counter", 0) + 1)
                total += ctx.get("counter", 0)
            return total

        for i in range(100):
            dag.add_node(TaskNode(f"w{i}", writer_fn))
        exe = make_executor(max_workers=8)
        start = time.perf_counter()
        results = exe.execute(dag)
        elapsed = time.perf_counter() - start
        assert len(results) == 100
        assert elapsed < 10.0

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


