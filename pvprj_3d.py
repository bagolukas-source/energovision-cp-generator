"""PV*SOL .pvprj → interaktívny 3D HTML (satelitný podklad + modulové stoly + budova).
Použité webhookom /webhook/pvprj-3d-supabase. Self-contained HTML (satelit base64 inline),
otáčateľný (OrbitControls), tlačidlo na stiahnutie PNG snímky pre prezentáciu."""
import zipfile, io, base64, json
import xml.etree.ElementTree as ET


def _t(e, tag, d="0"):
    if e is None:
        return d
    x = e.findtext(tag)
    return x.strip() if x else d


_TEMPLATE = r'''<!DOCTYPE html><html lang="sk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Energovision — 3D model FVE</title>
<style>*{margin:0;box-sizing:border-box}html,body{height:100%;overflow:hidden;font-family:-apple-system,Segoe UI,Arial,sans-serif}
#c{width:100%;height:100%;display:block;background:#dfeefc}
.ui{position:absolute;top:14px;left:14px;background:#fff;border-radius:12px;padding:12px 16px;box-shadow:0 8px 28px rgba(2,6,23,.16);max-width:300px}
.ui .b{font-weight:800;font-size:15px}.ui .b span{color:#92D050}.ui .s{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#64748b;margin:2px 0 8px}
.ui .m{font-size:13px;color:#334155;line-height:1.5}
#shot{position:absolute;top:14px;right:14px;background:#92D050;color:#0F172A;font-weight:700;border:none;border-radius:10px;padding:11px 16px;cursor:pointer;box-shadow:0 6px 20px rgba(2,6,23,.18);font-size:14px}
#shot:hover{filter:brightness(.96)}
.hint{position:absolute;bottom:14px;left:50%;transform:translateX(-50%);background:rgba(15,23,42,.82);color:#fff;font-size:12px;padding:8px 14px;border-radius:999px}
</style></head><body>
<canvas id="c"></canvas>
<div class="ui"><div class="b">Energovision <span>FVE</span></div><div class="s">3D model · __TITLE__</div>
<div class="m">__SUBT__</div></div>
<button id="shot">📸 Stiahnuť snímku</button>
<div class="hint">🖱️ ťahaj = otoč · pravé tlačidlo = posun · koliesko = zoom</div>
<script type="importmap">
{"imports":{"three":"https://unpkg.com/three@0.160.0/build/three.module.js","three/addons/":"https://unpkg.com/three@0.160.0/examples/jsm/"}}
</script>
<script type="module">
import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
const ROWS=__ROWS__, ANGLE=__ANGLE__, SAT="__SAT__";
const cv=document.getElementById('c');
const renderer=new THREE.WebGLRenderer({canvas:cv,antialias:true,preserveDrawingBuffer:true});
renderer.setPixelRatio(Math.min(devicePixelRatio,2));renderer.setSize(innerWidth,innerHeight);renderer.shadowMap.enabled=true;
const scene=new THREE.Scene();scene.background=new THREE.Color(0xdfeefc);
const cam=new THREE.PerspectiveCamera(45,innerWidth/innerHeight,0.5,4000);cam.position.set(70,90,150);
const ctrl=new OrbitControls(cam,renderer.domElement);ctrl.enableDamping=true;ctrl.target.set(0,0,0);ctrl.maxPolarAngle=Math.PI/2.05;
scene.add(new THREE.HemisphereLight(0xffffff,0x9ab07a,0.9));
const sun=new THREE.DirectionalLight(0xfff4e0,1.2);sun.position.set(120,200,80);sun.castShadow=true;
sun.shadow.mapSize.set(2048,2048);sun.shadow.camera.left=-300;sun.shadow.camera.right=300;sun.shadow.camera.top=300;sun.shadow.camera.bottom=-300;sun.shadow.camera.far=800;scene.add(sun);
if(SAT){const tex=new THREE.TextureLoader().load('data:image/jpeg;base64,'+SAT);tex.colorSpace=THREE.SRGBColorSpace;
const ground=new THREE.Mesh(new THREE.PlaneGeometry(661,411),new THREE.MeshStandardMaterial({map:tex,roughness:1}));
ground.rotation.x=-Math.PI/2;ground.rotation.z=Math.PI;ground.receiveShadow=true;scene.add(ground);}
else{const gr=new THREE.Mesh(new THREE.PlaneGeometry(500,500),new THREE.MeshStandardMaterial({color:0x9ccd6e}));gr.rotation.x=-Math.PI/2;gr.receiveShadow=true;scene.add(gr);}
const PTEX="__PANELTEX__";
let baseTex=null;
if(PTEX){baseTex=new THREE.TextureLoader().load('data:image/jpeg;base64,'+PTEX);baseTex.colorSpace=THREE.SRGBColorSpace;baseTex.wrapS=baseTex.wrapT=THREE.RepeatWrapping;baseTex.anisotropy=4;}
const fallbackMat=new THREE.MeshStandardMaterial({color:0x16233f,metalness:.5,roughness:.28,emissive:0x0a1530,emissiveIntensity:.2});
// jeden rad = n modulov vedľa seba; tenké, mierny sklon; orientované podľa fitu (ANGLE)
const MW=1.995, DEPTH=2.436, TILT=10*Math.PI/180, ANG=-ANGLE*Math.PI/180;
const panels=new THREE.Group();
ROWS.forEach(r=>{
  const w=Math.max(1,r.w)*MW, sd=DEPTH*0.47;
  const g=new THREE.Group();
  [[1,-DEPTH/4],[-1,DEPTH/4]].forEach(sl=>{   // dva sklony: východ + západ (A-rám)
    let mat=fallbackMat;
    if(baseTex){const t=baseTex.clone();t.needsUpdate=true;t.repeat.set(Math.max(1,r.w),1);mat=new THREE.MeshStandardMaterial({map:t,metalness:.35,roughness:.4});}
    const m=new THREE.Mesh(new THREE.BoxGeometry(w,0.05,sd),mat);
    m.castShadow=true;m.receiveShadow=true;m.rotation.x=sl[0]*TILT;m.position.z=sl[1];m.position.y=sd/2*Math.sin(TILT);
    g.add(m);
  });
  g.position.set(r.x,0.30,r.z);g.rotation.y=ANG;
  panels.add(g);
});
scene.add(panels);
addEventListener('resize',()=>{cam.aspect=innerWidth/innerHeight;cam.updateProjectionMatrix();renderer.setSize(innerWidth,innerHeight);});
document.getElementById('shot').onclick=()=>{renderer.render(scene,cam);const a=document.createElement('a');a.download='FVE_3D_'+Date.now()+'.png';a.href=cv.toDataURL('image/png');a.click();};
(function loop(){ctrl.update();renderer.render(scene,cam);requestAnimationFrame(loop);})();
</script></body></html>'''


