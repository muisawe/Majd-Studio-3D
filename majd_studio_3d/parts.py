"""Isolated Hunyuan3D-Part runtime and non-destructive part jobs."""

from __future__ import annotations

import importlib.metadata
import json
import os
import queue
import shutil
import subprocess
import sys
import sysconfig
import threading
import time
import uuid
from pathlib import Path

from .model_manager import PARTS_REVISION, DownloadCancelled, safe_relative, write_json


def console_python() -> str:
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        executable = executable.with_name("python.exe")
    return str(executable)


def run_process(command, cwd: Path, log: Path, progress=None, cancel=None, timeout=3600,
                *, progress_prefix="MAJD_PART_PROGRESS ", operation=None):
    progress = progress or (lambda fraction, text: None)
    log.parent.mkdir(parents=True, exist_ok=True)
    lines = queue.Queue()
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    process = subprocess.Popen(command, cwd=cwd, env=environment, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    def read_lines():
        try:
            for line in process.stdout:
                lines.put(line)
        finally:
            process.stdout.close()
            lines.put(None)
    reader = threading.Thread(target=read_lines, daemon=True)
    reader.start()
    start = time.monotonic()
    tail = []
    try:
        with log.open("a", encoding="utf-8") as output:
            while True:
                if cancel is not None and cancel.is_set():
                    raise DownloadCancelled("تم إيقاف العملية.")
                if time.monotonic() - start > timeout:
                    if operation:
                        raise TimeoutError(f"{operation} timed out after {timeout:g}s.")
                    raise RuntimeError("انتهت مهلة تشغيل أدوات الأجزاء.")
                try:
                    line = lines.get(timeout=0.2)
                except queue.Empty:
                    if process.poll() is not None and not reader.is_alive():
                        break
                    continue
                if line is None:
                    if process.poll() is not None:
                        break
                    continue
                output.write(line)
                output.flush()
                tail = (tail + [line.strip()])[-12:]
                if line.startswith(progress_prefix):
                    message = json.loads(line[len(progress_prefix):])
                    progress(message["fraction"], message["description"])
                elif line.strip():
                    progress(None, line.strip()[-180:])
        if process.wait(timeout=10) != 0:
            details="\n".join(tail)
            hint="\nجرّب دقة 128 أو التقسيم بدون إعادة بناء XPart لتقليل VRAM." if operation is None and "out of memory" in details.lower() else ""
            label = f"{operation} failed. " if operation else "تعذر تجهيز أو تشغيل P3-SAM/XPart. "
            raise RuntimeError(label + details + hint + f"\nالسجل: {log}")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        reader.join(timeout=2)


def runtime_requirements(source: str) -> list[str]:
    """Use official requirements with Sonata's supported non-flash attention path."""
    requirements = []
    for line in source.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "--")) or "flash-attention" in line:
            continue
        if line.startswith(("git+", "http:", "https:")):
            raise ValueError("Unexpected runtime package source")
        requirements.append(line)
    return requirements


