import abc
import json
from typing import Any, Dict, Optional, Tuple, Union

import aiohttp
import aiohttp.hdrs
import aiohttp.web
from yarl import URL


class RegistryError(Exception):
    pass


class InvalidRangeError(RegistryError):
    def __init__(self, last_valid_range: int) -> None:
        super().__init__(
            f"Invalid range uploaded, last valid range: 0-{last_valid_range}"
        )
        self.last_valid_range = last_valid_range


class Auth(abc.ABC):
    @abc.abstractproperty
    def header(self) -> str:
        pass


class BasicAuth(Auth):
    def __init__(self, username: str, password: str) -> None:
        super().__init__()

        self._username = username
        self._password = password

    def header(self) -> str:
        return aiohttp.BasicAuth(self._username, self._password).encode()


class V2Endpoints:
    def __init__(self, server: str) -> None:
        host, port = self._split_server(server)

        if port == 5000:
            # 5000 is considered to be insecure port
            self._url = URL.build(scheme="http", host=host, port=port)
        else:
            self._url = URL.build(scheme="https", host=host, port=port)

    def _split_server(self, server: str) -> Tuple[str, Optional[int]]:
        parts = server.split(":")
        if len(parts) == 1:
            return parts[0], None
        else:
            return parts[0], int(parts[1])

    @property
    def url(self) -> URL:
        return self._url

    def version(self) -> URL:
        return self._url / "v2/"

    def manifest(self, name: str, ref: str) -> URL:
        return self._url / "v2" / name / "manifests" / ref

    def blob(self, name: str, layer_digest: str) -> URL:
        return self._url / "v2" / name / "blobs" / layer_digest

    def blob_uploads(self, name: str) -> URL:
        return self._url / "v2" / name / "blobs/uploads/"


class RegistryClient:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    def _get_auth_header(self, auth: Optional[Auth]) -> Dict[str, str]:
        return {aiohttp.hdrs.AUTHORIZATION: auth.header} if auth else {}

    async def get_version(self, server: str, auth: Optional[Auth] = None) -> str:
        endpoints = V2Endpoints(server)
        async with self._session.get(
            endpoints.version(), headers=self._get_auth_header(auth)
        ) as resp:
            if resp.status == 404:
                return "v1"
            resp.raise_for_status()
            return "v2"

    async def check_layer(
        self, server: str, name: str, digest: str, auth: Optional[Auth] = None
    ) -> bool:
        endpoints = V2Endpoints(server)
        async with self._session.head(
            endpoints.blob(name, digest), headers=self._get_auth_header(auth)
        ) as resp:
            if resp.status == 404:
                return False
            resp.raise_for_status()
            return True

    async def start_layer_upload(
        self, server: str, name: str, auth: Optional[Auth] = None
    ) -> URL:
        endpoints = V2Endpoints(server)
        async with self._session.post(
            endpoints.blob_uploads(name), headers=self._get_auth_header(auth)
        ) as resp:
            resp.raise_for_status()
            return URL(resp.headers[aiohttp.hdrs.LOCATION])

    async def upload_layer_chunk(
        self,
        upload_url: Union[str, URL],
        offset: int,
        chunk: bytes,
        media_type: str,
        auth: Optional[Auth] = None,
    ) -> URL:
        range_len = len(chunk)
        range_start = offset
        range_end = offset + range_len - 1
        async with self._session.patch(
            upload_url,
            headers={
                **self._get_auth_header(auth),
                aiohttp.hdrs.CONTENT_RANGE: f"{range_start}-{range_end}",
                aiohttp.hdrs.CONTENT_LENGTH: str(range_len),
                aiohttp.hdrs.CONTENT_TYPE: media_type,
            },
            data=chunk,
        ) as resp:
            if resp.status == aiohttp.web.HTTPRequestRangeNotSatisfiable.status_code:
                _, last = resp.headers["Range"].split("-")
                raise InvalidRangeError(int(last))
            resp.raise_for_status()
            return URL(resp.headers[aiohttp.hdrs.LOCATION])

    async def complete_layer_upload(
        self,
        upload_url: Union[str, URL],
        media_type: str,
        digest: str,
        offset: int,
        chunk: bytes,
        auth: Optional[Auth] = None,
    ) -> None:
        range_len = len(chunk)
        range_start = offset
        range_end = offset + range_len - 1
        async with self._session.put(
            URL(upload_url).update_query(digest=digest),
            headers={
                **self._get_auth_header(auth),
                aiohttp.hdrs.CONTENT_RANGE: f"{range_start}-{range_end}",
                aiohttp.hdrs.CONTENT_LENGTH: str(range_len),
                aiohttp.hdrs.CONTENT_TYPE: media_type,
            },
            data=chunk,
        ) as resp:
            resp.raise_for_status()

    async def cancel_layer_upload(
        self,
        upload_url: Union[str, URL],
        auth: Optional[Auth] = None,
    ) -> None:
        async with self._session.delete(
            upload_url, headers=self._get_auth_header(auth)
        ) as resp:
            resp.raise_for_status()

    async def update_manifest(
        self,
        server: str,
        name: str,
        ref: str,
        media_type: str,
        manifest: Dict[str, Any],
        auth: Optional[Auth] = None,
    ) -> None:
        endpoints = V2Endpoints(server)
        async with self._session.put(
            endpoints.manifest(name, ref),
            headers={
                **self._get_auth_header(auth),
                aiohttp.hdrs.CONTENT_TYPE: media_type,
            },
            data=json.dumps(manifest),
        ) as resp:
            resp.raise_for_status()
