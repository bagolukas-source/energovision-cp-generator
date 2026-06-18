"""PV*SOL .pvprj -> interaktivny 3D HTML (univerzalny parser, vsetky schemy).
 - Budova: objekt s "udov" v nazve a najvacsim footprintom (typ 67/15/18/16/2...), fallback najvacsi footprint.
 - Moduly: MRSV (ModulreiheSparVar, aj vnoreny v ModulreihenFormation) ALEBO mriezka (ModulFormation + Module Zeile/Spalte).
 - Mapa: MapExtract.jpg ALEBO .png; mierka px/m = IW / map.BreiteR.
 - Registracia satelitu (deterministicka, bez svetlikov): orientacia z azimutu theta=baz+map_az,
   width=(cos,sin), depth=(sin,-cos); stred = velka biela strecha blizko stredu obrazu, inak stred obrazu.
 - Panely: A-ram (V-Z sklon 10deg), sirka/hlbka radu v metroch z dat, realna textura.
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
const ROWS=__ROWS__, ANGLE=__ANGLE__, SAT=__SATMETA__, PTEX="__PANELTEX__", SATB64="__SATB64__",SATMIME="__SATMIME__";
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
  const tex=new THREE.TextureLoader().load('data:image/'+SATMIME+';base64,'+SATB64);tex.colorSpace=THREE.SRGBColorSpace;tex.flipY=false;
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
const panels=new THREE.Group();
ROWS.forEach(r=>{
  const w=r.wm||(Math.max(1,r.w)*1.995), dep=r.dm||2.436;
  const tilt=(r.tilt!=null?r.tilt:10)*Math.PI/180;
  const yaw=(r.yaw!=null?r.yaw:0)*Math.PI/180;
  let mat=fallbackMat;
  if(baseTex){const t=baseTex.clone();t.needsUpdate=true;t.repeat.set(Math.max(1,r.w),1);mat=new THREE.MeshStandardMaterial({map:t,metalness:.35,roughness:.4});}
  const m=new THREE.Mesh(new THREE.BoxGeometry(w,0.04,dep),mat);
  m.castShadow=true;m.receiveShadow=true;
  m.rotation.x=-tilt;                      // sklon: +Z hrana dole = panel sa pozera v smere downslope (azimut)
  m.position.y=Math.sin(tilt)*dep/2+0.02;  // zdvihni aby spodna hrana sedela na streche
  const g=new THREE.Group();
  g.add(m);
  g.position.set(r.x,0.30,r.z);
  g.rotation.y=yaw;
  panels.add(g);
});
scene.add(panels);
// default pohlad: sikmy vtaci pohlad, adaptivny na velkost sceny (ramuje panely)
let _ex=12; ROWS.forEach(r=>{if(isFinite(r.x)&&isFinite(r.z))_ex=Math.max(_ex,Math.abs(r.x),Math.abs(r.z));});
const _D=_ex*2.5+60;
cam.position.set(_D*0.34,_D*0.62,_D*0.70); cam.up.set(0,1,0); ctrl.target.set(0,0,0); ctrl.update();
addEventListener('resize',()=>{cam.aspect=innerWidth/innerHeight;cam.updateProjectionMatrix();renderer.setSize(innerWidth,innerHeight);});
document.getElementById('shot').onclick=()=>{renderer.render(scene,cam);const a=document.createElement('a');a.download='FVE_3D_'+Date.now()+'.png';a.href=cv.toDataURL('image/png');a.click();};
(function loop(){ctrl.update();renderer.render(scene,cam);requestAnimationFrame(loop);})();
</script>
</body></html>'''


def _ff(o, path):
    if o is None:
        return None
    e = o.find(path)
    try:
        return float(e.text) if (e is not None and e.text) else None
    except Exception:
        return None


def _footprint(o):
    brs = [float(e.text) for e in o.iter("BreiteR") if e.text]
    tfs = [float(e.text) for e in o.iter("TiefeR") if e.text]
    return (max(brs) if brs else 0.0, max(tfs) if tfs else 0.0)


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
    objs = [o for o in root.iter("ZeichenObjekt") if o.find("StandardDaten") is not None]

    # autoritativny sklon/azimut: Modul_Verschaltung.xml (Neigung=sklon, Azimut=smer)
    import re as _re
    _mv = read("Visu3D/Modul_Verschaltung.xml")
    _neig = []; _azis = set()
    if _mv:
        _t = _mv.decode("utf-8", "ignore")
        _neig = [math.degrees(float(x)) for x in _re.findall(r"<Neigung>([^<]+)</Neigung>", _t)]
        _azis = {round(math.degrees(float(x))) % 360 for x in _re.findall(r"<Azimut>([^<]+)</Azimut>", _t)}
    fallback_tilt = (sorted(_neig)[len(_neig) // 2] if _neig else 10.0)

    # --- budova: objekt s "udov" v nazve a najvacsim footprintom; fallback = najvacsi footprint ---
    bld = None; bld_area = -1.0
    for o in objs:
        sd = o.find("StandardDaten")
        if "udov" not in (sd.findtext("Bezeichnung") or ""):
            continue
        bw, bd = _footprint(o)
        if bw * bd > bld_area:
            bld_area = bw * bd; bld = o
    if bld is None:
        for o in objs:
            t = o.find("StandardDaten").findtext("AnwObjTyp")
            if t in ("65", "4", "79"):
                continue
            bw, bd = _footprint(o)
            if bw * bd > bld_area:
                bld_area = bw * bd; bld = o

    bsd = bld.find("StandardDaten") if bld is not None else None
    bx = _ff(bsd, "PosAufBezugsFL/X") or 0.0
    by = _ff(bsd, "PosAufBezugsFL/Y") or 0.0
    baz = _ff(bsd, "Rotation/AzimutWinkel") or 0.0
    BW, BD = _footprint(bld) if bld is not None else (0.0, 0.0)

    # --- mapa (satelitny vyrez) ---
    mapobj = None
    for o in objs:
        if o.find("StandardDaten").findtext("AnwObjTyp") == "65":
            mapobj = o; break
    mapBW, mapTF = _footprint(mapobj) if mapobj is not None else (0.0, 0.0)
    maz = _ff(mapobj.find("StandardDaten"), "Rotation/AzimutWinkel") if mapobj is not None else None
    if maz is None:
        maz = 180.0

    # --- rozmery modulu z typ 37 (Hoehe = strany modulu) ---
    hoehe = sorted({round(float(e.text), 3) for o in objs
                    if o.find("StandardDaten").findtext("AnwObjTyp") == "37"
                    for e in o.iter("Hoehe") if e.text})
    mod_short = hoehe[0] if hoehe else 1.134
    mod_long = hoehe[-1] if len(hoehe) > 1 else 1.999

    MW, ROWD = 1.995, 2.436  # MRSV rad: sirka modulu, roztec radu

    # --- zber radov: (cx, cy) = STRED v roof-local, n, sirka_m, hlbka_m ---
    rows = []
    th = math.radians(baz); ct, st = math.cos(th), math.sin(th)
    for o in objs:
        sd = o.find("StandardDaten")
        mrsv = o.findall(".//ModulreiheSparVar")
        if mrsv:
            f_az = _ff(sd, "Rotation/AzimutWinkel"); f_ze = _ff(sd, "Rotation/ZenitWinkel")
            tilt = (90.0 - f_ze) if (f_ze is not None and f_ze > 5) else fallback_tilt
            tilt = max(0.0, min(60.0, tilt))
            az0 = f_az if f_az is not None else baz
            aframe = (f_ze is None or f_ze < 5) and (round((az0 + 180) % 360) in _azis)
            for ri, r in enumerate(mrsv):
                rp = r.find("PosAufBezugsFL")
                if rp is None:
                    continue
                ex = _ff(rp, "X"); ey = _ff(rp, "Y")
                if ex is None or ey is None:
                    continue
                anz = max(1, int(_ff(r, "AnzModuleHorz") or 1))
                az = az0 + 180 if (aframe and ri % 2) else az0
                yaw = math.degrees(math.atan2(math.cos(math.radians(2 * baz - az)), math.sin(math.radians(2 * baz - az))))
                rows.append((ex + anz * MW / 2.0, ey + ROWD / 2.0, anz, anz * MW, ROWD, round(tilt, 1), round(yaw, 1)))
            continue
        mf = o.find(".//ModulFormation")
        if mf is not None:
            fp = sd.find("PosAufBezugsFL")
            fx = _ff(fp, "X"); fy = _ff(fp, "Y")
            if fx is None or fy is None:
                continue
            faz = _ff(sd, "Rotation/AzimutWinkel")
            f_ze = _ff(sd, "Rotation/ZenitWinkel")
            g_tilt = max(0.0, min(60.0, (90.0 - f_ze) if (f_ze is not None and f_ze > 5) else fallback_tilt))
            g_az = faz if faz is not None else baz
            g_yaw = math.degrees(math.atan2(math.cos(math.radians(2 * baz - g_az)), math.sin(math.radians(2 * baz - g_az))))
            ab = mf.find("AbstandModule")
            gH = _ff(ab, "Horizontal") or 0.02
            gV = _ff(ab, "Vertikal") or 0.02
            stepH = mod_short + gH
            stepV = mod_long + gV
            dfa = math.radians((faz if faz is not None else baz) - baz)
            cfa, sfa = math.cos(dfa), math.sin(dfa)
            byrow = {}
            for m in mf.findall("Module"):
                ze = int(_ff(m, "Zeile") or 0); sp = int(_ff(m, "Spalte") or 0)
                byrow.setdefault(ze, []).append(sp)
            for ze, cols in byrow.items():
                cols.sort()
                seg = [cols[0]]
                segs = []
                for c in cols[1:]:
                    if c == seg[-1] + 1:
                        seg.append(c)
                    else:
                        segs.append(seg); seg = [c]
                segs.append(seg)
                for sgg in segs:
                    nc = len(sgg)
                    lx = (sgg[0] + sgg[-1]) / 2.0 * stepH + stepH / 2.0
                    lz = ze * stepV + stepV / 2.0
                    ex = fx + cfa * lx - sfa * lz
                    ey = fy + sfa * lx + cfa * lz
                    rows.append((ex, ey, nc, nc * stepH, stepV, round(g_tilt, 1), round(g_yaw, 1)))
            continue
    if not rows:
        raise ValueError("V projekte sa nenasli moduly (mozno nekompletny/prazdny .pvprj projekt)")

    # ground-mount fallback: ak nie je budova, odvod BW/BD z modulov
    if BW <= 0 or BD <= 0:
        xs = [r[0] for r in rows]; ys = [r[1] for r in rows]
        BW = max(xs) - min(xs) + 4 if xs else 50.0
        BD = max(ys) - min(ys) + 4 if ys else 30.0

    def to_terr(px, py):
        return (bx + ct * px - st * py, by + st * px + ct * py)

    tabs = [(to_terr(cx, cy), n, wm, dm, tl, yw) for (cx, cy, n, wm, dm, tl, yw) in rows]
    n_modules = sum(n for (_p, n, _w, _d, _t, _y) in tabs)
    mcx = sum(p[0] for (p, _n, _w, _d, _t, _y) in tabs) / len(tabs)
    mcy = sum(p[1] for (p, _n, _w, _d, _t, _y) in tabs) / len(tabs)

    # --- satelit: deterministicka registracia z azimutu (vseobecna, bez svetlikov) ---
    sat = read("MapExtract.jpg") or read("MapExtract.png")
    sat_mime = "png" if (sat and sat[:4] == b"\x89PNG") else "jpeg"
    sat_b64 = ""
    sat_meta = None
    if sat and mapBW > 0 and BW > 0 and BD > 0:
        try:
            import numpy as np
            from PIL import Image
            from scipy import ndimage
            im = Image.open(io.BytesIO(sat)).convert("RGB")
            arr = np.asarray(im).astype("int16")
            IH, IW = arr.shape[:2]
            scale = IW / mapBW
            theta = math.radians(baz + maz)
            wd = np.array([math.cos(theta), math.sin(theta)])
            dd = np.array([math.sin(theta), -math.cos(theta)])
            # stred budovy: detekuj velku svetlu strechu blizko stredu obrazu, inak stred obrazu
            center = np.array([IW / 2.0, IH / 2.0])
            try:
                white = (arr.min(2) > 185) & ((arr.max(2) - arr.min(2)) < 35)
                white = ndimage.binary_opening(white, iterations=2)
                lbl, nlab = ndimage.label(white)
                if nlab >= 1:
                    szs = ndimage.sum(np.ones_like(lbl), lbl, range(1, nlab + 1))
                    k = int(np.argmax(szs))
                    rc = np.array([np.where(lbl == k + 1)[1].mean(), np.where(lbl == k + 1)[0].mean()])
                    exp_px = scale * scale * 0 + (BW * BD) * (scale ** 2)  # ocakavana plocha strechy v px
                    if szs[k] > 0.25 * exp_px and np.linalg.norm(rc - center) < 0.28 * IW:
                        center = rc
            except Exception:
                pass
            # roof-local (ex,ey) -> pixel
            def px_of(ex, ey):
                return center + (ex - BW / 2.0) * scale * wd + (ey - BD / 2.0) * scale * dd
            # linearny map -> quad (image -> scene)
            p00 = px_of(0.0, 0.0)
            A2 = np.column_stack([px_of(1.0, 0.0) - p00, px_of(0.0, 1.0) - p00])
            A2inv = np.linalg.inv(A2)
            As = np.array([[ct, -st], [st, ct]])
            bs = np.array([bx - mcx, by - mcy])
            quad = []
            for (cpx, cpy) in [(0, 0), (IW, 0), (IW, IH), (0, IH)]:
                exy = A2inv @ (np.array([cpx, cpy], float) - p00)
                sc = As @ exy + bs
                quad.append([round(float(sc[0]), 2), round(float(sc[1]), 2)])
            sat_b64 = base64.b64encode(sat).decode()
            sat_meta = {"quad": quad, "total": len(rows)}
        except Exception:
            sat_meta = None
    if not sat_b64 and sat:
        sat_b64 = base64.b64encode(sat).decode()

    data = [{"x": round(p[0] - mcx, 2), "z": round(p[1] - mcy, 2),
             "w": n, "wm": round(wm, 2), "dm": round(dm, 2), "tilt": tl, "yaw": yw}
            for (p, n, wm, dm, tl, yw) in tabs]
    panel_tex = read("Visu3D/FrontTexturPvModul.jpg")
    ptex_b64 = base64.b64encode(panel_tex).decode() if panel_tex else ""

    subt = "FVE - %d modulov v %d radoch - satelitny podklad" % (n_modules, len(data))
    html = (_TEMPLATE
            .replace("__ROWS__", json.dumps(data))
            .replace("__ANGLE__", str(round(baz, 3)))
            .replace("__SATMETA__", json.dumps(sat_meta) if sat_meta else "null")
            .replace("__PANELTEX__", ptex_b64)
            .replace("__SATB64__", sat_b64)
            .replace("__SATMIME__", sat_mime if sat else "jpeg")
            .replace("__TITLE__", title)
            .replace("__SUBT__", subt))

    render = read("Visu3D/ProjScreenShot.jpg")
    if not render:
        for n in sorted(names):
            if "Screenshot" in n and n.lower().endswith(".jpg"):
                render = read(n); break
    return {"html": html, "render": render, "n_tables": len(data), "n_modules": n_modules,
            "has_satellite": bool(sat_b64), "calib": sat_meta}
