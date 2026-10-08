<#
Point the "kaggle" SSH host at a new Cloudflare tunnel URL (Windows client).

    .\client\kaggle_ssh.ps1 https://xxxx.trycloudflare.com          # update ~/.ssh/config
    .\client\kaggle_ssh.ps1 https://xxxx.trycloudflare.com -Setup   # first time: install cloudflared + create a key
    ssh kaggle

The public key to put into the Kaggle secret SSH_PUBLIC_KEY is printed with -Setup.
#>
param(
    [Parameter(Mandatory = $true)][string]$Url,
    [string]$HostAlias = "kaggle",
    [string]$KeyPath = "$HOME\.ssh\kaggle_ed25519",
    [switch]$Setup
)
$ErrorActionPreference = "Stop"
$sshDir = "$HOME\.ssh"
New-Item -ItemType Directory -Force $sshDir | Out-Null

if ($Setup) {
    if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue) -and
        -not (Test-Path "${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe")) {
        winget install --id Cloudflare.cloudflared -e --source winget --accept-source-agreements --accept-package-agreements
    }
    if (-not (Test-Path $KeyPath)) {
        ssh-keygen -t ed25519 -f $KeyPath -N '""' -C "kaggle-ssh"
    }
    Write-Host "`nPublic key (Kaggle secret SSH_PUBLIC_KEY):"
    Get-Content "$KeyPath.pub"
}

$cf = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
if (-not $cf) { $cf = "${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe" }
if (-not (Test-Path $cf)) { throw "cloudflared not found - run with -Setup first" }

$hostName = ($Url -replace '^https?://', '').TrimEnd('/')
$block = @"
Host $HostAlias
    HostName $hostName
    User root
    IdentityFile $KeyPath
    IdentitiesOnly yes
    ProxyCommand "$cf" access ssh --hostname %h
    StrictHostKeyChecking accept-new
    UserKnownHostsFile ~/.ssh/known_hosts_$HostAlias
    ServerAliveInterval 30
"@

$cfg = "$sshDir\config"
$text = if (Test-Path $cfg) { Get-Content $cfg -Raw } else { "" }
# replace an existing "Host <alias>" block (up to the next Host line) or append a new one
$pattern = "(?ms)^Host\s+$([regex]::Escape($HostAlias))\s*$.*?(?=^Host\s|\z)"
if ($text -match $pattern) {
    $text = [regex]::Replace($text, $pattern, $block.TrimEnd() + "`r`n`r`n")
} else {
    $text = $text.TrimEnd() + "`r`n`r`n" + $block + "`r`n"
}
[IO.File]::WriteAllText($cfg, $text.TrimStart(), (New-Object Text.UTF8Encoding $false))
Write-Host "Updated $cfg -> $hostName"
Write-Host "Connect with:  ssh $HostAlias"
