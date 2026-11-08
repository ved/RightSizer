PY ?= python3
PROM ?= http://127.0.0.1:9090
NS ?= explainer-lab
WINDOW ?= 6h
KRR ?=

.PHONY: test lab collect verdict

test:
	PYTHONPATH=src $(PY) -m unittest discover -s tests -v

lab:
	kubectl apply -f scenarios/explainer-lab.yaml

collect:
	PYTHONPATH=src $(PY) -m explainer.collect --prom $(PROM) -n $(NS) --window $(WINDOW) --out evidence $(if $(KRR),--krr $(KRR))

PROFILE ?= lab

verdict:
	PYTHONPATH=src $(PY) -m explainer.verdict --evidence evidence --out verdicts --profile $(PROFILE)

MODEL ?= qwen2.5-coder:14b
ONLY ?=

.PHONY: explain
explain:
	PYTHONPATH=src $(PY) -m explainer.explain --evidence evidence --out explanations --profile $(PROFILE) --model $(MODEL) $(if $(ONLY),--only $(ONLY))

MODELS ?= qwen2.5-coder:14b
.PHONY: benchmark
benchmark:
	PYTHONPATH=src $(PY) -m explainer.benchmark --models $(MODELS) --evidence evidence --out benchmark
