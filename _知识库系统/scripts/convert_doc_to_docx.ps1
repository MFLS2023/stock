param(
    [Parameter(Mandatory = $true)]
    [string]$InputPath,
    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

$ErrorActionPreference = "Stop"
$word = $null
$document = $null
try {
    $inputResolved = (Resolve-Path -LiteralPath $InputPath).Path
    $outputParent = Split-Path -Parent $OutputPath
    if ($outputParent) { New-Item -ItemType Directory -Path $outputParent -Force | Out-Null }
    $outputResolved = [System.IO.Path]::GetFullPath($OutputPath)
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0
    $document = $word.Documents.Open($inputResolved, $false, $true, $false)
    # 16 = wdFormatDocumentDefault (.docx)
    $document.SaveAs2($outputResolved, 16)
} finally {
    if ($null -ne $document) {
        $document.Close($false)
        [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($document)
    }
    if ($null -ne $word) {
        $word.Quit()
        [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($word)
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
