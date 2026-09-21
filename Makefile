.PHONY: test benchmark benchmark-matrix verify python-test domain-smoke

python-test:
	python3 -m pytest -q services/reconciler/tests

domain-smoke:
	bash scripts/jvm_domain_smoke.sh

benchmark:
	python3 scripts/consistency_benchmark.py --orders 20000 --seed 42 --output artifacts/consistency-benchmark.json

benchmark-matrix:
	python3 scripts/consistency_matrix.py --orders 20000 --seeds 11,22,33,44,55 --output artifacts/consistency-matrix.json

verify:
	python3 scripts/verify_repo.py

test: python-test domain-smoke benchmark benchmark-matrix verify
