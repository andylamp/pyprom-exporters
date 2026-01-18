"""Async task collector with support for retries and exponential back-off."""

import asyncio
import random
from collections.abc import Awaitable, Callable, Iterable
from typing import TypeVar

T = TypeVar("T")


# pylint: disable=too-many-arguments
async def _retry(  # noqa: PLR0913, UP047
    make_coro: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    delay: float,
    backoff: float,
    jitter: float,
    retry_exceptions: tuple[type[BaseException], ...],
) -> T:
    """Run a coroutine factory with retries & exponential back-off."""
    cur_delay = delay
    last_exc: BaseException | None = None
    for try_no in range(1, attempts + 1):
        try:
            return await make_coro()
        except retry_exceptions as exc:
            last_exc = exc
            if try_no == attempts:
                break  # reached number of attempts, break
            await asyncio.sleep(cur_delay + random.random() * jitter)  # noqa: S311
            cur_delay *= backoff

    msg = f"Task failed after {attempts} attempts."
    raise RuntimeError(msg) from last_exc


async def run_tasks_with_retry(  # noqa: PLR0913, UP047
    factories: Iterable[Callable[[], Awaitable[T]]],
    *,
    concurrency: int | None = None,
    attempts: int = 3,
    delay: float = 0.5,
    backoff: float = 2.0,
    jitter: float = 0.3,
    retry_exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> list[T]:
    """Take a list of coroutine factories and run them concurrently with retries.

    Parameters
    ----------
    factories : Iterable[Callable[[], &quot;asyncio.Future[T]&quot;]]
        A list of coroutine factories to run concurrently.
    concurrency : int | None, optional
        The number of, by default None
    attempts : int, optional
        Max attempts, by default 3
    delay : float, optional
        The delay after each attempt, by default 0.5
    backoff : float, optional
        The back-off value, by default 2.0
    jitter : float, optional
        The jitter to use for the delay, by default 0.3
    retry_exceptions : tuple[type[BaseException], ...], optional
        The exceptions to catch, by default (Exception,)

    Returns
    -------
    list[T]
        A list of results from the coroutine factories.

    """
    sem = asyncio.Semaphore(concurrency) if concurrency else None

    async def _guard(factory: Callable[[], Awaitable[T]]) -> T:
        async def _run() -> T:
            return await _retry(
                factory,
                attempts=attempts,
                delay=delay,
                backoff=backoff,
                jitter=jitter,
                retry_exceptions=retry_exceptions,
            )

        if sem is not None:
            async with sem:
                return await _run()

        # No semaphore, run without concurrency limit
        return await _run()

    async with asyncio.TaskGroup() as tg:  # structured concurrency
        tasks = [tg.create_task(_guard(f)) for f in factories]

    # If we reach here, every task has finished (or an exception escaped)
    return [t.result() for t in tasks]
