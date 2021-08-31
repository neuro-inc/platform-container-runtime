import functools
import sys
from typing import Any, AsyncContextManager, Callable, TypeVar


T_co = TypeVar("T_co", covariant=True)

if sys.version_info >= (3, 10):
    from contextlib import aclosing
else:

    class aclosing(AsyncContextManager[T_co]):
        def __init__(self, thing: T_co):
            self.thing = thing

        async def __aenter__(self) -> T_co:
            return self.thing

        async def __aexit__(self, *args: Any, **kwargs: Any) -> None:
            await self.thing.aclose()  # type: ignore


def asyncgeneratorcontextmanager(
    func: Callable[..., T_co]
) -> Callable[..., AsyncContextManager[T_co]]:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> AsyncContextManager[T_co]:
        return aclosing(func(*args, **kwargs))

    return wrapper
