param(
    [string]$Url = "https://elements.envato.com/stock-video/hello+kitty",
    [string]$AspectRatio = "all",
    [string]$Pages = "1",
    [int]$Threads = 5,
    [string]$Output = "./downloads",
    [string]$Limit = "",
    [string]$FrameStep = "",
    [switch]$NoSheet,
    [string]$Log = "scraper.log"
)

$argsList = @("scraper.py", "--url", $Url, "--aspect-ratio", $AspectRatio, "--pages", $Pages, "--threads", $Threads, "--output", $Output, "--log", $Log)

if ($Limit -ne "") {
    $argsList += @("--limit", $Limit)
}
if ($FrameStep -ne "") {
    $argsList += @("--frame-step", $FrameStep)
}
if ($NoSheet) {
    $argsList += @("--no-sheet")
}

$proc = Start-Process -FilePath "python" -ArgumentList $argsList -WindowStyle Hidden -PassThru
Write-Output "Скрапер запущено у фоні з PID: $($proc.Id)"
Write-Output "Слідкувати за прогресом можна у файлі: $Log"
Write-Output "Команда перегляду логів у реальному часі: Get-Content $Log -Wait -Tail 20"
