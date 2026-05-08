"""Manually Chain-of-Thought (CoT) system.

Wraps an LLM callback with explicit reasoning instructions,
parses structured <thinking>/<answer> output, and supports
self-verification and continuation from prior reasoning.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional


@dataclass
class CoTResult:
    """Result of a manual CoT prompt."""

    thinking: str
    quit: bool = False


_COT_SYSTEM = (
    "Think step by step. "
    "Put your reasoning in <thinking>...</thinking>. "
    "If you need more steps, output only <thinking>...</thinking>; the system will prompt again. "
    "When finished, write <quit/>. "
    "Be concise. No text outside tags."
)

_VERIFY_SUFFIX = (
    "\n\nReview your reasoning for errors or bad assumptions, correct them, then finalize."
)

_CONTINUE_PREFIX = (
    "Continue from the prior thinking. Verify, refine, then finalize.\n\n"
    "<thinking>\n{thinking}\n</thinking>"
)


def _build_prompt(
    user_prompt: str,
    existing_thinking: Optional[str] = None,
    self_verify: bool = False,
) -> str:
    parts: list[str] = []
    if existing_thinking is not None:
        parts.append(_CONTINUE_PREFIX.format(thinking=existing_thinking.strip()))
    parts.append(_COT_SYSTEM)
    parts.append(user_prompt.strip())
    prompt = "\n\n".join(parts)
    if self_verify:
        prompt += _VERIFY_SUFFIX
    return prompt


_THINKING_RE = re.compile(r"<thinking>(.*?)</thinking>", re.DOTALL | re.IGNORECASE)
_QUIT_RE = re.compile(r"<quit\s*/?>", re.IGNORECASE)


def _parse_response(text: str) -> CoTResult:
    thinking_match = _THINKING_RE.search(text)
    quit_match = _QUIT_RE.search(text)
    thinking = thinking_match.group(1).strip() if thinking_match else ""
    return CoTResult(thinking=thinking, quit=bool(quit_match))


async def cot_prompt_async(
    prompt_str: str,
    llm_callback: Callable[[str], Awaitable[str]],
    self_verify: bool = True,
    existing_thinking: Optional[str] = None,
    max_iterations: int = 10,
) -> CoTResult:
    """Run manual CoT with an async LLM callback.

    The callback is invoked in a loop until the model emits ``<quit/>``
    or ``max_iterations`` is reached.

    Parameters
    ----------
    prompt_str:
        The user prompt.
    llm_callback:
        Async callable that takes a prompt string and returns the raw LLM response.
    self_verify:
        If True, append a self-verification instruction to each prompt.
    existing_thinking:
        If provided, ask the model to continue from this prior thinking.
    max_iterations:
        Maximum number of LLM calls before forcing a return.
    """
    last_thinking = existing_thinking.strip() if existing_thinking is not None else None

    for _ in range(max_iterations):
        prompt = _build_prompt(prompt_str, last_thinking, self_verify)
        raw = await llm_callback(prompt)
        result = _parse_response(raw)

        if result.thinking:
            last_thinking = result.thinking

        if result.quit:
            return CoTResult(
                thinking=last_thinking or "",
                quit=result.quit,
            )

    return CoTResult(thinking=last_thinking or "", quit=False)


def cot_prompt(
    prompt_str: str,
    llm_callback: Callable[[str], str],
    self_verify: bool = True,
    existing_thinking: Optional[str] = None,
    max_iterations: int = 10,
) -> CoTResult:
    """Synchronous version of :func:`cot_prompt_async`.

    Parameters
    ----------
    prompt_str:
        The user prompt.
    llm_callback:
        Sync callable that takes a prompt string and returns the raw LLM response.
    self_verify:
        If True, append a self-verification instruction to each prompt.
    existing_thinking:
        If provided, ask the model to continue from this prior thinking.
    max_iterations:
        Maximum number of LLM calls before forcing a return.
    """
    last_thinking = existing_thinking.strip() if existing_thinking is not None else None

    for _ in range(max_iterations):
        prompt = _build_prompt(prompt_str, last_thinking, self_verify)
        raw = llm_callback(prompt)
        result = _parse_response(raw)

        if result.thinking:
            last_thinking = result.thinking

        if result.quit:
            return CoTResult(
                thinking=last_thinking or "",
                quit=result.quit,
            )

    return CoTResult(thinking=last_thinking or "", quit=False)


async def cot_prompt_with_verification_async(
    prompt_str: str,
    llm_callback: Callable[[str], Awaitable[str]],
    existing_thinking: Optional[str] = None,
) -> CoTResult:
    """Two-pass CoT: generate reasoning, then verify and refine.

    First pass runs without self-verify to get initial thinking.
    Second pass feeds the thinking back as ``existing_thinking`` with verification enabled.
    """
    first = await cot_prompt_async(
        prompt_str,
        llm_callback,
        self_verify=False,
        existing_thinking=existing_thinking,
    )
    if not first.thinking:
        return first
    second = await cot_prompt_async(
        prompt_str,
        llm_callback,
        self_verify=True,
        existing_thinking=first.thinking,
    )
    return second


def cot_prompt_with_verification(
    prompt_str: str,
    llm_callback: Callable[[str], str],
    existing_thinking: Optional[str] = None,
) -> CoTResult:
    """Synchronous two-pass CoT with verification."""
    first = cot_prompt(
        prompt_str,
        llm_callback,
        self_verify=False,
        existing_thinking=existing_thinking,
    )
    if not first.thinking:
        return first
    second = cot_prompt(
        prompt_str,
        llm_callback,
        self_verify=True,
        existing_thinking=first.thinking,
    )
    return second
