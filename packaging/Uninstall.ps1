param([string]$Destination = (Join-Path $env:LOCALAPPDATA 'Programs\TVCheck'))
$ErrorActionPreference = 'Stop'
$Destination = [IO.Path]::GetFullPath($Destination).TrimEnd('\')
$Marker = Join-Path $Destination 'TVCheck.install.json'
if (-not (Test-Path -LiteralPath $Marker)) { throw 'Installation marker not found. No files were removed.' }
$Info = Get-Content -LiteralPath $Marker -Raw | ConvertFrom-Json
if ($Info.Product -ne 'TVCheck' -or $Info.Path -ne $Destination) { throw 'Invalid installation marker. No files were removed.' }
if ($Info.Integration -ne $false) {
    $Exe = Join-Path $Destination 'TVCheck.exe'
    if (Test-Path -LiteralPath $Exe) {
        & $Exe --uninstall-menu
        if ($LASTEXITCODE -ne 0) { throw 'Context menu removal failed. Files have not been removed.' }
    }
    $Shortcut = Join-Path ([Environment]::GetFolderPath('Programs')) 'TVCheck.lnk'
    if (Test-Path -LiteralPath $Shortcut) {
        $Shell = New-Object -ComObject WScript.Shell
        if ($Shell.CreateShortcut($Shortcut).TargetPath -eq $Exe) { Remove-Item -LiteralPath $Shortcut -Force }
    }
    $Key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\TVCheck'
    if (Test-Path -LiteralPath $Key) {
        if ((Get-ItemProperty -LiteralPath $Key).InstallLocation -eq $Destination) { Remove-Item -LiteralPath $Key -Recurse -Force }
    }
}
Remove-Item -LiteralPath $Destination -Recurse -Force
Write-Host 'TVCheck removed. Your video files, reports elsewhere, settings and custom TV profiles were not removed.'
