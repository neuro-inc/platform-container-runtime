import asyncio
import json
import os
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional

import pytest
import yaml
from yarl import URL

from platform_container_runtime.kube_client import (
    KubeClient,
    KubeClientAuthType,
    KubeConfig,
)


@dataclass(frozen=True)
class Pod:
    name: str
    container_id: str


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
        opts += " -i --leave-stdin-open=true"

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


async def get_service_cluster_ip(service_name: str, namespace: str = "default") -> str:
    process = await asyncio.create_subprocess_shell(
        f"kubectl --context minikube -n {namespace} get svc {service_name} -o json",
        stdout=subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    if process.returncode and process.returncode > 0:
        pytest.fail(f"Service {service_name} is unavailable.")
    payload = json.loads(stdout)
    return payload["spec"]["clusterIP"]


@pytest.fixture(scope="session")
def _kube_config_payload() -> Dict[str, Any]:
    kube_config_path = os.path.expanduser("~/.kube/config")
    with open(kube_config_path) as kube_config:
        return yaml.safe_load(kube_config)


@pytest.fixture(scope="session")
def _kube_config_cluster_payload(_kube_config_payload: Dict[str, Any]) -> Any:
    cluster_name = "minikube"
    clusters = {
        cluster["name"]: cluster["cluster"]
        for cluster in _kube_config_payload["clusters"]
    }
    return clusters[cluster_name]


@pytest.fixture(scope="session")
def _cert_authority_data_pem(
    _kube_config_cluster_payload: Dict[str, Any]
) -> Optional[str]:
    ca_path = _kube_config_cluster_payload["certificate-authority"]
    if ca_path:
        return Path(ca_path).read_text()
    return None


@pytest.fixture(scope="session")
def _kube_config_user_payload(_kube_config_payload: Dict[str, Any]) -> Any:
    user_name = "minikube"
    users = {user["name"]: user["user"] for user in _kube_config_payload["users"]}
    return users[user_name]


@pytest.fixture
def kube_config(
    _kube_config_cluster_payload: Dict[str, Any],
    _kube_config_user_payload: Dict[str, Any],
    _cert_authority_data_pem: Optional[str],
) -> KubeConfig:
    cluster = _kube_config_cluster_payload
    user = _kube_config_user_payload
    return KubeConfig(
        url=URL(cluster["server"]),
        auth_type=KubeClientAuthType.CERTIFICATE,
        cert_authority_data_pem=_cert_authority_data_pem,
        client_cert_path=user["client-certificate"],
        client_key_path=user["client-key"],
        # https://docs.aiohttp.org/en/stable/client_advanced.html#graceful-shutdown
        conn_force_close=True,
    )


@pytest.fixture
async def kube_client(kube_config: KubeConfig) -> AsyncIterator[KubeClient]:
    async with KubeClient(kube_config) as client:
        yield client


@pytest.fixture
async def kube_node(kube_client: KubeClient) -> str:
    nodes = await kube_client.get_nodes()
    assert len(nodes) == 1, "Should be exactly one minikube node"
    return nodes[0].metadata.name
