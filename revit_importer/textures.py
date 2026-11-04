"""Automatic PBR texturing for imported Revit materials.

Revit material names ("Holz - Weißeiche natur", "Stahlbeton - Ortbeton", ...) are mapped to a
search query + ambientCG category, the best CC0 match is downloaded at the chosen resolution
into a local library (reused across projects), and applied with world-space box mapping at the
real-world size of the scan, so no UVs are needed.
"""

import json
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import bpy

API = "https://ambientcg.com/api/v2/full_json"
UA = {"User-Agent": "BlenderRevitBridge/0.2"}

# (keywords, search query, ambientCG displayCategory, default real size in metres)
# First match wins, so specific rules come before generic ones.
RULES = [
    (("dachziegel", "dachdeckung, ziegel", "roof tile", "roofing", "dachstein", "schindel", "shingle"), "roofing tiles", "Roofing Tiles", 1.0),
    (("parkett", "parquet", "dielen", "wood floor", "holzboden", "laminat", "laminate", "کف چوب", "پارکت"), "wood floor", "Wood Floor", 2.0),
    (("holz", "wood", "timber", "eiche", "oak", "birke", "birch", "nuss", "walnut", "buche", "beech", "kiefer", "pine", "ahorn", "maple", "teak", "چوب"), "wood", "Wood", 2.0),
    (("putz", "verputzt", "plaster", "stucco", "gips", "drywall", "gipskarton", "rigips", "گچ", "اندود"), "plaster", "Plaster", 2.0),
    (("klinker", "ziegel", "brick", "backstein", "آجر"), "bricks", "Bricks", 1.5),
    (("stahlbeton", "ortbeton", "sichtbeton", "beton", "concrete", "screed", "estrich", "بتن"), "concrete", "Concrete", 2.5),
    (("pflaster", "paving", "pavers", "سنگفرش"), "paving stones", "Paving Stones", 2.0),
    (("asphalt", "آسفالت"), "asphalt", "Asphalt", 3.0),
    (("fliese", "fliesen", "kachel", "tile", "tiles", "ceramic", "keramik", "کاشی", "سرامیک"), "tiles", "Tiles", 1.2),
    (("marmor", "marble", "مرمر"), "marble", "Marble", 2.0),
    (("granit", "granite", "گرانیت"), "granite", "Granite", 2.0),
    (("naturstein", "stone", "stein", "travertin", "travertine", "سنگ"), "stone wall", "Rock", 2.0),
    (("kies", "gravel", "schotter", "شن"), "gravel", "Gravel", 2.0),
    (("rasen", "gras", "grass", "lawn", "چمن"), "grass", "Grass", 2.5),
    (("sand", "ماسه"), "sand", "Ground", 3.0),
    (("erde", "soil", "ground", "gelände", "terrain", "topo", "خاک", "زمین"), "ground", "Ground", 3.0),
    (("titanzink", "zink", "zinc", "blech", "sheet metal", "standing seam", "trapez"), "sheet metal", "Sheet Metal", 1.5),
    (("edelstahl", "stainless", "stahl", "steel", "metall", "metal", "alumin", "eisen", "iron", "kupfer", "copper", "فلز", "آلومینیوم", "استیل"), "metal", "Metal", 1.0),
    (("teppich", "carpet", "موکت", "فرش"), "carpet", "Carpet", 1.0),
    (("kunststoff", "plastic", "pvc", "پلاستیک"), "plastic", "Plastic", 1.0),
    (("leder", "leather", "چرم"), "leather", "Leather", 0.8),
    (("stoff", "fabric", "textil", "textile", "leinen", "linen", "پارچه"), "fabric", "Fabric", 0.8),
]
GLASS = ("glas", "glass", "verglasung", "glazing", "شیشه")
WATER = ("wasser", "water", "pool", "آب")
SKIP = ("dämm", "insulation", "air -", "luft", "unresolved", "default", "clearance", "عایق")

