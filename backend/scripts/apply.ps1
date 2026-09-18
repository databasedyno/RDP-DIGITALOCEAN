# CloudInitApply - runs at every Windows boot (scheduled task, SYSTEM).
# Reads the droplet's DigitalOcean user-data, sets the Administrator password
# for this order, ensures RDP is on, and calls back to the reseller backend.
$ErrorActionPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$md = "http://169.254.169.254/metadata/v1/user-data"
$data = ""
for ($i = 0; $i -lt 40; $i++) {
    try {
        $data = (Invoke-WebRequest -UseBasicParsing -Uri $md -TimeoutSec 10).Content
        if ($data) { break }
    } catch {}
    Start-Sleep -Seconds 5
}

$cfg = @{}
foreach ($line in ($data -split "`n")) {
    $line = $line.Trim()
    if ($line -match '^([A-Z_]+)=(.*)$') { $cfg[$matches[1]] = $matches[2] }
}

# Always ensure Remote Desktop is enabled.
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Terminal Server" /v fDenyTSConnections /t REG_DWORD /d 0 /f | Out-Null
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp" /v UserAuthentication /t REG_DWORD /d 1 /f | Out-Null
netsh advfirewall firewall set rule group="remote desktop" new enable=Yes | Out-Null

$sid = $cfg["SERVER_ID"]
$marker = "C:\cloudinit\applied.txt"
$already = (Test-Path $marker) -and ((Get-Content $marker -ErrorAction SilentlyContinue) -eq $sid)

if ($cfg["ADMIN_PASSWORD"] -and $sid -and -not $already) {
    try { net user Administrator "$($cfg['ADMIN_PASSWORD'])" | Out-Null } catch {}
    Set-Content -Path $marker -Value $sid
    if ($cfg["CALLBACK_URL"] -and $cfg["CALLBACK_TOKEN"]) {
        $body = @{
            server_id = $sid; token = $cfg["CALLBACK_TOKEN"];
            stage = "rdp_ready"; progress = 100;
            message = "Windows booted from golden image; RDP ready"
        } | ConvertTo-Json -Compress
        for ($j = 0; $j -lt 10; $j++) {
            try { Invoke-RestMethod -UseBasicParsing -Uri $cfg["CALLBACK_URL"] -Method Post -ContentType "application/json" -Body $body; break } catch {}
            Start-Sleep -Seconds 10
        }
    }
}
