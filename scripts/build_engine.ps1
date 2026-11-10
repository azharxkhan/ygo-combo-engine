# Builds the rules engine (ygopro-core) into build\ocgcore.dll on Windows.
#
# Needs: Visual Studio 2019 or later with "Desktop development with C++"
# (or the standalone Build Tools). No other dependencies.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\build_engine.ps1
#
# Flags follow the engine's own premake5.lua Windows settings.
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$Core = Join-Path $Root 'vendor\ygopro-core'
$Out  = Join-Path $Root 'build'
$Obj  = Join-Path $Out 'obj'

if (-not (Test-Path (Join-Path $Core 'ocgapi.h')) -or -not (Test-Path (Join-Path $Core 'lua\src\lapi.c'))) {
    throw 'Engine source missing. Run scripts/fetch_vendor.sh first (from Git Bash).'
}

# Find the newest Visual Studio install that has the x64 C++ tools.
$VsWhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (-not (Test-Path $VsWhere)) { throw 'vswhere.exe not found. Install Visual Studio with "Desktop development with C++".' }
$Vs = & $VsWhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $Vs) { throw 'No Visual Studio install with the C++ tools. Add "Desktop development with C++" in Visual Studio Installer.' }
$VcVars = Join-Path $Vs 'VC\Auxiliary\Build\vcvars64.bat'
$env:PATH = "$(Split-Path $VsWhere);$env:PATH"   # vcvars64.bat calls vswhere

New-Item -ItemType Directory -Force (Join-Path $Obj 'lua'), (Join-Path $Obj 'core') | Out-Null

# Lua must be compiled as C++ (/TP) because the engine relies on C++ exceptions
# passing through Lua, with the engine's config header forced in front (/FI).
$LuaFiles = 'lapi lauxlib lbaselib lcode lctype ldebug ldo ldump lfunc lgc liolib llex lmathlib lmem lobject lopcodes lparser lstate lstring lstrlib ltable ltablib ltm lundump lvm lzio'.Split(' ') |
    ForEach-Object { "`"$Core\lua\src\$_.c`"" }
$CoreFiles = Get-ChildItem $Core -Filter *.cpp | ForEach-Object { "`"$($_.FullName)`"" }
if (Test-Path "$Core\RNG\*.cpp") { $CoreFiles += Get-ChildItem "$Core\RNG" -Filter *.cpp | ForEach-Object { "`"$($_.FullName)`"" } }

# /EHs (not /EHsc): exceptions may cross Lua's functions.
$Common = '/nologo /c /MP /O2 /MT /EHs /std:c++17 /W0 /DWIN32 /D_WIN32 /DNOMINMAX /DNDEBUG'
$LuaCmd  = "cl $Common /TP /FI`"$Core\lua\luaconf-customize.h`" /I`"$Core\lua\src`" /Fo`"$Obj\lua\\`" $($LuaFiles -join ' ')"
$CoreCmd = "cl $Common /GR- /DOCGCORE_EXPORT_FUNCTIONS /I`"$Core`" /I`"$Core\lua\src`" /Fo`"$Obj\core\\`" $($CoreFiles -join ' ')"
$LinkCmd = "link /nologo /DLL /OUT:`"$Out\ocgcore.dll`" `"$Obj\core\*.obj`" `"$Obj\lua\*.obj`""

foreach ($step in @(@('compiling Lua', $LuaCmd), @('compiling ygopro-core', $CoreCmd), @("linking $Out\ocgcore.dll", $LinkCmd))) {
    Write-Host "==> $($step[0])"
    cmd /c "`"$VcVars`" >nul && $($step[1])"
    if ($LASTEXITCODE -ne 0) { throw "Failed while $($step[0])." }
}

Write-Host "Built $Out\ocgcore.dll"