# When the converter could not resolve a material, guess from the element's Revit category.
CATEGORY_FALLBACK = {
    "Walls": "plaster", "Floors": "concrete", "Ceilings": "plaster", "Roofs": "roofing tiles",
    "Stairs": "concrete", "Stairs Runs": "concrete", "Stairs Landings": "concrete",
    "Stairs Supports": "concrete", "Structural Columns": "concrete", "Structural Framing": "concrete",
    "Structural Foundations": "concrete", "Toposolid": "ground", "Topography": "ground",
    "Railings": "metal", "Top Rails": "metal", "Handrails": "metal",
    "Curtain Wall Mullions": "metal", "Doors": "wood", "Casework": "wood", "Furniture": "wood",
}
UNRESOLVED = ("unresolved", "ent_entwurf", "körpermodell", "standard - ", "default", "generic", "generisch")
NO_FALLBACK_CATS = {"Rooms", "Mass", "Planting", "Entourage", "Areas", "Spaces"}

MAPS = {  # ambientCG map suffix -> (shader input, non-colour)
    "Color": ("Base Color", False),
    "Roughness": ("Roughness", True),
    "Metalness": ("Metallic", True),
    "NormalGL": ("Normal", True),
    "Opacity": ("Alpha", True),
}


# --------------------------------------------------------------------------- matching
def classify(name):
    n = name.lower()
    if any(k in n for k in GLASS):
        return "GLASS"
    if any(k in n for k in WATER):
        return "WATER"
    if any(k in n for k in SKIP):
        return None
    for keys, query, cat, size in RULES:
        if any(k in n for k in keys):
            return {"query": query, "category": cat, "size": size}
    return None


def _rule_by_query(query):
    return next(({"query": q, "category": c, "size": s} for _, q, c, s in RULES if q == query), None)


def assign_fallbacks(objects):
    """Give objects with unresolved/generic materials a per-category stand-in material.

    Tries the element's Family/Type name first (e.g. 'Außenwand Putz'), then the category.
    Returns the new/used fallback materials so they join the texture job.
    """
    out = {}
    for obj in objects:
        if obj.type != "MESH":
            continue
        cat = obj.get("revit_category", "")
        if cat in NO_FALLBACK_CATS:
            continue
        label = " ".join(str(obj.get(k, "")) for k in ("revit_family", "revit_type_name"))
        for slot in obj.material_slots:
            m = slot.material
            if m is None:
                continue
            name = m.get("revit_material_name", m.name).lower()
            if classify(name) or not any(k in name for k in UNRESOLVED):
                continue
            rule = classify(label)
            if not isinstance(rule, dict):
                rule = _rule_by_query(CATEGORY_FALLBACK.get(cat, ""))
            if rule is None:
                continue
            fb_name = f"RVT {cat or 'Element'} ({rule['query']})"
            fb = bpy.data.materials.get(fb_name)
            if fb is None:
                fb = m.copy()
                fb.name = fb_name
                fb["revit_material_name"] = rule["query"]
                fb["revit_fallback_from"] = m.name
            slot.material = fb
            out[fb.name] = fb
    return list(out.values())


# --------------------------------------------------------------------------- library / network
_CURL = shutil.which("curl") or "/usr/bin/curl"


def _get(url, dest=None, timeout=300):
    """HTTP GET via system curl (fast, system certs/proxy); urllib fallback."""
    if Path(_CURL).exists():
        cmd = [_CURL, "-sSL", "--fail", "--retry", "2", "-m", str(timeout), "-A", UA["User-Agent"], url]
        if dest:
            cmd[1:1] = ["-o", str(dest)]
        r = subprocess.run(cmd, capture_output=True, timeout=timeout + 30)
        if r.returncode != 0:
            raise RuntimeError(f"curl {r.returncode}: {r.stderr.decode(errors='ignore').strip()[:120]}")
        return None if dest else r.stdout
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        if dest is None:
            return r.read()
        with open(dest, "wb") as fh:
            while chunk := r.read(1 << 20):
                fh.write(chunk)