class PartsService:
    def __init__(self, app_dir: Path, manager):
        self.app_dir = Path(app_dir)
        self.manager = manager
        self.runtime_dir = self.app_dir / "models" / "parts-runtime"
        self.worker = Path(__file__).with_name("parts_worker.py")

    def python(self) -> Path:
        return self.runtime_dir / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def prepare(self, progress=None, cancel=None, reconstruct=False) -> dict:
        progress = progress or (lambda fraction, text: None)
        # Fail before multi-GB downloads if this device cannot execute the official CUDA runtime.
        probe = subprocess.run([console_python(), "-c",
            "import json,torch; print(json.dumps({'torch':torch.__version__,'cuda':torch.version.cuda,'ready':torch.cuda.is_available()}))"],
            capture_output=True, text=True, timeout=30, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if probe.returncode:
            raise RuntimeError("بيئة PyTorch غير جاهزة لتشغيل أدوات الأجزاء.")
        hardware = json.loads(probe.stdout.strip().splitlines()[-1])
        if not hardware["ready"] or not str(hardware["cuda"]).startswith("12."):
            raise RuntimeError("P3-SAM/XPart يحتاجان GPU مع PyTorch CUDA 12. يمكن تنزيل الأوزان من شاشة النماذج، ثم التشغيل على الجهاز المتوافق.")
        code = self.manager.ensure("parts_code", progress, cancel)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        log = self.runtime_dir / "install.log"
        python = self.python()
        if not python.exists():
            progress(None, "إنشاء بيئة مستقلة لأدوات الأجزاء")
            run_process([console_python(), "-m", "venv", "--system-site-packages", str(self.runtime_dir / "venv")],
                        self.runtime_dir, log, progress, cancel)
        # A nested venv inherits the system site-packages, not its parent's venv.
        # Link the current app environment explicitly so its CUDA Torch is reused.
        location = subprocess.run([str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
            capture_output=True, text=True, timeout=30, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if location.returncode:
            raise RuntimeError("تعذر تهيئة بيئة الأجزاء المنفصلة.")
        runtime_site = Path(location.stdout.strip())
        parent_sites = sorted({sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"]})
        (runtime_site / "majd_parent_environment.pth").write_text("\n".join(parent_sites) + "\n", encoding="utf-8")
        receipt = self.runtime_dir / "ready.json"
        fingerprint = {"code_revision": PARTS_REVISION, "torch": hardware["torch"], "cuda": hardware["cuda"], "adapter": 1}
        try:
            installed = json.loads(receipt.read_text()) == fingerprint
        except (OSError, ValueError):
            installed = False
        if not installed:
            requirements = runtime_requirements((code / "requirements.txt").read_text())
            (self.runtime_dir / "requirements.txt").write_text("\n".join(requirements) + "\n")
            constraints = ["torch==" + hardware["torch"]]
            for package in ("torchvision", "torchaudio"):
                try:
                    constraints.append(package + "==" + importlib.metadata.version(package))
                except importlib.metadata.PackageNotFoundError:
                    pass
            (self.runtime_dir / "constraints.txt").write_text("\n".join(constraints) + "\n")
            torch_version = hardware["torch"].split("+")[0].split(".")
            cuda = hardware["cuda"].replace(".", "")
            wheel_url = f"https://data.pyg.org/whl/torch-{torch_version[0]}.{torch_version[1]}.0+cu{cuda}.html"
            progress(None, "تثبيت الاعتماديات المتوافقة داخل بيئة الأجزاء")
            run_process([str(python), "-m", "pip", "install", "-r", str(self.runtime_dir / "requirements.txt"),
                "-c", str(self.runtime_dir / "constraints.txt"), "--find-links", wheel_url,
                "--only-binary=torch-scatter,torch-cluster", "--progress-bar", "off"],
                code, log, progress, cancel)
        progress(None, "فحص تشغيل مكتبات التقسيم")
        run_process([str(python), str(self.worker), "--check", "--code", str(code)], code, log, progress, cancel, 120)
        write_json(receipt, fingerprint)
        # Download only the needed weights; P3-SAM and XPart share the same repository directory.
        weights = self.manager.ensure("xpart" if reconstruct else "p3sam", progress, cancel)
        sonata = self.manager.ensure("sonata", progress, cancel)
        return {"code": str(code), "python": str(python), "weights": str(weights), "sonata": str(sonata / "sonata.pth")}

    def run(self, mesh_path: str, settings: dict, progress=None, cancel=None, runtime=None) -> dict:
        source = Path(mesh_path)
        if not source.is_file() or source.suffix.lower() not in {".glb", ".ply", ".obj"}:
            raise ValueError("اختر ملف GLB أو PLY أو OBJ صالحًا.")
        job_id = uuid.uuid4().hex
        job = self.app_dir / "part_jobs" / job_id
        output = job / "output"
        output.mkdir(parents=True)
        snapshot = job / ("source" + source.suffix.lower())
        shutil.copy2(source, snapshot)
        runtime = runtime or self.prepare(progress, cancel, bool(settings.get("reconstruct")))
        request = {**runtime, "job_id": job_id, "input": str(snapshot.resolve()), "output": str(output.resolve()),
                   "settings": settings, "source_name": source.name}
        write_json(job / "request.json", request)
        run_process([runtime["python"], str(self.worker), "--request", str((job / "request.json").resolve())],
                    Path(runtime["code"]), job / "runtime.log", progress, cancel)
        result = json.loads((output / "result.json").read_text())
        if result.get("job_id") != job_id or not result.get("parts"):
            raise RuntimeError("لم تُنتج العملية أجزاء صالحة؛ راجع سجل التشغيل.")
        for part in result["parts"]:
            path = output / safe_relative(part["path"])
            if not path.is_file() or path.stat().st_size == 0:
                raise RuntimeError("ملف جزء مفقود أو غير مكتمل.")
        return {**result, "output_dir": str(output.resolve())}


def part_file(result: dict, field: str) -> str | None:
    relative = result.get(field)
    return str(Path(result["output_dir"]) / safe_relative(relative)) if relative else None
