from setuptools import find_packages, setup


setup_requires = ("setuptools_scm",)


install_requires = (
    "aiohttp==3.7.4",
    "platform-logging==21.5.13",
    "aiozipkin==1.1.0",
    "sentry-sdk==1.1.0",
)

setup(
    name="platform-container-runtime",
    use_scm_version={
        "git_describe_command": "git describe --dirty --tags --long --match v*.*.*",
    },
    url="https://github.com/neuro-inc/platform-container-runtime",
    packages=find_packages(),
    install_requires=install_requires,
    setup_requires=setup_requires,
    python_requires=">=3.7",
    entry_points={
        "console_scripts": [
            "platform-container-runtime=platform_container_runtime.api:main"
        ]
    },
    zip_safe=False,
)
