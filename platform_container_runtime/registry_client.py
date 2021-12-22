import abc
import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Optional, Union

import aiohttp
import aiohttp.hdrs
import aiohttp.web
from yarl import URL


logger = logging.getLogger(__name__)


class Auth(abc.ABC):
    @abc.abstractproperty
    def header(self) -> str:
        pass


class BasicAuth(Auth):
    def __init__(self, username: str, password: str) -> None:
        super().__init__()

        self._username = username
        self._password = password

    @property
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

    def _split_server(self, server: str) -> tuple[str, Optional[int]]:
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

    def _get_auth_header(self, auth: Optional[Auth]) -> dict[str, str]:
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

    async def check_blob(
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

    async def start_blob_upload(
        self, server: str, name: str, auth: Optional[Auth] = None
    ) -> URL:
        endpoints = V2Endpoints(server)
        async with self._session.post(
            endpoints.blob_uploads(name), headers=self._get_auth_header(auth)
        ) as resp:
            resp.raise_for_status()
            return URL(resp.headers[aiohttp.hdrs.LOCATION])

    async def upload_blob(
        self,
        upload_url: Union[str, URL],
        media_type: str,
        digest: str,
        data_length: int,
        data: Union[bytes, AsyncIterator[bytes]],
        auth: Optional[Auth] = None,
    ) -> None:
        async with self._session.put(
            URL(upload_url).update_query(digest=digest),
            headers={
                **self._get_auth_header(auth),
                aiohttp.hdrs.CONTENT_LENGTH: str(data_length),
                aiohttp.hdrs.CONTENT_TYPE: media_type,
            },
            data=data,
        ) as resp:
            resp.raise_for_status()

    async def update_manifest(
        self,
        server: str,
        name: str,
        ref: str,
        media_type: str,
        manifest: dict[str, Any],
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
