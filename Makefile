.PHONY: verify test python-test demo-test ops-test jvm-test domain-smoke evidence docker-check

verify:
	python3 scripts/verify_repo.py

python-test:
	python3 -m pytest -q services/reconciler/tests

demo-test:
	python3 -m pytest -q demo/test_demo.py demo/test_repair_integration.py

ops-test:
	python3 -m pytest -q services/ops-console/tests

jvm-test:
	mvn -B -DskipTests=false test

domain-smoke:
	bash scripts/jvm_domain_smoke.sh

evidence:
	python3 scripts/consistency_benchmark.py --orders 20000 --seed 42 --output /tmp/pickup-pact-benchmark.json
	python3 scripts/consistency_matrix.py --orders 20000 --seeds 11,22,33,44,55 --output /tmp/pickup-pact-matrix.json
	python3 scripts/pickup_policy_lab.py --orders 20000 --seed 20260922 --output /tmp/pickup-policy-lab.json
	python3 scripts/verify_evidence.py --benchmark-actual /tmp/pickup-pact-benchmark.json --matrix-actual /tmp/pickup-pact-matrix.json --policy-actual /tmp/pickup-policy-lab.json

docker-check:
	docker compose config >/dev/null
	docker compose build commitment merchant-fulfillment ledger reconciler ops-console
	docker build -f Dockerfile.demo -t pickup-pact-demo:local-check .

test: verify python-test demo-test ops-test jvm-test evidence
