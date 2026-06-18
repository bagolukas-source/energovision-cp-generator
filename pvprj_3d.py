"""PV*SOL .pvprj -> interaktivny 3D model.
Presny layout modulov z Uebersichtsplan.xml (per-modul polygony) + registracia cez
mapovy ramec (PosAufBezugsFL stred + BreiteR/TiefeR) + sklon z Modul_Verschaltung.
Fallback: ak chyba plan/mapa -> galeria PV*SOL renderov (ProjScreenShot).
"""
import zipfile, io, base64, json, math, re
import xml.etree.ElementTree as ET


def _ff(e, p):
    if e is None:
        return None
    x = e.find(p)
    try:
        return float(x.text) if (x is not None and x.text) else None
    except Exception:
        return None


def _gallery(z, names, title):
    """Fallback: galeria PV*SOL renderov."""
    shots = []
    proj = None
    for k in sorted(names):
        b = k.rsplit("/", 1)[-1].lower()
        if not b.endswith((".jpg", ".jpeg", ".png")):
            continue
        if b.startswith("projscreenshot"):
            proj = k
        elif b.startswith("screenshot"):
            shots.append(k)
    ordered = ([proj] if proj else []) + [s for s in shots if s != proj]
    if not ordered:
        raise ValueError("V .pvprj nie su moduly ani PV*SOL vizualizacie")
    imgs = []
    for k in ordered:
        data = z.read(names[k])
        mime = "png" if data[:4] == b"\x89PNG" else "jpeg"
        imgs.append({"src": "data:image/%s;base64,%s" % (mime, base64.b64encode(data).decode()),
                     "label": (k.rsplit("-", 1)[-1].rsplit(".", 1)[0] if "-" in k else "Pohlad")})
    html = _GALLERY.replace("__IMGS__", json.dumps(imgs)).replace("__TITLE__", (title or "")[:60])
    render = z.read(names[proj]) if proj else z.read(names[ordered[0]])
    return {"html": html, "render": render, "n_tables": len(imgs), "n_modules": 0,
            "has_satellite": True, "calib": {"gallery": len(imgs)}}


def _plan_modules(z, names):
    """Presne moduly z Uebersichtsplan: zoznam [(4x(x,y)) plan-metre]."""
    real = names.get("Visu3D/Uebersichtsplan.xml")
    if not real:
        return []
    root = ET.fromstring(z.read(real))
    lays = {l.findtext("Id"): l.findtext("Name") for l in root.findall("Layers")}
    out = []
    for el in root.findall("Elements"):
        if lays.get(el.findtext("LayerId")) != "MODULES":
            continue
        pos = el.find("Position")
        px = float(pos.findtext("X") or 0) if pos is not None else 0.0
        py = float(pos.findtext("Y") or 0) if pos is not None else 0.0
        poly = el.find(".//Polygon")
        if poly is None:
            continue
        pts = [(float(v.findtext("X")) + px, float(v.findtext("Y")) + py)
               for v in poly.findall("Vectors") if v.findtext("X") is not None]
        if len(pts) >= 4:
            out.append(pts[:4])
    return out


