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

    # budovy: VSETKY budovy, kazda box (Etage extenty + rotacia) nad svoje panely
    maz_b = _ff(msd, "Rotation/AzimutWinkel") or 180.0
    buildings = []
    panel_h = [0.0] * len(mods)
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
    for _area, o in blds:
        et = o.find(".//Etage")
        eb = et.find(".//Ebene1") if et is not None else None
        esd = et.find("StandardDaten") if et is not None else None
        if esd is None or eb is None:
            continue
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
                if panel_h[idx] > 0:
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
        for idx in inside:
            panel_h[idx] = H
        corn = []
        for (ui, vj) in [(-bl, -tl), (brr, -tl), (brr, tr2), (-bl, tr2)]:
            cx = bfx + ui * wv[0] + vj * dv[0] - mcx
            cy = bfy + ui * wv[1] + vj * dv[1] - mcy
            corn.append((cx, cy))
        buildings.append({"corners": [[round(c[0], 2), round(c[1], 2)] for c in corn], "h": round(H, 2)})

    # sklon (median Neigung z Modul_Verschaltung)
    tilt = 12.0
    mv = names.get("Visu3D/Modul_Verschaltung.xml")
    if mv:
        neig = [math.degrees(float(x)) for x in re.findall(r"<Neigung>([^<]+)</Neigung>", z.read(mv).decode("utf-8", "ignore"))]
        if neig:
            tilt = max(0.0, min(55.0, sorted(neig)[len(neig) // 2]))

    # 3D vrcholy modulov: scene (X = plan_x - mcx, Z = plan_y - mcy), sklon okolo dlhsej hrany
    tr = math.radians(tilt)
    verts = []
    for _qi, q in enumerate(mods):
        P = [(x - mcx, y - mcy) for (x, y) in q]  # scene XZ
        ea = (P[1][0] - P[0][0], P[1][1] - P[0][1])
        eb = (P[3][0] - P[0][0], P[3][1] - P[0][1])
        la = math.hypot(*ea); lb = math.hypot(*eb)
        depth = min(la, lb)
        h = depth * math.sin(tr)
        # vyssia strana = tie 2 rohy, ktore su na konci kratsej hrany
        if lb <= la:
            ys = [0.0, 0.0, h, h]  # P0,P1 dole; P2,P3 hore (P3 = P0+eb)
        else:
            ys = [0.0, h, h, 0.0]  # P1,P2 hore
        ph = panel_h[_qi]
        c = [(P[i][0], ys[i] + ph + 0.25, P[i][1]) for i in range(4)]
        # 2 trojuholniky: 0,1,2 a 0,2,3
        for i in (0, 1, 2, 0, 2, 3):
            verts.extend(c[i])

    n_modules = len(mods)
    sat_b64 = base64.b64encode(z.read(mapk)).decode()
    sat_mime = "png" if z.read(mapk)[:4] == b"\x89PNG" else "jpeg"
    subt = "Interaktivny 3D - %d modulov" % n_modules

    html = (_TEMPLATE
            .replace("__BUILDINGS__", json.dumps(buildings))
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
const VERTS=__VERTS__, MBW=__MBW__, MTF=__MTF__, BUILDINGS=__BUILDINGS__;
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
if(BUILDINGS&&BUILDINGS.length){
  const bv=[];
  function quad(a,b,c,d){bv.push(a[0],a[1],a[2], b[0],b[1],b[2], c[0],c[1],c[2], a[0],a[1],a[2], c[0],c[1],c[2], d[0],d[1],d[2]);}
  BUILDINGS.forEach(B=>{
    const C=B.corners, H=B.h;
    for(let i=0;i<4;i++){const p=C[i],q=C[(i+1)%4];quad([p[0],0,p[1]],[q[0],0,q[1]],[q[0],H,q[1]],[p[0],H,p[1]]);}
    quad([C[0][0],H,C[0][1]],[C[1][0],H,C[1][1]],[C[2][0],H,C[2][1]],[C[3][0],H,C[3][1]]);
  });
  const bg=new THREE.BufferGeometry();bg.setAttribute('position',new THREE.BufferAttribute(new Float32Array(bv),3));bg.computeVertexNormals();
  const wallMat=new THREE.MeshStandardMaterial({color:0xeef0f2,roughness:.85,metalness:0,side:THREE.DoubleSide});
  scene.add(new THREE.Mesh(bg,wallMat));
}
// ramuj na moduly
g.computeBoundingBox();const bb=g.boundingBox;const ctr=new THREE.Vector3();bb.getCenter(ctr);
const sz=new THREE.Vector3();bb.getSize(sz);const ext=Math.max(sz.x,sz.z,8);
ctrl.target.copy(ctr);
cam.position.set(ctr.x+ext*0.3,ctr.y+ext*1.1,ctr.z+ext*1.5);ctrl.update();
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
