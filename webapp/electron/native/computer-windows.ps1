param([Parameter(Mandatory = $true)][string]$ScreenshotDir)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Drawing

Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
using System.Text;

public static class MarionetteNativeComputer {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X, Y; }
    [StructLayout(LayoutKind.Sequential)] public struct INPUT { public uint type; public InputUnion U; }
    [StructLayout(LayoutKind.Explicit)] public struct InputUnion {
        [FieldOffset(0)] public MOUSEINPUT mi;
        [FieldOffset(0)] public KEYBDINPUT ki;
    }
    [StructLayout(LayoutKind.Sequential)] public struct MOUSEINPUT {
        public int dx, dy; public uint mouseData, dwFlags, time; public UIntPtr dwExtraInfo;
    }
    [StructLayout(LayoutKind.Sequential)] public struct KEYBDINPUT {
        public ushort wVk, wScan; public uint dwFlags, time; public UIntPtr dwExtraInfo;
    }
    public delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr lParam);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr hwnd, StringBuilder name, int capacity);
    [DllImport("user32.dll", EntryPoint="GetWindowLongW")] static extern int GetWindowLong(IntPtr hwnd, int index);
    [DllImport("user32.dll")] static extern bool PrintWindow(IntPtr hwnd, IntPtr hdc, uint flags);
    [DllImport("user32.dll")] static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll", SetLastError=true)] static extern uint SendInput(uint count, INPUT[] inputs, int size);

    const uint INPUT_MOUSE = 0, INPUT_KEYBOARD = 1;
    const uint MOUSEEVENTF_LEFTDOWN = 0x0002, MOUSEEVENTF_LEFTUP = 0x0004;
    const uint MOUSEEVENTF_WHEEL = 0x0800, MOUSEEVENTF_HWHEEL = 0x01000;
    const uint KEYEVENTF_KEYUP = 0x0002, KEYEVENTF_UNICODE = 0x0004;

    public static IntPtr[] TopWindows(int processId) {
        var result = new List<IntPtr>();
        EnumWindows((hwnd, _) => { uint pid; GetWindowThreadProcessId(hwnd, out pid); if (pid == processId && IsWindowVisible(hwnd)) result.Add(hwnd); return true; }, IntPtr.Zero);
        return result.ToArray();
    }
    public static uint WindowProcessId(IntPtr hwnd) { uint pid; GetWindowThreadProcessId(hwnd, out pid); return pid; }
    public static bool IsPasswordWindow(IntPtr hwnd) {
        if (hwnd == IntPtr.Zero) return false;
        var name = new StringBuilder(256);
        GetClassName(hwnd, name, name.Capacity);
        return name.ToString().IndexOf("EDIT", StringComparison.OrdinalIgnoreCase) >= 0 && (GetWindowLong(hwnd, -16) & 0x20) != 0;
    }
    public static void Capture(IntPtr hwnd, string destination) {
        RECT rect; if (!GetWindowRect(hwnd, out rect)) throw new InvalidOperationException("Cannot read selected window bounds");
        int width = rect.Right - rect.Left, height = rect.Bottom - rect.Top;
        if (width <= 0 || height <= 0 || width > 20000 || height > 20000) throw new InvalidOperationException("Selected window bounds are invalid");
        using (var bitmap = new Bitmap(width, height, PixelFormat.Format32bppArgb))
        using (var graphics = Graphics.FromImage(bitmap)) {
            IntPtr hdc = graphics.GetHdc();
            bool ok;
            try { ok = PrintWindow(hwnd, hdc, 2); } finally { graphics.ReleaseHdc(hdc); }
            if (!ok) throw new InvalidOperationException("The selected window refused capture");
            bitmap.Save(destination, ImageFormat.Png);
        }
    }
    static void Send(params INPUT[] inputs) {
        if (SendInput((uint)inputs.Length, inputs, Marshal.SizeOf(typeof(INPUT))) != inputs.Length) throw new InvalidOperationException("Windows rejected native input");
    }
    static INPUT Mouse(uint flags, uint data = 0) { return new INPUT { type = INPUT_MOUSE, U = new InputUnion { mi = new MOUSEINPUT { dwFlags = flags, mouseData = data } } }; }
    static INPUT Key(ushort vk, ushort scan, uint flags) { return new INPUT { type = INPUT_KEYBOARD, U = new InputUnion { ki = new KEYBDINPUT { wVk = vk, wScan = scan, dwFlags = flags } } }; }
    public static void Click(int x, int y) { if (!SetCursorPos(x, y)) throw new InvalidOperationException("Cannot position pointer"); Send(Mouse(MOUSEEVENTF_LEFTDOWN), Mouse(MOUSEEVENTF_LEFTUP)); }
    public static void TypeText(string text) {
        var inputs = new List<INPUT>();
        foreach (char value in text) { inputs.Add(Key(0, value, KEYEVENTF_UNICODE)); inputs.Add(Key(0, value, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)); }
        for (int index = 0; index < inputs.Count; index += 128) Send(inputs.GetRange(index, Math.Min(128, inputs.Count - index)).ToArray());
    }
    static ushort VirtualKey(string key) {
        var names = new Dictionary<string, ushort>(StringComparer.OrdinalIgnoreCase) {
            {"RETURN", 0x0D}, {"ENTER", 0x0D}, {"TAB", 0x09}, {"SPACE", 0x20}, {"DELETE", 0x2E}, {"BACKSPACE", 0x08},
            {"ESCAPE", 0x1B}, {"LEFT", 0x25}, {"UP", 0x26}, {"RIGHT", 0x27}, {"DOWN", 0x28}, {"HOME", 0x24}, {"END", 0x23}
        };
        ushort value; if (names.TryGetValue(key, out value)) return value;
        if (key.Length == 1) { char c = Char.ToUpperInvariant(key[0]); if ((c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')) return c; }
        throw new InvalidOperationException("Unsupported key " + key);
    }
    public static void Keypress(string[] keys) {
        if (keys == null || keys.Length == 0) throw new InvalidOperationException("No keys supplied");
        var modifiers = new List<ushort>();
        for (int i = 0; i < keys.Length - 1; i++) {
            switch (keys[i].ToUpperInvariant()) {
                case "CTRL": case "CONTROL": modifiers.Add(0x11); break;
                case "ALT": case "OPTION": modifiers.Add(0x12); break;
                case "SHIFT": modifiers.Add(0x10); break;
                case "META": case "WIN": case "WINDOWS": modifiers.Add(0x5B); break;
                default: throw new InvalidOperationException("Unsupported modifier " + keys[i]);
            }
        }
        ushort final = VirtualKey(keys[keys.Length - 1]);
        var inputs = new List<INPUT>();
        foreach (ushort modifier in modifiers) inputs.Add(Key(modifier, 0, 0));
        inputs.Add(Key(final, 0, 0)); inputs.Add(Key(final, 0, KEYEVENTF_KEYUP));
        modifiers.Reverse(); foreach (ushort modifier in modifiers) inputs.Add(Key(modifier, 0, KEYEVENTF_KEYUP));
        Send(inputs.ToArray());
    }
    public static void Scroll(IntPtr hwnd, string direction) {
        RECT rect; if (!GetWindowRect(hwnd, out rect)) throw new InvalidOperationException("Cannot read selected window bounds");
        SetCursorPos(rect.Left + (rect.Right - rect.Left) / 2, rect.Top + (rect.Bottom - rect.Top) / 2);
        int delta = (direction == "up" || direction == "left") ? 480 : -480;
        Send(Mouse((direction == "left" || direction == "right") ? MOUSEEVENTF_HWHEEL : MOUSEEVENTF_WHEEL, unchecked((uint)delta)));
    }
}
'@

[MarionetteNativeComputer]::SetProcessDpiAwarenessContext([IntPtr](-4)) | Out-Null

New-Item -ItemType Directory -Force -Path $ScreenshotDir | Out-Null
$script:Snapshots = @{}
$Automation = [System.Windows.Automation.AutomationElement]
$TreeWalker = [System.Windows.Automation.TreeWalker]::RawViewWalker

function Get-ExecutableId([System.Diagnostics.Process]$Process) {
    try { return "exe:" + $Process.MainModule.FileName.ToLowerInvariant() } catch { return $null }
}

function Get-RunningApps {
    $rows = [System.Collections.Generic.List[object]]::new()
    foreach ($process in [System.Diagnostics.Process]::GetProcesses()) {
        if ($rows.Count -ge 200) { break }
        try {
            if ($process.MainWindowHandle -eq [IntPtr]::Zero) { continue }
            $id = Get-ExecutableId $process
            if (-not $id) { continue }
            $rows.Add([pscustomobject]@{ app_id = $id; name = $process.ProcessName; pid = $process.Id; instance_id = [string]$process.StartTime.ToUniversalTime().Ticks })
        } catch {}
    }
    return @($rows | Sort-Object name, pid)
}

function Resolve-App([string]$AppId) {
    if (-not $AppId.StartsWith("exe:", [StringComparison]::Ordinal)) { throw "Invalid Windows app_id" }
    $matches = @([System.Diagnostics.Process]::GetProcesses() | Where-Object {
        try { $_.MainWindowHandle -ne [IntPtr]::Zero -and (Get-ExecutableId $_) -eq $AppId } catch { $false }
    })
    if ($matches.Count -eq 0) { throw "The selected app is no longer running" }
    if ($matches.Count -ne 1) { throw "Multiple processes share this app_id; select an unambiguous app" }
    return $matches[0]
}

function Get-FrontWindow([System.Diagnostics.Process]$Process) {
    $windows = [MarionetteNativeComputer]::TopWindows($Process.Id)
    if ($windows.Count -eq 0) { throw "The selected app has no visible window" }
    return $windows[0]
}

function Get-ElementLabel([System.Windows.Automation.AutomationElement]$Element) {
    try {
        if ($Element.Current.IsPassword -or [MarionetteNativeComputer]::IsPasswordWindow([IntPtr]$Element.Current.NativeWindowHandle)) { return "[password]" }
        if ($Element.Current.Name) { return [string]$Element.Current.Name }
        $pattern = $null
        if ($Element.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$pattern)) { return [string]$pattern.Current.Value }
    } catch {}
    return ""
}

function Read-Tree([System.Windows.Automation.AutomationElement]$Root) {
    $queue = [System.Collections.Generic.Queue[object]]::new()
    $queue.Enqueue(@($Root, 0))
    $elements = [System.Collections.Generic.List[object]]::new()
    $references = @{}
    $text = [System.Collections.Generic.List[string]]::new()
    while ($queue.Count -gt 0 -and $elements.Count -lt 200) {
        $entry = $queue.Dequeue()
        $element = [System.Windows.Automation.AutomationElement]$entry[0]
        $depth = [int]$entry[1]
        try {
            $ref = "r" + ($elements.Count + 1)
            $role = $element.Current.ControlType.ProgrammaticName.Replace("ControlType.", "")
            $label = Get-ElementLabel $element
            if ($label.Length -gt 160) { $label = $label.Substring(0, 160) }
            if ($label) { $text.Add($label) }
            $bounds = $element.Current.BoundingRectangle
            $row = [ordered]@{ ref = $ref; role = $role; label = $label }
            if (-not $bounds.IsEmpty -and $bounds.Width -gt 0 -and $bounds.Height -gt 0) {
                $row.bounds = [ordered]@{ x = $bounds.X; y = $bounds.Y; width = $bounds.Width; height = $bounds.Height }
            }
            $elements.Add([pscustomobject]$row)
            $references[$ref] = $element
            if ($depth -lt 12) {
                $child = $TreeWalker.GetFirstChild($element)
                while ($null -ne $child -and ($queue.Count + $elements.Count) -lt 500) {
                    $queue.Enqueue(@($child, $depth + 1)); $child = $TreeWalker.GetNextSibling($child)
                }
            }
        } catch {}
    }
    $joined = [string]::Join("`n", $text)
    if ($joined.Length -gt 20000) { $joined = $joined.Substring(0, 20000) }
    return @{ elements = $elements.ToArray(); references = $references; text = $joined }
}

function New-Snapshot($Request) {
    $process = Resolve-App ([string]$Request.app_id)
    $hwnd = Get-FrontWindow $process
    $root = $Automation::FromHandle($hwnd)
    if ($null -eq $root) { throw "Cannot inspect the selected app window" }
    $tree = Read-Tree $root
    $snapshotId = [Guid]::NewGuid().ToString()
    $windowId = "$($process.Id):$($hwnd.ToInt64()):$($process.StartTime.ToUniversalTime().Ticks)"
    $destination = Join-Path $ScreenshotDir ("snapshot-" + [Guid]::NewGuid().ToString() + ".png")
    [MarionetteNativeComputer]::Capture($hwnd, $destination)
    $rect = [MarionetteNativeComputer+RECT]::new()
    if (-not [MarionetteNativeComputer]::GetWindowRect($hwnd, [ref]$rect)) { throw "Cannot read selected window bounds" }
    $script:Snapshots.Clear()
    $script:Snapshots[$snapshotId] = @{ app_id = [string]$Request.app_id; pid = $process.Id; started = $process.StartTime.ToUniversalTime().Ticks; hwnd = $hwnd; title = [string]$root.Current.Name; references = $tree.references; rect = $rect }
    return [pscustomobject]@{
        snapshot_id = $snapshotId; app_id = [string]$Request.app_id; window_id = $windowId
        title = [string]$root.Current.Name; text = $tree.text; elements = $tree.elements
        screenshot_path = $destination; width = $rect.Right - $rect.Left; height = $rect.Bottom - $rect.Top
    }
}

function Get-CheckedSnapshot($Request) {
    $snapshotId = [string]$Request.snapshot_id
    if (-not $script:Snapshots.ContainsKey($snapshotId)) { throw "Stale or unknown snapshot reference" }
    $state = $script:Snapshots[$snapshotId]
    if ($state.app_id -ne [string]$Request.app_id -or -not [MarionetteNativeComputer]::IsWindow($state.hwnd)) { $script:Snapshots.Clear(); throw "Stale or unknown snapshot reference" }
    $process = Resolve-App $state.app_id
    if ($process.Id -ne $state.pid -or $process.StartTime.ToUniversalTime().Ticks -ne $state.started -or [MarionetteNativeComputer]::WindowProcessId($state.hwnd) -ne $state.pid) {
        $script:Snapshots.Clear(); throw "The selected process or window changed; take a new snapshot"
    }
    $rect = [MarionetteNativeComputer+RECT]::new()
    if (-not [MarionetteNativeComputer]::GetWindowRect($state.hwnd, [ref]$rect) -or $rect.Left -ne $state.rect.Left -or $rect.Top -ne $state.rect.Top -or $rect.Right -ne $state.rect.Right -or $rect.Bottom -ne $state.rect.Bottom) { $script:Snapshots.Clear(); throw "The selected window moved or resized; take a new snapshot" }
    $current = $Automation::FromHandle($state.hwnd)
    if ($null -eq $current -or [string]$current.Current.Name -ne $state.title) { $script:Snapshots.Clear(); throw "The selected window navigated or changed; take a new snapshot" }
    [MarionetteNativeComputer]::SetForegroundWindow($state.hwnd) | Out-Null
    if ([MarionetteNativeComputer]::GetForegroundWindow() -ne $state.hwnd) { $script:Snapshots.Clear(); throw "The selected window could not receive focus" }
    return $state
}

function Invoke-Click($Request, $State) {
    if ($Request.ref) {
        $ref = [string]$Request.ref
        if (-not $State.references.ContainsKey($ref)) { throw "Stale or unknown element reference" }
        $element = $State.references[$ref]
        $pattern = $null
        if ($element.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) { $pattern.Invoke(); return }
        $bounds = $element.Current.BoundingRectangle
        if ($bounds.IsEmpty) { throw "The element cannot be clicked" }
        [MarionetteNativeComputer]::Click([int]($bounds.X + $bounds.Width / 2), [int]($bounds.Y + $bounds.Height / 2)); return
    }
    $x = [double]$Request.x; $y = [double]$Request.y
    $width = $State.rect.Right - $State.rect.Left; $height = $State.rect.Bottom - $State.rect.Top
    if ($x -lt 0 -or $y -lt 0 -or $x -ge $width -or $y -ge $height) { throw "Click coordinates are outside the selected window" }
    [MarionetteNativeComputer]::Click($State.rect.Left + [int]$x, $State.rect.Top + [int]$y)
}

function Invoke-Request($Request) {
    switch ([string]$Request.operation) {
        "status" { return [pscustomobject]@{ supported = $true; accessibility = $true; screenRecording = $true; apartment = [Threading.Thread]::CurrentThread.GetApartmentState().ToString(); setup = "Windows UI Automation and window capture are available. Marionette and the target app must run at the same integrity level." } }
        "apps" { return [pscustomobject]@{ apps = @(Get-RunningApps) } }
        "snapshot" { return New-Snapshot $Request }
        { $_ -in @("click", "type", "keypress", "scroll") } {
            $state = Get-CheckedSnapshot $Request
            try {
                if ($Request.operation -eq "click") { Invoke-Click $Request $state }
                elseif ($Request.operation -eq "type") {
                    if ($Request.ref) {
                        $ref = [string]$Request.ref
                        if (-not $state.references.ContainsKey($ref)) { throw "Stale or unknown element reference" }
                        $state.references[$ref].SetFocus()
                    }
                    [MarionetteNativeComputer]::TypeText([string]$Request.text)
                }
                elseif ($Request.operation -eq "keypress") { [MarionetteNativeComputer]::Keypress([string[]]$Request.keys) }
                else { [MarionetteNativeComputer]::Scroll($state.hwnd, [string]$Request.direction) }
            } finally { $script:Snapshots.Clear() }
            return [pscustomobject]@{ ok = $true }
        }
        default { throw "Unsupported operation" }
    }
}

while ($null -ne ($line = [Console]::In.ReadLine())) {
    $response = [ordered]@{ id = ""; ok = $false }
    try {
        if ([Text.Encoding]::UTF8.GetByteCount($line) -gt 1048576) { throw "Request exceeds its size limit" }
        $request = $line | ConvertFrom-Json
        if (-not $request.id) { throw "Invalid request" }
        $response.id = [string]$request.id
        $response.result = Invoke-Request $request
        $response.ok = $true
    } catch { $response.error = $_.Exception.Message }
    [Console]::Out.WriteLine(($response | ConvertTo-Json -Compress -Depth 10))
    [Console]::Out.Flush()
}
