$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$env:PYTHONPATH = ".;services/reconciler"
python -m app.workbench --db .repair-review/review.sqlite --port 8765
