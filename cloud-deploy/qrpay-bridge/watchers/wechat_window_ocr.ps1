param(
    [string]$OutputImage = "",
    [string]$Language = "zh-Hans-CN",
    [int]$DelayMilliseconds = 300,
    [switch]$NoForeground
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.DataWriter, Windows.Storage.Streams, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class QrpayWin32 {
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hWnd, IntPtr hdcBlt, uint nFlags);
  public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
}
"@

function Await-WinRt($Async, $ResultType) {
    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
        Where-Object { $_.Name -eq "AsTask" -and $_.GetParameters().Count -eq 1 -and $_.IsGenericMethod })[0]
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $task = $asTask.Invoke($null, @($Async))
    $task.Wait()
    return $task.Result
}

function Capture-WindowPngBytes {
    param(
        [IntPtr]$Hwnd,
        [switch]$UsePrintWindow
    )
    [QrpayWin32+RECT]$rect = New-Object QrpayWin32+RECT
    [void][QrpayWin32]::GetWindowRect($Hwnd, [ref]$rect)
    $width = [Math]::Max(1, $rect.Right - $rect.Left)
    $height = [Math]::Max(1, $rect.Bottom - $rect.Top)
    $bitmap = New-Object System.Drawing.Bitmap $width, $height
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    if ($UsePrintWindow) {
        $hdc = $graphics.GetHdc()
        try {
            $ok = [QrpayWin32]::PrintWindow($Hwnd, $hdc, 2)
            if (-not $ok) {
                throw "PrintWindow returned false."
            }
        }
        finally {
            $graphics.ReleaseHdc($hdc)
        }
    }
    else {
        $graphics.CopyFromScreen($rect.Left, $rect.Top, 0, 0, (New-Object System.Drawing.Size $width, $height))
    }
    $stream = New-Object System.IO.MemoryStream
    $bitmap.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
    $graphics.Dispose()
    $bitmap.Dispose()
    return $stream.ToArray()
}

function Read-OcrText($Bytes) {
    $stream = [Windows.Storage.Streams.InMemoryRandomAccessStream]::new()
    $writer = [Windows.Storage.Streams.DataWriter]::new($stream)
    $writer.WriteBytes($Bytes)
    [void](Await-WinRt ($writer.StoreAsync()) ([UInt32]))
    [void](Await-WinRt ($writer.FlushAsync()) ([Boolean]))
    $writer.DetachStream() | Out-Null
    $stream.Seek(0)

    $decoder = Await-WinRt ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await-WinRt ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $lang = [Windows.Globalization.Language]::new($Language)
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
    if ($null -eq $engine) {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    }
    if ($null -eq $engine) {
        throw "Windows OCR engine unavailable."
    }
    $result = Await-WinRt ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    return $result.Text
}

$process = Get-Process Weixin -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 } |
    Select-Object -First 1

if ($null -eq $process) {
    throw "Weixin main window not found. Open PC WeChat first."
}

$bytes = $null
$text = ""

try {
    $bytes = Capture-WindowPngBytes -Hwnd $process.MainWindowHandle -UsePrintWindow
    $text = Read-OcrText $bytes
}
catch {
    if ($NoForeground) {
        throw "background WeChat window OCR failed: $($_.Exception.Message)"
    }
}

if ([string]::IsNullOrWhiteSpace($text) -and -not $NoForeground) {
    [void][QrpayWin32]::SetForegroundWindow($process.MainWindowHandle)
    Start-Sleep -Milliseconds $DelayMilliseconds
    $bytes = Capture-WindowPngBytes -Hwnd $process.MainWindowHandle
    $text = Read-OcrText $bytes
}

if ([string]::IsNullOrWhiteSpace($text) -and $NoForeground) {
    throw "background WeChat window OCR returned no text. Keep the WeChat receipt window visible, or unset WECHAT_WINDOW_OCR_NO_FOREGROUND to allow foreground fallback."
}

if ($OutputImage) {
    [System.IO.File]::WriteAllBytes($OutputImage, $bytes)
}
$text
