pipeline {
  agent any
  options { timestamps(); disableConcurrentBuilds() }
  stages {
    stage('Repository guardrails') {
      steps { sh 'python3 scripts/verify_repo.py' }
    }
    stage('Python tests') {
      steps { sh 'python3 -m pytest -q services/reconciler/tests' }
    }
    stage('Pure JVM domain smoke') {
      steps { sh 'bash scripts/jvm_domain_smoke.sh' }
    }
    stage('Consistency benchmark') {
      steps { sh 'python3 scripts/consistency_benchmark.py --orders 20000 --seed 42 --output artifacts/consistency-benchmark.json'
        sh 'python3 scripts/consistency_matrix.py --orders 20000 --seeds 11,22,33,44,55 --output artifacts/consistency-matrix.json' }
    }
    stage('JVM build when Maven is available') {
      steps {
        sh '''
          if command -v mvn >/dev/null 2>&1; then
            mvn -B -DskipTests=false test
          else
            echo "Maven is not installed on this runner; pure-domain smoke compilation already ran."
          fi
        '''
      }
    }
    stage('Container build') {
      when { expression { sh(script: 'command -v docker >/dev/null 2>&1', returnStatus: true) == 0 } }
      steps { sh 'docker compose build' }
    }
  }
  post {
    always { archiveArtifacts artifacts: 'artifacts/*.json', allowEmptyArchive: true }
  }
}
