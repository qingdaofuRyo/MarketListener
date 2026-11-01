param(
    [switch]$OnlyVisual
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Net.Http
$project = Split-Path -Parent $PSScriptRoot
$modelDirectory = Join-Path $project 'data_control\research\models'
New-Item -ItemType Directory -Force -Path $modelDirectory | Out-Null

$models = @(
    @{ Name = 'qwen3-4b-instruct-2507-q8_0.gguf'; Size = 4280403520L;
       Sha = 'ae916ede1c010a26955ee8ae2e908bf8815a3f135ec860439ab924701c69d5f1';
       Url = 'https://huggingface.co/ggml-org/Qwen3-4B-Instruct-2507-Q8_0-GGUF/resolve/main/qwen3-4b-instruct-2507-q8_0.gguf'; Visual = $false },
    @{ Name = 'Qwen3VL-4B-Instruct-Q4_K_M.gguf'; Size = 2497281664L;
       Sha = '66358cb18bb6b3b1b6675aa412c7a88ef01d228f481184d13668e5201c730a0a';
       Url = 'https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF/resolve/main/Qwen3VL-4B-Instruct-Q4_K_M.gguf'; Visual = $true },
    @{ Name = 'mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf'; Size = 453974304L;
       Sha = '30ba2c7dd3127a4561b6cba9d13d0f711c91bdb38742e2f56d73c8cb596bd06d';
       Url = 'https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF/resolve/main/mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf'; Visual = $true }
)

$handler = [System.Net.Http.HttpClientHandler]::new()
$client = [System.Net.Http.HttpClient]::new($handler)
$client.Timeout = [TimeSpan]::FromHours(2)
try {
    foreach ($model in $models) {
        if ($OnlyVisual -and -not $model.Visual) { continue }
        $target = Join-Path $modelDirectory $model.Name
        $existing = if (Test-Path -LiteralPath $target) { (Get-Item -LiteralPath $target).Length } else { 0L }
        if ($existing -gt $model.Size) { throw "Model file is larger than expected: $($model.Name)" }
        if ($existing -lt $model.Size) {
            $request = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Get, $model.Url)
            try {
                if ($existing -gt 0) {
                    $request.Headers.Range = [System.Net.Http.Headers.RangeHeaderValue]::new([long]$existing, $null)
                }
                $response = $client.SendAsync($request, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
                try {
                    if ($existing -gt 0) {
                        if ($response.StatusCode -ne [System.Net.HttpStatusCode]::PartialContent -or
                            $response.Content.Headers.ContentRange.From -ne $existing) {
                            throw "Remote server did not honor the expected byte range for $($model.Name)"
                        }
                    } elseif (-not $response.IsSuccessStatusCode) {
                        throw "Model download returned $($response.StatusCode)"
                    }
                    $stream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
                    $mode = if ($existing -gt 0) { [System.IO.FileMode]::Append } else { [System.IO.FileMode]::CreateNew }
                    $output = [System.IO.File]::Open($target, $mode, [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read)
                    try { $stream.CopyTo($output) } finally { $output.Dispose(); $stream.Dispose() }
                } finally { $response.Dispose() }
            } finally { $request.Dispose() }
        }
        $actualSize = (Get-Item -LiteralPath $target).Length
        if ($actualSize -ne $model.Size) { throw "Incomplete model download: $($model.Name), $actualSize bytes" }
        $actualSha = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualSha -ne $model.Sha) { throw "Model SHA-256 mismatch: $($model.Name)" }
        Write-Output "Verified $($model.Name): $actualSize bytes"
    }
} finally {
    $client.Dispose()
    $handler.Dispose()
}
