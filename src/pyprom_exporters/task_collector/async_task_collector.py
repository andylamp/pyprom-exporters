# Copyright (c) 2026 pyprom-exporters contributors
# SPDX-License-Identifier: Apache-2.0

"""Async task collector with support for retries and exponential back-off."""

from __future__ import annotations

import asyncio
import math
import random
from itertools import chain, islice
from typing import TYPE_CHECKING, Literal, TypeVar, overload

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

T = TypeVar("T")


# Retry controls are forwarded without changing the public keyword-only API.
async def _retry(  # ruff: ignore[too-many-arguments]
    make_coro: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    delay: float,
    backoff: float,
    jitter: float,
    retry_exceptions: tuple[type[BaseException], ...],
) -> T:
    """Run a coroutine factory with retries & exponential back-off.

    Returns
    -------
    T
        The successful result returned by the coroutine factory.

    Raises
    ------
    RuntimeError
        If all attempts fail with a retryable exception.
    CancelledError
        If the task is cancelled; cancellation is never retried.
    KeyboardInterrupt
        If the operation requests interruption.
    SystemExit
        If the operation requests process exit.
    AssertionError
        If the internal caller supplies no retry attempts.

    """
    cur_delay = delay
    for try_no in range(1, attempts + 1):
        try:
            return await make_coro()
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            # Shutdown must work even with a broad retry_exceptions tuple.
            raise
        except retry_exceptions as exc:
            if try_no == attempts:
                msg = f"Task failed after {attempts} attempts."
                raise RuntimeError(msg) from exc
            # Jitter spreads retry load; it does not protect secrets.
            await asyncio.sleep(cur_delay + random.random() * jitter)  # ruff: ignore[suspicious-non-cryptographic-random-usage]  # nosec B311
            cur_delay *= backoff

    message = "Retry attempts must be positive."
    raise AssertionError(message)


