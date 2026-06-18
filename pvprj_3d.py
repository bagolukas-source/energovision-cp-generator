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
if(SAT&&SAT.quad){
  const q=SAT.quad;
  const tex=new THREE.TextureLoader().load('data:image/jpeg;base64,'+SATB64);tex.colorSpace=THREE.SRGBColorSpace;tex.flipY=false;
  const g=new THREE.BufferGeometry();
  g.setAttribute('position',new THREE.BufferAttribute(new Float32Array([q[0][0],0,q[0][1],q[1][0],0,q[1][1],q[2][0],0,q[2][1],q[0][0],0,q[0][1],q[2][0],0,q[2][1],q[3][0],0,q[3][1]]),3));
  g.setAttribute('uv',new THREE.BufferAttribute(new Float32Array([0,0,1,0,1,1,0,0,1,1,0,1]),2));
  g.computeVertexNormals();
  const gr=new THREE.Mesh(g,new THREE.MeshStandardMaterial({map:tex,roughness:1,side:THREE.DoubleSide}));
  gr.receiveShadow=true;scene.add(gr);
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
// default pohlad: takmer zhora, sever hore (ako satelitna mapa) - bez "zrkadla" z naklonu
let _ex=1; ROWS.forEach(r=>{_ex=Math.max(_ex,Math.abs(r.x),Math.abs(r.z));});
const _D=_ex*2.3+40;
cam.position.set(0,_D*0.96,_D*0.30); cam.up.set(0,1,0); ctrl.target.set(0,0,0); ctrl.update();
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
    mapobj = None
    rows = []
    skylights = []
    for o in root.findall(".//ZeichenObjekt"):
        sd = o.find("StandardDaten")
        if sd is None:
            continue
        nm = sd.findtext("Bezeichnung", "")
        typ = sd.findtext("AnwObjTyp", "")
        if nm == "Budovy 01" or (bldobj is None and typ == "67"):
            bldobj = o
        if nm == "Otevřené prostranství (Výřez mapy)" or (mapobj is None and typ == "65"):
            mapobj = o
        if typ == "85":
            sp = sd.find("PosAufBezugsFL")
            if sp is not None:
                sx, sy = _f(sp, "X"), _f(sp, "Y")
                if sx is not None and sy is not None:
                    skylights.append((sx, sy))
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

    # --- satelit: self-kalibracia (orientovany obdlznik strechy <-> podorys budovy, 4-rohove mapovanie) ---
    sat = read("MapExtract.jpg")
    sat_b64 = ""
    sat_meta = None
    if sat and BW > 0 and BD > 0:
        try:
            import numpy as np
            from PIL import Image
            from scipy import ndimage
            im = Image.open(io.BytesIO(sat)).convert("RGB")
            arr = np.asarray(im).astype("int16")
            IH, IW = arr.shape[:2]
            mn = arr.min(2); rng = arr.max(2) - arr.min(2)
            white = (mn > 185) & (rng < 35)
            white = ndimage.binary_opening(white, iterations=2)
            lbl, nlab = ndimage.label(white)
            if nlab >= 1:
                szs = ndimage.sum(np.ones_like(lbl), lbl, range(1, nlab + 1))
                roofmask = (lbl == int(np.argmax(szs)) + 1)
                ys, xs = np.where(roofmask)
                cc = np.array([xs.mean(), ys.mean()])
                Pm = np.stack([xs - cc[0], ys - cc[1]])
                _, evec = np.linalg.eigh(np.cov(Pm))
                majorPx = evec[:, 1]; minorPx = evec[:, 0]
                pjm = Pm.T @ majorPx; pjn = Pm.T @ minorPx
                Lmaj = np.percentile(pjm, 99.5) - np.percentile(pjm, 0.5)
                Lmin = np.percentile(pjn, 99.5) - np.percentile(pjn, 0.5)
                notwhite = (mn < 175)

                def px_of(ex, ey, swap, sM, sm):
                    fW = ex / BW - 0.5; fD = ey / BD - 0.5
                    if swap: fW, fD = fD, fW
                    return cc + sM * fW * Lmaj * majorPx + sm * fD * Lmin * minorPx

                def sky_hits(swap, sM, sm):
                    hit = 0
                    for (ex, ey) in skylights:
                        p = px_of(ex, ey, swap, sM, sm)
                        x, y = int(p[0]), int(p[1])
                        if 0 <= x < IW and 0 <= y < IH:
                            x0, x1 = max(0, x - 3), min(IW, x + 4)
                            y0, y1 = max(0, y - 3), min(IH, y + 4)
                            if notwhite[y0:y1, x0:x1].any():
                                hit += 1
                    return hit

                def mod_in_roof(swap, sM, sm):
                    ins = 0
                    for (px0, py0, anz, nmod) in rows:
                        p = px_of(px0 + anz * MW / 2.0, py0 + ROWD / 2.0, swap, sM, sm)
                        ix, iy = int(p[0]), int(p[1])
                        if 0 <= ix < IW and 0 <= iy < IH and roofmask[iy, ix]:
                            ins += 1
                    return ins

                # disambiguacia orientacie: svetliky musia sadnut na sive znacky strechy
                # (asymetricky vzor -> jednoznacne); fallback = max panelov v maske
                use_sky = len(skylights) >= 5
                best = None
                for swap in (0, 1):
                    for sM in (1, -1):
                        for sm in (1, -1):
                            sc = sky_hits(swap, sM, sm) if use_sky else mod_in_roof(swap, sM, sm)
                            if best is None or sc > best[0]:
                                best = (sc, swap, sM, sm)
                _, swap, sM, sm = best

                # linearny map (ex,ey)->pixel a (ex,ey)->scene -> 4-rohovy satelit quad
                p00 = px_of(0.0, 0.0, swap, sM, sm)
                A2 = np.column_stack([px_of(1.0, 0.0, swap, sM, sm) - p00,
                                      px_of(0.0, 1.0, swap, sM, sm) - p00])
                A2inv = np.linalg.inv(A2)
                As = np.array([[ct, -st], [st, ct]])
                bs = np.array([bx - mcx, by - mcy])
                quad = []
                for (cpx, cpy) in [(0, 0), (IW, 0), (IW, IH), (0, IH)]:
                    exy = A2inv @ (np.array([cpx, cpy], dtype=float) - p00)
                    sc = As @ exy + bs
                    quad.append([round(float(sc[0]), 2), round(float(sc[1]), 2)])
                ins = mod_in_roof(swap, sM, sm)
                sat_b64 = base64.b64encode(sat).decode()
                sat_meta = {"quad": quad, "inside": int(ins), "total": len(rows),
                            "skyhit": int(best[0]), "nsky": len(skylights)}
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
