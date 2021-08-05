from platform_container_runtime.service import Stream


class TestStream:
    def test_parse_error_channel_v4_success(self) -> None:
        result = Stream.parse_error_channel(b'\x03{"metadata":{},"status":"Success"}')

        assert result == {"exit_code": 0}

    def test_parse_error_channel_v4_failure(self) -> None:
        result = Stream.parse_error_channel(
            b'\x03{"metadata":{},"status":"Failure","message":"error","reason":"NonZeroExitCode","details":{"causes":[{"reason":"ExitCode","message":"42"}]}}'  # noqa: E501
        )

        assert result == {"exit_code": 42, "message": "error"}

    def test_parse_error_channel_v1(self) -> None:
        result = Stream.parse_error_channel(b"\x03error")

        assert result == {"exit_code": 1, "message": "error"}
