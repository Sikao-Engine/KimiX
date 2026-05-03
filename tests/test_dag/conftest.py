"""Shared fixtures for DAG tests."""
from __future__ import annotations

import time
from typing import Any

import pytest

from kimix.dag import Context, DAG, TaskNode


# ---------------------------------------------------------------------------
# Simple deterministic tasks
# ---------------------------------------------------------------------------
def make_adder(key: str, value: int) -> Any:
    """Return a task that adds *value* to ctx[key]."""
    def _task(ctx: Context) -> Any:
        current = ctx.get(key, 0)
        new = current + value
        ctx.set(key, new)
        return new
    return _task


def make_slow_task(key: str, delay: float, value: int) -> Any:
    """Return a task that sleeps then writes a value."""
    def _task(ctx: Context) -> int:
        time.sleep(delay)
        ctx.set(key, value)
        return value
    return _task


def failing_task(_ctx: Context) -> None:
    """Always raises ValueError."""
    raise ValueError("boom")


def make_fail_once(key: str) -> Any:
    """Return a task that fails on first call, succeeds on second."""
    calls = 0
    def _task(ctx: Context) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("retry me")
        ctx.set(key, calls)
        return calls
    return _task


# ---------------------------------------------------------------------------
# Async tasks
# ---------------------------------------------------------------------------
async def async_add_one(ctx: Context) -> Any:
    """Async task that increments ctx['counter']."""
    current = ctx.get("counter", 0)
    new = current + 1
    ctx.set("counter", new)
    return new


async def async_fail(_ctx: Context) -> None:
    """Async task that always fails."""
    raise RuntimeError("async boom")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def ctx() -> Context:
    return Context()


@pytest.fixture
def empty_dag() -> DAG:
    return DAG()


@pytest.fixture
def single_node_dag() -> DAG:
    dag = DAG()
    dag.add_node(TaskNode("a", make_adder("x", 1)))
    return dag


@pytest.fixture
def linear_dag() -> DAG:
    """a -> b -> c (c depends on b, b depends on a)."""
    dag = DAG()
    dag.add_node(TaskNode("a", make_adder("x", 1)))
    dag.add_node(TaskNode("b", make_adder("x", 10), dependencies={"a"}))
    dag.add_node(TaskNode("c", make_adder("x", 100), dependencies={"b"}))
    return dag


@pytest.fixture
def diamond_dag() -> DAG:
    r"""
      a
     / \
    b   c
     \ /
      d
    """
    dag = DAG()
    dag.add_node(TaskNode("a", make_adder("x", 1)))
    dag.add_node(TaskNode("b", make_adder("x", 10), dependencies={"a"}))
    dag.add_node(TaskNode("c", make_adder("x", 100), dependencies={"a"}))
    dag.add_node(TaskNode("d", make_adder("x", 1000), dependencies={"b", "c"}))
    return dag


@pytest.fixture
def fan_out_dag() -> DAG:
    """a -> b, a -> c, a -> d."""
    dag = DAG()
    dag.add_node(TaskNode("a", make_adder("x", 1)))
    dag.add_node(TaskNode("b", make_adder("x", 10), dependencies={"a"}))
    dag.add_node(TaskNode("c", make_adder("x", 100), dependencies={"a"}))
    dag.add_node(TaskNode("d", make_adder("x", 1000), dependencies={"a"}))
    return dag
