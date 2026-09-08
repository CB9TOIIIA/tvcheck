param(
    [string]$Destination = (Join-Path $env:LOCALAPPDATA 'Programs\TVCheck'),
    [switch]$NoLaunch,
    [switch]$PortableOnly
)
$ErrorActionPreference = 'Stop'
$Source = $PSScriptRoot
$Destination = [IO.Path]::GetFullPath($Destination).TrimEnd('\')
if (-not (Test-Path -LiteralPath (Join-Path $Source 'TVCheck.exe'))) {
    throw 'Run Install.cmd from the complete distribution folder, after extracting the ZIP.'
}
if ($Destination -eq [IO.Path]::GetFullPath($Source).TrimEnd('\')) {
    throw 'The installation target must differ from the distribution folder.'
}
if ((Test-Path -LiteralPath $Destination) -and -not (Test-Path -LiteralPath (Join-Path $Destination 'TVCheck.install.json'))) {
    throw 'Target folder already exists and is not owned by TVCheck. Choose a different destination.'
}
Write-Host 'Checking bundled analyzers...'
& (Join-Path $Source 'TVCheck.exe') --check-deps
if ($LASTEXITCODE -ne 0) { throw 'Dependency check failed. Extract the complete distribution again.' }
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
foreach ($Name in @('TVCheck.exe', '_internal', 'tools', 'Install.cmd', 'Install.ps1', 'Uninstall.cmd', 'Uninstall.ps1', 'Usage.txt', 'THIRD_PARTY.txt', 'CHANGELOG.txt')) {
    $Item = Join-Path $Source $Name
    if (Test-Path -LiteralPath $Item) { Copy-Item -LiteralPath $Item -Destination $Destination -Recurse -Force }
}
@{ Product = 'TVCheck'; Version = '2.0'; Path = $Destination; Integration = (-not $PortableOnly) } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Destination 'TVCheck.install.json') -Encoding UTF8
$Exe = Join-Path $Destination 'TVCheck.exe'
if (-not $PortableOnly) {
& $Exe --install-menu
if ($LASTEXITCODE -ne 0) { throw 'Files copied, but context menu registration failed. Retry from the application Settings menu.' }
$Shell = New-Object -ComObject WScript.Shell
$Programs = [Environment]::GetFolderPath('Programs')
$Shortcut = $Shell.CreateShortcut((Join-Path $Programs 'TVCheck.lnk'))
$Shortcut.TargetPath = $Exe
$Shortcut.WorkingDirectory = $Destination
$Shortcut.IconLocation = "$Exe,0"
$Shortcut.Save()
$Key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\TVCheck'
New-Item -Path $Key -Force | Out-Null
New-ItemProperty -Path $Key -Name DisplayName -Value 'TVCheck' -PropertyType String -Force | Out-Null
New-ItemProperty -Path $Key -Name DisplayVersion -Value '2.0' -PropertyType String -Force | Out-Null
New-ItemProperty -Path $Key -Name InstallLocation -Value $Destination -PropertyType String -Force | Out-Null
New-ItemProperty -Path $Key -Name DisplayIcon -Value "$Exe,0" -PropertyType String -Force | Out-Null
$Uninstall = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $Destination 'Uninstall.ps1') + '" -Destination "' + $Destination + '"'
New-ItemProperty -Path $Key -Name UninstallString -Value $Uninstall -PropertyType String -Force | Out-Null
}
Write-Host "Installed: $Destination"
Write-Host 'Python is not required. Explorer integration is skipped only with -PortableOnly.'
if (-not $NoLaunch) { Start-Process -FilePath $Exe }
