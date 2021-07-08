import pytest

from platform_container_runtime.service import Service


pytestmark = pytest.mark.asyncio


class TestService:
    @pytest.fixture
    def service(self) -> Service:
        return Service()

    async def test_created(self, service: Service) -> None:
        assert service
