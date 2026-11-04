"""Blender Revit Bridge — import Autodesk Revit (.rvt) files without Revit.

Wraps the DDC `rvt-decode` command line converter (Revit 2016-2027 files),
converts the .rvt to glTF in the background, imports it, then rebuilds the
BIM structure: one collection per Revit category, real Revit materials, and
Revit parameters as Custom Properties on each object.
"""

import csv
import os
import shlex
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix

import sys

if sys.platform == "darwin":
    _DATA = Path.home() / "Library/Application Support/BlenderRevitBridge"
    CACHE_ROOT = Path.home() / "Library/Caches/BlenderRevitBridge"
else:
    _DATA = Path.home() / ".local/share/blender-revit-bridge"
    CACHE_ROOT = Path.home() / ".cache/blender-revit-bridge"


def _find_converter():
    """First rvt-decode found under the data folder (unpacked DDC release), else empty."""
    exe = "rvt-decode.exe" if sys.platform == "win32" else "rvt-decode"
    hits = []
    if _DATA.exists():  # one/two folder levels; glob("*/x") also follows symlinked folders
        hits = sorted([*_DATA.glob(exe), *_DATA.glob(f"*/{exe}"), *_DATA.glob(f"*/*/{exe}")], reverse=True)
    return str(hits[0]) if hits else str(_DATA / "rvt-decode-<version>-<platform>" / exe)


DEFAULT_BIN = _find_converter()
DEFAULT_TEX_LIB = str(_DATA / "textures")
QUALITY_ITEMS = [
    ("1K", "1K", "Fast, ~5-15 MB per material"),
    ("2K", "2K", "Balanced, ~20-40 MB per material"),
    ("4K", "4K", "High, ~80-150 MB per material"),
    ("8K", "8K", "Very high, ~300-500 MB per material"),
]

# Categories that carry geometry but get in the way of a render / layout.
HIDDEN_BY_DEFAULT = {"Rooms", "Mass", "Areas", "Spaces", "Guide Grid", "Scope Boxes", "Levels", "Grids"}
CORE_FIELDS = ("Category", "Family", "Type Name", "Level", "IfcClass", "GlobalId")


# --------------------------------------------------------------------------- prefs
class RVTIMPORT_Prefs(bpy.types.AddonPreferences):
    bl_idname = __package__

    binary_path: StringProperty(
        name="rvt-decode binary",
        description="Path to the DDC rvt-decode command line converter",
        subtype="FILE_PATH",
        default=DEFAULT_BIN,
    )
    use_remote: BoolProperty(
        name="Use remote Windows converter",
        description="Fall back to (or force) the newer DDC RVTconverter on a Windows machine over SSH",
        default=False,
    )
    remote_host: StringProperty(name="SSH host", default="",
                                description="Host alias from ~/.ssh/config (key-based login)")
    remote_exe: StringProperty(name="Remote RVTconverter.exe",
                               default=r"C:\DDC\RVT-Converter\cli\RVTconverter.exe")
    remote_dir: StringProperty(name="Remote work folder", default=r"C:\DDC\blender",
                               description="Temporary jobs folder on the Windows machine (cleaned after each import)")
    texture_library: StringProperty(
        name="Texture library", subtype="DIR_PATH", default=DEFAULT_TEX_LIB,
        description="Downloaded CC0 textures (ambientCG) are cached here and reused across projects")
    keep_files: BoolProperty(
        name="Keep converted files",
        description="Keep the converted GLB / IFC / CSV in the cache folder after import "
                    "(the huge diagnostic report.json is always removed)",
        default=False,
    )

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "binary_path")
        col.prop(self, "keep_files")
        col.prop(self, "texture_library")
        box = col.box()
        box.prop(self, "use_remote")
        sub = box.column()
        sub.enabled = self.use_remote
        sub.prop(self, "remote_host")
        sub.prop(self, "remote_exe")
        sub.prop(self, "remote_dir")
        ok = os.access(bpy.path.abspath(self.binary_path), os.X_OK)
        col.label(text="Converter found" if ok else "Converter NOT found / not executable",
                  icon="CHECKMARK" if ok else "ERROR")
        col.label(text=f"Cache: {CACHE_ROOT}", icon="FILE_FOLDER")


def _prefs(context):
    addon = context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


