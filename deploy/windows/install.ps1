<#
  CS2 Mod Management Tool: Windows agent setup. Run in PowerShell from the repository root:
    powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1

  What it does:
    1. checks the tools (Python 3.9+, Git, .NET SDK, Claude Code, ilspycmd) and prints install commands;
    2. creates %LOCALAPPDATA%\cs2mmt\secrets.toml (outside the workspace) and config\agent.toml;
    3. clones the mods into mods\ (from config\mods.toml) if they are missing;
    4. registers the scheduled task "CS2MMT Agent" that runs at log-on with highest privileges;
    5. runs the self-test.
  It does not change BIOS, power, auto-logon or Windows Update settings: docs\SETUP.md explains those.
#>
$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location $Repo

function Need($cmd, $hint) {
    $c = Get-Command $cmd -ErrorAction SilentlyContinue
    if (-not $c) { Write-Host "  [missing] $cmd  ->  $hint" -ForegroundColor Yellow; return $false }
    Write-Host "  [ok] $cmd ($($c.Source))"; return $true
}

Write-Host "1) tools"
$ok = $true
$ok = (Need 'python' 'winget install -e --id Python.Python.3.12') -and $ok
$ok = (Need 'git' 'winget install -e --id Git.Git') -and $ok
$ok = (Need 'dotnet' 'winget install -e --id Microsoft.DotNet.SDK.8') -and $ok
$ok = (Need 'claude' 'irm https://claude.ai/install.ps1 | iex   (then: claude setup-token)') -and $ok
if (-not (Need 'ilspycmd' 'dotnet tool install -g ilspycmd   (optional: API diff for Claude)')) { }
if (-not $ok) { Write-Host "Install the missing tools, open a new PowerShell and run this script again."; exit 1 }

Write-Host "2) configuration"
$SecretsDir = Join-Path $env:LOCALAPPDATA 'cs2mmt'
New-Item -ItemType Directory -Force -Path $SecretsDir | Out-Null
$Secrets = Join-Path $SecretsDir 'secrets.toml'
if (-not (Test-Path $Secrets)) {
    Copy-Item (Join-Path $Repo 'config\agent-secrets.example.toml') $Secrets
    # Only this user may read it.
    icacls $Secrets /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
    Write-Host "  created $Secrets  -> fill in api_token and claude_oauth_token"
}
$AgentToml = Join-Path $Repo 'config\agent.toml'
if (-not (Test-Path $AgentToml)) {
    Copy-Item (Join-Path $Repo 'config\agent.example.toml') $AgentToml
    Write-Host "  created $AgentToml  -> set the controller URLs, test_save, git author"
}

Write-Host "3) mods"
$py = @'
import sys, subprocess
from pathlib import Path
sys.path.insert(0, ".")
from cs2mmt.common.registry import load_registry
for m in load_registry(Path("config/mods.toml")).mods:
    d = Path(m.dir)
    if (d / ".git").exists():
        print("  [ok] %s" % d)
        continue
    print("  cloning %s -> %s" % (m.repo, d))
    subprocess.run(["git", "clone", "--branch", m.default_branch, m.repo, str(d)], check=True)
'@
python -c $py

Write-Host "4) scheduled task"
$action = New-ScheduledTaskAction -Execute (Get-Command python).Source -Argument "-m cs2mmt agent run --config `"$AgentToml`" --mode boot" -WorkingDirectory $Repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 12) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) -StartWhenAvailable
Register-ScheduledTask -TaskName 'CS2MMT Agent' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "  registered 'CS2MMT Agent' (at log-on of $env:USERNAME, highest privileges)"

Write-Host "5) self-test"
python -m cs2mmt agent selftest --config $AgentToml
Write-Host ""
Write-Host "Next: docs\SETUP.md section 'Windows' (auto-logon, Fast Startup off, Wake-on-LAN, Steam, Paradox login, test save),"
Write-Host "then a full rehearsal from Discord: !run rehearsal"
