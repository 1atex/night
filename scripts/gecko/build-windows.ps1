param([Parameter(Mandatory)] [ValidateSet('tools', 'archive', 'source', 'configure', 'build')] [string] $Phase)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

$repository = (Resolve-Path "$PSScriptRoot/../..").Path
$root = 'C:/gecko'
$pin = Get-Content "$repository/scripts/preview/runtime.json" -Raw | ConvertFrom-Json
$archive = "$root/source.tar.xz"
$setup = "$root/MozillaBuild.exe"
New-Item -ItemType Directory -Force $root, "$root/src", "$root/runtime" | Out-Null

function Get-PinnedFile($Uri, $Path, $Sha256) {
    curl.exe --fail --location --retry 3 --silent --show-error $Uri --output $Path
    if ((Get-FileHash $Path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Sha256) {
        throw "SHA-256 mismatch for $Uri"
    }
}

$msys = "$env:MOZILLABUILD/msys2/usr/bin"
if ($Phase -eq 'tools') {
    Get-PinnedFile 'https://ftp.mozilla.org/pub/mozilla/libraries/win32/MozillaBuildSetup-4.2.1.exe' $setup '8ef5b3d2087e4867dc75bf19f5e62f12e917f518239415723cc70896b6f1e3d8'
    $installer = Start-Process $setup -ArgumentList '/S', "/D=$env:MOZILLABUILD" -Wait -PassThru
    if ($installer.ExitCode -ne 0) { throw "MozillaBuild installation failed ($($installer.ExitCode))" }
    if (!(Test-Path "$msys/sh.exe")) { throw "MozillaBuild shell is missing" }
    return
}

if ($Phase -eq 'archive') {
    if (!(Test-Path $archive) -or (Get-FileHash $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $pin.source_archive.sha256) {
        Get-PinnedFile $pin.source_archive.url $archive $pin.source_archive.sha256
    }
    return
}

if ($Phase -eq 'source') {
    tar.exe -xf $archive --strip-components 1 -C "$root/src"
    if (!(Test-Path "$root/src/mach")) { throw "Source archive did not contain mach" }
    return
}

if (!(Test-Path "$msys/sh.exe")) { throw "MozillaBuild shell is missing" }
$env:MOZ_SOURCE_REPO = ($pin.source -split '/rev/')[0]
$env:MOZ_SOURCE_CHANGESET = $pin.revision
$env:MOZCONFIG = "$root/src/mozconfig"
$env:Path = "$msys;$env:MOZILLABUILD/bin;$env:Path"
Copy-Item "$PSScriptRoot/windows.mozconfig" $env:MOZCONFIG
Push-Location "$root/src"
try {
    if ($Phase -eq 'configure') {
        python -u mach configure 2>&1 | Tee-Object -FilePath "$root/configure.log"
        return
    }
    python -u mach build 2>&1 | Tee-Object -FilePath "$root/build.log"
    python -u mach package 2>&1 | Tee-Object -FilePath "$root/package.log"
} finally {
    Pop-Location
}
$packages = @(Get-ChildItem "$root/obj/dist" -Filter '*.win64.zip')
if ($packages.Count -ne 1) { throw "Expected one Firefox Windows archive" }
7z x -y "-o$root/runtime" $packages[0].FullName
if (!(Test-Path "$root/runtime/firefox/xul.dll")) { throw "Compiled xul.dll is missing" }
Get-FileHash "$root/runtime/firefox/xul.dll" -Algorithm SHA256