# --------------------------------------------------------------------------- converter
def _remote_script(prefs, src, out_dir):
    """Shell pipeline: upload .rvt -> run RVTconverter.exe over SSH -> download GLB + CSV -> clean up."""
    host = prefs.remote_host
    job = "job_" + uuid.uuid4().hex[:10]
    wdir = prefs.remote_dir.rstrip("\\/") + "\\" + job
    wfwd = wdir.replace("\\", "/")
    ssh = f"ssh -o ConnectTimeout=30 -o ServerAliveInterval=15 {shlex.quote(host)}"
    q = shlex.quote
    return "\n".join([
        "cleanup() { " + f"{ssh} {q('rmdir /s /q ' + wdir)} >/dev/null 2>&1; " + "}",
        "trap cleanup EXIT INT TERM",
        "set -e",
        f"mkdir -p {q(str(out_dir))}",
        "echo 'STAGE connecting'",
        f"{ssh} {q('mkdir ' + wdir)}",
        "echo 'STAGE uploading'",
        f"scp -q {q(src)} {q(host + ':' + wfwd + '/in.rvt')}",
        "echo 'STAGE converting (remote 2.0.7)'",
        "rc=0",
        f"{ssh} {q(prefs.remote_exe + ' ' + wdir + chr(92) + 'in.rvt -o ' + wdir + chr(92) + 'out --csv --report-detail summary')} || rc=$?",
        '[ "$rc" -le 2 ] || { echo "Conversion failed: remote exit $rc"; exit 1; }',
        "echo 'STAGE downloading'",
        f"scp -q {q(host + ':' + wfwd + '/out/native_graph.glb')} {q(str(out_dir) + '/')}",
        f"scp -q {q(host + ':' + wfwd + '/out/elements.csv')} {q(str(out_dir) + '/')} || true",
        f"scp -q {q(host + ':' + wfwd + '/out/warnings.txt')} {q(str(out_dir) + '/')} || true",
        "echo 'STAGE done'",
    ])


def _build_cmd(binary, src, out_dir, mode, colors, prefs=None):
    if mode == "REMOTE":
        return ["/bin/sh", "-c", _remote_script(prefs, src, out_dir)]
    if mode == "NATIVE":
        cmd = [binary, "native-graph", src, "--out", str(out_dir), "--ifc"]
        if colors:
            cmd.append("--source-colors")
        return cmd
    out_dir.mkdir(parents=True, exist_ok=True)
    return [binary, "glb", src, "-o", str(out_dir / "legacy.glb")]


def _result_glb(out_dir, mode):
    p = out_dir / ("legacy.glb" if mode == "LEGACY" else "native_graph.glb")
    return p if p.exists() and p.stat().st_size > 0 else None


def _last_error(text):
    lines = [l for l in text.splitlines() if l.strip() and not l.startswith("[")]
    return lines[-1] if lines else "unknown error"


def _new_out_dir(src):
    stem = Path(src).stem.replace(" ", "_")[:60]
    d = CACHE_ROOT / f"{stem}_{time.strftime('%Y%m%d_%H%M%S')}"
    if d.exists():
        shutil.rmtree(d)
    return d  # native-graph insists on creating it itself


def _load_table(out_dir):
    table = {}
    csv_path = out_dir / "elements.csv"
    if not csv_path.exists():
        return table
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                table[int(row["ElementId"])] = row
            except (KeyError, ValueError):
                pass
    return table


# --------------------------------------------------------------------------- scene build
def _element_id(obj):
    eid = obj.get("element_id")
    if eid is None and obj.name.startswith("element_"):
        try:
            eid = int(obj.name.split("_", 1)[1].split(".")[0])
        except ValueError:
            eid = None
    return int(eid) if eid is not None else None


def _child_collection(parent, name):
    for c in parent.children:
        if c.get("revit_group") == name:
            return c
    c = bpy.data.collections.new(name)
    c["revit_group"] = name
    parent.children.link(c)
    return c


def _exclude(context, coll):
    def walk(lc):
        if lc.collection == coll:
            lc.exclude = True
            return True
        return any(walk(ch) for ch in lc.children)
    walk(context.view_layer.layer_collection)


