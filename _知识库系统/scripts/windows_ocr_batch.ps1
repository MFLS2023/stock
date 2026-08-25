param(
    [Parameter(Mandatory = $true)]
    [string]$ManifestPath,
    [string]$Language = "zh-Hans"
)

$ErrorActionPreference = "Stop"

# stderr/stdout 统一 UTF-8：Python 侧按 utf-8 解码，缺这行中文错误信息到
# Python 侧就是 GBK 乱码（2026-08-25 冒烟实测踩到）。
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime

$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.FileAccessMode, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]

$script:AsTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq "AsTask" -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1
} | Select-Object -First 1)

function Await-WinRT {
    param($Operation, [Type]$ResultType)
    $method = $script:AsTaskGeneric.MakeGenericMethod($ResultType)
    $task = $method.Invoke($null, @($Operation))
    $task.Wait()
    return $task.Result
}

$languageObject = New-Object Windows.Globalization.Language($Language)
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($languageObject)
if ($null -eq $engine) {
    throw "Windows OCR language is unavailable: $Language"
}

$items = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($item in $items) {
    # 2026-08-25 审查（🟡）：此前任一分片失败（解码/写盘/路径）会让整个脚本中止、
    # returncode≠0，Python 侧整批 raise，同批已成功分片的结果全部作废——
    # 与 rapid 轮「单片炸整批」是同一类病。改为逐项容错：失败项写 stderr 留痕、
    # 不产出 txt，由 Python 侧 read_key 的 missing 计数接住（missing_tile_outputs 标记）。
    # 引擎创建失败（上方 throw）仍是致命的：那是环境问题，重试无意义。
    try {
        $resolved = (Resolve-Path -LiteralPath $item.image_path).Path
        $file = Await-WinRT ([Windows.Storage.StorageFile]::GetFileFromPathAsync($resolved)) ([Windows.Storage.StorageFile])
        $stream = Await-WinRT ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        try {
            $decoder = Await-WinRT ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
            $bitmap = Await-WinRT ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
            try {
                $result = Await-WinRT ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
                $parent = Split-Path -Parent $item.output_path
                if ($parent) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
                [System.IO.File]::WriteAllText($item.output_path, $result.Text, [System.Text.UTF8Encoding]::new($false))
            } finally {
                if ($null -ne $bitmap) { $bitmap.Dispose() }
            }
        } finally {
            if ($null -ne $stream) { $stream.Dispose() }
        }
    } catch {
        # 不能用 Write-Error：$ErrorActionPreference=Stop 会把它再抛出去。
        [Console]::Error.WriteLine(("分片OCR失败 {0}: {1}" -f $item.image_path, $_.Exception.Message))
        continue
    }
}