def build_pvprj_3d(pvprj_bytes, title="FVE projekt"):
    z = zipfile.ZipFile(io.BytesIO(pvprj_bytes))
    names = {n.replace("\\", "/"): n for n in z.namelist()}

    # SPOLAHLIVE: vzdy zobraz PV*SOL vlastny render (ProjScreenShot + pohlady).
    # Vlastny 3D rekonstrukt (budova/strecha) nie je z dat spolahlivo dosiahnutelny -> vypnuty.
    return _gallery(z, names, title)

    geo = names.get("Visu3D/GeometrischeDaten.xml")
    mapk = names.get("MapExtract.png") or names.get("MapExtract.jpg")
    mods = _plan_modules(z, names)
    if not (geo and mapk and mods):
        return _gallery(z, names, title)

    root = ET.fromstring(z.read(geo))
    # mapa: stred (PosAufBezugsFL) + rozmer (BreiteR x TiefeR)
    mapobj = None
    for o in root.iter("ZeichenObjekt"):
        sd = o.find("StandardDaten")
        if sd is not None and sd.findtext("AnwObjTyp") == "65":
            mapobj = o
            break
    if mapobj is None:
        return _gallery(z, names, title)
    msd = mapobj.find("StandardDaten")
    mcx = _ff(msd, "PosAufBezugsFL/X"); mcy = _ff(msd, "PosAufBezugsFL/Y")
    mBW = max((float(e.text) for e in mapobj.iter("BreiteR") if e.text), default=0.0)
    mTF = max((float(e.text) for e in mapobj.iter("TiefeR") if e.text), default=0.0)
    if not (mcx is not None and mcy is not None and mBW > 0 and mTF > 0):
        return _gallery(z, names, title)

    # sklon panelov (median Neigung) - potrebny aj na tvar sikmej strechy
    tilt = 12.0
    mv = names.get("Visu3D/Modul_Verschaltung.xml")
    if mv:
        neig = [math.degrees(float(x)) for x in re.findall(r"<Neigung>([^<]+)</Neigung>", z.read(mv).decode("utf-8", "ignore"))]
        if neig:
            tilt = max(0.0, min(55.0, sorted(neig)[len(neig) // 2]))
    tan_p = math.tan(math.radians(tilt))

    def _sub(a, b): return (a[0] - b[0], a[1] - b[1])
    def _dot(a, b): return a[0] * b[0] + a[1] * b[1]
    def _ln(a): return math.hypot(a[0], a[1])

    # budovy: typ -> tvar strechy (sikma sedlova vs plocha); kazda nad svoje panely
    PITCHED = {"14", "15", "16", "17", "18", "77"}
    maz_b = _ff(msd, "Rotation/AzimutWinkel") or 180.0
    bld_tris = []           # trojuholniky budov (steny + strecha), [x,y,z,...]
    panel_roof = [None] * len(mods)   # per modul: (pitched, H, c0, bhat, Lb)
    mc_plan = [(sum(pp[0] for pp in q[:4]) / 4.0, sum(pp[1] for pp in q[:4]) / 4.0) for q in mods]
    blds = []
    for o in root.iter("ZeichenObjekt"):
        sd = o.find("StandardDaten")
        if sd is None or "udov" not in (sd.findtext("Bezeichnung") or ""):
            continue
        bw = max((float(e.text) for e in o.iter("BreiteR") if e.text), default=0.0)
        bd = max((float(e.text) for e in o.iter("TiefeR") if e.text), default=0.0)
        blds.append((bw * bd, o))
    blds.sort(reverse=True, key=lambda x: x[0])

    def tri(a, b, c):
        bld_tris.extend([a[0], a[1], a[2], b[0], b[1], b[2], c[0], c[1], c[2]])

    for _area, o in blds:
        et = o.find(".//Etage")
        eb = et.find(".//Ebene1") if et is not None else None
        esd = et.find("StandardDaten") if et is not None else None
        if esd is None or eb is None:
            continue
        roof_typ = o.find("StandardDaten").findtext("AnwObjTyp")
        pitched = roof_typ in PITCHED
        bfx = _ff(esd, "PosAufBezugsFL/X"); bfy = _ff(esd, "PosAufBezugsFL/Y")
        baz = _ff(esd, "Rotation/AzimutWinkel") or 0.0
        bl = _ff(eb, "BreiteL") or 0.0; tl = _ff(eb, "TiefeL") or 0.0
        brr = _ff(eb, "BreiteR") or 0.0; tr2 = _ff(eb, "TiefeR") or 0.0
        hh = [float(e.text) for e in o.iter("Hoehe") if e.text]
        H = max([h for h in hh if 2.0 <= h <= 40.0] or [6.0])
        if bfx is None or brr <= 0 or tr2 <= 0:
            continue
        best = None
        for mode in (-(baz - maz_b), 0.0, (baz - maz_b)):
            pa = math.radians(mode); wv = (math.cos(pa), math.sin(pa)); dv = (-math.sin(pa), math.cos(pa))
            inside = []
            for idx, (mx, my) in enumerate(mc_plan):
                if panel_roof[idx] is not None:
                    continue
                rx = mx - bfx; ry = my - bfy
                u = rx * wv[0] + ry * wv[1]; v = rx * dv[0] + ry * dv[1]
                if -bl - 2 <= u <= brr + 2 and -tl - 2 <= v <= tr2 + 2:
                    inside.append(idx)
            if best is None or len(inside) > len(best[0]):
                best = (inside, wv, dv)
        inside, wv, dv = best
        if not inside:
            continue
        # rohy podorysu v scene
        C = []
        for (ui, vj) in [(-bl, -tl), (brr, -tl), (brr, tr2), (-bl, tr2)]:
            C.append((bfx + ui * wv[0] + vj * dv[0] - mcx, bfy + ui * wv[1] + vj * dv[1] - mcy))
        # kratsia os (b) -> hrebenovanie sedlovej strechy
        e1 = _sub(C[1], C[0]); e2 = _sub(C[2], C[1])
        L1 = _ln(e1); L2 = _ln(e2)
        if L1 <= L2:
            bvec = e1; Lb = L1
        else:
            bvec = e2; Lb = L2
        bhat = (bvec[0] / (Lb or 1), bvec[1] / (Lb or 1))
        rise = (Lb / 2.0) * tan_p if pitched else 0.0
        for idx in inside:
            panel_roof[idx] = (pitched, H, C[0], bhat, Lb, rise)
        # geometria budovy
        def P3(c, y): return (c[0], y, c[1])
        # steny po eave (H)
        for i in range(4):
            a = C[i]; b = C[(i + 1) % 4]
            tri(P3(a, 0), P3(b, 0), P3(b, H)); tri(P3(a, 0), P3(b, H), P3(a, H))
        if not pitched:
            tri(P3(C[0], H), P3(C[1], H), P3(C[2], H)); tri(P3(C[0], H), P3(C[2], H), P3(C[3], H))
        else:
            # hrebenove body = stredy kratsich hran, zdvihnute
            def rh(p): return H + (Lb / 2.0 - abs(_dot(_sub(p, C[0]), bhat) - Lb / 2.0)) * tan_p
            # kratsie hrany su tie kolme na long; najdi 2 hrany s dlzkou Lb
            edges = [(C[0], C[1]), (C[1], C[2]), (C[2], C[3]), (C[3], C[0])]
            sh = [eg for eg in edges if abs(_ln(_sub(eg[1], eg[0])) - Lb) < 0.5]
            longs = [eg for eg in edges if abs(_ln(_sub(eg[1], eg[0])) - Lb) >= 0.5]
            if len(sh) >= 2 and len(longs) >= 2:
                rA = (((sh[0][0][0] + sh[0][1][0]) / 2.0, (sh[0][0][1] + sh[0][1][1]) / 2.0))
                rB = (((sh[1][0][0] + sh[1][1][0]) / 2.0, (sh[1][0][1] + sh[1][1][1]) / 2.0))
                top = H + rise
                # dve sklonene roviny (od kazdej dlhej hrany k hrebenu)
                for (ea, eb2) in longs:
                    tri(P3(ea, H), P3(eb2, H), P3(rB, top)); tri(P3(ea, H), P3(rB, top), P3(rA, top))
                # stitove trojuholniky na kratsich hranach
                for (ea, eb2) in sh:
                    mid = ((ea[0] + eb2[0]) / 2.0, (ea[1] + eb2[1]) / 2.0)
                    tri(P3(ea, H), P3(eb2, H), P3(mid, top))
            else:
                tri(P3(C[0], H), P3(C[1], H), P3(C[2], H)); tri(P3(C[0], H), P3(C[2], H), P3(C[3], H))

    # panely: ploche plne obdlzniky (presny layout z planu), mierne nad satelitom - CISTE, bez zubkovania
    verts = []
    for q in mods:
        P = [(x - mcx, 0.35, y - mcy) for (x, y) in q]
        for i in (0, 1, 2, 0, 2, 3):
            verts.extend(P[i])

    n_modules = len(mods)
    sat_b64 = base64.b64encode(z.read(mapk)).decode()
    sat_mime = "png" if z.read(mapk)[:4] == b"\x89PNG" else "jpeg"
    subt = "Interaktivny 3D - %d modulov" % n_modules

    html = (_TEMPLATE
            .replace("__BLDVERTS__", "[]")
            .replace("__VERTS__", json.dumps([round(v, 2) for v in verts]))
            .replace("__MBW__", str(round(mBW, 2)))
            .replace("__MTF__", str(round(mTF, 2)))
            .replace("__SATB64__", sat_b64)
            .replace("__SATMIME__", sat_mime)
            .replace("__SUBT__", subt)
            .replace("__TITLE__", (title or "").replace("<", "").replace(">", "")))

    render = z.read(names["Visu3D/ProjScreenShot.jpg"]) if "Visu3D/ProjScreenShot.jpg" in names else z.read(mapk)
    return {"html": html, "render": render, "n_tables": n_modules, "n_modules": n_modules,
            "has_satellite": True, "calib": {"modules": n_modules, "tilt": tilt}}


_TEMPLATE = r'''<!DOCTYPE html><html lang="sk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Energovision - 3D model FVE</title>
<style>*{margin:0;box-sizing:border-box}html,body{height:100%;overflow:hidden;font-family:-apple-system,Segoe UI,Arial,sans-serif}
#c{width:100%;height:100%;display:block;background:#aac4e0}
.ui{position:absolute;top:14px;left:14px;background:#fff;border-radius:12px;padding:12px 16px;box-shadow:0 8px 28px rgba(2,6,23,.16);max-width:300px}
.ui .b{font-weight:800;font-size:15px}.ui .b span{color:#92D050}.ui .m{font-size:13px;color:#334155;margin-top:4px}
#shot{position:absolute;top:14px;right:14px;background:#92D050;color:#0F172A;font-weight:700;border:none;border-radius:10px;padding:11px 16px;cursor:pointer;font-size:14px}
.hint{position:absolute;bottom:14px;left:50%;transform:translateX(-50%);background:rgba(15,23,42,.82);color:#fff;font-size:12px;padding:8px 14px;border-radius:999px}</style></head><body>
<canvas id="c"></canvas>
<div class="ui"><div class="b">Energovision <span>FVE</span></div><div class="m">__SUBT__<br>__TITLE__</div></div>
<button id="shot">Stiahnut snimku</button>
<div class="hint">tahaj = otoc, prave tlacidlo = posun, koliesko = zoom</div>
<script type="importmap">{"imports":{"three":"https://unpkg.com/three@0.160.0/build/three.module.js","three/addons/":"https://unpkg.com/three@0.160.0/examples/jsm/"}}</script>
<script type="module">
import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
const VERTS=__VERTS__, MBW=__MBW__, MTF=__MTF__, BLDVERTS=__BLDVERTS__;
const cv=document.getElementById('c');
const renderer=new THREE.WebGLRenderer({canvas:cv,antialias:true,preserveDrawingBuffer:true});
renderer.setPixelRatio(Math.min(devicePixelRatio,2));renderer.setSize(innerWidth,innerHeight);
const scene=new THREE.Scene();scene.background=new THREE.Color(0xaac4e0);
const cam=new THREE.PerspectiveCamera(45,innerWidth/innerHeight,0.5,8000);
const ctrl=new OrbitControls(cam,renderer.domElement);ctrl.enableDamping=true;ctrl.maxPolarAngle=Math.PI/2.05;
scene.add(new THREE.HemisphereLight(0xffffff,0x8a9bb0,1.05));
const sun=new THREE.DirectionalLight(0xfff3e0,1.0);sun.position.set(80,160,40);scene.add(sun);
// satelit ako podklad (XZ rovina): plan_x-mcx -> X, plan_y-mcy -> Z
const tex=new THREE.TextureLoader().load('data:image/__SATMIME__;base64,__SATB64__');tex.colorSpace=THREE.SRGBColorSpace;
const gmat=new THREE.MeshBasicMaterial({map:tex});
const gp=new THREE.Mesh(new THREE.PlaneGeometry(MBW,MTF),gmat);
gp.rotation.x=-Math.PI/2;            // do XZ
// UV: PlaneGeometry default ma (0,0) vlavo-dole; chceme plan_y -> v. Po rotacii X o -90 sa Z mapuje. Doladime cez flip.
gp.position.set(0,0,0);scene.add(gp);
// moduly (jedna geometria)
const g=new THREE.BufferGeometry();
g.setAttribute('position',new THREE.BufferAttribute(new Float32Array(VERTS),3));
g.computeVertexNormals();
const pmat=new THREE.MeshStandardMaterial({color:0x16243f,metalness:.5,roughness:.32,emissive:0x0a1430,emissiveIntensity:.16,side:THREE.DoubleSide});
const panels=new THREE.Mesh(g,pmat);panels.renderOrder=2;scene.add(panels);
// budova: kvader (steny + strecha) z footprintu
if(BLDVERTS&&BLDVERTS.length){
  const bg=new THREE.BufferGeometry();bg.setAttribute('position',new THREE.BufferAttribute(new Float32Array(BLDVERTS),3));bg.computeVertexNormals();
  const wallMat=new THREE.MeshStandardMaterial({color:0xeef0f2,roughness:.82,metalness:0,side:THREE.DoubleSide});
  scene.add(new THREE.Mesh(bg,wallMat));
}
// ramuj na moduly
// ramuj CELU scenu (budovy + panely) z vtacieho oblique pohladu
let _xn=1e9,_xx=-1e9,_zn=1e9,_zx=-1e9,_yx=1;
for(let i=0;i<VERTS.length;i+=3){_xn=Math.min(_xn,VERTS[i]);_xx=Math.max(_xx,VERTS[i]);_zn=Math.min(_zn,VERTS[i+2]);_zx=Math.max(_zx,VERTS[i+2]);_yx=Math.max(_yx,VERTS[i+1]);}
for(let i=0;i<BLDVERTS.length;i+=3){_xn=Math.min(_xn,BLDVERTS[i]);_xx=Math.max(_xx,BLDVERTS[i]);_zn=Math.min(_zn,BLDVERTS[i+2]);_zx=Math.max(_zx,BLDVERTS[i+2]);_yx=Math.max(_yx,BLDVERTS[i+1]);}
const _cx=(_xn+_xx)/2,_cz=(_zn+_zx)/2,_span=Math.max(_xx-_xn,_zx-_zn,12);
ctrl.target.set(_cx,_yx*0.35,_cz);
cam.position.set(_cx+_span*0.12,_yx+_span*1.05,_cz+_span*1.25);ctrl.update();
addEventListener('resize',()=>{cam.aspect=innerWidth/innerHeight;cam.updateProjectionMatrix();renderer.setSize(innerWidth,innerHeight);});
document.getElementById('shot').onclick=()=>{renderer.render(scene,cam);const a=document.createElement('a');a.download='FVE_3D_'+Date.now()+'.png';a.href=cv.toDataURL('image/png');a.click();};
(function loop(){ctrl.update();renderer.render(scene,cam);requestAnimationFrame(loop);})();
</script></body></html>'''


_GALLERY = r'''<!DOCTYPE html><html lang="sk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Energovision FVE</title><style>*{margin:0;box-sizing:border-box}body{font-family:-apple-system,Arial,sans-serif;background:#0f172a;color:#e2e8f0;padding:16px}
.b{font-weight:800;font-size:18px}.b span{color:#92D050}img{max-width:100%;border-radius:12px;margin-top:12px}
.t{display:flex;gap:8px;overflow:auto;margin-top:10px}.t img{width:120px;height:74px;object-fit:cover;cursor:pointer}</style></head><body>
<div class="b">Energovision <span>FVE</span></div><div id="cap" style="font-size:12px;color:#94a3b8"></div>
<img id="m" src=""><div class="t" id="t"></div>
<script>const I=__IMGS__;const m=document.getElementById('m'),t=document.getElementById('t'),cap=document.getElementById('cap');
function s(i){m.src=I[i].src;cap.textContent=I[i].label;}I.forEach((im,i)=>{const e=document.createElement('img');e.src=im.src;e.onclick=()=>s(i);t.appendChild(e);});s(0);
</script></body></html>'''
