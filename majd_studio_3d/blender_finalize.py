"""Blender discovery and non-interactive asset finalization; no Gradio dependency."""

from __future__ import annotations

import glob
import os
import subprocess
from pathlib import Path

from .store import slugify

BLENDER_PATTERNS = (
    r"C:\Program Files\Blender Foundation\Blender*\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender *\blender.exe",
)


def find_blender():
    found = []
    for pattern in BLENDER_PATTERNS:
        found.extend(glob.glob(pattern))
    return sorted(found)[-1] if found else None


class BlenderFinalizer:
    def __init__(self, executable):
        self.executable = executable

    def finalize(self, row, source_glb):
        """Return (blend_path, thumbnail_path), or (None, None) when Blender is missing or fails."""
        if not self.executable:
            return None, None
        work = Path(row["output_dir"]) / "blender_work"
        work.mkdir(parents=True, exist_ok=True)
        blend = work / f"{slugify(row['name'])}.blend"
        thumb = work / "thumbnail.png"
        script = work / "_finalize.py"
        code=f'''import bpy\nfrom mathutils import Vector\nGLB={repr(str(Path(source_glb).resolve()))}\nBLEND={repr(str(blend.resolve()))}\nTHUMB={repr(str(thumb.resolve()))}\nTARGET={float(row['target_size'])!r}\nUNIT={repr(row['unit'])}\nbpy.ops.object.select_all(action="SELECT")\nbpy.ops.object.delete(use_global=False)\nbpy.ops.import_scene.gltf(filepath=GLB)\nmeshes=[o for o in bpy.context.scene.objects if o.type=="MESH"]\nif not meshes: raise RuntimeError("No mesh")\ndef bounds():\n pts=[]\n for o in meshes:\n  for c in o.bound_box: pts.append(o.matrix_world@Vector(c))\n mn=Vector((min(p.x for p in pts),min(p.y for p in pts),min(p.z for p in pts)))\n mx=Vector((max(p.x for p in pts),max(p.y for p in pts),max(p.z for p in pts)))\n return mn,mx\nmn,mx=bounds(); h=max(mx.z-mn.z,1e-8); target=TARGET/100.0 if UNIT=="cm" else TARGET; sc=target/h\nfor o in meshes:\n o.scale=tuple(v*sc for v in o.scale)\n for poly in o.data.polygons: poly.use_smooth=True\nbpy.context.view_layer.update(); mn,mx=bounds()\nfor o in meshes: o.location.z-=mn.z\nbpy.context.view_layer.update(); mn,mx=bounds(); center=(mn+mx)*.5; extent=max(mx.x-mn.x,mx.y-mn.y,mx.z-mn.z,.1)\ncamd=bpy.data.cameras.new("PreviewCamera"); cam=bpy.data.objects.new("PreviewCamera",camd); bpy.context.scene.collection.objects.link(cam)\ncam.location=(center.x+extent*1.4,center.y-extent*2,center.z+extent); cam.rotation_euler=(center-cam.location).to_track_quat("-Z","Y").to_euler(); camd.type="ORTHO"; camd.ortho_scale=extent*1.35; bpy.context.scene.camera=cam\nld=bpy.data.lights.new("Key","AREA"); l=bpy.data.objects.new("Key",ld); bpy.context.scene.collection.objects.link(l); l.location=(center.x+extent,center.y-extent,center.z+extent*2); ld.energy=900; ld.size=extent\nscene=bpy.context.scene; scene.render.engine="BLENDER_EEVEE_NEXT"; scene.render.resolution_x=512; scene.render.resolution_y=512; scene.render.resolution_percentage=100; scene.render.image_settings.file_format="PNG"; scene.render.filepath=THUMB\nbpy.ops.render.render(write_still=True); bpy.ops.wm.save_as_mainfile(filepath=BLEND)\n'''

        script.write_text(code, encoding="utf-8")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        result = subprocess.run([self.executable, "--background", "--python", str(script)], creationflags=flags)
        if result.returncode != 0 or not blend.exists():
            return None, None
        return str(blend), str(thumb) if thumb.exists() else None
