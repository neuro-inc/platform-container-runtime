class ContainerRuntimeError(Exception):
    pass


class ContainerRuntimeNotAvailableError(ContainerRuntimeError):
    def __init__(self) -> None:
        super().__init__("Container runtime is not available")


class MediaTypeNotSupportedError(ContainerRuntimeError):
    def __init__(self, media_type: str) -> None:
        super().__init__(f"Media type {media_type!r} not supported")


class PlatformNotSupportedError(ContainerRuntimeError):
    def __init__(self, architecture: str, os: str) -> None:
        super().__init__(f"Platform ({architecture},{os}) not supported")


class ContainerRuntimeClientError(ValueError):
    pass


class ContainerNotFoundError(ContainerRuntimeClientError):
    def __init__(self, container_id: str) -> None:
        super().__init__(f"Container {container_id!r} not found")


class ContainerNotRunningError(ContainerRuntimeClientError):
    def __init__(self, container_id: str) -> None:
        super().__init__(f"Container {container_id!r} not running")


class ImageNotFoundError(ContainerRuntimeClientError):
    def __init__(self, name: str) -> None:
        super().__init__(f"Image {name!r} not found")
