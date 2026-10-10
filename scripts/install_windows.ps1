$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$project   = if ($env:MAJD_STUDIO_ROOT) { $env:MAJD_STUDIO_ROOT } else { "E:\AI\Hunyuan3D-2.1" }
$mvRepo    = if ($env:MAJD_MV_REPO) { $env:MAJD_MV_REPO } else { "E:\AI\Hunyuan3D-2-MV" }
$sourceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$packageDir = Split-Path -Parent $sourceDir
$appSrc = Join-Path $packageDir "majd_studio_3d"
$appDst = Join-Path $project "majd_studio_3d"
$viewerSrc = Join-Path $packageDir "viewer"
$viewerDst = Join-Path $project "majd_viewer_v9"

if (!(Test-Path $project)) {
    throw "لم أجد تثبيت Hunyuan3D-2.1 في: $project"
}
if (Get-NetTCPConnection -LocalPort 7864 -State Listen -ErrorAction SilentlyContinue) {
    throw "أغلق Majd Studio 3D قبل تثبيت حزمة Bootstrap."
}

$python  = "$project\.venv\Scripts\python.exe"
$pythonw = "$project\.venv\Scripts\pythonw.exe"

if (!(Test-Path $python)) {
    throw "لم أجد البيئة الافتراضية: $python"
}

Write-Host ""
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host " Majd Studio 3D V9 Phase 2 - Windows Installer" -ForegroundColor Cyan
Write-Host " Style References + Preflight + Calibration + Conformance" -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host ""

# Git + official Hunyuan3D-2mv repo.
if (!(Get-Command git.exe -ErrorAction SilentlyContinue)) {
    if (!(Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        throw "Git غير موجود و winget غير متاح."
    }
    Write-Host "Installing Git..."
    winget install --id Git.Git -e --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "Git installation failed." }
    $env:PATH += ";C:\Program Files\Git\cmd"
}

if (!(Test-Path "$mvRepo\.git")) {
    Write-Host "Cloning official Hunyuan3D-2 for 2mv..."
    git clone --depth 1 https://github.com/Tencent-Hunyuan/Hunyuan3D-2.git $mvRepo
    if ($LASTEXITCODE -ne 0) { throw "Hunyuan3D-2 clone failed." }
} else {
    Write-Host "Updating Hunyuan3D-2-MV..."
    git -C $mvRepo pull --ff-only
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Could not fast-forward Hunyuan3D-2-MV. Existing checkout will be used."
    }
}

