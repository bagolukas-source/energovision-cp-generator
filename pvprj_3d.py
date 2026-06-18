"""PV*SOL .pvprj -> interaktivny 3D HTML.
Logika (vseobecna, bez hardcode):
 - PosAufBezugsFL = poloha na terene (spolocny system s mapou); 3D 'Position' ma inu mierku, NEpouzivat.
 - Modul rad: terrain = budovaPAB + R(azimut)*roofPAB (roh-kotva -> stred stola).
 - Satelit: self-kalibracia — detekuj bielu strechu v MapExtract, napaaruj na podorys budovy (BreiteR x TiefeR)
   => mierka px/m + flip; obrazok prevratim aby mapovanie bolo priame.
 - Panely: A-ram (vychod-zapad sklon 10deg), modul 1.99 x 1.134 m, roztec radu 2.436 m, realna textura.
"""
import zipfile, io, base64, json, math
import xml.etree.ElementTree as ET


def _f(e, t):
    if e is None:
        return 0.0
    x = e.findtext(t)
    return float(x) if x else 0.0


_TEMPLATE = r'''<!DOCTYPE html><html lang="sk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Energovision - 3D model FVE</title>
<style>*{margin:0;box-sizing:border-box}html,body{height:100%;overflow:hidden;font-family:-apple-system,Segoe UI,Arial,sans-serif}
#c{width:100%;height:100%;display:block;background:#cfe2f5}
.ui{position:absolute;top:14px;left:14px;background:#fff;border-radius:12px;padding:12px 16px;box-shadow:0 8px 28px rgba(2,6,23,.16);max-width:300px}
.ui .b{font-weight:800;font-size:15px}.ui .b span{color:#92D050}.ui .s{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#64748b;margin:2px 0 8px}
.ui .m{font-size:13px;color:#334155;line-height:1.5}
#shot{position:absolute;top:14px;right:14px;background:#92D050;color:#0F172A;font-weight:700;border:none;border-radius:10px;padding:11px 16px;cursor:pointer;box-shadow:0 6px 20px rgba(2,6,23,.18);font-size:14px}
.hint{position:absolute;bottom:14px;left:50%;transform:translateX(-50%);background:rgba(15,23,42,.82);color:#fff;font-size:12px;padding:8px 14px;border-radius:999px}
</style></head><body>
<canvas id="c"></canvas>
<div class="ui"><div class="b">Energovision <span>FVE</span></div><div class="s">3D model - __TITLE__</div><div class="m">__SUBT__</div></div>
<button id="shot">📸 Stiahnut snimku</button>
<div class="hint">🖱 tahaj = otoc · prave tlacidlo = posun · koliesko = zoom</div>
<script type="importmap">
{"imports":{"three":"https://unpkg.com/three@0.160.0/build/three.module.js","three/addons/":"https://unpkg.com/three@0.160.0/examples/jsm/"}}
</script>
<script type="module">
import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
const ROWS=__ROWS__, ANGLE=__ANGLE__, SAT=__SATMETA__, PTEX="__PANELTEX__", SATB64="__SATB64__";
const cv=document.getElementById('c');
const renderer=new THREE.WebGLRenderer({canvas:cv,antialias:true,preserveDrawingBuffer:true});
renderer.setPixelRatio(Math.min(devicePixelRatio,2));renderer.setSize(innerWidth,innerHeight);renderer.shadowMap.enabled=true;renderer.shadowMap.type=THREE.PCFSoftShadowMap;
const scene=new THREE.Scene();scene.background=new THREE.Color(0xcfe2f5);
const cam=new THREE.PerspectiveCamera(45,innerWidth/innerHeight,0.5,6000);cam.position.set(60,80,150);
const ctrl=new OrbitControls(cam,renderer.domElement);ctrl.enableDamping=true;ctrl.target.set(0,0,0);ctrl.maxPolarAngle=Math.PI/2.04;
scene.add(new THREE.HemisphereLight(0xffffff,0x9ab07a,0.95));
const sun=new THREE.DirectionalLight(0xfff4e0,1.15);sun.position.set(90,200,120);sun.castShadow=true;
sun.shadow.mapSize.set(2048,2048);sun.shadow.camera.left=-300;sun.shadow.camera.right=300;sun.shadow.camera.top=300;sun.shadow.camera.bottom=-300;sun.shadow.camera.far=900;scene.add(sun);
// satelit (samostatny global SATB64 string)
if(SAT){
  const tex=new THREE.TextureLoader().load('data:image/jpeg;base64,'+SATB64);tex.colorSpace=THREE.SRGBColorSpace;
  const gr=new THREE.Mesh(new THREE.PlaneGeometry(SAT.w,SAT.h),new THREE.MeshStandardMaterial({map:tex,roughness:1}));
  gr.rotation.x=-Math.PI/2;gr.position.set(SAT.cx,0,SAT.cz);gr.receiveShadow=true;scene.add(gr);
}else{
  const gr=new THREE.Mesh(new THREE.PlaneGeometry(400,400),new THREE.MeshStandardMaterial({color:0x9ccd6e}));gr.rotation.x=-Math.PI/2;gr.receiveShadow=true;scene.add(gr);
}
// panely
let baseTex=null;
if(PTEX){baseTex=new THREE.TextureLoader().load('data:image/jpeg;base64,'+PTEX);baseTex.colorSpace=THREE.SRGBColorSpace;baseTex.wrapS=baseTex.wrapT=THREE.RepeatWrapping;baseTex.anisotropy=4;}
const fallbackMat=new THREE.MeshStandardMaterial({color:0x16233f,metalness:.5,roughness:.3,emissive:0x0a1530,emissiveIntensity:.18});
const MW=1.995, DEPTH=2.436, TILT=10*Math.PI/180, ANG=-ANGLE*Math.PI/180;
const panels=new THREE.Group();
ROWS.forEach(r=>{
  const w=Math.max(1,r.w)*MW, sd=DEPTH*0.48;
  const g=new THREE.Group();
  [[-1,-DEPTH/4],[1,DEPTH/4]].forEach(sl=>{   // A-ram: vrchol v strede, sklony k vonkajsim hranam (V/Z)
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
</script>
</body></html>'''


