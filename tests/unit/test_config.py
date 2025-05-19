from typing import Any

from yarl import URL

from platform_container_runtime.config import (
    Config,
    KubeClientAuthType,
    KubeConfig,
    ServerConfig,
)
from platform_container_runtime.config_factory import EnvironConfigFactory


class TestEnvironConfigFactory:
    def test_create_default(self) -> None:
        environ: dict[str, Any] = {
            "NP_HOST": "0.0.0.0",
            "NP_PORT": 8080,
            "NP_NODE_NAME": "minikube",
            "NP_KUBE_URL": "https://kubernetes.default.svc",
            "SENTRY_CLUSTER_NAME": "test",
        }
        config = EnvironConfigFactory(environ).create()
        assert config == Config(
            server=ServerConfig(host="0.0.0.0", port=8080),
            node_name="minikube",
            kube=KubeConfig(url=URL("https://kubernetes.default.svc")),
        )

    def test_create_custom(self) -> None:
        environ: dict[str, Any] = {
            "NP_HOST": "0.0.0.0",
            "NP_PORT": 8080,
            "NP_NODE_NAME": "minikube",
            "NP_KUBE_URL": "https://kubernetes.default.svc",
            "NP_CRI_ADDRESS": "unix://var/run/dockershim.sock",
            "NP_RUNTIME_ADDRESS": "unix://var/run/docker.sock",
            "SENTRY_DSN": "https://test.com",
            "SENTRY_CLUSTER_NAME": "test",
        }
        config = EnvironConfigFactory(environ).create()
        assert config == Config(
            server=ServerConfig(host="0.0.0.0", port=8080),
            node_name="minikube",
            cri_address="unix://var/run/dockershim.sock",
            runtime_address="unix://var/run/docker.sock",
            kube=KubeConfig(url=URL("https://kubernetes.default.svc")),
        )

    def test_create_kube(self) -> None:
        env = {
            "NP_KUBE_URL": "https://kubernetes.default.svc",
            "NP_KUBE_AUTH_TYPE": "token",
            "NP_KUBE_TOKEN": "k8s-token",
            "NP_KUBE_TOKEN_PATH": "k8s-token-path",
            "NP_KUBE_CERT_AUTHORITY_DATA": "k8s-ca-data",
            "NP_KUBE_CERT_AUTHORITY_PATH": "k8s-ca-path",
            "NP_KUBE_CLIENT_CERT_PATH": "k8s-client-cert-path",
            "NP_KUBE_CLIENT_KEY_PATH": "k8s-client-key-path",
            "NP_KUBE_CONN_FORCE_CLOSE": "1",
            "NP_KUBE_CONN_TIMEOUT": "100",
            "NP_KUBE_READ_TIMEOUT": "200",
            "NP_KUBE_CONN_POOL_SIZE": "300",
            "NP_KUBE_CONN_KEEP_ALIVE_TIMEOUT": "400",
        }
        result = EnvironConfigFactory(env).create_kube()

        assert result == KubeConfig(
            url=URL("https://kubernetes.default.svc"),
            auth_type=KubeClientAuthType.TOKEN,
            token="k8s-token",
            token_path="k8s-token-path",
            cert_authority_data_pem="k8s-ca-data",
            cert_authority_path="k8s-ca-path",
            client_cert_path="k8s-client-cert-path",
            client_key_path="k8s-client-key-path",
            conn_force_close=True,
            conn_timeout_s=100,
            read_timeout_s=200,
            conn_pool_size=300,
            conn_keep_alive_timeout_s=400,
        )