def _isolate_far(context, root, objs, factor, min_dist):
    """Move objects whose centre lies far from the model's core into a hidden collection.

    Core = median of object centres; radius = 80th percentile distance. Anything beyond
    max(min_dist, factor * radius) is treated as a stray (survey points, lost families...).
    """
    from mathutils import Vector
    meshes = [o for o in objs if o.type == "MESH"]
    if len(meshes) < 10:
        return []
    centres = {}
    for o in meshes:
        pts = [o.matrix_world @ Vector(c) for c in o.bound_box]
        centres[o] = sum(pts, Vector()) / 8.0
    xs, ys, zs = (sorted(c[i] for c in centres.values()) for i in range(3))
    mid = len(xs) // 2
    core = Vector((xs[mid], ys[mid], zs[mid]))
    dists = sorted((c - core).length for c in centres.values())
    radius = dists[int(len(dists) * 0.8)]
    limit = max(min_dist, factor * radius)
    far = [o for o, c in centres.items() if (c - core).length > limit]
    if not far:
        return []
    coll = _child_collection(root, "_Far Elements")
    for o in far:
        o["revit_far_distance_m"] = round((centres[o] - core).length, 1)
        for c in list(o.users_collection):
            c.objects.unlink(o)
        coll.objects.link(o)
    _exclude(context, coll)
    root["revit_far_limit_m"] = round(limit, 1)
    return far


def build_scene(context, glb, table, src, organize, apply_scale, hide_helpers, add_params,
                isolate_far=True, far_factor=4.0, far_min=150.0):
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=str(glb))
    new_objs = [o for o in bpy.data.objects if o not in before]

    root = bpy.data.collections.new(f"RVT {Path(src).stem}")
    context.scene.collection.children.link(root)
    root["revit_source"] = src

    groups = {}
    for obj in new_objs:
        eid = _element_id(obj)
        row = table.get(eid, {}) if eid is not None else {}
        cat = row.get("Category") or "Uncategorized"
        level = row.get("Level") or "No Level"

        if obj.type == "MESH" and apply_scale:
            loc, rot, scl = obj.matrix_basis.decompose()
            if obj.data.users == 1 and any(abs(s - 1.0) > 1e-6 for s in scl):
                obj.data.transform(Matrix.Diagonal(scl).to_4x4())
                obj.scale = (1.0, 1.0, 1.0)

        if eid is not None:
            obj["revit_element_id"] = eid
        if row:
            for k in CORE_FIELDS:
                if row.get(k):
                    obj[f"revit_{k.lower().replace(' ', '_')}"] = row[k]
            if add_params:
                for k, v in row.items():
                    if v and k not in CORE_FIELDS and k != "ElementId":
                        obj[f"rvt: {k}"[:63]] = v[:500]
            label = " - ".join(x for x in (row.get("Family"), row.get("Type Name")) if x)
            obj.name = f"{label or cat} [{eid}]"[:63]
            if obj.data is not None and obj.data.users == 1:
                obj.data.name = obj.name

        if organize == "CATEGORY":
            key = (cat,)
        elif organize == "LEVEL":
            key = (level,)
        elif organize == "LEVEL_CATEGORY":
            key = (level, cat)
        else:
            key = ()
        groups.setdefault(key, []).append((obj, cat))

    hidden = set()
    for key, items in groups.items():
        target = root
        for part in key:
            target = _child_collection(target, part)
        for obj, cat in items:
            for c in list(obj.users_collection):
                c.objects.unlink(obj)
            target.objects.link(obj)
            if hide_helpers and cat in HIDDEN_BY_DEFAULT:
                if organize == "CATEGORY":
                    hidden.add(target)
                else:
                    obj.hide_set(True)
                    obj.hide_render = True
    for coll in hidden:
        _exclude(context, coll)
    far = _isolate_far(context, root, new_objs, far_factor, far_min) if isolate_far else []
    return root, len(new_objs), len(groups), len(far)


