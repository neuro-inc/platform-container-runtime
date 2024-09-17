GOPATH ?= $(HOME)/go

WAIT_FOR_IT_URL = https://raw.githubusercontent.com/eficode/wait-for/master/wait-for
WAIT_FOR_IT = curl -s $(WAIT_FOR_IT_URL) | bash -s --

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
	kubectl --context minikube get po

	export CRI_ADDRESS=$$(minikube service cri --url --wait=30 | sed -e "s/^http:\/\///"); \
	$(WAIT_FOR_IT) $$CRI_ADDRESS -- echo "cri is up"
	export RUNTIME_ADDRESS=$$(minikube service runtime --url --wait=30 | sed -e "s/^http:\/\///"); \
	$(WAIT_FOR_IT) $$RUNTIME_ADDRESS -- echo "runtime is up"
	export REGISTRY_ADDRESS=$$(minikube service registry --url --wait=30 | sed -e "s/^http:\/\///"); \
	$(WAIT_FOR_IT) $$REGISTRY_ADDRESS -- echo "registry is up"
	export SVC_ADDRESS=$$(minikube service platform-container-runtime --url --wait=30 | sed -e "s/^http:\/\///"); \
	$(WAIT_FOR_IT) $$SVC_ADDRESS -- echo "service is up"

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
