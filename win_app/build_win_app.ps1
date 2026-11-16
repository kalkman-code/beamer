# Builds dist\Beamer.exe. With -Release it also builds Beamer-Setup-<version>.exe with Inno Setup
# into ..\..\docs\beamer-releases\<version>\, from a clean commit.
param([switch]$Release, [switch]$ExperimentalSecureDesktop)

$ErrorActionPreference = 'Stop'

# Windows PowerShell 5.1 turns a native command's stderr into an error record, and with the
# preference above a pip upgrade notice or unittest's own progress -- both ordinary stderr --
# aborts the build with NativeCommandError. Merging with 2>&1 is not enough; the preference has
# to be relaxed for the call itself, which this does inside a function so it stays local. Every
# call checks $LASTEXITCODE afterwards, which survives the function, so nothing is lost.
function Invoke-Native {
    param([Parameter(Mandatory = $true)][scriptblock]$Command)
    $ErrorActionPreference = 'Continue'
    & $Command
}

function Get-BeamerSignTool {
    $Certificate = Get-ChildItem Cert:\CurrentUser\My |
        Where-Object Thumbprint -eq 'EA6F2A81A84E49D31FF7965A50D7DE771601DA0A' |
        Select-Object -First 1
    if (-not $Certificate) {
        Write-Host 'Windows signing: unsigned (Certum certificate absent from CurrentUser\My).'
        return $null
    }

    $Tool = Get-Command signtool.exe -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty Source
    if (-not $Tool) {
        $SdkBin = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin'
        $Tool = Get-ChildItem "$SdkBin\*\x64\signtool.exe" -ErrorAction SilentlyContinue |
            Sort-Object { [version]$_.Directory.Parent.Name } -Descending |
            Select-Object -First 1 -ExpandProperty FullName
    }
    if (-not $Tool) { throw 'Certum certificate is present, but Windows SDK SignTool is missing' }
    return $Tool
}

function Sign-BeamerArtifact {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not $SignTool) { return }
    Invoke-Native { & $SignTool sign /sha1 EA6F2A81A84E49D31FF7965A50D7DE771601DA0A /tr http://time.certum.pl /td sha256 /fd sha256 $Path }
    if ($LASTEXITCODE -ne 0) { throw "Certum signing failed: $Path" }
    Invoke-Native { & $SignTool verify /pa /v /tw $Path }
    if ($LASTEXITCODE -ne 0) { throw "Authenticode verification failed: $Path" }
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
if (-not $ScriptDir) { $ScriptDir = Get-Location }
Set-Location $ScriptDir
$ProjectDir = Split-Path -Parent $ScriptDir

if ($Release) {
    # docs\ is not part of the build and changes independently of it; anything else uncommitted
    # would ship code that no commit records.
    $Dirty = git -C $ProjectDir status --porcelain -- . ':(exclude)docs'
    if ($Dirty) { throw 'Uncommitted changes outside docs\ - commit them so the release matches a commit' }
    $Iscc = Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'
    if (-not (Test-Path $Iscc)) { throw "Inno Setup 6 is not installed at $Iscc" }
}

$SignTool = Get-BeamerSignTool

$Python = Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source
if (-not $Python) { throw 'Python was not found on PATH' }

if (-not (Test-Path .venv\Scripts\python.exe)) {
    Invoke-Native { & $Python -m venv .venv }
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed' }
}

Invoke-Native { & .\.venv\Scripts\python.exe -m pip install -r requirements-win.txt }
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }

Invoke-Native { & .\.venv\Scripts\python.exe -m unittest discover -s tests -v }
if ($LASTEXITCODE -ne 0) { throw 'Windows tests failed' }

Invoke-Native { & .\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm --distpath dist --workpath build build.spec }
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }

$Output = Join-Path $ScriptDir 'dist\Beamer.exe'
if (-not (Test-Path $Output)) { throw 'PyInstaller did not create Beamer.exe' }
Sign-BeamerArtifact $Output
Write-Output "Built $Output"

if ($ExperimentalSecureDesktop) {
    Invoke-Native { & .\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm --distpath dist --workpath build-secure secure_build.spec }
    if ($LASTEXITCODE -ne 0) { throw 'Secure desktop PyInstaller build failed' }
}

if ($Release) {
    $Version = (Get-Content (Join-Path $ProjectDir 'VERSION') -Raw).Trim()
    $ReleaseDir = [IO.Path]::GetFullPath((Join-Path $ProjectDir "..\docs\beamer-releases\$Version"))
    $IsccArgs = @("/O$ReleaseDir")
    if ($SignTool) {
        # Same signtool line as Sign-BeamerArtifact. Inno Setup turns $q into a quote and $f into the
        # quoted file it signs; literal quotes here would be re-quoted on the way to ISCC.
        $IsccArgs += '/DSignInstaller'
        $IsccArgs += '/Sbeamer=$q' + $SignTool + '$q sign /sha1 EA6F2A81A84E49D31FF7965A50D7DE771601DA0A /tr http://time.certum.pl /td sha256 /fd sha256 $f'
    }
    if ($ExperimentalSecureDesktop) { $IsccArgs += '/DSecureDesktop=1' }
    $IsccArgs += 'Beamer-Setup.iss'
    & $Iscc @IsccArgs
    if ($LASTEXITCODE -ne 0) { throw 'Inno Setup failed' }
    $Setup = Join-Path $ReleaseDir "Beamer-Setup-$Version.exe"
    Sign-BeamerArtifact $Setup
    $Commit = git -C $ProjectDir rev-parse --short HEAD
    Write-Output "Released $Setup from $Commit"
}
