.PHONY: setup download warehouse quality train defect dashboard demo test lint serve clean

PYTHON ?= python

setup:
	$(PYTHON) -m venv .venv
	.venv/Scripts/python -m pip install --upgrade pip
	.venv/Scripts/python -m pip install -r requirements-dev.txt

download:
	$(PYTHON) -m src.cli download

warehouse:
	$(PYTHON) -m src.cli warehouse

quality:
	$(PYTHON) -m src.cli quality

train:
	$(PYTHON) -m src.cli train

defect:
	$(PYTHON) -m src.cli defect

dashboard:
	$(PYTHON) -m src.cli dashboard

demo:
	$(PYTHON) -m src.cli all

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m ruff check src tests

serve:
	$(PYTHON) -m src.cli serve

clean:
	rm -rf data artifacts logs dashboard/data dashboard/index.html
