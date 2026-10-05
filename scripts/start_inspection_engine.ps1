param(
    [string]$WorkspaceRoot = 'C:\Users\user\hacknation7th',
    [int]$Port = 8877
)
$ErrorActionPreference = 'Stop'
$engineSource = Split-Path -Parent $PSScriptRoot
$enginePython = Join-Path $WorkspaceRoot 'output\post-hackathon\data-efficient-20261005\env\Scripts\python.exe'
$engineModel = Join-Path $WorkspaceRoot 'output\post-hackathon\sem-build-20261005\integrated-pretrained64-e3-r2\model'
$engineOutput = Join-Path $WorkspaceRoot 'output\post-hackathon\inspection-engine-20261005\live-runs'
$engineExample = Join-Path $WorkspaceRoot 'output\post-hackathon\inspection-engine-20261005\native-input\frame.json'
$engineImage = Join-Path $WorkspaceRoot 'output\post-hackathon\sem-build-20261005\data\data\images\84e1a58a093b4b61b28452d17973be64.jpg'
foreach ($engineRequired in @($enginePython, $engineModel, $engineExample, $engineImage)) {
    if (-not (Test-Path -LiteralPath $engineRequired)) { throw "Required local asset missing: $engineRequired" }
}
& $enginePython (Join-Path $engineSource 'scripts\serve_inspection_engine.py') --model-root $engineModel --out $engineOutput --example-frame $engineExample --example-image $engineImage --port $Port
exit $LASTEXITCODE