Write-Host "Installing/updating V9 runtime packages..."
& $python -m pip install --upgrade `
    "gradio==5.33.0" "huggingface_hub<1" requests `
    pillow numpy trimesh `
    omegaconf pymeshlab pygltflib xatlas accelerate rembg onnxruntime
if ($LASTEXITCODE -ne 0) { throw "Runtime dependency installation failed." }

Write-Host "Installing official hy3dgen without replacing Torch..."
& $python -m pip install -e $mvRepo --no-deps
if ($LASTEXITCODE -ne 0) { throw "hy3dgen installation failed." }

if (!(Test-Path $viewerSrc)) { throw "Viewer folder missing: $viewerSrc" }
$viewerFiles = @(Get-ChildItem -Path $viewerSrc -Recurse -File | Where-Object {
    $relative = $_.FullName.Substring($viewerSrc.Length + 1)
    $relative -notmatch '^(data|vendor)\\' -and $_.Extension.ToLowerInvariant() -in @(".html", ".css", ".js", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".ico")
})
New-Item -ItemType Directory -Force -Path "$viewerDst\data" | Out-Null
New-Item -ItemType Directory -Force -Path "$viewerDst\vendor" | Out-Null

# Download Three.js locally once. V9 viewer is offline after this.
$threeVersion = "0.180.0"
$vendor = "$viewerDst\vendor"
function Download-File {
    param([string]$Url,[string]$Destination)
    Write-Host "Downloading $([System.IO.Path]::GetFileName($Destination))..."
    Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing
    if (!(Test-Path $Destination)) { throw "Download failed: $Url" }
}
if (!(Test-Path "$vendor\three.module.min.js")) {
    Download-File "https://cdn.jsdelivr.net/npm/three@$threeVersion/build/three.module.min.js" "$vendor\three.module.min.js"
}
if (!(Test-Path "$vendor\OrbitControls.js")) {
    Download-File "https://cdn.jsdelivr.net/npm/three@$threeVersion/examples/jsm/controls/OrbitControls.js" "$vendor\OrbitControls.js"
}
if (!(Test-Path "$vendor\GLTFLoader.js")) {
    Download-File "https://cdn.jsdelivr.net/npm/three@$threeVersion/examples/jsm/loaders/GLTFLoader.js" "$vendor\GLTFLoader.js"
}

# Preflight.
Write-Host "Checking V9 Python syntax..."
$appPythonFiles = @(Get-ChildItem -Path $appSrc -Filter "*.py" -Recurse -File | Select-Object -ExpandProperty FullName)
$compileArgs = @("-m", "py_compile") + $appPythonFiles + @("$packageDir\majd_studio_3d_v9.py")
& $python @compileArgs
if ($LASTEXITCODE -ne 0) { throw "V9 Python syntax check failed." }

Write-Host "Checking V9 data layer..."
& $python -c "import sys,tempfile; from pathlib import Path; from PIL import Image,ImageDraw; sys.path.insert(0,r'$packageDir'); from majd_studio_3d.store import V9Store; from majd_studio_3d.input_qa import run_preflight; d=tempfile.TemporaryDirectory(); root=Path(d.name); s=V9Store(root/'db.sqlite',root/'projects',root/'library'); p=s.list_projects()[0]; sid=p['default_style_id']; s.update_style(sid,preflight_required=1,min_preflight_score=.5,calibration_enabled=1,min_style_geometry_score=0); img=Image.new('RGB',(640,640),'white'); ImageDraw.Draw(img).rectangle((220,60,420,580),fill='navy'); f=root/'front.png'; img.save(f); a=s.create_asset({'project_id':p['id'],'style_id':sid,'name':'preflight','asset_type':'Prop','front_path':str(f)}); r=run_preflight({'front':str(f),'back':None,'left':None,'right':None,'threeq':None,'detail':None},root/'cal',True,512,.8); s.save_preflight(a,r); assert r['status'] in ('PASS','WARN'); print('V9 Phase 2 data/preflight OK')"
if ($LASTEXITCODE -ne 0) { throw "V9 data-layer preflight failed." }

Write-Host "Checking Hunyuan3D-2.1..."
& $python -c "import sys; sys.path.insert(0,r'$project\hy3dshape'); import torch; from hy3dshape.pipelines import Hunyuan3DDiTFlowMatchingPipeline; print(torch.cuda.get_device_name(0)); print('2.1 OK')"
if ($LASTEXITCODE -ne 0) { throw "Hunyuan3D-2.1 preflight failed." }

Write-Host "Checking Hunyuan3D-2mv..."
& $python -c "import sys; sys.path.insert(0,r'$mvRepo'); from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline; print('2mv OK')"
if ($LASTEXITCODE -ne 0) { throw "Hunyuan3D-2mv preflight failed." }

$requiredViewerFiles = @(
    "$viewerSrc\viewer.html",
    "$viewerSrc\viewer.css",
    "$viewerSrc\viewer.js",
    "$vendor\three.module.min.js",
    "$vendor\OrbitControls.js",
    "$vendor\GLTFLoader.js"
)
foreach ($file in $requiredViewerFiles) {
    if (!(Test-Path $file)) { throw "Viewer preflight failed, missing: $file" }
}

# Commit application files after preflight; project data and V8 files remain untouched.
New-Item -ItemType Directory -Force -Path $appDst | Out-Null
foreach ($file in $appPythonFiles) {
    $relative = $file.Substring($appSrc.Length + 1)
    $destination = Join-Path $appDst $relative
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
    Copy-Item $file $destination -Force
}
Copy-Item "$packageDir\majd_studio_3d_v9.py" "$project\majd_studio_3d_v9.py" -Force
Copy-Item "$packageDir\launch_majd_studio_3d_v9.ps1" "$project\launch_majd_studio_3d_v9.ps1" -Force
New-Item -ItemType Directory -Force -Path "$project\scripts" | Out-Null
Copy-Item "$sourceDir\launch_windows.ps1" "$project\scripts\launch_windows.ps1" -Force
foreach ($file in $viewerFiles) {
    $relative = $file.FullName.Substring($viewerSrc.Length + 1)
    $destination = Join-Path $viewerDst $relative
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
    Copy-Item $file.FullName $destination -Force
}
if (!(Test-Path "$project\update_config.json")) {
    if (Test-Path "$project\majd_update_config.json") {
        Copy-Item "$project\majd_update_config.json" "$project\update_config.json"
    } else {
        Copy-Item "$packageDir\update_config.json" "$project\update_config.json"
    }
}
Copy-Item "$packageDir\version.json" "$project\version.json" -Force

# Desktop shortcut replaces the common Majd Studio 3D shortcut with V9.
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "Majd Studio 3D.lnk"
$launcher = "$project\launch_majd_studio_3d_v9.ps1"
$wsh = New-Object -ComObject WScript.Shell
$shortcut = $wsh.CreateShortcut($shortcutPath)
$shortcut.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`""
$shortcut.WorkingDirectory = $project
$shortcut.Description = "Majd Studio 3D V9"

$blender = Get-ChildItem "C:\Program Files\Blender Foundation\Blender*\blender.exe" -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending | Select-Object -First 1
if ($blender) { $shortcut.IconLocation = "$($blender.FullName),0" }
else { $shortcut.IconLocation = "$pythonw,0" }
$shortcut.Save()

Write-Host ""
Write-Host "==============================================" -ForegroundColor Green
Write-Host " V9 PHASE 2 READY" -ForegroundColor Green
Write-Host "==============================================" -ForegroundColor Green
Write-Host "Main UI:   http://127.0.0.1:7864"
Write-Host "3D Viewer: http://127.0.0.1:7865/viewer.html"
Write-Host "V9 data:   $project\majd_v9"
Write-Host "V8 was not deleted or overwritten."
Write-Host ""
Write-Host "Opening Majd Studio 3D V9 Phase 2..."
Start-Process powershell.exe -ArgumentList "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`""