class Library:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        try:
            self.index = json.loads(self.index_path.read_text())
        except (OSError, ValueError):
            self.index = {"queries": {}, "assets": {}}

    def save(self):
        self.index_path.write_text(json.dumps(self.index, indent=1))

    def candidates(self, rule):
        """Ordered asset ids for a rule (cached search)."""
        key = f"{rule['query']}|{rule['category']}"
        if key in self.index["queries"]:
            return self.index["queries"][key]
        url = API + "?" + urllib.parse.urlencode(
            {"q": rule["query"], "type": "Material", "sort": "Popular", "limit": 30,
             "include": "downloadData,dimensionsData,displayData"})
        data = json.loads(_get(url, timeout=60))
        good, rest = [], []
        for a in data.get("foundAssets", []):
            aid = a["assetId"]
            dls = {}
            try:
                for d in a["downloadFolders"]["default"]["downloadFiletypeCategories"]["zip"]["downloads"]:
                    dls[d["attribute"]] = [d["fullDownloadPath"], d["size"]]
            except (KeyError, TypeError):
                continue
            dx, dy = a.get("dimensionX") or 0, a.get("dimensionY") or 0
            self.index["assets"][aid] = {"downloads": dls, "size_m": (max(dx, dy) / 100.0) or None,
                                         "category": a.get("displayCategory", "")}
            (good if a.get("displayCategory", "").lower() == rule["category"].lower() else rest).append(aid)
        ids = good or rest
        self.index["queries"][key] = ids
        self.save()
        return ids

    def asset_dir(self, aid, quality):
        return self.root / f"{aid}_{quality}-JPG"

    def fetch(self, aid, quality):
        """Download + unpack one asset (no-op if cached). Returns the folder."""
        d = self.asset_dir(aid, quality)
        if d.exists() and any(d.glob("*_Color.*")):
            return d
        info = self.index["assets"].get(aid, {})
        dls = info.get("downloads", {})
        attr = f"{quality}-JPG"
        if attr not in dls:  # fall back to the nearest lower resolution available
            for q in ("8K", "4K", "2K", "1K"):
                if f"{q}-JPG" in dls and int(q[0]) <= int(quality[0]):
                    attr = f"{q}-JPG"
                    break
        url = dls[attr][0]
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "download.zip"
        _get(url, dest=tmp, timeout=900)
        with zipfile.ZipFile(tmp) as z:
            for m in z.namelist():
                base = m.rsplit("/", 1)[-1]
                if not base.startswith("._") and any(base.endswith(f"_{s}.jpg") or base.endswith(f"_{s}.png") for s in MAPS):
                    z.extract(m, d)
        tmp.unlink(missing_ok=True)
        return d


# --------------------------------------------------------------------------- UVs
UV_NAME = "RevitBoxUV"


def ensure_box_uv(obj):
    """Real UVs in metres (1 UV unit = 1 m) by per-face box projection.

    Single-user meshes use world coordinates, so textures run continuously across
    neighbouring walls/slabs; shared (instanced) meshes use local coords x object scale.
    UVs work in Solid > Texture view, Eevee/Cycles and survive FBX/glTF export.
    """
    import numpy as np
    me = obj.data
    if me is None or not len(me.polygons) or me.get("revit_box_uv"):
        return
    nv, nl, np_ = len(me.vertices), len(me.loops), len(me.polygons)
    co = np.empty(nv * 3, dtype=np.float64)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    if me.users == 1:
        m = np.array(obj.matrix_world, dtype=np.float64)
        co = co @ m[:3, :3].T + m[:3, 3]
        nrm_m = np.linalg.inv(m[:3, :3]).T
    else:
        sc = obj.matrix_world.to_scale()
        co = co * np.array([abs(sc.x), abs(sc.y), abs(sc.z)])
        nrm_m = None
    fn = np.empty(np_ * 3, dtype=np.float64)
    me.polygons.foreach_get("normal", fn)
    fn = fn.reshape(-1, 3)
    if nrm_m is not None:
        fn = fn @ nrm_m.T
    axis = np.abs(fn).argmax(axis=1)                         # dominant normal axis per face
    loop_start = np.empty(np_, dtype=np.int64); me.polygons.foreach_get("loop_start", loop_start)
    loop_total = np.empty(np_, dtype=np.int64); me.polygons.foreach_get("loop_total", loop_total)
    face_of_loop = np.repeat(np.arange(np_), loop_total)
    vi = np.empty(nl, dtype=np.int64); me.loops.foreach_get("vertex_index", vi)
    p = co[vi]
    a = axis[face_of_loop]
    sign = np.sign(fn[face_of_loop, a]); sign[sign == 0] = 1
    u = np.where(a == 0, p[:, 1] * sign, np.where(a == 1, -p[:, 0] * sign, p[:, 0] * sign))
    v = np.where(a == 2, p[:, 1] * sign, p[:, 2])
    uv = me.uv_layers.get(UV_NAME) or me.uv_layers.new(name=UV_NAME)
    uv.data.foreach_set("uv", np.column_stack([u, v]).astype(np.float32).ravel())
    me.uv_layers.active = uv
    uv.active_render = True
    me["revit_box_uv"] = True