# --------------------------------------------------------------------------- operator
class IMPORT_SCENE_OT_revit_rvt(bpy.types.Operator, ImportHelper):
    """Import an Autodesk Revit .rvt file (no Revit needed)"""
    bl_idname = "import_scene.revit_rvt"
    bl_label = "Import Revit (.rvt)"
    bl_options = {"REGISTER", "UNDO", "PRESET"}

    filename_ext = ".rvt"
    filter_glob: StringProperty(default="*.rvt;*.rfa", options={"HIDDEN"})

    mode: EnumProperty(
        name="Decoder",
        items=[
            ("AUTO", "Auto", "Local converter first, then the remote Windows converter if it fails"),
            ("NATIVE", "Local (Mac 2.0.1)", "Fast, on this Mac; fails on some large/older files"),
            ("REMOTE", "Remote (Windows 2.0.7)", "Newest converter on the Windows machine over SSH; slower"),
            ("LEGACY", "Legacy GLB", "Old experimental decoder, geometry only - last resort"),
        ],
        default="AUTO",
    )
    source_colors: BoolProperty(name="Revit materials", default=True,
                                description="Bring Revit material names and colours")
    organize: EnumProperty(
        name="Organize",
        items=[
            ("CATEGORY", "By Category", "Walls, Floors, Doors... as collections"),
            ("LEVEL", "By Level", "One collection per Revit level"),
            ("LEVEL_CATEGORY", "Level > Category", "Nested collections"),
            ("NONE", "Flat", "Single collection"),
        ],
        default="CATEGORY",
    )
    add_params: BoolProperty(name="Revit parameters", default=True,
                             description="Store every Revit parameter as a Custom Property")
    apply_scale: BoolProperty(name="Apply feet→metre scale", default=True,
                              description="Bake the 0.3048 unit scale into the mesh so objects have scale 1")
    hide_helpers: BoolProperty(name="Hide rooms / masses", default=True,
                               description="Exclude Rooms, Mass and similar volumes from view and render")
    auto_texture: BoolProperty(name="Auto-texture materials", default=True,
                               description="Find CC0 PBR textures (ambientCG) matching each Revit material "
                                           "name, download them and apply world-space box mapping")
    texture_quality: EnumProperty(name="Texture quality", items=QUALITY_ITEMS, default="2K")
    isolate_far: BoolProperty(name="Isolate far elements", default=True,
                              description="Move stray elements far from the building (survey points, lost "
                                          "families) into a hidden '_Far Elements' collection so Frame All works")
    far_factor: FloatProperty(name="Distance factor", default=4.0, min=1.5, max=50.0,
                              description="Element is 'far' beyond this many times the model's core radius")
    far_min: FloatProperty(name="Min distance (m)", default=150.0, min=10.0, max=100000.0,
                           description="Never treat anything closer than this as far")

    _proc = None
    _timer = None

    def draw(self, context):
        l = self.layout
        l.prop(self, "mode")
        l.prop(self, "source_colors")
        l.prop(self, "organize")
        l.prop(self, "add_params")
        l.prop(self, "apply_scale")
        l.prop(self, "hide_helpers")
        l.prop(self, "auto_texture")
        if self.auto_texture:
            l.prop(self, "texture_quality")
        l.prop(self, "isolate_far")
        if self.isolate_far:
            row = l.row(align=True)
            row.prop(self, "far_factor")
            row.prop(self, "far_min")

    # -- helpers -------------------------------------------------------------
    def _binary(self, context):
        p = _prefs(context)
        b = bpy.path.abspath(p.binary_path) if p else DEFAULT_BIN
        if not os.access(b, os.X_OK):
            b = _find_converter()  # auto-detect a freshly unpacked converter
        if not os.access(b, os.X_OK):
            raise RuntimeError(f"rvt-decode not found or not executable: {b} — download the DDC converter "
                               "(see README) and set its path in Add-on Preferences")
        return b

    def _modes(self, context):
        p = _prefs(context)
        if self.mode == "AUTO":
            return ["NATIVE", "REMOTE"] if (p and p.use_remote and p.remote_host) else ["NATIVE"]
        if self.mode == "REMOTE" and not (p and p.use_remote and p.remote_host):
            raise RuntimeError("Remote converter is disabled or has no SSH host (Add-on Preferences)")
        return [self.mode]

    def _finish(self, context, glb, out_dir, used_mode, t0):
        table = _load_table(out_dir) if used_mode != "LEGACY" else {}
        root, n, g, nfar = build_scene(context, glb, table, self.filepath, self.organize,
                                       self.apply_scale, self.hide_helpers, self.add_params,
                                       self.isolate_far, self.far_factor, self.far_min)
        p = _prefs(context)
        if p and p.keep_files:
            (out_dir / "report.json").unlink(missing_ok=True)
            root["revit_cache"] = str(out_dir)
        else:
            shutil.rmtree(out_dir, ignore_errors=True)
        if self.auto_texture:
            start_texture_job(context, root, self.texture_quality)
        msg = (f"Revit import: {n} objects, {g} groups, {nfar} far elements hidden, "
               f"{used_mode.lower()} decoder, {time.time() - t0:.1f}s")
        self.report({"INFO"}, msg)
        print(msg)

    # -- blocking path (scripts / background) -------------------------------
    def execute(self, context):
        if context.window is not None and not bpy.app.background:
            return self._start_modal(context)
        t0 = time.time()
        try:
            binary = self._binary(context)
            errors = []
            for m in self._modes(context):
                out_dir = _new_out_dir(self.filepath)
                r = subprocess.run(_build_cmd(binary, self.filepath, out_dir, m, self.source_colors,
                                              _prefs(context)),
                                   capture_output=True, text=True)
                glb = _result_glb(out_dir, m)
                if r.returncode == 0 and glb:
                    self._finish(context, glb, out_dir, m, t0)
                    return {"FINISHED"}
                errors.append(f"{m}: {_last_error(r.stdout + r.stderr)}")
                shutil.rmtree(out_dir, ignore_errors=True)
            raise RuntimeError(" | ".join(errors))
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, f"Revit import failed: {exc}")
            print(f"Revit import failed: {exc}")
            return {"CANCELLED"}

    # -- non-blocking path (UI) ---------------------------------------------
    def _start_modal(self, context):
        try:
            self._bin = self._binary(context)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        try:
            self._queue = self._modes(context)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self._prefs_ref = _prefs(context)
        self._errors = []
        self._t0 = time.time()
        self._launch_next()
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.25, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def _launch_next(self):
        self._cur = self._queue.pop(0)
        self._out = _new_out_dir(self.filepath)
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        self._log = open(CACHE_ROOT / f"{self._out.name}_{self._cur}.log", "w+", encoding="utf-8")
        self._proc = subprocess.Popen(
            _build_cmd(self._bin, self.filepath, self._out, self._cur, self.source_colors, self._prefs_ref),
            stdout=self._log, stderr=subprocess.STDOUT, text=True, start_new_session=True)

    def _status(self, context, text):
        if context.workspace:
            context.workspace.status_text_set(text)

    def modal(self, context, event):
        if event.type == "ESC":
            try:
                os.killpg(self._proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                self._proc.kill()
            self._cleanup(context)
            self.report({"WARNING"}, "Revit import cancelled")
            return {"CANCELLED"}
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        rc = self._proc.poll()
        if rc is None:
            stage = self._cur.lower()
            if self._cur == "REMOTE":
                self._log.flush()
                with open(self._log.name, encoding="utf-8", errors="ignore") as fh:
                    stages = [l[6:].strip() for l in fh if l.startswith("STAGE ")]
                stage = f"remote: {stages[-1]}" if stages else "remote"
            self._status(context, f"Revit import ({stage})… {time.time() - self._t0:.0f}s  —  ESC to cancel")
            return {"RUNNING_MODAL"}

        self._log.seek(0)
        out = self._log.read()
        glb = _result_glb(self._out, self._cur)
        if rc == 0 and glb:
            self._status(context, "Importing geometry into Blender…")
            try:
                self._finish(context, glb, self._out, self._cur, self._t0)
            except Exception as exc:  # noqa: BLE001
                self.report({"ERROR"}, f"Revit import failed while building scene: {exc}")
                self._cleanup(context)
                return {"CANCELLED"}
            self._cleanup(context)
            return {"FINISHED"}

        self._errors.append(f"{self._cur}: {_last_error(out)}")
        shutil.rmtree(self._out, ignore_errors=True)
        if self._queue:
            self._log.close()
            self._launch_next()
            return {"RUNNING_MODAL"}
        self.report({"ERROR"}, "Revit import failed — " + " | ".join(self._errors))
        self._cleanup(context)
        return {"CANCELLED"}

    def _cleanup(self, context):
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
        if self._log:
            self._log.close()
        self._status(context, None)


# --------------------------------------------------------------------------- texturing
def _tex_library(context):
    p = _prefs(context)
    return bpy.path.abspath(p.texture_library) if p else DEFAULT_TEX_LIB


def _materials_of(objs):
    mats = {}
    for o in objs:
        for slot in getattr(o, "material_slots", []):
            if slot.material:
                mats[slot.material.name] = slot.material
    return list(mats.values())


def start_texture_job(context, root, quality, materials=None, choose_index=None):
    from . import textures
    import re
    if root is not None:
        textures.assign_fallbacks(list(root.all_objects))
    mats = materials if materials is not None else _materials_of(root.all_objects)
    for m in mats:
        if "revit_material_name" not in m:
            m["revit_material_name"] = re.sub(r"\.\d{3}$", "", m.name)
    job = textures.TextureJob(mats, _tex_library(context), quality, choose_index)
    job.start()
    return job


def _revit_materials(context, selected_only):
    objs = context.selected_objects if selected_only else [
        o for c in bpy.data.collections if c.get("revit_source") for o in c.all_objects]
    return _materials_of(objs)


class REVIT_OT_auto_texture(bpy.types.Operator):
    """Find, download and apply CC0 PBR textures for Revit materials"""
    bl_idname = "revit.auto_texture"
    bl_label = "Auto-texture"
    bl_options = {"REGISTER", "UNDO"}

    selected_only: BoolProperty(name="Selected objects only", default=False)

    def execute(self, context):
        from . import textures
        objs = context.selected_objects if self.selected_only else [
            o for c in bpy.data.collections if c.get("revit_source") for o in c.all_objects]
        textures.assign_fallbacks(objs)
        mats = _materials_of(objs)
        if not mats:
            self.report({"WARNING"}, "No Revit materials found")
            return {"CANCELLED"}
        start_texture_job(context, None, context.scene.revit_tex_quality, materials=mats)
        self.report({"INFO"}, f"Texturing {len(mats)} materials in background ({context.scene.revit_tex_quality})…")
        return {"FINISHED"}


class REVIT_OT_swap_texture(bpy.types.Operator):
    """Try the next matching texture for the active material"""
    bl_idname = "revit.swap_texture"
    bl_label = "Swap Texture"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = context.active_object
        mat = obj.active_material if obj else None
        if mat is None:
            self.report({"WARNING"}, "Select an object with a material")
            return {"CANCELLED"}
        nxt = int(mat.get("revit_texture_index", -1)) + 1
        start_texture_job(context, None, context.scene.revit_tex_quality,
                          materials=[mat], choose_index={mat.name: nxt})
        self.report({"INFO"}, f"Fetching texture option #{nxt + 1} for {mat.name}…")
        return {"FINISHED"}


class REVIT_OT_revert_texture(bpy.types.Operator):
    """Remove auto textures and restore the original Revit colour"""
    bl_idname = "revit.revert_texture"
    bl_label = "Revert to Revit Colour"
    bl_options = {"REGISTER", "UNDO"}

    all_materials: BoolProperty(name="All Revit materials", default=False)

    def execute(self, context):
        from . import textures
        if self.all_materials:
            mats = _revit_materials(context, False)
        else:
            obj = context.active_object
            mats = [obj.active_material] if obj and obj.active_material else []
        for m in mats:
            textures.clear_textures(m)
        return {"FINISHED"}


class REVIT_PT_textures(bpy.types.Panel):
    bl_label = "Revit Textures"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Revit"

    def draw(self, context):
        l = self.layout
        l.prop(context.scene, "revit_tex_quality")
        row = l.row(align=True)
        row.operator("revit.auto_texture", text="Texture All").selected_only = False
        row.operator("revit.auto_texture", text="Selected").selected_only = True
        obj = context.active_object
        mat = obj.active_material if obj else None
        if mat:
            box = l.box()
            box.label(text=mat.name, icon="MATERIAL")
            box.label(text=f"Texture: {mat.get('revit_texture', '— Revit colour —')}")
            tint = mat.node_tree.nodes.get("Revit Tint") if mat.use_nodes and mat.node_tree else None
            if tint:
                box.prop(tint.inputs["Factor"], "default_value", text="Revit colour tint", slider=True)
            r = box.row(align=True)
            r.operator("revit.swap_texture", icon="FILE_REFRESH")
            r.operator("revit.revert_texture", text="Revert", icon="LOOP_BACK").all_materials = False
        l.operator("revit.revert_texture", text="Revert All to Revit Colours").all_materials = True


def menu_func_import(self, context):
    self.layout.operator(IMPORT_SCENE_OT_revit_rvt.bl_idname, text="Revit (.rvt)")


classes = (RVTIMPORT_Prefs, IMPORT_SCENE_OT_revit_rvt, REVIT_OT_auto_texture,
           REVIT_OT_swap_texture, REVIT_OT_revert_texture, REVIT_PT_textures)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.revit_tex_quality = EnumProperty(name="Quality", items=QUALITY_ITEMS, default="2K")
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    del bpy.types.Scene.revit_tex_quality
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
