AWS_ACCOUNT_ID ?= 771188043543
AWS_REGION ?= us-east-1

GITHUB_OWNER ?= neuro-inc

IMAGE_TAG ?= latest

IMAGE_REPO_aws    = $(AWS_ACCOUNT_ID).dkr.ecr.$(AWS_REGION).amazonaws.com
IMAGE_REPO_github = ghcr.io/$(GITHUB_OWNER)

IMAGE_REGISTRY ?= aws

IMAGE_NAME      = platformcontainerruntime
IMAGE_REPO_BASE = $(IMAGE_REPO_$(IMAGE_REGISTRY))
IMAGE_REPO      = $(IMAGE_REPO_BASE)/$(IMAGE_NAME)

HELM_ENV           ?= dev
HELM_CHART          = platform-container-runtime
HELM_CHART_VERSION ?= 1.0.0
HELM_APP_VERSION   ?= 1.0.0

GOPATH ?= $(HOME)/go

WAIT_FOR_IT_URL = https://raw.githubusercontent.com/eficode/wait-for/master/wait-for
WAIT_FOR_IT = curl -s $(WAIT_FOR_IT_URL) | bash -s --

setup:
	pip install -U pip
	pip install -e .[dev]
	pre-commit install

setup_pb2:
	scripts/genpb2.sh

lint: format
	mypy platform_container_runtime tests

format:
ifdef CI_LINT_RUN
	pre-commit run --all-files --show-diff-on-failure
else
	pre-commit run --all-files
endif

test_unit:
	pytest -vv --cov=platform_container_runtime --cov-report xml:.coverage-unit.xml tests/unit

test_integration: docker_build
	docker tag $(IMAGE_NAME):latest localhost/$(IMAGE_NAME):latest
	minikube image load localhost/$(IMAGE_NAME):latest
	echo tests/integration/k8s/* | xargs -n 1 kubectl --context minikube apply -f

	export CRI_ADDRESS=$$(minikube service cri --url | sed -e "s/^http:\/\///"); \
	$(WAIT_FOR_IT) $$CRI_ADDRESS -- echo "cri is up"
	export RUNTIME_ADDRESS=$$(minikube service runtime --url | sed -e "s/^http:\/\///"); \
	$(WAIT_FOR_IT) $$RUNTIME_ADDRESS -- echo "runtime is up"
	export REGISTRY_ADDRESS=$$(minikube service registry --url | sed -e "s/^http:\/\///"); \
	$(WAIT_FOR_IT) $$REGISTRY_ADDRESS -- echo "registry is up"
	export SVC_ADDRESS=$$(minikube service platform-container-runtime --url | sed -e "s/^http:\/\///"); \
	$(WAIT_FOR_IT) $$SVC_ADDRESS -- echo "service is up"

	pytest -vv --cov=platform_container_runtime --cov-report xml:.coverage-integration.xml tests/integration

docker_build:
	rm -rf build dist
	pip install -U build
	python -m build
	docker build \
		--build-arg PYTHON_BASE=slim-buster \
		-t $(IMAGE_NAME):latest .

docker_push: docker_build
	docker tag $(IMAGE_NAME):latest $(IMAGE_REPO):$(IMAGE_TAG)
	docker push $(IMAGE_REPO):$(IMAGE_TAG)

	docker tag $(IMAGE_NAME):latest $(IMAGE_REPO):latest
	docker push $(IMAGE_REPO):latest

helm_create_chart:
	export IMAGE_REPO=$(IMAGE_REPO); \
	export IMAGE_TAG=$(IMAGE_TAG); \
	export CHART_VERSION=$(HELM_CHART_VERSION); \
	export APP_VERSION=$(HELM_APP_VERSION); \
	VALUES=$$(cat charts/$(HELM_CHART)/values.yaml | envsubst); \
	echo "$$VALUES" > charts/$(HELM_CHART)/values.yaml; \
	CHART=$$(cat charts/$(HELM_CHART)/Chart.yaml | envsubst); \
	echo "$$CHART" > charts/$(HELM_CHART)/Chart.yaml

helm_deploy: helm_create_chart
	helm upgrade $(HELM_CHART) charts/$(HELM_CHART) \
		-f charts/$(HELM_CHART)/values-$(HELM_ENV).yaml \
		--namespace platform --install --wait --timeout 600s