# --------------------------------------------------------------------------- shading
def _principled(mat):
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf is None:
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    return nt, bsdf


def remember_base(mat):
    if "revit_base_color" not in mat:
        _, bsdf = _principled(mat)
        mat["revit_base_color"] = list(bsdf.inputs["Base Color"].default_value)


def clear_textures(mat):
    nt, bsdf = _principled(mat)
    for n in [n for n in nt.nodes if n.get("revit_tex")]:
        nt.nodes.remove(n)
    if "revit_base_color" in mat:
        bsdf.inputs["Base Color"].default_value = mat["revit_base_color"]
    for k in ("revit_texture", "revit_texture_size"):
        mat.pop(k, None)


def apply_glass(mat, water=False):
    remember_base(mat)
    clear_textures(mat)
    nt, bsdf = _principled(mat)
    for name, val in (("Transmission Weight", 1.0), ("Roughness", 0.02 if water else 0.03), ("IOR", 1.33 if water else 1.5)):
        if name in bsdf.inputs:
            bsdf.inputs[name].default_value = val
    mat["revit_texture"] = "procedural water" if water else "procedural glass"


TINT_DEFAULT = {  # how much of the Revit colour's hue/saturation is laid over the texture
    "Plastic": 1.0, "Fabric": 0.9, "Leather": 0.8, "Plaster": 0.8, "Tiles": 0.8, "Carpet": 0.9,
    "Metal": 0.5, "Sheet Metal": 0.4, "Wood": 0.5, "Wood Floor": 0.4,
}


def apply_pbr(mat, folder, size_m, category=""):
    """World-space box-mapped PBR textures at real-world scale."""
    remember_base(mat)
    clear_textures(mat)
    nt, bsdf = _principled(mat)
    x0, y0 = bsdf.location.x - 900, bsdf.location.y

    def node(t, x, y):
        n = nt.nodes.new(t)
        n.location = (x0 + x, y0 + y)
        n["revit_tex"] = True
        return n

    # Metric box-projected UVs (see ensure_box_uv) -> scale to the scan's real size.
    uvn = node("ShaderNodeUVMap", 0, 0)
    uvn.uv_map = UV_NAME
    mapping = node("ShaderNodeMapping", 200, 0)
    s = 1.0 / max(size_m, 0.05)
    mapping.inputs["Scale"].default_value = (s, s, s)
    nt.links.new(uvn.outputs["UV"], mapping.inputs["Vector"])

    y = 300
    for suffix, (inp, noncolor) in MAPS.items():
        f = next(iter(sorted(p for p in Path(folder).rglob(f"*_{suffix}.*")
                              if not p.name.startswith("._"))), None)  # skip exFAT AppleDouble files
        if f is None or inp not in bsdf.inputs:
            continue
        tex = node("ShaderNodeTexImage", 450, y)
        y -= 280
        tex.image = bpy.data.images.load(str(f), check_existing=True)
        if noncolor:
            tex.image.colorspace_settings.name = "Non-Color"
        nt.links.new(mapping.outputs["Vector"], tex.inputs["Vector"])
        if suffix == "NormalGL":
            nm = node("ShaderNodeNormalMap", 750, y + 280)
            nt.links.new(tex.outputs["Color"], nm.inputs["Color"])
            nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
        elif suffix == "Color" and "revit_base_color" in mat:
            mix = node("ShaderNodeMix", 750, y + 280)
            mix.name = mix.label = "Revit Tint"
            mix.data_type = "RGBA"
            mix.blend_type = "COLOR"
            mix.inputs["Factor"].default_value = TINT_DEFAULT.get(category, 0.0)
            a_in = next(s for s in mix.inputs if s.name == "A" and s.type == "RGBA")
            b_in = next(s for s in mix.inputs if s.name == "B" and s.type == "RGBA")
            out = next(s for s in mix.outputs if s.type == "RGBA")
            b_in.default_value = mat["revit_base_color"]
            nt.links.new(tex.outputs["Color"], a_in)
            nt.links.new(out, bsdf.inputs["Base Color"])
        else:
            nt.links.new(tex.outputs["Color"], bsdf.inputs[inp])
    mat["revit_texture"] = Path(folder).name
    mat["revit_texture_size"] = round(size_m, 2)


