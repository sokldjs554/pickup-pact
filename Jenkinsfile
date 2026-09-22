pipeline {
  agent any

  options {
    timestamps()
    disableConcurrentBuilds()
  }

  stages {
    stage('Repository guardrails') {
      steps {
        sh 'python3 scripts/verify_repo.py'
      }
    }

    stage('Python tests') {
      parallel {
        stage('Reconciler') {
          steps { sh 'python3 -m pytest -q services/reconciler/tests' }
        }
        stage('Interview demo') {
          steps { sh 'python3 -m pytest -q demo/test_demo.py' }
        }
        stage('Flask ops console') {
          steps { sh 'python3 -m pytest -q services/ops-console/tests' }
        }
      }
    }

    stage('JVM tests') {
      steps {
        sh 'mvn -B -DskipTests=false test'
        sh 'bash scripts/jvm_domain_smoke.sh'
      }
    }

    stage('Reproduce committed evidence') {
      steps {
        sh 'python3 scripts/consistency_benchmark.py --orders 20000 --seed 42 --output /tmp/pickup-pact-benchmark.json'
        sh 'python3 scripts/consistency_matrix.py --orders 20000 --seeds 11,22,33,44,55 --output /tmp/pickup-pact-matrix.json'
        sh 'python3 scripts/pickup_policy_lab.py --orders 20000 --seed 20260922 --output /tmp/pickup-policy-lab.json'
        sh 'python3 scripts/verify_evidence.py --benchmark-actual /tmp/pickup-pact-benchmark.json --matrix-actual /tmp/pickup-pact-matrix.json --policy-actual /tmp/pickup-policy-lab.json'
      }
    }

    stage('Container build') {
      steps {
        sh 'docker compose config >/dev/null'
        sh 'docker compose build commitment ledger reconciler ops-console'
        sh 'docker build -f Dockerfile.demo -t pickup-pact-demo:jenkins .'
      }
    }
  }

  post {
    always {
      archiveArtifacts artifacts: 'artifacts/*.json', allowEmptyArchive: true
    }
  }
}