def build_pvprj_3d(pvprj_bytes, title="FVE projekt"):
    z = zipfile.ZipFile(io.BytesIO(pvprj_bytes))
    names = {n.replace("\\", "/"): n for n in z.namelist()}

    def read(name):
        real = names.get(name)
        return z.read(real) if real else None

    geo = read("Visu3D/GeometrischeDaten.xml")
    if not geo:
        raise ValueError("V .pvprj chyba Visu3D/GeometrischeDaten.xml")
    root = ET.fromstring(geo)

    bldobj = None
    rows = []
    for o in root.findall(".//ZeichenObjekt"):
        sd = o.find("StandardDaten")
        if sd is None:
            continue
        nm = sd.findtext("Bezeichnung", "")
        typ = sd.findtext("AnwObjTyp", "")
        if nm == "Budovy 01" or (bldobj is None and typ == "67"):
            bldobj = o
        if typ == "38":
            for r in o.findall(".//ModulreiheSparVar"):
                rp = r.find("PosAufBezugsFL")
                if rp is None:
                    continue
                anz = max(1, int(_f(r, "AnzModuleHorz") or 1))
                nmod = len(r.findall(".//Modul")) or anz
                rows.append((_f(rp, "X"), _f(rp, "Y"), anz, nmod))
    if not rows:
        raise ValueError("V projekte nie su rady modulov")
    if bldobj is None:
        raise ValueError("V projekte nie je budova")

    bsd = bldobj.find("StandardDaten")
    bx = _f(bsd.find("PosAufBezugsFL"), "X")
    by = _f(bsd.find("PosAufBezugsFL"), "Y")
    baz = _f(bsd.find("Rotation"), "AzimutWinkel")
    BW = max((float(e.text) for e in bldobj.iter() if e.tag == "BreiteR" and e.text), default=0.0)
    BD = max((float(e.text) for e in bldobj.iter() if e.tag == "TiefeR" and e.text), default=0.0)

    th = math.radians(baz)
    ct, st = math.cos(th), math.sin(th)

    def to_terr(px, py):
        return (bx + ct * px - st * py, by + st * px + ct * py)

    MW, ROWD = 1.995, 2.436
    tabs = []
    for (px, py, anz, nmod) in rows:
        cx, cy = to_terr(px + anz * MW / 2.0, py + ROWD / 2.0)
        tabs.append((cx, cy, anz, nmod))
    mcx = sum(t[0] for t in tabs) / len(tabs)
    mcy = sum(t[1] for t in tabs) / len(tabs)
    n_modules = sum(t[3] for t in tabs)

    # --- satelit: self-kalibracia na strechu ---
    sat = read("MapExtract.jpg")
    sat_b64 = ""
    sat_meta = None
    if sat and BW > 0 and BD > 0:
        try:
            import numpy as np
            from PIL import Image
            im = Image.open(io.BytesIO(sat)).convert("RGB")
            arr = np.asarray(im).astype("int16")
            IH, IW = arr.shape[:2]
            bright = (arr.min(2) > 165) & ((arr.max(2) - arr.min(2)) < 40)
            ys, xs = np.where(bright)
            rx0, rx1 = np.percentile(xs, 4), np.percentile(xs, 96)
            ry0, ry1 = np.percentile(ys, 4), np.percentile(ys, 96)
            rcx, rcy = (rx0 + rx1) / 2, (ry0 + ry1) / 2
            rW, rH = rx1 - rx0, ry1 - ry0
            corn = [(bx + ct * a - st * b, by + st * a + ct * b) for a, b in [(0, 0), (BW, 0), (BW, BD), (0, BD)]]
            fcx = sum(c[0] for c in corn) / 4
            fcy = sum(c[1] for c in corn) / 4
            fW = max(c[0] for c in corn) - min(c[0] for c in corn)
            fH = max(c[1] for c in corn) - min(c[1] for c in corn)
            sx, sy = rW / fW, rH / fH  # px/m
            # flip: vyber kombinaciu kde najviac stolov padne do strechy
            best = None
            for fxs in (1, -1):
                for fys in (1, -1):
                    ins = 0
                    for (cx, cy, a, nm) in tabs:
                        px2 = rcx + fxs * sx * (cx - fcx)
                        py2 = rcy + fys * sy * (cy - fcy)
                        if rx0 <= px2 <= rx1 and ry0 <= py2 <= ry1:
                            ins += 1
                    if best is None or ins > best[0]:
                        best = (ins, fxs, fys)
            _, fxs, fys = best
            # prevrat obrazok aby mapovanie bolo priame (positivne)
            if fxs < 0:
                im = im.transpose(Image.FLIP_LEFT_RIGHT); rcx = IW - rcx
            if fys < 0:
                im = im.transpose(Image.FLIP_TOP_BOTTOM); rcy = IH - rcy
            buf = io.BytesIO(); im.save(buf, "JPEG", quality=85)
            sat_b64 = base64.b64encode(buf.getvalue()).decode()
            # plane v terene: image (px,py) -> terrain (fcx+(px-rcx)/sx, fcy+(py-rcy)/sy)
            sat_cx_terr = fcx + (IW / 2 - rcx) / sx
            sat_cy_terr = fcy + (IH / 2 - rcy) / sy
            sat_meta = {
                "cx": round(float(sat_cx_terr - mcx), 2),
                "cz": round(float(sat_cy_terr - mcy), 2),
                "w": round(float(IW / sx), 1),
                "h": round(float(IH / sy), 1),
                "inside": int(best[0]), "total": len(tabs),
            }
        except Exception:
            sat_meta = None
    if not sat_b64 and sat:
        sat_b64 = base64.b64encode(sat).decode()

    data = [{"x": round(t[0] - mcx, 2), "z": round(t[1] - mcy, 2), "w": t[2]} for t in tabs]
    panel_tex = read("Visu3D/FrontTexturPvModul.jpg")
    ptex_b64 = base64.b64encode(panel_tex).decode() if panel_tex else ""

    subt = "Strecha/pozemna FVE - %d modulov v %d radoch - satelitny podklad" % (n_modules, len(data))
    html = (_TEMPLATE
            .replace("__ROWS__", json.dumps(data))
            .replace("__ANGLE__", str(round(baz, 3)))
            .replace("__SATMETA__", json.dumps(sat_meta) if sat_meta else "null")
            .replace("__PANELTEX__", ptex_b64)
            .replace("__SATB64__", sat_b64)
            .replace("__TITLE__", title)
            .replace("__SUBT__", subt))

    render = read("Visu3D/ProjScreenShot.jpg")
    if not render:
        for n in sorted(names):
            if "Screenshot" in n and n.lower().endswith(".jpg"):
                render = read(n); break
    return {"html": html, "render": render, "n_tables": len(data), "n_modules": n_modules,
            "has_satellite": bool(sat_b64), "calib": sat_meta}