# --------------------------------------------------------------------------- job runner
class TextureJob:
    """Downloads in a worker thread; shading is applied on the main thread via a timer."""

    def __init__(self, materials, library_root, quality, choose_index=None):
        self.lib = Library(library_root)
        self.quality = quality
        self.choose_index = choose_index or {}
        self.mat_names = [m.name for m in materials]
        self.plan = {}       # mat name -> ("GLASS"/"WATER") or rule dict
        self.ready = {}      # mat name -> (folder, size_m, asset_id, idx)
        self.errors = []
        self.done = False
        self.total = 0
        self.fetched = 0
        for m in materials:
            c = classify(m.get("revit_material_name", m.name))
            if c:
                self.plan[m.name] = c

    def _work(self):
        # 1) resolve which asset each material gets (searches are cached per rule)
        picks = {}
        for name, rule in self.plan.items():
            if rule in ("GLASS", "WATER"):
                continue
            try:
                ids = self.lib.candidates(rule)
                if ids:
                    idx = self.choose_index.get(name, 0) % len(ids)
                    picks[name] = (ids[idx], idx, rule)
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"{name}: {exc}")
        # 2) download every distinct asset once, 4 in parallel
        assets = sorted({aid for aid, _, _ in picks.values()})
        self.total = len(assets)
        folders = {}

        def grab(aid):
            try:
                folders[aid] = self.lib.fetch(aid, self.quality)
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"{aid}: {exc}")
            self.fetched += 1
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(grab, assets))
        for name, (aid, idx, rule) in picks.items():
            if aid in folders:
                size = self.lib.index["assets"].get(aid, {}).get("size_m") or rule["size"]
                self.ready[name] = (folders[aid], size, aid, idx)
        self.done = True

    def start(self):
        threading.Thread(target=self._work, daemon=True).start()
        bpy.app.timers.register(self._tick, first_interval=1.0)

    @staticmethod
    def _status(text):
        try:
            for w in bpy.context.window_manager.windows:
                w.workspace.status_text_set(text)
        except Exception:  # noqa: BLE001 - background mode / no windows
            pass

    def _clear_status(self):
        self._status(None)
        return None

    def _tick(self):
        if not self.done:
            self._status(f"Revit textures ({self.quality}): downloading {self.fetched}/{self.total or '?'} "
                         f"texture sets… (Blender stays usable)")
            return 1.0
        applied = 0
        planned = set(self.plan)
        for obj in bpy.data.objects:  # metric box UVs for every mesh using a textured material
            if obj.type == "MESH" and any(s.material and s.material.name in planned for s in obj.material_slots):
                try:
                    ensure_box_uv(obj)
                except Exception as exc:  # noqa: BLE001
                    self.errors.append(f"UV {obj.name}: {exc}")
        for name in self.mat_names:
            mat = bpy.data.materials.get(name)
            if mat is None or name not in self.plan:
                continue
            rule = self.plan[name]
            if rule in ("GLASS", "WATER"):
                apply_glass(mat, water=(rule == "WATER"))
                applied += 1
            elif name in self.ready:
                folder, size, aid, idx = self.ready[name]
                apply_pbr(mat, folder, size, self.plan[name]["category"])
                mat["revit_texture_index"] = idx
                applied += 1
        msg = f"Revit auto-texture: {applied}/{len(self.mat_names)} materials textured ({self.quality})"
        if self.errors:
            msg += f", {len(self.errors)} failed: " + "; ".join(self.errors[:3])
        print(msg)
        self._status(msg + "  —  textures show in Material Preview / Rendered, or Solid > Color: Texture")
        try:
            for w in bpy.context.window_manager.windows:
                for a in w.screen.areas:
                    if a.type == "VIEW_3D":
                        for sp in a.spaces:
                            if sp.type == "VIEW_3D" and sp.shading.type == "SOLID":
                                sp.shading.color_type = "TEXTURE"  # make textures visible in Solid mode
                    a.tag_redraw()
        except Exception:  # noqa: BLE001
            pass
        bpy.app.timers.register(self._clear_status, first_interval=12.0)
        return None
