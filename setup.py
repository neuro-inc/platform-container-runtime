from setuptools import find_namespace_packages, setup


setup_requires = ("setuptools_scm",)


install_requires = (
    "aiohttp==3.7.4.post0",
    "neuro-logging==21.8.4.1",
    "aiozipkin==1.1.0",
    "sentry-sdk==1.3.1",
    "grpcio==1.40.0",
    "protobuf==3.17.3",
    "aiodocker==0.21.0",
    "docker-image-py==0.1.12",
)

setup(
    name="platform-container-runtime",
    use_scm_version={
        "git_describe_command": "git describe --dirty --tags --long --match v*.*.*",
    },
    url="https://github.com/neuro-inc/platform-container-runtime",
    packages=find_namespace_packages(exclude=("tests",)),
    install_requires=install_requires,
    setup_requires=setup_requires,
    python_requires=">=3.8",
    entry_points={
        "console_scripts": [
            "platform-container-runtime=platform_container_runtime.api:main"
        ]
    },
    zip_safe=False,
)
