# Teaching Assistant Jarvis - one-line installer / updater for Windows.
#   irm https://raw.githubusercontent.com/hsinhsu64/teaching-assistant-jarvis/main/install.ps1 | iex
# Re-running the same line updates. Finished steps are skipped; config.toml and tools\ are kept.
# No administrator rights needed. Everything goes into $HOME\TeachingAssistantJarvis.
#
# Local test before a release (install folder overridden by TAJ_HOME):
#   $env:TAJ_HOME = 'C:\somewhere\TeachingAssistantJarvis'
#   powershell -NoProfile -ExecutionPolicy Bypass -File release\install.ps1 -Source C:\path\to\release.zip
#
# This file must stay pure ASCII: PowerShell 5.1 garbles non-ASCII text under `irm | iex`.
# Chinese messages are written as \uXXXX escapes and decoded at run time by function Z.
# Pinned versions: copied from release/versions.env by the release script; all three places must match.
param([string]$Source = $env:TAJ_SOURCE)

$script:NODE_VERSION = '26.13.1'
$script:CLOUDFLARED_VERSION = '2026.11.0'
$script:OE_MCP_COMMIT = '6c6b7e85bb98e9a6e85820485d917af423422843'

$TajScriptDir = $PSScriptRoot

function Z([string]$s) { [regex]::Unescape($s) }

# Read versions.env next to this file (only when run as a file; under `irm | iex` the header values are used).
function Read-VersionsEnv {
    if (-not $TajScriptDir) { return }
    $f = Join-Path $TajScriptDir 'versions.env'
    if (-not (Test-Path -LiteralPath $f)) { return }
    foreach ($line in Get-Content -LiteralPath $f) {
        if ($line -match '^\s*(NODE_VERSION|CLOUDFLARED_VERSION|OE_MCP_COMMIT)\s*=\s*([A-Za-z0-9._-]+)\s*$') {
            Set-Variable -Scope Script -Name $Matches[1] -Value $Matches[2]
        }
    }
}

function Step([int]$n, [string]$name, [string]$hint) {
    $state.n = $n
    $state.name = $name
    $state.hint = $hint
    Write-Host ''
    Write-Host ("[{0}/10] {1}" -f $n, $name) -ForegroundColor Cyan
}

function Skip([string]$what) {
    Write-Host ((Z '  \u5df2\u5b8c\u6210\uff0c\u7565\u904e\uff08{0}\uff09') -f $what)
}

function Get-File([string]$url, [string]$dest) {
    Write-Host ((Z '  \u4e0b\u8f09 {0}') -f $url)
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
}

function Expand-Zip([string]$zip, [string]$dest) {
    if (Test-Path -LiteralPath $dest) { Remove-Item -LiteralPath $dest -Recurse -Force }
    [System.IO.Compression.ZipFile]::ExtractToDirectory($zip, $dest)
    # A GitHub / Node.js zip holds one top-level folder: return it; otherwise the folder itself.
    $items = @(Get-ChildItem -LiteralPath $dest -Force)
    if ($items.Count -eq 1 -and $items[0].PSIsContainer) { return $items[0].FullName }
    return $dest
}

function Test-Marker([string]$path, [string]$value) {
    if (-not (Test-Path -LiteralPath $path)) { return $false }
    return ((Get-Content -LiteralPath $path -Raw).Trim() -eq $value)
}

function Set-Marker([string]$path, [string]$value) {
    Set-Content -LiteralPath $path -Value $value -Encoding Ascii
}

function Remove-IfExists([string]$path) {
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
}

# Run a program in this console window (live output, keyboard input works) and fail on a non-zero exit code.
function Invoke-Tool([string]$exe, [string[]]$argv, [string]$cwd) {
    $quoted = @($argv | ForEach-Object { if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } })
    $p = Start-Process -FilePath $exe -ArgumentList $quoted -WorkingDirectory $cwd -NoNewWindow -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw ("{0} {1} -> exit code {2}" -f $exe, ($argv -join ' '), $p.ExitCode) }
}

