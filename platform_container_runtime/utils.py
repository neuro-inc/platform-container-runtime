import functools
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, aclosing
from typing import Any, TypeVar

T_co = TypeVar("T_co", covariant=True)


def asyncgeneratorcontextmanager(
    func: Callable[..., T_co],
) -> Callable[..., AbstractAsyncContextManager[T_co]]:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> AbstractAsyncContextManager[T_co]:
        return aclosing(func(*args, **kwargs))  # type: ignore

    return wrapper
