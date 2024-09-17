GOPATH ?= $(HOME)/go

setup:
	pip install -U pip
	pip install -e .[build-tools]
	scripts/genpb2.sh
	pip install -e .[dev]
	pre-commit install

lint: format
	mypy platform_container_runtime tests

format:
ifdef CI
	pre-commit run --all-files --show-diff-on-failure
else
	pre-commit run --all-files
endif

test_unit:
	pytest -vv --cov=platform_container_runtime --cov-report xml:.coverage-unit.xml tests/unit

test_integration: minikube_image_load
	echo tests/integration/k8s/* | xargs -n 1 kubectl --context minikube apply -f
	kubectl --context minikube get po -o name | xargs -n 1 kubectl --context minikube wait --for=jsonpath='{.status.phase}'=Running
	kubectl --context minikube get po

	pytest -vv --cov=platform_container_runtime --cov-report xml:.coverage-integration.xml tests/integration

docker_build:
	rm -rf build dist
	pip install -U build
	python -m build
	docker build -t platformcontainerruntime:latest .

minikube_image_load: docker_build
	docker tag platformcontainerruntime:latest localhost/platformcontainerruntime:latest
	minikube image load localhost/platformcontainerruntime:latest
