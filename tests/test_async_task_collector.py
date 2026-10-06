# Copyright (c) 2026 pyprom-exporters contributors
# SPDX-License-Identifier: Apache-2.0
"""Regression tests for retry, concurrency, failure isolation, and cancellation."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

import pytest

from pyprom_exporters.task_collector import async_task_collector as collector
from pyprom_exporters.task_collector import run_tasks_with_retry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator


@pytest.mark.parametrize("concurrency", [None, 1, 3])
def test_results_preserve_input_order(concurrency: int | None) -> None:
    """Out-of-order completion and None results retain their input positions."""

    async def factory(index: int) -> int | None:
        for _ in range(5 - index):
            await asyncio.sleep(0)
        return index if index % 2 else None

    results = asyncio.run(
        run_tasks_with_retry((partial(factory, index) for index in range(5)), concurrency=concurrency)
    )
    assert results == [None, 1, None, 3, None]


def test_retries_use_fresh_awaitables_and_exponential_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each retry creates a new coroutine and sleeps with backoff and jitter."""
    calls = 0
    delays: list[float] = []

    def factory() -> str:
        nonlocal calls
        calls += 1
        if calls < 4:
            message = "transient"
            raise OSError(message)
        return "done"

    monkeypatch.setattr(collector.asyncio, "sleep", AsyncMock(side_effect=delays.append))
    monkeypatch.setattr(collector.random, "random", lambda: 0.5)
    result = asyncio.run(
        run_tasks_with_retry(
            [AsyncMock(side_effect=factory)], attempts=4, delay=1, backoff=2, jitter=0.2, retry_exceptions=(OSError,)
        )
    )
    assert result == ["done"]
    assert calls == 4
    assert delays == pytest.approx([1.1, 2.1, 4.1])


def test_exhausted_retries_preserve_cause_and_skip_final_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """The final error remains inspectable without an unnecessary last delay."""
    failures: list[OSError] = []
    delays: list[float] = []

    def factory() -> None:
        failure = OSError(f"failure {len(failures)}")
        failures.append(failure)
        raise failure

    monkeypatch.setattr(collector.asyncio, "sleep", AsyncMock(side_effect=delays.append))
    with pytest.raises(ExceptionGroup) as caught:
        asyncio.run(run_tasks_with_retry([AsyncMock(side_effect=factory)], attempts=3, delay=1, jitter=0))
    assert len(failures) == 3
    assert delays == [1, 2]
    assert len(caught.value.exceptions) == 1
    error = caught.value.exceptions[0]
    assert isinstance(error, RuntimeError)
    assert error.__cause__ is failures[-1]


def test_non_retryable_failure_is_not_retried() -> None:
    """A failure outside retry_exceptions escapes on its first attempt."""
    calls = 0

    def factory() -> None:
        nonlocal calls
        calls += 1
        message = "permanent"
        raise ValueError(message)

    with pytest.raises(ExceptionGroup) as caught:
        asyncio.run(run_tasks_with_retry([AsyncMock(side_effect=factory)], retry_exceptions=(OSError,)))
    assert calls == 1
    assert isinstance(caught.value.exceptions[0], ValueError)


def test_concurrency_bounds_tasks_and_consumes_input_lazily() -> None:
    """A large iterable uses only the configured number of worker tasks."""

    async def scenario() -> None:
        release = asyncio.Event()
        all_started = asyncio.Event()
        consumed = 0
        active = 0
        maximum_active = 0
        maximum_tasks = 0
        baseline_tasks = len(asyncio.all_tasks())

        async def factory(index: int) -> int:
            nonlocal active, maximum_active, maximum_tasks
            active += 1
            maximum_active = max(maximum_active, active)
            maximum_tasks = max(maximum_tasks, len(asyncio.all_tasks()))
            if active == 3:
                all_started.set()
            try:
                await release.wait()
                await asyncio.sleep(0)
                return index
            finally:
                active -= 1

        def factories() -> Iterator[Callable[[], Awaitable[int]]]:
            nonlocal consumed
            for index in range(1000):
                consumed += 1
                yield partial(factory, index)

        running = asyncio.create_task(run_tasks_with_retry(factories(), concurrency=3))
        await asyncio.wait_for(all_started.wait(), timeout=2)
        assert consumed == 3
        release.set()
        assert await running == list(range(1000))
        assert maximum_active == 3
        # Three workers, their parent runner, and the temporary wait_for task.
        assert maximum_tasks <= baseline_tasks + 5

    asyncio.run(scenario())