def _validate_positive_integer(name: str, value: int) -> None:
    """Reject booleans and non-integers for counts before creating tasks.

    Raises
    ------
    ValueError
        If the value is a boolean, not an integer, or less than one.

    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        message = f"{name} must be a positive integer."
        raise ValueError(message)


def _validate_options(
    *,
    attempts: int,
    delay: float,
    backoff: float,
    jitter: float,
    retry_exceptions: tuple[type[BaseException], ...],
) -> None:
    """Reject invalid settings before consuming factories or starting work.

    Raises
    ------
    ValueError
        If a retry count, delay, backoff, or jitter is invalid.
    TypeError
        If retry_exceptions is not a tuple of exception classes.

    """
    _validate_positive_integer("attempts", attempts)
    for name, value in (("delay", delay), ("backoff", backoff), ("jitter", jitter)):
        if not math.isfinite(value) or value < 0:
            message = f"{name} must be finite and non-negative."
            raise ValueError(message)
    if not isinstance(retry_exceptions, tuple) or not all(
        isinstance(exception, type) and issubclass(exception, BaseException) for exception in retry_exceptions
    ):
        message = "retry_exceptions must be a tuple of exception classes."
        raise TypeError(message)


@overload
async def run_tasks_with_retry(
    factories: Iterable[Callable[[], Awaitable[T]]],
    *,
    concurrency: int | None = None,
    attempts: int = 3,
    delay: float = 0.5,
    backoff: float = 2.0,
    jitter: float = 0.3,
    retry_exceptions: tuple[type[BaseException], ...] = (Exception,),
    return_exceptions: Literal[False] = False,
) -> list[T]: ...


@overload
async def run_tasks_with_retry(
    factories: Iterable[Callable[[], Awaitable[T]]],
    *,
    concurrency: int | None = None,
    attempts: int = 3,
    delay: float = 0.5,
    backoff: float = 2.0,
    jitter: float = 0.3,
    retry_exceptions: tuple[type[BaseException], ...] = (Exception,),
    return_exceptions: Literal[True],
) -> list[T | Exception]: ...


@overload
async def run_tasks_with_retry(
    factories: Iterable[Callable[[], Awaitable[T]]],
    *,
    concurrency: int | None = None,
    attempts: int = 3,
    delay: float = 0.5,
    backoff: float = 2.0,
    jitter: float = 0.3,
    retry_exceptions: tuple[type[BaseException], ...] = (Exception,),
    return_exceptions: bool,
) -> list[T | Exception]: ...


# Preserve the public keyword-only retry controls for existing callers.
async def run_tasks_with_retry(  # ruff: ignore[too-many-arguments]
    factories: Iterable[Callable[[], Awaitable[T]]],
    *,
    concurrency: int | None = None,
    attempts: int = 3,
    delay: float = 0.5,
    backoff: float = 2.0,
    jitter: float = 0.3,
    retry_exceptions: tuple[type[BaseException], ...] = (Exception,),
    return_exceptions: bool = False,
) -> list[T | Exception]:
    """Run coroutine factories concurrently, returning results in input order.

    Parameters
    ----------
    factories : Iterable[Callable[[], Awaitable[T]]]
        Factories that create a fresh awaitable for each attempt.
    concurrency : int | None, optional
        Positive maximum number of tasks, including tasks waiting to retry.
        A worker pool consumes factories lazily when a limit is supplied.
        If None, all tasks run concurrently.
    attempts : int, optional
        Positive maximum number of attempts, by default 3.
    delay : float, optional
        Initial retry delay in seconds, by default 0.5. Must be finite and non-negative.
    backoff : float, optional
        Multiplier for each retry delay, by default 2.0. Must be finite and non-negative.
    jitter : float, optional
        Maximum random extra delay in seconds, by default 0.3. Must be finite and non-negative.
    retry_exceptions : tuple[type[BaseException], ...], optional
        Exceptions to retry, by default (Exception,). Cancellation and process
        exit signals are never retried.
    return_exceptions : bool, optional
        If True, return exhausted or non-retryable exceptions in their input
        positions, allowing independent tasks to finish. If False (the default),
        a failure cancels the remaining tasks and raises an ExceptionGroup.
        Cancellation always propagates.

    Returns
    -------
    list[T | Exception]
        Results in input order. Exceptions are included only when requested.
        Exhausted retries produce RuntimeError with the final failure as its cause.

    Raises
    ------
    ValueError
        If a retry or concurrency setting is outside its allowed range.
    TypeError
        If retry_exceptions is not a tuple of exception classes.

    """  # ruff: ignore[docstring-extraneous-exception]
    # Validation helpers raise the documented public errors before tasks are started.
    if concurrency is not None:
        _validate_positive_integer("concurrency", concurrency)
    _validate_options(
        attempts=attempts,
        delay=delay,
        backoff=backoff,
        jitter=jitter,
        retry_exceptions=retry_exceptions,
    )

    async def _run(factory: Callable[[], Awaitable[T]]) -> T | Exception:
        try:
            return await _retry(
                factory,
                attempts=attempts,
                delay=delay,
                backoff=backoff,
                jitter=jitter,
                retry_exceptions=retry_exceptions,
            )
        except Exception as exc:
            if return_exceptions:
                return exc
            raise

    if concurrency is None:
        async with asyncio.TaskGroup() as task_group:
            tasks = [task_group.create_task(_run(factory)) for factory in factories]
        return [task.result() for task in tasks]

    # Keep the number of asyncio tasks bounded as well as the number of active
    # operations. A semaphore alone would allocate a task for every factory.
    indexed_factories = enumerate(factories)
    results: dict[int, T | Exception] = {}

    async def _worker(first: tuple[int, Callable[[], Awaitable[T]]]) -> None:
        for index, factory in chain((first,), indexed_factories):
            results[index] = await _run(factory)

    async with asyncio.TaskGroup() as task_group:
        workers = [task_group.create_task(_worker(first)) for first in islice(indexed_factories, concurrency)]
    for worker in workers:
        # TaskGroup does not raise when a child cancels itself. Do not return
        # incomplete results in that case.
        worker.result()
    return [results[index] for index in range(len(results))]