function Find-Uv {
    $local = Join-Path $HOME '.local\bin\uv.exe'
    if (Test-Path -LiteralPath $local) { return $local }
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Test-HasKey {
    if ($env:GEMINI_API_KEY) { return $true }
    return [bool][Environment]::GetEnvironmentVariable('GEMINI_API_KEY', 'User')
}

function Install-TeachingAssistant([string]$Source) {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    Read-VersionsEnv

    $state = @{ n = 0; name = ''; hint = '' }
    $netHint = Z '\u78ba\u8a8d\u7db2\u8def\u9023\u7dda\u5f8c\uff0c\u91cd\u65b0\u57f7\u884c\u540c\u4e00\u884c\u5b89\u88dd\u6307\u4ee4\uff08\u5df2\u5b8c\u6210\u7684\u6b65\u9a5f\u6703\u81ea\u52d5\u7565\u904e\uff09\u3002'
    $closeHint = Z '\u5148\u7d50\u675f\u6559\u5b78\u52a9\u7406\uff08\u95dc\u6389\u5b83\u7684\u547d\u4ee4\u8996\u7a97\uff09\uff0c\u78ba\u8a8d\u7db2\u8def\u9023\u7dda\u5f8c\uff0c\u91cd\u65b0\u57f7\u884c\u540c\u4e00\u884c\u5b89\u88dd\u6307\u4ee4\u3002'
    if ($env:TAJ_HOME) { $dir = $env:TAJ_HOME } else { $dir = Join-Path $HOME 'TeachingAssistantJarvis' }
    $tools = Join-Path $dir 'tools'
    $stage = Join-Path $dir '.install-staging'
    $oe = Join-Path $tools 'openevidence-mcp'
    $savedPath = $env:PATH
    try {
        Remove-IfExists $stage
        New-Item -ItemType Directory -Force -Path $dir, $tools, $stage | Out-Null
        Write-Host ((Z '\u5b89\u88dd\u6559\u5b78\u52a9\u7406\u8cc8\u7dad\u65af\u5230\uff1a{0}') -f $dir)

        # STEPS-BEGIN: fixed order (design doc section 8.2)
        Step 1 (Z '\u5b89\u88dd uv') $netHint
        $uv = Find-Uv
        if ($uv) {
            Skip $uv
        } else {
            Invoke-Tool 'powershell.exe' @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command',
                'irm https://astral.sh/uv/install.ps1 | iex') $stage
            $uv = Find-Uv
            if (-not $uv) { throw 'uv not found after installing it' }
        }

        Step 2 (Z '\u4e0b\u8f09\u7a0b\u5f0f') $closeHint
        if ($Source) {
            if (-not (Test-Path -LiteralPath $Source)) { throw "Source zip not found: $Source" }
            $zip = (Resolve-Path -LiteralPath $Source).Path
            Write-Host ((Z '  \u4f7f\u7528\u672c\u6a5f\u58d3\u7e2e\u6a94 {0}') -f $zip)
        } else {
            $zip = Join-Path $stage 'program.zip'
            Get-File 'https://github.com/hsinhsu64/teaching-assistant-jarvis/archive/refs/heads/main.zip' $zip
        }
        $sum = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLower()
        $sumFile = Join-Path $dir '.program.sha256'
        if ((Test-Path -LiteralPath (Join-Path $dir 'start.bat')) -and (Test-Marker $sumFile $sum)) {
            Skip (Z '\u7a0b\u5f0f\u5df2\u662f\u6700\u65b0\u7248')
        } else {
            $root = Expand-Zip $zip (Join-Path $stage 'program')
            if (-not (Test-Path -LiteralPath (Join-Path $root 'pyproject.toml'))) { throw 'pyproject.toml not found in the zip' }
            # Keep config.toml, tools\ (and .venv) when updating; replace every other top-level item.
            $keep = @('config.toml', 'tools', '.venv', '.install-staging', '.program.sha256')
            foreach ($item in Get-ChildItem -LiteralPath $root -Force) {
                if ($keep -contains $item.Name) { continue }
                Remove-IfExists (Join-Path $dir $item.Name)
                Move-Item -LiteralPath $item.FullName -Destination (Join-Path $dir $item.Name)
            }
            Set-Marker $sumFile $sum
        }

        Step 3 (Z '\u5b89\u88dd Python \u5957\u4ef6\uff08uv sync\uff09') $closeHint
        Invoke-Tool $uv @('sync') $dir

        Step 4 (Z '\u4e0b\u8f09 cloudflared') $netHint
        $cf = Join-Path $tools 'cloudflared.exe'
        $cfMark = Join-Path $tools 'cloudflared.version'
        if ((Test-Path -LiteralPath $cf) -and (Test-Marker $cfMark $CLOUDFLARED_VERSION)) {
            Skip "cloudflared $CLOUDFLARED_VERSION"
        } else {
            $tmp = Join-Path $stage 'cloudflared.exe'
            Get-File "https://github.com/cloudflare/cloudflared/releases/download/$CLOUDFLARED_VERSION/cloudflared-windows-amd64.exe" $tmp
            Move-Item -LiteralPath $tmp -Destination $cf -Force
            Set-Marker $cfMark $CLOUDFLARED_VERSION
        }

        Step 5 (Z '\u4e0b\u8f09 Node.js') $netHint
        $nodeDir = Join-Path $tools 'node'
        $node = Join-Path $nodeDir 'node.exe'
        $nodeMark = Join-Path $tools 'node.version'
        if ((Test-Path -LiteralPath $node) -and (Test-Marker $nodeMark $NODE_VERSION)) {
            Skip "Node.js $NODE_VERSION"
        } else {
            $tmp = Join-Path $stage 'node.zip'
            Get-File "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-win-x64.zip" $tmp
            $root = Expand-Zip $tmp (Join-Path $stage 'node')
            if (-not (Test-Path -LiteralPath (Join-Path $root 'node.exe'))) { throw 'node.exe not found in the zip' }
            Remove-IfExists $nodeMark
            Remove-IfExists $nodeDir
            Move-Item -LiteralPath $root -Destination $nodeDir
            Set-Marker $nodeMark $NODE_VERSION
        }

        Step 6 (Z '\u4e0b\u8f09\u4e26\u5efa\u7f6e OpenEvidence \u8f49\u63a5\u7a0b\u5f0f\u8207\u64f4\u5145\u529f\u80fd') $closeHint
        $oeMark = Join-Path $tools 'openevidence-mcp.version'
        $relay = Join-Path $oe 'dist\relay-daemon.js'
        $extManifest = Join-Path $oe 'extension\dist\manifest.json'
        if ((Test-Path -LiteralPath $relay) -and (Test-Path -LiteralPath $extManifest) -and (Test-Marker $oeMark $OE_MCP_COMMIT)) {
            Skip ("openevidence-mcp " + $OE_MCP_COMMIT.Substring(0, 7))
        } else {
            $tmp = Join-Path $stage 'oe.zip'
            Get-File "https://github.com/htlin222/openevidence-mcp/archive/$OE_MCP_COMMIT.zip" $tmp
            $root = Expand-Zip $tmp (Join-Path $stage 'oe')
            if (-not (Test-Path -LiteralPath (Join-Path $root 'package.json'))) { throw 'package.json not found in the zip' }
            Remove-IfExists $oeMark
            Remove-IfExists $oe
            Move-Item -LiteralPath $root -Destination $oe
            # npm scripts call `node`, so the bundled Node.js must be first on PATH (restored in finally).
            $env:PATH = "$nodeDir;$savedPath"
            $npm = Join-Path $nodeDir 'node_modules\npm\bin\npm-cli.js'
            $ext = Join-Path $oe 'extension'
            Invoke-Tool $node @($npm, 'install', '--no-audit', '--no-fund') $oe
            Invoke-Tool $node @($npm, 'run', 'build') $oe
            Invoke-Tool $node @($npm, 'install', '--no-audit', '--no-fund') $ext
            Invoke-Tool $node @($npm, 'run', 'build') $ext
            if (-not ((Test-Path -LiteralPath $relay) -and (Test-Path -LiteralPath $extManifest))) {
                throw 'relay-daemon.js or extension\dist missing after the build'
            }
            Set-Marker $oeMark $OE_MCP_COMMIT
        }

        Step 7 (Z '\u4e0b\u8f09\u559a\u9192\u8a5e\u6a21\u578b') $netHint
        Invoke-Tool $uv @('run', 'python', 'scripts/get_wake_model.py') $dir

        Step 8 (Z '\u5efa\u7acb\u684c\u9762\u6377\u5f91') (Z '\u91cd\u65b0\u57f7\u884c\u540c\u4e00\u884c\u5b89\u88dd\u6307\u4ee4\uff1b\u6216\u76f4\u63a5\u96d9\u64ca\u5b89\u88dd\u8cc7\u6599\u593e\u88e1\u7684 start.bat \u555f\u52d5\u3002')
        $name = -join @([char]0x6559, [char]0x5B78, [char]0x52A9, [char]0x7406, [char]0x8CC8, [char]0x7DAD, [char]0x65AF)
        $lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) ($name + '.lnk')
        $target = Join-Path $dir 'start.bat'
        $shell = New-Object -ComObject WScript.Shell
        if ((Test-Path -LiteralPath $lnk) -and ($shell.CreateShortcut($lnk).TargetPath -eq $target)) {
            Skip $lnk
        } else {
            $sc = $shell.CreateShortcut($lnk)
            $sc.TargetPath = $target
            $sc.WorkingDirectory = $dir
            $sc.Save()
        }

        Step 9 (Z '\u8a2d\u5b9a Gemini \u91d1\u9470') $netHint
        if (Test-HasKey) {
            Skip 'GEMINI_API_KEY'
        } else {
            try {
                Invoke-Tool $uv @('run', 'python', '-m', 'xiaoshen.keysetup') $dir
            } catch {
                Write-Host (Z '  \u91d1\u9470\u6c92\u6709\u8a2d\u5b9a\u6210\u529f\uff1b\u4e4b\u5f8c\u53ef\u4ee5\u96d9\u64ca\u5b89\u88dd\u8cc7\u6599\u593e\u88e1\u7684 set-key.bat \u518d\u8a2d\u5b9a\u3002') -ForegroundColor Yellow
            }
        }

        Step 10 (Z '\u6253\u958b OpenEvidence \u64f4\u5145\u529f\u80fd\u8aaa\u660e') $netHint
        $readme = 'https://github.com/hsinhsu64/teaching-assistant-jarvis#openevidence-%E6%93%B4%E5%85%85%E5%8A%9F%E8%83%BD'
        Write-Host (Z '  \u64f4\u5145\u529f\u80fd\u8cc7\u6599\u593e\uff08\u5728 Chrome \u6216 Edge\u300c\u8f09\u5165\u672a\u5c01\u88dd\u9805\u76ee\u300d\u6642\u9078\u9019\u500b\uff09\uff1a')
        Write-Host ('  ' + (Join-Path $oe 'extension\dist'))
        try { Start-Process $readme } catch { Write-Host ((Z '  \u8acb\u5728\u700f\u89bd\u5668\u6253\u958b\u8aaa\u660e\uff1a{0}') -f $readme) }

        $state.n = 0
        Write-Host ''
        Write-Host ((Z '\u5b89\u88dd\u5b8c\u6210\uff01\u96d9\u64ca\u684c\u9762\u4e0a\u7684\u300c{0}\u300d\u5c31\u80fd\u555f\u52d5\uff1b\u7b2c\u4e00\u6b21\u555f\u52d5\u6703\u8df3\u51fa\u8a2d\u5b9a\u8996\u7a97\u3002') -f $name) -ForegroundColor Green
        return 0
    } catch {
        Write-Host ''
        if ($state.n -gt 0) {
            Write-Host ((Z '\u7b2c {0} \u6b65\uff1a{1}\u5931\u6557\uff0c\u8acb{2}') -f $state.n, $state.name, $state.hint) -ForegroundColor Red
        }
        Write-Host ('  ' + $_.Exception.Message) -ForegroundColor Red
        return 1
    } finally {
        $env:PATH = $savedPath
        if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

$TajExitCode = @(Install-TeachingAssistant $Source)[-1]
if ($PSCommandPath) { exit $TajExitCode }
$global:LASTEXITCODE = $TajExitCode
