PYTHON ?= python3
export PYTHONPATH := src

.PHONY: test accept install

test:
	$(PYTHON) -m unittest discover -s tests -t . -v

accept:
	$(PYTHON) scripts/acceptance.py

install:
	$(PYTHON) -m pip install -e .
