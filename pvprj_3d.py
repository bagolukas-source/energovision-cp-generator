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
const TABLES=__TABLES__, BLD=__BLD__, SAT="__SAT__";
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
ground.rotation.x=-Math.PI/2;ground.receiveShadow=true;scene.add(ground);}
else{const gr=new THREE.Mesh(new THREE.PlaneGeometry(500,500),new THREE.MeshStandardMaterial({color:0x9ccd6e}));gr.rotation.x=-Math.PI/2;gr.receiveShadow=true;scene.add(gr);}
const panelMat=new THREE.MeshStandardMaterial({color:0x16233f,metalness:.45,roughness:.3,emissive:0x0a1330,emissiveIntensity:.22});
const legMat=new THREE.MeshStandardMaterial({color:0x9aa3ad});
const tilt=22*Math.PI/180;
function table(x,z,azDeg){const g=new THREE.Group();const cols=18,rows=2,pw=1.0,ph=1.9,gap=.04;
const W=cols*pw+(cols-1)*gap,H=rows*ph+(rows-1)*gap;
const m=new THREE.Mesh(new THREE.BoxGeometry(W,0.08,H),panelMat);m.castShadow=true;m.receiveShadow=true;m.rotation.x=-tilt;m.position.y=H/2*Math.sin(tilt)+0.4;g.add(m);
const leg=new THREE.Mesh(new THREE.BoxGeometry(W,0.6,0.1),legMat);leg.position.y=0.3;g.add(leg);
g.position.set(x,0,z);g.rotation.y=-(azDeg-90)*Math.PI/180;return g;}
TABLES.forEach(t=>scene.add(table(t.x,t.z,t.az)));
if(BLD){const b=new THREE.Mesh(new THREE.BoxGeometry(60,9,30),new THREE.MeshStandardMaterial({color:0xeae4d8,roughness:.9}));
b.position.set(BLD.x,4.5,BLD.z);b.rotation.y=-(BLD.az)*Math.PI/180;b.castShadow=b.receiveShadow=true;scene.add(b);}
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
    tables = []
    bld = None
    for o in root.findall(".//ZeichenObjekt"):
        sd = o.find("StandardDaten")
        if sd is None:
            continue
        nm = sd.findtext("Bezeichnung", "")
        typ = sd.findtext("AnwObjTyp", "")
        pos = sd.find("Position")
        rot = sd.find("Rotation")
        if pos is None:
            continue
        x = float(_t(pos, "X")); y = float(_t(pos, "Y")); zc = float(_t(pos, "Z"))
        az = float(_t(rot, "AzimutWinkel")) if rot is not None else 0.0
        if typ == "38":
            tables.append((x, y, zc, az))
        if nm == "Budovy 01":
            bld = (x, zc, az)
    if not tables:
        raise ValueError("V projekte nie sú modulové plochy (typ 38)")
    cx = sum(t[0] for t in tables) / len(tables)
    cz = sum(t[2] for t in tables) / len(tables)
    data = [{"x": round(t[0] - cx, 2), "z": round(t[2] - cz, 2), "az": round(t[3], 2)} for t in tables]
    bldc = {"x": round(bld[0] - cx, 2), "z": round(bld[1] - cz, 2), "az": round(bld[2], 2)} if bld else None
    sat = read("MapExtract.jpg")
    sat_b64 = base64.b64encode(sat).decode() if sat else ""
    subt = "Pozemná/strešná FVE · %d modulových plôch · satelitný podklad" % len(tables)
    html = (_TEMPLATE
            .replace("__TABLES__", json.dumps(data))
            .replace("__BLD__", json.dumps(bldc))
            .replace("__SAT__", sat_b64)
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
    return {"html": html, "render": render, "n_tables": len(tables),
            "has_satellite": bool(sat), "has_building": bool(bld)}
