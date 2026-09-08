$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    if (-not (Test-Path -LiteralPath '.buildenv\Scripts\python.exe')) {
        py -3 -m venv .buildenv
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.12+ with tkinter is required only for building from source.' }
    }
    & .\.buildenv\Scripts\python.exe -m pip install 'pyinstaller==6.22.2'
    if ($LASTEXITCODE -ne 0) { throw 'Could not install PyInstaller.' }
    & .\.buildenv\Scripts\python.exe -m PyInstaller --noconfirm --onedir --console --name TVCheck --icon "$PSScriptRoot/assets/tvcheck.ico" --add-data "$PSScriptRoot/assets;assets" --add-data "$PSScriptRoot/profiles;profiles" --distpath dist --workpath .build --specpath .build "$PSScriptRoot/main.py"
    if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
    Copy-Item -LiteralPath tools -Destination dist\TVCheck -Recurse -Force
    foreach ($Name in @('Install.cmd','Install.ps1','Uninstall.cmd','Uninstall.ps1')) {
        Copy-Item -LiteralPath (Join-Path packaging $Name) -Destination dist\TVCheck -Force
    }
    foreach ($Name in @('Usage.txt','THIRD_PARTY.txt','CHANGELOG.txt')) {
        Copy-Item -LiteralPath $Name -Destination dist\TVCheck -Force
    }
    New-Item -ItemType Directory -Path dist\TVCheck\source -Force | Out-Null
    Get-ChildItem -LiteralPath . -Filter '*.py' | Copy-Item -Destination dist\TVCheck\source -Force
    Copy-Item -LiteralPath profiles -Destination dist\TVCheck\source -Recurse -Force
    Copy-Item -LiteralPath assets -Destination dist\TVCheck\source -Recurse -Force
    & .\dist\TVCheck\TVCheck.exe --check-deps
    if ($LASTEXITCODE -ne 0) { throw 'Packaged dependency check failed.' }
    Compress-Archive -Path dist\TVCheck -DestinationPath dist\TVCheck-Windows-x64.zip -Force
    Write-Host 'Ready: dist\TVCheck\TVCheck.exe and dist\TVCheck-Windows-x64.zip'
} finally { Pop-Location }
