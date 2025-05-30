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
    @property
    @abc.abstractmethod
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
            await self._raise_for_status(resp)
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
            await self._raise_for_status(resp)
            return True

    async def start_blob_upload(
        self, server: str, name: str, auth: Optional[Auth] = None
    ) -> URL:
        endpoints = V2Endpoints(server)
        async with self._session.post(
            endpoints.blob_uploads(name), headers=self._get_auth_header(auth)
        ) as resp:
            await self._raise_for_status(resp)
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
        headers = {
            aiohttp.hdrs.CONTENT_LENGTH: str(data_length),
            aiohttp.hdrs.CONTENT_TYPE: media_type,
        }
        upload_url = URL(upload_url).update_query(digest=digest)

        # Google returns an error if the Authorization header is included.
        # The upload URL is a signed URL that already contains authentication
        # credentials, so adding extra credentials (like an Authorization header)
        # causes a conflict.
        # Response returned from POST /v2/{name}/blobs/uploads/ doesn't have
        # any Google related headers headers (like X-Goog-*).
        if (
            not upload_url.host_subcomponent
            or not upload_url.host_subcomponent.endswith(".pkg.dev")
        ):
            headers.update(**self._get_auth_header(auth))

        async with self._session.put(
            upload_url,
            headers=headers,
            data=data,
        ) as resp:
            await self._raise_for_status(resp)

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
            await self._raise_for_status(resp)

    @classmethod
    async def _raise_for_status(cls, response: aiohttp.ClientResponse) -> None:
        try:
            response.raise_for_status()
        except aiohttp.ClientResponseError as exc:
            content = await response.text()
            exc.message += f"\n\n{content}"
            raise exc
