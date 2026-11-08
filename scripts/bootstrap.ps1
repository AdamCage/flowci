# Bootstraps local Vikunja for the FlowCI pilot.
# Re-runnable: skips the download, user, project, token, and .env keys that already exist.
# Does not start the FlowCI Python app.

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$Root = Split-Path -Parent $PSScriptRoot
$BinRoot = Join-Path $Root "tools\vikunja"
$DataRoot = Join-Path $Root "data\vikunja"
$FilesRoot = Join-Path $DataRoot "files"
$ExePath = Join-Path $BinRoot "vikunja.exe"
$ConfigPath = Join-Path $DataRoot "config.yml"
$DbPath = Join-Path $DataRoot "vikunja.db"
$EnvPath = Join-Path $Root ".env"
$ZipPath = Join-Path $BinRoot "vikunja-v2.8.0-windows-4.0-amd64.exe-full.zip"
$BaseUrl = "http://127.0.0.1:3456"
$PrimaryZipUrl = "https://github.com/go-vikunja/vikunja/releases/download/v2.8.0/vikunja-v2.8.0-windows-4.0-amd64.exe-full.zip"
$Username = "flowci"
$UserEmail = "flowci@example.com"
$TokenTitle = "flowci"
$ProjectTitle = "FlowCI"

$script:Secrets = New-Object System.Collections.Generic.List[string]
$script:AppendedKeys = New-Object System.Collections.Generic.List[string]
$script:EnvPrefix = $null

function Add-Secret {
    param([string]$Value)
    if ([string]::IsNullOrEmpty($Value) -or $Value.Length -lt 6) {
        return
    }
    if (-not $script:Secrets.Contains($Value)) {
        $script:Secrets.Add($Value)
    }
}

function Protect-Text {
    param([string]$Text)
    if ([string]::IsNullOrEmpty($Text)) {
        return ""
    }
    $out = $Text
    foreach ($secret in $script:Secrets) {
        if (-not [string]::IsNullOrEmpty($secret)) {
            $out = $out.Replace($secret, "[redacted]")
        }
    }
    $out = [regex]::Replace($out, "tk_[0-9a-fA-F]+", "tk_[redacted]")
    $out = [regex]::Replace($out, "Bearer\s+\S+", "Bearer [redacted]")
    $out = [regex]::Replace($out, "eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}", "[jwt]")
    $out = [regex]::Replace($out, "https?://\S+", "[url]")
    if ($out.Length -gt 500) {
        $out = $out.Substring(0, 500)
    }
    return $out
}

function New-HexSecret {
    param([int]$ByteCount)
    $buffer = New-Object byte[] $ByteCount
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($buffer)
    } finally {
        $rng.Dispose()
    }
    $builder = New-Object System.Text.StringBuilder
    foreach ($b in $buffer) {
        [void]$builder.Append($b.ToString("x2"))
    }
    return $builder.ToString()
}

function New-UrlSafeSecret {
    param([int]$ByteCount)
    $buffer = New-Object byte[] $ByteCount
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($buffer)
    } finally {
        $rng.Dispose()
    }
    return ([Convert]::ToBase64String($buffer)).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

function ConvertTo-ApiJson {
    param($Value)
    Add-Type -AssemblyName System.Web.Extensions
    $serializer = New-Object System.Web.Script.Serialization.JavaScriptSerializer
    $serializer.MaxJsonLength = 67108864
    $serializer.RecursionLimit = 16
    return $serializer.Serialize($Value)
}

function Get-DotEnvKeys {
    param([string]$Path)
    $keys = New-Object System.Collections.Generic.HashSet[string]
    if (-not (Test-Path -LiteralPath $Path)) {
        return $keys
    }
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=') {
            [void]$keys.Add($Matches[1])
        }
    }
    return $keys
}

function Get-DotEnvValue {
    param([string]$Path, [string]$Key)
    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }
    $pattern = '^\s*' + [regex]::Escape($Key) + '\s*=(.*)$'
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        if ($line -match $pattern) {
            return $Matches[1]
        }
    }
    return $null
}

