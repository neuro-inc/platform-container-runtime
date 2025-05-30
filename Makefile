GOPATH ?= $(HOME)/go
IMAGE_NAME   ?= platformcontainerruntime

.PHONY: venv
venv:
	poetry lock
	poetry install --with dev --no-root
	poetry run scripts/genpb2.sh
	poetry install --only-root

.PHONY: setup
setup: venv
	poetry run pre-commit install;

.PHONY: lint
lint: format
	poetry run mypy platform_container_runtime tests

.PHONY: format
format:
ifdef CI
	poetry run pre-commit run --all-files --show-diff-on-failure
else
	poetry run pre-commit run --all-files
endif

.PHONY: test_unit
test_unit:
	poetry run pytest -vv --cov-config=pyproject.toml --cov-report xml:.coverage-unit.xml tests/unit

.PHONY: ensure-minikube
ensure-minikube:
	@kubectl config get-contexts minikube >/dev/null 2>&1 || ( \
	  echo "⏳ Starting Minikube…" && \
	  minikube start \
	)

.PHONY: test_integration
test_integration: ensure-minikube minikube_image_load
	echo tests/integration/k8s/* | xargs -n 1 kubectl --context minikube apply -f
	kubectl --context minikube get po -o name | xargs -n 1 kubectl --context minikube wait --for=jsonpath='{.status.phase}'=Running
	kubectl --context minikube get po

	poetry run pytest -vv --cov-config=pyproject.toml --cov-report xml:.coverage-integration.xml tests/integration

.PHONY: docker_build
docker_build: dist
	docker build \
		--build-arg PY_VERSION=$$(cat .python-version) \
		-t $(IMAGE_NAME):latest .

.python-version:
	@echo "Error: .python-version file is missing!" && exit 1

.PHONY: dist
dist:
	rm -rf build dist; \
	poetry export -f requirements.txt --without-hashes -o requirements.txt; \
	poetry build -f wheel;

.PHONY: minikube_image_load
minikube_image_load: docker_build
	@echo "💾 Saving $(IMAGE_NAME):latest to tar…"
	docker save $(IMAGE_NAME):latest -o $(IMAGE_NAME).tar
	@echo "🚚 Loading into Minikube…"
	minikube image load $(IMAGE_NAME).tar
	@rm -f $(IMAGE_NAME).tar

.PHONY: clean-protos
clean-protos:
	rm -rf scripts/temp
