# Run with &, not dot-sourcing. Adds only project-owned tools for this invocation.
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskRoot 'host-tools\python\Scripts\python.exe'
$taskPlatformTools = Join-Path $taskRoot 'host-tools\platform-tools'
$taskUsbDirectory = Join-Path $taskRoot 'host-tools\python\Lib\site-packages\libusb_package'
foreach ($taskRequired in @($taskPython, (Join-Path $taskPlatformTools 'adb.exe'),
                          (Join-Path $taskPlatformTools 'fastboot.exe'),
                          (Join-Path $taskUsbDirectory 'libusb-1.0.dll'))) {
    if (-not (Test-Path -LiteralPath $taskRequired -PathType Leaf)) {
        throw "Missing local dependency: $taskRequired. See README.md#setup."
    }
}
$taskOriginalPath = $env:PATH
try {
    $env:PATH = "$taskPlatformTools;$taskUsbDirectory;$taskOriginalPath"
    if ($MyInvocation.ExpectingInput) {
        $input | & $taskPython -X utf8 @args
    } else {
        & $taskPython -X utf8 @args
    }
    $taskExit = $LASTEXITCODE
} finally {
    $env:PATH = $taskOriginalPath
}
exit $taskExit
