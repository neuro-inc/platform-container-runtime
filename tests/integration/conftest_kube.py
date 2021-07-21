import asyncio
import json
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict

import pytest
from yarl import URL


@dataclass(frozen=True)
class Pod:
    name: str
    container_id: str


@pytest.fixture
async def cri_address() -> str:
    url = URL(await get_service_url("cri"))
    return f"{url.host}:{url.port}"


async def get_pod(
    pod_name: str,
    namespace: str = "default",
) -> Dict[str, Any]:
    process = await asyncio.create_subprocess_shell(
        f"kubectl --context=minikube -n {namespace} get pod {pod_name} -o json",
        stdout=subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    return json.loads(stdout)


async def get_container_id(
    pod_name: str,
    namespace: str = "default",
    timeout_s: int = 30,
    interval_s: int = 1,
) -> str:
    while timeout_s:
        output = await get_pod(pod_name, namespace=namespace)
        container_statuses = output["status"].get("containerStatuses", ())

        if not container_statuses or any(
            "running" not in s.get("state", {}) for s in container_statuses
        ):
            time.sleep(interval_s)
            timeout_s -= interval_s
            continue

        return container_statuses[0]["containerID"]

    pytest.fail(f"Pod {pod_name!r} container is not running.")


@asynccontextmanager
async def run(
    image: str,
    cmd: str,
    namespace: str = "default",
    tty: bool = False,
    stdin: bool = False,
    attach: bool = False,
    timeout_s: int = 60,
    interval_s: int = 1,
) -> AsyncIterator[Pod]:
    pod_name = str(uuid.uuid4())
    opts = f" --attach={str(attach).lower()}"

    if stdin:
        opts += " -i"

    if tty:
        opts += " -t"

    try:
        kubectl = f"kubectl --context=minikube -n {namespace}"
        process = await asyncio.create_subprocess_shell(
            f"{kubectl} run {pod_name}{opts} --image {image} -- {cmd}"
        )
        await process.communicate()
        yield Pod(
            name=pod_name,
            container_id=await get_container_id(
                pod_name, timeout_s=timeout_s, interval_s=interval_s
            ),
        )
    finally:
        process = await asyncio.create_subprocess_shell(
            f"{kubectl} delete pod {pod_name} --force --grace-period=0"
        )
        await process.communicate()


async def get_service_url(service_name: str, namespace: str = "default") -> str:
    timeout_s = 60
    interval_s = 1

    while timeout_s:
        process = await asyncio.create_subprocess_shell(
            f"minikube service -n {namespace} {service_name} --url",
            stdout=subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        if stdout:
            url = stdout.decode().strip()
            # Sometimes `minikube service ... --url` returns a prefixed
            # string such as: "* https://127.0.0.1:8081/"
            start_idx = url.find("http")
            if start_idx > 0:
                url = url[start_idx:]
            return url
        time.sleep(interval_s)
        timeout_s -= interval_s

    pytest.fail(f"Service {service_name} is unavailable.")
