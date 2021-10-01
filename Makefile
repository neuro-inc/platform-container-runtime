AWS_ACCOUNT_ID ?= 771188043543
AWS_REGION ?= us-east-1

AZURE_RG_NAME ?= dev
AZURE_ACR_NAME ?= crc570d91c95c6aac0ea80afb1019a0c6f

ARTIFACTORY_DOCKER_REPO ?= neuro-docker-local-public.jfrog.io
ARTIFACTORY_HELM_REPO ?= https://neuro.jfrog.io/artifactory/helm-local-public
ARTIFACTORY_HELM_VIRTUAL_REPO ?= https://neuro.jfrog.io/artifactory/helm-virtual-public

IMAGE_REPO_gke         = $(GKE_DOCKER_REGISTRY)/$(GKE_PROJECT_ID)
IMAGE_REPO_aws         = $(AWS_ACCOUNT_ID).dkr.ecr.$(AWS_REGION).amazonaws.com
IMAGE_REPO_azure       = $(AZURE_ACR_NAME).azurecr.io
IMAGE_REPO_artifactory = $(ARTIFACTORY_DOCKER_REPO)

IMAGE_REGISTRY ?= artifactory

IMAGE_NAME = platformcontainerruntime
IMAGE_REPO = $(IMAGE_REPO_$(IMAGE_REGISTRY))/$(IMAGE_NAME)

HELM_CHART = platform-container-runtime

TAG ?= latest

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
	docker tag $(IMAGE_NAME):latest $(IMAGE_REPO):$(TAG)
	docker push $(IMAGE_REPO):$(TAG)

	docker tag $(IMAGE_NAME):latest $(IMAGE_REPO):latest
	docker push $(IMAGE_REPO):latest

aws_k8s_login:
	aws eks --region $(AWS_REGION) update-kubeconfig --name $(CLUSTER_NAME)

azure_k8s_login:
	az aks get-credentials --resource-group $(AZURE_RG_NAME) --name $(CLUSTER_NAME)

helm_install:
	curl https://raw.githubusercontent.com/kubernetes/helm/master/scripts/get | bash -s -- -v $(HELM_VERSION)
	helm plugin install https://github.com/belitre/helm-push-artifactory-plugin --version 1.0.2
	@helm repo add neuro $(ARTIFACTORY_HELM_VIRTUAL_REPO) \
		--username ${ARTIFACTORY_USERNAME} \
		--password ${ARTIFACTORY_PASSWORD}
	helm repo update

_helm_fetch:
	rm -rf temp_deploy
	mkdir -p temp_deploy/$(HELM_CHART)
	cp -Rf deploy/$(HELM_CHART) temp_deploy/
	find temp_deploy/$(HELM_CHART) -type f -name 'values*' -delete
	helm dependency update temp_deploy/$(HELM_CHART)

_helm_expand_vars:
	export IMAGE_REPO=$(IMAGE_REPO); \
	export IMAGE_TAG=$(TAG); \
	export DOCKER_SERVER=$(ARTIFACTORY_DOCKER_REPO); \
	cat deploy/$(HELM_CHART)/values-template.yaml | envsubst > temp_deploy/$(HELM_CHART)/values.yaml

helm_deploy: _helm_fetch _helm_expand_vars
	helm upgrade $(HELM_CHART) temp_deploy/$(HELM_CHART) \
		-f deploy/$(HELM_CHART)/values-$(HELM_ENV).yaml \
		--namespace platform --install --wait --timeout 600s

artifactory_helm_push: _helm_fetch _helm_expand_vars
	helm package --app-version=$(TAG) --version=$(TAG) temp_deploy/$(HELM_CHART)
	helm push-artifactory $(HELM_CHART)-$(TAG).tgz $(ARTIFACTORY_HELM_REPO) \
		--username $(ARTIFACTORY_USERNAME) \
		--password $(ARTIFACTORY_PASSWORD)

artifactory_helm_deploy:
	helm upgrade $(HELM_CHART) neuro/$(HELM_CHART) \
		-f deploy/$(HELM_CHART)/values-$(HELM_ENV).yaml \
		--version $(TAG) --namespace platform --install --wait --timeout 600s