def build_pvprj_3d(pvprj_bytes, title="FVE projekt"):
    z = zipfile.ZipFile(io.BytesIO(pvprj_bytes))
    names = {n.replace("\\", "/"): n for n in z.namelist()}

    def read(name):
        real = names.get(name)
        return z.read(real) if real else None

    geo = read("Visu3D/GeometrischeDaten.xml")
    if not geo:
        raise ValueError("V .pvprj chýba Visu3D/GeometrischeDaten.xml — projekt nemá 3D vizualizáciu")
    root = ET.fromstring(geo)
    import math as _m
    anchors = []   # (pab_x, pab_y, world_x, world_z) z polí typ 38
    rows = []      # (pab_x, pab_y, anz) — jednotlivé rady modulov
    bld = None
    for o in root.findall(".//ZeichenObjekt"):
        sd = o.find("StandardDaten")
        if sd is None:
            continue
        nm = sd.findtext("Bezeichnung", "")
        typ = sd.findtext("AnwObjTyp", "")
        pos = sd.find("Position"); pab = sd.find("PosAufBezugsFL")
        if nm == "Budovy 01" and pos is not None:
            bld = (float(_t(pos, "X")), float(_t(pos, "Z")))
        if typ != "38" or pos is None or pab is None:
            continue
        anchors.append((float(_t(pab, "X")), float(_t(pab, "Y")), float(_t(pos, "X")), float(_t(pos, "Z"))))
        for r in o.findall(".//ModulreiheSparVar"):
            rp = r.find("PosAufBezugsFL")
            if rp is None:
                continue
            anz = 0
            try:
                anz = int(float(r.findtext("AnzModuleHorz", "0") or 0))
            except Exception:
                anz = 0
            nforms = len(r.findall("FormsMRF")) or 1
            nmod = len(r.findall(".//Modul")) or anz
            rows.append((float(_t(rp, "X")), float(_t(rp, "Y")), max(1, anz), nforms, nmod))
    if not rows:
        raise ValueError("V projekte nie sú rady modulov (ModulreiheSparVar)")

    # rigidný fit (scale=1): world(X,Z) = R(theta)*PAB(x,y) + t  — z kotiev polí
    n = len(anchors)
    mpx = sum(a[0] for a in anchors)/n; mpy = sum(a[1] for a in anchors)/n
    mwx = sum(a[2] for a in anchors)/n; mwz = sum(a[3] for a in anchors)/n
    Sxx = sum((a[0]-mpx)*(a[2]-mwx) + (a[1]-mpy)*(a[3]-mwz) for a in anchors)
    Sxy = sum((a[0]-mpx)*(a[3]-mwz) - (a[1]-mpy)*(a[2]-mwx) for a in anchors)
    theta = _m.atan2(Sxy, Sxx)
    ct, st = _m.cos(theta), _m.sin(theta)
    tx = mwx - (ct*mpx - st*mpy)
    tz = mwz - (st*mpx + ct*mpy)
    def to_world(px, py):
        return (ct*px - st*py + tx, st*px + ct*py + tz)
    # rady -> svet, recentruj na ťažisko modulov
    MW = 1.995   # rozteč modulu naležato (1.99 + 0.005 medzera)
    ROWD = 2.436  # ReihenAbst — rozteč/hĺbka radu z MoSys
    rw = []
    for (px, py, anz, nf, nm) in rows:
        wx, wz = to_world(px + anz*MW/2.0, py + ROWD/2.0)  # PAB je roh -> posun na stred stola
        rw.append((wx, wz, anz, nm))
    mod_cx = sum(r[0] for r in rw)/len(rw); mod_cz = sum(r[1] for r in rw)/len(rw)
    # recenter scény na BUDOVU (ak je), aby satelit vycentrovaný na budovu sadol pod moduly
    cx, cz = (bld[0], bld[1]) if bld else (mod_cx, mod_cz)
    data = [{"x": round(r[0]-cx, 2), "z": round(r[1]-cz, 2), "w": r[2]} for r in rw]
    n_modules_real = sum(r[3] for r in rw)
    angle_deg = round(_m.degrees(theta), 2)
    bldc = {"x": round(bld[0]-cx, 2), "z": round(bld[1]-cz, 2)} if bld else None

    panel_tex = read("Visu3D/FrontTexturPvModul.jpg")
    sat = read("MapExtract.jpg")
    sat_b64 = base64.b64encode(sat).decode() if sat else ""
    ptex_b64 = base64.b64encode(panel_tex).decode() if panel_tex else ""
    total_mod = n_modules_real
    subt = "Strešná/pozemná FVE · %d modulov v %d radoch · satelitný podklad" % (total_mod, len(data))
    html = (_TEMPLATE
            .replace("__ROWS__", json.dumps(data))
            .replace("__ANGLE__", str(angle_deg))
            .replace("__SAT__", sat_b64)
            .replace("__PANELTEX__", ptex_b64)
            .replace("__TITLE__", title)
            .replace("__SUBT__", subt))
    # najlepší PV*SOL render (pre PDF prezentáciu): preferuj prehľad/Juh, inak prvý Screenshot
    render = read("Visu3D/ProjScreenShot.jpg")
    if not render:
        for n in sorted(names):
            if "Screenshot" in n and ("Jih" in n or "Juh" in n):
                render = read(n); break
    if not render:
        for n in sorted(names):
            if n.lower().endswith(".jpg") and "Screenshot" in n:
                render = read(n); break
    return {"html": html, "render": render, "n_tables": len(data), "n_modules": n_modules_real,
            "has_satellite": bool(sat), "has_building": bool(bld)}