@pytest.mark.parametrize("concurrency", [None, 2])
def test_failure_cancels_and_joins_siblings(concurrency: int | None) -> None:
    """Default failure handling finishes sibling cleanup before returning."""

    async def scenario() -> None:
        started = asyncio.Event()
        cleaned_up = asyncio.Event()

        async def blocked() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned_up.set()

        async def failing() -> None:
            await started.wait()
            message = "unavailable"
            raise OSError(message)

        with pytest.raises(ExceptionGroup):
            await run_tasks_with_retry([blocked, failing], concurrency=concurrency, attempts=1)
        assert cleaned_up.is_set()

    asyncio.run(scenario())


@pytest.mark.parametrize("concurrency", [None, 1, 2])
def test_return_exceptions_isolates_failures(concurrency: int | None) -> None:
    """Exhausted and permanent failures leave other factories free to finish."""
    calls = 0

    def transient_failure() -> str:
        nonlocal calls
        calls += 1
        message = "unavailable"
        raise OSError(message)

    def permanent_failure() -> str:
        message = "invalid"
        raise ValueError(message)

    async def successful() -> str:
        await asyncio.sleep(0)
        return "healthy"

    results = asyncio.run(
        run_tasks_with_retry(
            [
                AsyncMock(side_effect=transient_failure),
                successful,
                AsyncMock(side_effect=permanent_failure),
                successful,
            ],
            concurrency=concurrency,
            attempts=2,
            delay=0,
            jitter=0,
            retry_exceptions=(OSError,),
            return_exceptions=True,
        )
    )
    assert calls == 2
    assert isinstance(results[0], RuntimeError)
    assert isinstance(results[0].__cause__, OSError)
    assert results[1] == "healthy"
    assert isinstance(results[2], ValueError)
    assert results[3] == "healthy"


@pytest.mark.parametrize("concurrency", [None, 2])
@pytest.mark.parametrize("return_exceptions", [False, True])
def test_cancellation_is_not_retried_or_collected(concurrency: int | None, *, return_exceptions: bool) -> None:
    """Even retry_exceptions=(BaseException,) respects caller cancellation."""

    async def scenario() -> None:
        started = asyncio.Event()
        cleaned_up = asyncio.Event()
        calls = 0

        async def factory() -> None:
            nonlocal calls
            calls += 1
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned_up.set()

        running = asyncio.create_task(
            run_tasks_with_retry(
                [factory],
                concurrency=concurrency,
                delay=0,
                jitter=0,
                retry_exceptions=(BaseException,),
                return_exceptions=return_exceptions,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert calls == 1
        assert cleaned_up.is_set()
        assert len(asyncio.all_tasks()) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("concurrency", [None, 1])
def test_self_cancellation_propagates(concurrency: int | None) -> None:
    """A factory cancelling itself must not yield incomplete result lists."""

    def factory() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            run_tasks_with_retry([AsyncMock(side_effect=factory)], concurrency=concurrency, return_exceptions=True)
        )


@pytest.mark.parametrize("concurrency", [None, 2])
@pytest.mark.parametrize("return_exceptions", [False, True])
@pytest.mark.parametrize("cancel_mode", ["raise", "yield", "return"])
def test_self_cancellation_cancels_and_joins_hanging_siblings(
    concurrency: int | None, *, return_exceptions: bool, cancel_mode: str
) -> None:
    """A cancelled factory must interrupt siblings without waiting for their results."""

    async def scenario() -> None:
        sibling_started = asyncio.Event()
        sibling_cleaned_up = asyncio.Event()

        async def self_cancel() -> None:
            await sibling_started.wait()
            if cancel_mode != "raise":
                task = asyncio.current_task()
                assert task is not None
                task.cancel()
                if cancel_mode == "return":
                    return
                await asyncio.sleep(0)
            raise asyncio.CancelledError

        async def sibling() -> None:
            sibling_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                sibling_cleaned_up.set()

        running = asyncio.create_task(
            run_tasks_with_retry(
                [self_cancel, sibling],
                concurrency=concurrency,
                return_exceptions=return_exceptions,
                retry_exceptions=(BaseException,),
            )
        )
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, timeout=2)
        assert sibling_cleaned_up.is_set()
        assert running.cancelled()
        assert running.cancelling() == 1
        assert len(asyncio.all_tasks()) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "options",
    [
        {"attempts": 0},
        {"attempts": -1},
        {"attempts": 1.5},
        {"attempts": True},
        {"concurrency": 0},
        {"concurrency": -1},
        {"concurrency": 1.5},
        {"concurrency": True},
        {"delay": -0.1},
        {"delay": float("inf")},
        {"delay": float("nan")},
        {"backoff": -1},
        {"backoff": float("inf")},
        {"jitter": -1},
        {"jitter": float("nan")},
    ],
)
def test_invalid_options_fail_before_consuming_factories(options: dict[str, Any]) -> None:
    """Bad settings cannot silently disable the limit or produce a hang."""

    def factories() -> Iterator[Callable[[], Awaitable[int]]]:
        pytest.fail("Invalid options must be rejected before consuming input")
        yield

    with pytest.raises(ValueError, match="must be"):
        asyncio.run(run_tasks_with_retry(factories(), **options))


