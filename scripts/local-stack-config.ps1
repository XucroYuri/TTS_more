function Read-LocalStackConfig {
    param([string]$ConfigPath)
    if (!(Test-Path -LiteralPath $ConfigPath)) { throw 'Copy deployment/tts-repos/local-sources.example.json to data/local/comfyui/source-config.json and configure your paths.' }
    $config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($config.version -ne 1) { throw 'Expected local source configuration version 1.' }
    return $config
}
function Get-LocalEndpoint {
    param([string]$Url)
    $endpoint = [Uri]$Url
    $localHost = $endpoint.IsAbsoluteUri -and $endpoint.DnsSafeHost -eq 'localhost'
    $address = $null
    if ($endpoint.IsAbsoluteUri -and [Net.IPAddress]::TryParse($endpoint.DnsSafeHost, [ref]$address)) { $localHost = [Net.IPAddress]::IsLoopback($address) }
    if (!$endpoint.IsAbsoluteUri -or $endpoint.Scheme -ne 'http' -or !$localHost -or $endpoint.UserInfo -or $endpoint.AbsolutePath -ne '/' -or $endpoint.Query -or $endpoint.Fragment) { throw 'Local launchers require an HTTP loopback URL without credentials, path, query, or fragment.' }
    return $endpoint
}
function Resolve-LocalStackPath {
    param([string]$Root, [string]$Path)
    if ([IO.Path]::IsPathRooted($Path)) { return [IO.Path]::GetFullPath($Path) }
    return [IO.Path]::GetFullPath((Join-Path $Root $Path))
}