function Add-DotEnvIfMissing {
    param([string]$Path, [string]$Key, [string]$Value)
    if ([string]::IsNullOrEmpty($Value)) {
        throw "refusing to append empty value for $Key"
    }
    $keys = Get-DotEnvKeys -Path $Path
    if ($keys.Contains($Key)) {
        return $false
    }
    $prefix = ""
    if (Test-Path -LiteralPath $Path) {
        $existing = [System.IO.File]::ReadAllText($Path)
        if ($existing.Length -gt 0 -and -not $existing.EndsWith("`n")) {
            $prefix = "`n"
        }
    } else {
        $directory = Split-Path -Parent $Path
        if (-not (Test-Path -LiteralPath $directory)) {
            New-Item -ItemType Directory -Force -Path $directory | Out-Null
        }
    }
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::AppendAllText($Path, ($prefix + $Key + "=" + $Value + "`n"), $utf8)
    if (-not $script:AppendedKeys.Contains($Key)) {
        $script:AppendedKeys.Add($Key)
    }
    return $true
}

function Assert-EnvKeyOnce {
    param([string]$Path, [string]$Key)
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $count = 0
    $pattern = '^\s*' + [regex]::Escape($Key) + '\s*='
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        if ($line -match $pattern) {
            $count++
        }
    }
    if ($count -gt 1) {
        throw "duplicate .env key $Key"
    }
}

function Invoke-VikunjaApi {
    param(
        [string]$Method,
        [string]$Uri,
        [string]$JsonBody,
        [string]$Bearer
    )
    $request = [System.Net.HttpWebRequest]::Create($Uri)
    $request.Method = $Method
    $request.Accept = "application/json"
    $request.Timeout = 30000
    $request.ReadWriteTimeout = 30000
    if (-not [string]::IsNullOrEmpty($Bearer)) {
        $request.Headers["Authorization"] = "Bearer $Bearer"
    }
    if (-not [string]::IsNullOrEmpty($JsonBody)) {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($JsonBody)
        $request.ContentType = "application/json; charset=utf-8"
        $request.ContentLength = $bytes.Length
        $stream = $request.GetRequestStream()
        try {
            $stream.Write($bytes, 0, $bytes.Length)
        } finally {
            $stream.Close()
        }
    }
    try {
        $response = $request.GetResponse()
    } catch [System.Net.WebException] {
        $status = 0
        $body = ""
        $http = $_.Exception.Response
        if ($http -is [System.Net.HttpWebResponse]) {
            $status = [int]$http.StatusCode
            $reader = New-Object System.IO.StreamReader($http.GetResponseStream())
            try {
                $body = $reader.ReadToEnd()
            } finally {
                $reader.Close()
                $http.Close()
            }
        }
        $safe = Protect-Text -Text $body
        throw ("HTTP {0} {1} {2} {3}" -f $status, $Method, $Uri, $safe)
    }
    $okReader = New-Object System.IO.StreamReader($response.GetResponseStream())
    try {
        $text = $okReader.ReadToEnd()
    } finally {
        $okReader.Close()
        $response.Close()
    }
    if ([string]::IsNullOrWhiteSpace($text)) {
        return $null
    }
    return ($text | ConvertFrom-Json)
}

function Get-AllItems {
    param([string]$Resource, [string]$Bearer)
    $all = @()
    $seen = @{}
    for ($page = 1; $page -le 20; $page++) {
        $uri = "{0}/api/v1/{1}?page={2}&per_page=50" -f $BaseUrl, $Resource, $page
        $batch = Invoke-VikunjaApi -Method "GET" -Uri $uri -Bearer $Bearer
        if ($null -eq $batch) {
            break
        }
        $items = @($batch)
        if ($items.Count -eq 0) {
            break
        }
        $firstId = [string]$items[0].id
        if ($seen.ContainsKey($firstId)) {
            break
        }
        $seen[$firstId] = $true
        $all += $items
        if ($items.Count -lt 50) {
            break
        }
    }
    return @($all)
}

function Test-VikunjaHealthy {
    try {
        $info = Invoke-VikunjaApi -Method "GET" -Uri ($BaseUrl + "/api/v1/info")
        if ($null -eq $info -or [string]::IsNullOrEmpty([string]$info.version)) {
            return $false
        }
        return $true
    } catch {
        return $false
    }
}

