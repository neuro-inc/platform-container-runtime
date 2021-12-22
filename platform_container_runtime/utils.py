import functools
import sys
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, TypeVar


T_co = TypeVar("T_co", covariant=True)

if sys.version_info >= (3, 10):
    from contextlib import aclosing
else:

    class aclosing(AbstractAsyncContextManager[T_co]):
        def __init__(self, thing: T_co):
            self.thing = thing

        async def __aenter__(self) -> T_co:
            return self.thing

        async def __aexit__(self, *args: Any, **kwargs: Any) -> None:
            await self.thing.aclose()  # type: ignore


def asyncgeneratorcontextmanager(
    func: Callable[..., T_co]
) -> Callable[..., AbstractAsyncContextManager[T_co]]:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> AbstractAsyncContextManager[T_co]:
        return aclosing(func(*args, **kwargs))

    return wrapper