@pytest.mark.parametrize("exceptions", [[OSError], ("not an exception",), (str,)])
def test_invalid_exception_types_fail_early(exceptions: object) -> None:
    """Retry types are checked even when there is no work to trigger except."""
    with pytest.raises(TypeError, match="exception classes"):
        asyncio.run(run_tasks_with_retry([], retry_exceptions=cast("tuple[type[BaseException], ...]", exceptions)))


@pytest.mark.parametrize("concurrency", [None, 1, 5])
def test_empty_input(concurrency: int | None) -> None:
    """Empty batches return an empty result list."""
    assert asyncio.run(run_tasks_with_retry([], concurrency=concurrency)) == []


@pytest.mark.parametrize("concurrency", [None, 2])
@pytest.mark.parametrize("prior_failure", [False, True])
def test_child_cancellation_propagates_after_an_earlier_handled_cancellation(
    concurrency: int | None, *, prior_failure: bool
) -> None:
    """Historical cancellation counts cannot suppress cancellation of a new batch."""

    async def exercise() -> None:
        owner = asyncio.current_task()
        assert owner is not None
        if prior_failure:
            with pytest.raises(ExceptionGroup):
                await run_tasks_with_retry([AsyncMock(side_effect=ValueError)], attempts=1)
        else:
            owner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.sleep(0)
        initial_cancellations = owner.cancelling()
        started = asyncio.Event()
        stopped = asyncio.Event()
        watchdog_fired = False

        async def blocked() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        async def cancelled() -> None:
            await started.wait()
            raise asyncio.CancelledError

        def watchdog() -> None:
            nonlocal watchdog_fired
            watchdog_fired = True
            owner.cancel()

        deadline = asyncio.get_running_loop().call_later(2, watchdog)
        try:
            with pytest.raises(asyncio.CancelledError):
                await run_tasks_with_retry([cancelled, blocked], concurrency=concurrency)
            assert not watchdog_fired
            assert stopped.is_set()
            assert owner.cancelling() == initial_cancellations + 1
        finally:
            deadline.cancel()

    asyncio.run(exercise())


@pytest.mark.parametrize("concurrency", [None, 2])
def test_pending_cancellation_is_delivered_before_consuming_factories(concurrency: int | None) -> None:
    """A caller's pending cancellation must not become a second synthetic request."""

    async def exercise() -> None:
        owner = asyncio.current_task()
        assert owner is not None
        consumed = False
        operation = AsyncMock(return_value=1)

        def factories() -> Iterator[Callable[[], Awaitable[int]]]:
            nonlocal consumed
            consumed = True
            yield operation

        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_tasks_with_retry(factories(), concurrency=concurrency)
        assert owner.cancelling() == 1
        assert not consumed
        operation.assert_not_called()
        assert len(asyncio.all_tasks()) == 1

    asyncio.run(exercise())
