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

export PIP_EXTRA_INDEX_URL ?= $(shell python pip_extra_index_url.py)

setup:
	pip install -U pip
	pip install -r requirements/dev.txt
	pre-commit install

setup_cri_grpc:
	go get -d github.com/gogo/protobuf/gogoproto
	go get -d k8s.io/cri-api/pkg/apis/runtime/v1

	rm -rf k8s github

	python -m grpc_tools.protoc \
		--proto_path=$(GOPATH)/src \
		--python_out=. \
		--grpc_python_out=. \
		--mypy_out=. \
		github.com/gogo/protobuf/gogoproto/gogo.proto \
		k8s.io/cri-api/pkg/apis/runtime/v1alpha2/api.proto \
		k8s.io/cri-api/pkg/apis/runtime/v1/api.proto

	# Fix folder structure after generation
	mv github.com/gogo/protobuf/gogoproto/*.py github/com/gogo/protobuf/gogoproto
	mv k8s.io/cri_api/pkg/apis/runtime/v1alpha2/*.py k8s/io/cri_api/pkg/apis/runtime/v1alpha2
	mv k8s.io/cri_api/pkg/apis/runtime/v1/*.py k8s/io/cri_api/pkg/apis/runtime/v1

	touch github/com/gogo/protobuf/gogoproto/__init__.py
	touch k8s/io/cri_api/pkg/apis/runtime/v1/__init__.py
	touch k8s/io/cri_api/pkg/apis/runtime/v1alpha2/__init__.py

	rm -rf github.com
	rm -rf k8s.io

lint: format
	mypy platform_container_runtime tests setup.py

format:
ifdef CI_LINT_RUN
	pre-commit run --all-files --show-diff-on-failure
else
	pre-commit run --all-files
endif

test_unit:
	pytest -vv --cov=platform_container_runtime --cov-report xml:.coverage-unit.xml tests/unit

test_integration:
ifeq ($(MINIKUBE_DRIVER),none)
	make docker_build
else
	eval $$(minikube -p minikube docker-env); make docker_build
endif
	echo tests/integration/k8s/* | xargs -n 1 kubectl --context minikube apply -f
	docker-compose -f tests/integration/docker/docker-compose.yaml up -d
	export CRI_ADDRESS=$$(minikube service cri --url | sed -e "s/^http:\/\///"); \
	export RUNTIME_ADDRESS=$$(minikube service runtime --url | sed -e "s/^http:\/\///"); \
	export SVC_ADDRESS=$$(minikube service platform-container-runtime --url | sed -e "s/^http:\/\///"); \
	$(WAIT_FOR_IT) $$CRI_ADDRESS -- echo "cri is up"; \
	$(WAIT_FOR_IT) $$SVC_ADDRESS -- echo "service is up"; \
	$(WAIT_FOR_IT) 0.0.0.0:5000 -- echo "registry is up";
	pytest -vv --cov=platform_container_runtime --cov-report xml:.coverage-integration.xml tests/integration; \
	exit_code=$$?; \
	docker-compose -f tests/integration/docker/docker-compose.yaml down -v; \
	exit $$exit_code

docker_build:
	python setup.py sdist
	docker build \
		--build-arg PIP_EXTRA_INDEX_URL \
		--build-arg DIST_FILENAME=`python setup.py --fullname`.tar.gz \
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
	helm init --client-only
	helm plugin install https://github.com/belitre/helm-push-artifactory-plugin
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
		--namespace platform --install --wait --timeout 600

artifactory_helm_push: _helm_fetch _helm_expand_vars
	helm package --app-version=$(TAG) --version=$(TAG) temp_deploy/$(HELM_CHART)
	helm push-artifactory $(HELM_CHART)-$(TAG).tgz $(ARTIFACTORY_HELM_REPO) \
		--username $(ARTIFACTORY_USERNAME) \
		--password $(ARTIFACTORY_PASSWORD)

artifactory_helm_deploy:
	helm upgrade $(HELM_CHART) neuro/$(HELM_CHART) \
		-f deploy/$(HELM_CHART)/values-$(HELM_ENV).yaml \
		--version $(TAG) --namespace platform --install --wait --timeout 600