function Get-SafeLogTail {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return ""
    }
    $lines = Get-Content -LiteralPath $Path -Tail 40 -ErrorAction SilentlyContinue
    $kept = New-Object System.Collections.Generic.List[string]
    foreach ($line in @($lines)) {
        $safe = Protect-Text -Text ([string]$line)
        if ($safe -match "secret|password|token|authorization") {
            $kept.Add("[log line redacted]")
        } else {
            $kept.Add($safe)
        }
    }
    return ($kept -join "`n")
}

function Remember-ConfigSecret {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        if ($line -match '^\s*secret:\s*"?([^"\s#]+)"?\s*$') {
            Add-Secret -Value $Matches[1]
        }
    }
}

function ConvertTo-VikunjaPath {
    param([string]$Path)
    $full = [System.IO.Path]::GetFullPath($Path)
    return ($full -replace '\\', '/')
}

function Write-VikunjaConfig {
    param(
        [string]$Path,
        [string]$Secret,
        [string]$RootPath,
        [string]$DatabasePath,
        [string]$FilesPath
    )
    $lines = @(
        "service:",
        "  interface: `"127.0.0.1:3456`"",
        "  publicurl: `"http://127.0.0.1:3456`"",
        ("  rootpath: `"{0}`"" -f $RootPath),
        "  enableregistration: false",
        "  timezone: `"Europe/Moscow`"",
        ("  secret: `"{0}`"" -f $Secret),
        "database:",
        "  type: sqlite",
        ("  path: `"{0}`"" -f $DatabasePath),
        "files:",
        ("  basepath: `"{0}`"" -f $FilesPath),
        "mailer:",
        "  enabled: false"
    )
    $utf8 = New-Object System.Text.UTF8Encoding $false
    $text = ($lines -join "`n") + "`n"
    [System.IO.File]::WriteAllText($Path, $text, $utf8)
}

function Save-OfficialZip {
    param(
        [string]$Url,
        [string]$Destination,
        [string]$Accept
    )
    $errorFile = Join-Path $env:TEMP ("flowci-vikunja-curl-" + [guid]::NewGuid().ToString("n") + ".txt")
    $curlArgs = @(
        "-4", "-L", "--fail", "--http1.1", "--retry", "2", "--retry-all-errors", "--retry-delay", "2",
        "-sS", "-A", "flowci-bootstrap"
    )
    if (-not [string]::IsNullOrEmpty($Accept)) {
        $curlArgs += @("-H", ("Accept: " + $Accept))
    }
    $curlArgs += @("-o", $Destination, $Url)
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & curl.exe @curlArgs 2>$errorFile
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorAction
    }
    $errorText = ""
    if (Test-Path -LiteralPath $errorFile) {
        $errorText = [System.IO.File]::ReadAllText($errorFile)
        Remove-Item -LiteralPath $errorFile -Force
    }
    return @{ Code = $code; Error = (Protect-Text -Text $errorText) }
}

function Get-ReleaseZipUrl {
    $request = [System.Net.HttpWebRequest]::Create("https://api.github.com/repos/go-vikunja/vikunja/releases/tags/v2.8.0")
    $request.UserAgent = "flowci-bootstrap"
    $request.Accept = "application/vnd.github+json"
    $request.Timeout = 60000
    $response = $request.GetResponse()
    $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
    try {
        $payload = $reader.ReadToEnd() | ConvertFrom-Json
    } finally {
        $reader.Close()
        $response.Close()
    }
    $asset = $payload.assets | Where-Object { $_.name -eq "vikunja-v2.8.0-windows-4.0-amd64.exe-full.zip" } | Select-Object -First 1
    if (-not $asset) {
        $asset = $payload.assets | Where-Object {
            $_.name -like "vikunja-*windows-4.0-amd64*full.zip" -and $_.name -notlike "*Desktop*"
        } | Select-Object -First 1
    }
    if (-not $asset) {
        throw "v2.8.0 release has no windows 4.0 amd64 full zip"
    }
    $script:ResolvedAssetName = [string]$asset.name
    $script:ResolvedAssetApiUrl = [string]$asset.url
    return [string]$asset.browser_download_url
}

function Install-VikunjaBinary {
    New-Item -ItemType Directory -Force -Path $BinRoot | Out-Null
    if ((Test-Path -LiteralPath $ExePath) -and ((Get-Item -LiteralPath $ExePath).Length -gt 1MB)) {
        Write-Output "download_skipped=true"
        return
    }
    Write-Output "downloading_vikunja=v2.8.0-windows-4.0-amd64-full"
    $download = Save-OfficialZip -Url $PrimaryZipUrl -Destination $ZipPath
    if ($download.Code -ne 0) {
        if ($download.Error -match "404") {
            Write-Output "primary_asset_missing=true"
        } else {
            Write-Output ("primary_download_failed=" + $download.Code)
        }
        if (Test-Path -LiteralPath $ZipPath) {
            Remove-Item -LiteralPath $ZipPath -Force
        }
        [void](Get-ReleaseZipUrl)
        Write-Output ("asset_name=" + $script:ResolvedAssetName)
        Write-Output "download_via=github_api_asset"
        $download = Save-OfficialZip -Url $script:ResolvedAssetApiUrl -Destination $ZipPath -Accept "application/octet-stream"
        if ($download.Code -ne 0) {
            if (Test-Path -LiteralPath $ZipPath) {
                Remove-Item -LiteralPath $ZipPath -Force
            }
            throw ("fallback download failed exit={0} detail={1}" -f $download.Code, $download.Error)
        }
    }
    if (-not (Test-Path -LiteralPath $ZipPath) -or (Get-Item -LiteralPath $ZipPath).Length -lt 1MB) {
        throw "downloaded zip is missing or too small"
    }
    $zipStream = [System.IO.File]::OpenRead($ZipPath)
    try {
        $magic = New-Object byte[] 2
        [void]$zipStream.Read($magic, 0, 2)
    } finally {
        $zipStream.Close()
    }
    if ($magic[0] -ne 0x50 -or $magic[1] -ne 0x4B) {
        Remove-Item -LiteralPath $ZipPath -Force
        throw "download is not a zip archive"
    }
    Write-Output ("download_bytes=" + (Get-Item -LiteralPath $ZipPath).Length)
    $extract = Join-Path $BinRoot "_extract"
    if (Test-Path -LiteralPath $extract) {
        Remove-Item -LiteralPath $extract -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $extract | Out-Null
    Expand-Archive -LiteralPath $ZipPath -DestinationPath $extract -Force
    $exeItem = Get-ChildItem -LiteralPath $extract -Recurse -Filter "*.exe" -File |
        Where-Object { $_.Name -notmatch "Desktop" } |
        Select-Object -First 1
    if (-not $exeItem) {
        throw "zip did not contain a Vikunja executable"
    }
    $sourceDir = $exeItem.DirectoryName
    Get-ChildItem -Force -LiteralPath $sourceDir | ForEach-Object {
        $target = Join-Path $BinRoot $_.Name
        if ($_.FullName -eq $extract) {
            return
        }
        if (Test-Path -LiteralPath $target) {
            Remove-Item -LiteralPath $target -Recurse -Force
        }
        Move-Item -LiteralPath $_.FullName -Destination $BinRoot
    }
    $movedExe = Join-Path $BinRoot $exeItem.Name
    if ((Resolve-Path -LiteralPath $movedExe).Path -ne (Join-Path $BinRoot "vikunja.exe")) {
        $existingFinal = Join-Path $BinRoot "vikunja.exe"
        if (Test-Path -LiteralPath $existingFinal) {
            Remove-Item -LiteralPath $existingFinal -Force
        }
        Rename-Item -LiteralPath $movedExe -NewName "vikunja.exe"
    }
    if (Test-Path -LiteralPath $extract) {
        Remove-Item -LiteralPath $extract -Recurse -Force
    }
    if (Test-Path -LiteralPath $ZipPath) {
        Remove-Item -LiteralPath $ZipPath -Force
    }
    if (-not (Test-Path -LiteralPath $ExePath) -or (Get-Item -LiteralPath $ExePath).Length -lt 1MB) {
        throw "vikunja.exe was not installed"
    }
}

function Test-FlowciUser {
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $ExePath --config $ConfigPath user list 2>&1 | Out-String
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorAction
    }
    if ($code -ne 0) {
        throw ("user list failed exit={0} detail={1}" -f $code, (Protect-Text -Text $output))
    }
    return ($output -match '(?m)(?:^|\s)flowci(?:\s|$)')
}

function New-PermissionMap {
    param($Routes)
    $map = @{}
    if ($null -eq $Routes) {
        return $map
    }
    foreach ($group in $Routes.PSObject.Properties) {
        $rights = New-Object System.Collections.Generic.List[string]
        if ($null -eq $group.Value) {
            continue
        }
        foreach ($permission in $group.Value.PSObject.Properties) {
            $rights.Add([string]$permission.Name)
        }
        if ($rights.Count -gt 0) {
            $map[$group.Name] = $rights.ToArray()
        }
    }
    return $map
}

function Select-FlowCIPermissions {
    param($Routes)
    $full = New-PermissionMap -Routes $Routes
    $selected = @{}
    foreach ($name in @($full.Keys)) {
        $lower = ([string]$name).ToLowerInvariant()
        $rights = @($full[$name])
        $readOnly = $false
        $keep = $false
        if ($lower -match '(^|[^a-z])(tasks?|projects?|labels?|comments?)([^a-z]|$)') {
            $keep = $true
        } elseif ($lower -eq "user") {
            $keep = $true
            $readOnly = $true
        }
        if (-not $keep) {
            continue
        }
        if ($readOnly) {
            $rights = @($rights | Where-Object { ([string]$_).ToLowerInvariant() -match 'read' })
        }
        if ($rights.Count -gt 0) {
            $selected[$name] = @($rights)
        }
    }
    return $selected
}

function Get-LabelTitles {
    return @(
        "Нужно сделать",
        "В работе",
        "Заблокировано",
        "Завершено",
        "Сегодня",
        "Завтра",
        "Неделя",
        "Позже"
    )
}

function Get-LabelColors {
    return @{
        "Нужно сделать" = "#4c6ef5"
        "В работе" = "#fab005"
        "Заблокировано" = "#fa5252"
        "Завершено" = "#40c057"
        "Сегодня" = "#15aabf"
        "Завтра" = "#7950f2"
        "Неделя" = "#fd7e14"
        "Позже" = "#868e96"
    }
}

try {
    if (Test-Path -LiteralPath $EnvPath) {
        $script:EnvPrefix = [System.IO.File]::ReadAllBytes($EnvPath)
    }

    New-Item -ItemType Directory -Force -Path $DataRoot | Out-Null
    New-Item -ItemType Directory -Force -Path $FilesRoot | Out-Null
    Install-VikunjaBinary
    Write-Output ("binary=" + $ExePath)

    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $versionOutput = & $ExePath version 2>&1 | Out-String
    } finally {
        $ErrorActionPreference = $previousErrorAction
    }
    if ($versionOutput -notmatch "2\.8\.0") {
        throw ("unexpected vikunja version detail=" + (Protect-Text -Text $versionOutput))
    }
    Write-Output "binary_version=v2.8.0"

    if (Test-Path -LiteralPath $ConfigPath) {
        Write-Output "config_exists=true"
    } else {
        $serviceSecret = New-HexSecret -ByteCount 32
        Add-Secret -Value $serviceSecret
        Write-VikunjaConfig -Path $ConfigPath -Secret $serviceSecret -RootPath (ConvertTo-VikunjaPath $DataRoot) -DatabasePath (ConvertTo-VikunjaPath $DbPath) -FilesPath (ConvertTo-VikunjaPath $FilesRoot)
        Write-Output "config_created=true"
    }
    Remember-ConfigSecret -Path $ConfigPath

    $serverAlreadyRunning = Test-VikunjaHealthy
    $password = Get-DotEnvValue -Path $EnvPath -Key "VIKUNJA_PASSWORD"
    Add-Secret -Value $password

    if (-not $serverAlreadyRunning) {
        $userExists = Test-FlowciUser
        if ($userExists) {
            Write-Output "user_exists=true"
            if ([string]::IsNullOrEmpty($password)) {
                throw "user flowci exists but VIKUNJA_PASSWORD is absent; password was not reset"
            }
        } else {
            if ([string]::IsNullOrEmpty($password)) {
                $password = New-HexSecret -ByteCount 32
                Add-Secret -Value $password
            }
            $previousErrorAction = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            try {
                $createOutput = & $ExePath --config $ConfigPath user create -u $Username -e $UserEmail -p $password 2>&1 | Out-String
                $createCode = $LASTEXITCODE
            } finally {
                $ErrorActionPreference = $previousErrorAction
            }
            if ($createCode -ne 0) {
                throw ("user create failed exit={0} detail={1}" -f $createCode, (Protect-Text -Text $createOutput))
            }
            Write-Output "user_created=true"
        }
        [void](Add-DotEnvIfMissing -Path $EnvPath -Key "VIKUNJA_USERNAME" -Value $Username)
        [void](Add-DotEnvIfMissing -Path $EnvPath -Key "VIKUNJA_PASSWORD" -Value $password)

        $stdoutLog = Join-Path $DataRoot "vikunja.stdout.log"
        $stderrLog = Join-Path $DataRoot "vikunja.stderr.log"
        $server = Start-Process -FilePath $ExePath -ArgumentList @("--config", $ConfigPath, "web") -WorkingDirectory $BinRoot -WindowStyle Hidden -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -PassThru
        Write-Output ("server_started_pid=" + $server.Id)
        $healthy = $false
        for ($attempt = 0; $attempt -lt 90; $attempt++) {
            if ($server.HasExited) {
                $detail = Get-SafeLogTail -Path $stderrLog
                if ([string]::IsNullOrWhiteSpace($detail)) {
                    $detail = Get-SafeLogTail -Path $stdoutLog
                }
                throw ("vikunja exited early code={0} detail={1}" -f $server.ExitCode, $detail)
            }
            if (Test-VikunjaHealthy) {
                $healthy = $true
                break
            }
            Start-Sleep -Seconds 1
        }
        if (-not $healthy) {
            $detail = Get-SafeLogTail -Path $stderrLog
            if ([string]::IsNullOrWhiteSpace($detail)) {
                $detail = Get-SafeLogTail -Path $stdoutLog
            }
            throw ("vikunja health check timed out detail={0}" -f $detail)
        }
    } else {
        Write-Output "server_already_running=true"
        if ([string]::IsNullOrEmpty($password)) {
            throw "Vikunja is already running but VIKUNJA_PASSWORD is absent; password was not reset"
        }
    }

    $info = Invoke-VikunjaApi -Method "GET" -Uri ($BaseUrl + "/api/v1/info")
    $registration = $null
    if ($info.auth -and $info.auth.local) {
        $registration = $info.auth.local.registration_enabled
    }
    Write-Output "health_status=200"
    Write-Output ("health_version=" + $info.version)
    Write-Output ("registration_enabled=" + $registration)
    Write-Output ("sqlite_exists=" + (Test-Path -LiteralPath $DbPath))
    if (-not (Test-Path -LiteralPath $DbPath)) {
        throw "sqlite database was not created at the configured path"
    }

    $rootRequest = [System.Net.HttpWebRequest]::Create($BaseUrl + "/")
    $rootRequest.Method = "GET"
    $rootRequest.Timeout = 15000
    $rootRequest.AllowAutoRedirect = $true
    $rootResponse = $rootRequest.GetResponse()
    try {
        Write-Output ("root_status=" + [int]$rootResponse.StatusCode)
    } finally {
        $rootResponse.Close()
    }

    $loginBody = ConvertTo-ApiJson @{
        username = $Username
        password = $password
    }
    $login = Invoke-VikunjaApi -Method "POST" -Uri ($BaseUrl + "/api/v1/login") -JsonBody $loginBody
    $jwt = [string]$login.token
    if ([string]::IsNullOrEmpty($jwt)) {
        throw "login response did not include a token"
    }
    Add-Secret -Value $jwt
    Write-Output "login_ok=true"

    $envToken = Get-DotEnvValue -Path $EnvPath -Key "VIKUNJA_TOKEN"
    Add-Secret -Value $envToken
    $apiToken = $envToken
    if (-not [string]::IsNullOrEmpty($envToken)) {
        try {
            [void](Invoke-VikunjaApi -Method "GET" -Uri ($BaseUrl + "/api/v1/projects?per_page=1") -Bearer $envToken)
            Write-Output "token_reused=true"
        } catch {
            throw ("existing VIKUNJA_TOKEN was rejected; not creating a replacement and not rewriting .env detail=" + (Protect-Text -Text $_.Exception.Message))
        }
    } else {
        $existingTokens = @(Get-AllItems -Resource "tokens" -Bearer $jwt)
        $named = @($existingTokens | Where-Object { [string]::Equals([string]$_.title, $TokenTitle, [System.StringComparison]::Ordinal) })
        if ($named.Count -gt 0) {
            throw "an API token titled flowci already exists but VIKUNJA_TOKEN is absent; not creating a second token"
        }
        $routes = Invoke-VikunjaApi -Method "GET" -Uri ($BaseUrl + "/api/v1/routes") -Bearer $jwt
        $permissions = Select-FlowCIPermissions -Routes $routes
        if ($permissions.Count -lt 1) {
            throw "API route permission list was empty"
        }
        Write-Output ("permission_groups=" + $permissions.Count)
        $expires = (Get-Date).ToUniversalTime().AddDays(90).ToString("yyyy-MM-ddTHH:mm:ssZ")
        $tokenBody = ConvertTo-ApiJson @{
            title = $TokenTitle
            permissions = $permissions
            expires_at = $expires
        }
        $createdToken = Invoke-VikunjaApi -Method "PUT" -Uri ($BaseUrl + "/api/v1/tokens") -JsonBody $tokenBody -Bearer $jwt
        $apiToken = [string]$createdToken.token
        if ([string]::IsNullOrEmpty($apiToken)) {
            throw "token create response did not include a token"
        }
        Add-Secret -Value $apiToken
        [void](Add-DotEnvIfMissing -Path $EnvPath -Key "VIKUNJA_TOKEN" -Value $apiToken)
        Write-Output "token_created=true"
    }

    $projects = @(Get-AllItems -Resource "projects" -Bearer $jwt)
    $project = $projects | Where-Object { [string]::Equals([string]$_.title, $ProjectTitle, [System.StringComparison]::Ordinal) } | Select-Object -First 1
    if ($null -eq $project) {
        $projectBody = ConvertTo-ApiJson @{ title = $ProjectTitle }
        $project = Invoke-VikunjaApi -Method "PUT" -Uri ($BaseUrl + "/api/v1/projects") -JsonBody $projectBody -Bearer $jwt
        Write-Output "project_created=true"
    } else {
        Write-Output "project_exists=true"
    }
    $projectId = [string]$project.id
    if ([string]::IsNullOrEmpty($projectId)) {
        throw "FlowCI project id was empty"
    }
    Write-Output ("project_id=" + $projectId)
    Write-Output ("project_title=" + $project.title)
    [void](Add-DotEnvIfMissing -Path $EnvPath -Key "VIKUNJA_PROJECT_ID" -Value $projectId)

    $colors = Get-LabelColors
    $wanted = Get-LabelTitles
    $labels = @(Get-AllItems -Resource "labels" -Bearer $jwt)
    foreach ($title in $wanted) {
        $found = $labels | Where-Object { [string]::Equals([string]$_.title, $title, [System.StringComparison]::Ordinal) } | Select-Object -First 1
        if ($null -eq $found) {
            $labelBody = ConvertTo-ApiJson @{
                title = $title
                hex_color = [string]$colors[$title]
            }
            [void](Invoke-VikunjaApi -Method "PUT" -Uri ($BaseUrl + "/api/v1/labels") -JsonBody $labelBody -Bearer $jwt)
            Write-Output ("label_created=" + $title)
        } else {
            Write-Output ("label_exists=" + $title)
        }
    }

    $confirmedProjects = @(Get-AllItems -Resource "projects" -Bearer $apiToken)
    $confirmedProject = $confirmedProjects | Where-Object { [string]::Equals([string]$_.title, $ProjectTitle, [System.StringComparison]::Ordinal) } | Select-Object -First 1
    if ($null -eq $confirmedProject) {
        throw "API token could not read the FlowCI project"
    }
    $confirmedLabels = @(Get-AllItems -Resource "labels" -Bearer $apiToken)
    $confirmedNames = New-Object System.Collections.Generic.List[string]
    $missing = New-Object System.Collections.Generic.List[string]
    foreach ($title in $wanted) {
        $found = $confirmedLabels | Where-Object { [string]::Equals([string]$_.title, $title, [System.StringComparison]::Ordinal) } | Select-Object -First 1
        if ($null -eq $found) {
            $missing.Add($title)
        } else {
            $confirmedNames.Add([string]$found.title)
        }
    }
    if ($missing.Count -gt 0) {
        throw ("labels missing after create: " + ($missing -join ", "))
    }
    Write-Output ("labels_confirmed=" + ($confirmedNames -join ", "))
    Write-Output ("confirmed_project_id=" + $confirmedProject.id)

    $ingest = Get-DotEnvValue -Path $EnvPath -Key "FLOWCI_INGEST_TOKEN"
    if ([string]::IsNullOrEmpty($ingest)) {
        $ingest = New-UrlSafeSecret -ByteCount 32
    }
    Add-Secret -Value $ingest

    $staticKeys = @(
        @{ Key = "VIKUNJA_URL"; Value = $BaseUrl },
        @{ Key = "VIKUNJA_TOKEN"; Value = $apiToken },
        @{ Key = "VIKUNJA_PROJECT_ID"; Value = $projectId },
        @{ Key = "VIKUNJA_USERNAME"; Value = $Username },
        @{ Key = "VIKUNJA_PASSWORD"; Value = $password },
        @{ Key = "FLOWCI_INGEST_TOKEN"; Value = $ingest },
        @{ Key = "FLOWCI_INGEST_HOST"; Value = "127.0.0.1" },
        @{ Key = "FLOWCI_INGEST_PORT"; Value = "8787" },
        @{ Key = "FLOWCI_TIMEZONE"; Value = "Europe/Moscow" },
        @{ Key = "FLOWCI_MORNING_HOUR"; Value = "9" },
        @{ Key = "ROUTERAI_CHAT_MODEL"; Value = "openai/gpt-6.1-sol" },
        @{ Key = "ROUTERAI_STT_MODEL"; Value = "openai/whisper-1" }
    )
    foreach ($entry in $staticKeys) {
        [void](Add-DotEnvIfMissing -Path $EnvPath -Key $entry.Key -Value $entry.Value)
        Assert-EnvKeyOnce -Path $EnvPath -Key $entry.Key
    }

    if ($script:AppendedKeys.Count -eq 0) {
        Write-Output "env_appended=none"
    } else {
        Write-Output ("env_appended=" + ($script:AppendedKeys -join ","))
    }

    $prefixPreserved = $true
    if ($null -ne $script:EnvPrefix) {
        $now = [System.IO.File]::ReadAllBytes($EnvPath)
        if ($now.Length -lt $script:EnvPrefix.Length) {
            $prefixPreserved = $false
        } else {
            for ($i = 0; $i -lt $script:EnvPrefix.Length; $i++) {
                if ($now[$i] -ne $script:EnvPrefix[$i]) {
                    $prefixPreserved = $false
                    break
                }
            }
        }
    }
    Write-Output ("env_prefix_preserved=" + $prefixPreserved)

    $processes = @(Get-Process -Name "vikunja" -ErrorAction SilentlyContinue)
    Write-Output ("process_running=" + ($processes.Count -gt 0))
    Write-Output ("process_count=" + $processes.Count)

    $ignorePath = Join-Path $Root ".gitignore"
    $ignored = $false
    if (Test-Path -LiteralPath $ignorePath) {
        $ignoreText = [System.IO.File]::ReadAllText($ignorePath)
        if ($ignoreText -match '(?m)^\s*(?:/)?tools(?:/|\s|$)') {
            $ignored = $true
        } elseif ($ignoreText -match 'tools/vikunja') {
            $ignored = $true
        }
    }
    Write-Output ("tools_vikunja_gitignored=" + $ignored)
    Write-Output "bootstrap_ok=true"
} catch {
    Write-Output ("bootstrap_failed=" + (Protect-Text -Text $_.Exception.Message))
    exit 1
}
