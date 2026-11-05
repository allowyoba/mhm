SHELL := /bin/bash
.DEFAULT_GOAL := help
PYTHON ?= python3
VENV_PY := $(if $(filter Windows_NT,$(OS)),.venv/Scripts/python.exe,.venv/bin/python)
PY := $(if $(wildcard $(VENV_PY)),$(VENV_PY),$(PYTHON))

.PHONY: version linux deb apt-repository help setup build build-setup install diagnose repair uninstall test lint smoke extensions checksums validate clean
help:
	@echo "make setup / build / install ARGS='...'"
	@echo "make diagnose / repair / uninstall ARGS='...'"
	@echo "make test / lint / smoke / extensions / checksums / validate / clean"
setup: $(VENV_PY)
$(VENV_PY): native-host/requirements-build.txt native-host/requirements-runtime.txt
	$(PYTHON) -m venv .venv
	$(VENV_PY) -m pip install -r native-host/requirements-build.txt
	@touch $(VENV_PY)
build: $(VENV_PY)
	$(PY) scripts/build_setup.py $(BUILD_ARGS)
build-setup: build
install: $(VENV_PY)
	$(PY) -B scripts/install.py $(ARGS)
diagnose repair uninstall: $(VENV_PY)
	$(PY) -B setup/main.py --$@ $(ARGS)
test: $(VENV_PY)
	$(PY) -B -m unittest discover -s tests -v
lint:
	$(PY) -B scripts/sync_version.py --check
	$(PY) -B scripts/validate_release.py
	$(PY) -B -c 'import ast, pathlib; [ast.parse(p.read_text(encoding="utf-8"), filename=str(p)) for d in ("setup", "scripts", "native-host", "tests") for p in pathlib.Path(d).rglob("*.py")]'
smoke: build
	$(PY) -B scripts/smoke_setup.py $(SMOKE_ARGS)
extensions: $(VENV_PY)
	$(PY) scripts/build_browser_packages.py $(ARGS)
checksums:
	$(PY) scripts/make_checksums.py
validate:
	$(PY) -B scripts/validate_release.py $(ARGS)
clean:
	$(PY) -c 'import shutil; [shutil.rmtree(p, ignore_errors=True) for p in (".build", "dist")]'

version:
	$(PY) scripts/sync_version.py
linux:
	./scripts/build_linux.sh
deb:
	$(PY) scripts/build_deb.py $(ARGS)
apt-repository:
	$(PY) scripts/build_apt_repository.py $(ARGS)
