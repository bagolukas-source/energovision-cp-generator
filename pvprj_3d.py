"""PV*SOL .pvprj -> branded HTML galeria PV*SOL vlastnych renderov.
Spolahlive: zobrazi ProjScreenShot.jpg (hlavny render) + dostupne pohlady (Screenshot-*),
bez rekonstrukcie geometrie. Funguje pre ~99 % .pvprj (tie co maju render).
"""
import zipfile, io, base64, json, re
import xml.etree.ElementTree as ET


def _label(fname):
    n = fname.rsplit("/", 1)[-1]
    n = re.sub(r"\.(jpg|jpeg|png)$", "", n, flags=re.I)
    if n.lower().startswith("projscreenshot"):
        return "Celkovy pohlad"
    parts = n.split("-")
    if len(parts) >= 2:
        return parts[-1].strip()
    return n


def _count_modules(z, names):
    try:
        real = names.get("Visu3D/Uebersichtsplan.xml")
        if not real:
            return 0
        root = ET.fromstring(z.read(real))
        lays = {l.findtext("Id"): l.findtext("Name") for l in root.findall("Layers")}
        return sum(1 for el in root.findall("Elements") if lays.get(el.findtext("LayerId")) == "MODULES")
    except Exception:
        return 0


_TEMPLATE = r'''<!DOCTYPE html><html lang="sk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Energovision - vizualizacia FVE</title>
<style>
*{margin:0;box-sizing:border-box}
html,body{height:100%;font-family:-apple-system,Segoe UI,Arial,sans-serif;background:#0f172a;color:#e2e8f0}
.wrap{max-width:1100px;margin:0 auto;padding:18px}
.hdr{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}
.hdr .b{font-weight:800;font-size:18px}.hdr .b span{color:#92D050}
.hdr .s{font-size:12px;color:#94a3b8;margin-top:2px}
#dl{background:#92D050;color:#0F172A;font-weight:700;border:none;border-radius:10px;padding:11px 16px;cursor:pointer;font-size:14px;text-decoration:none}
.stage{background:#020617;border-radius:14px;overflow:hidden;box-shadow:0 10px 40px rgba(0,0,0,.4);position:relative}
.stage img{width:100%;display:block;max-height:74vh;object-fit:contain;background:#020617}
.cap{position:absolute;left:14px;bottom:12px;background:rgba(2,6,23,.72);padding:7px 13px;border-radius:999px;font-size:13px}
.thumbs{display:flex;gap:10px;overflow-x:auto;padding:14px 2px 4px}
.thumbs button{flex:0 0 auto;border:2px solid transparent;border-radius:10px;overflow:hidden;cursor:pointer;background:none;padding:0;width:138px}
.thumbs button.active{border-color:#92D050}
.thumbs img{width:138px;height:84px;object-fit:cover;display:block}
.thumbs .tl{font-size:11px;color:#cbd5e1;padding:5px 6px;text-align:center;background:#1e293b;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.note{font-size:12px;color:#64748b;margin-top:12px;text-align:center}
</style></head><body>
<div class="wrap">
  <div class="hdr">
    <div><div class="b">Energovision <span>FVE</span></div><div class="s">__SUBT__</div></div>
    <a id="dl" download="FVE_vizualizacia.jpg">Stiahnut snimku</a>
  </div>
  <div class="stage"><img id="main" src=""><div class="cap" id="cap"></div></div>
  <div class="thumbs" id="thumbs"></div>
  <div class="note">Vizualizacia z PV*SOL - __TITLE__</div>
</div>
<script>
const IMGS=__IMGS__;
const main=document.getElementById('main'),cap=document.getElementById('cap'),dl=document.getElementById('dl'),tw=document.getElementById('thumbs');
function show(i){
  main.src=IMGS[i].src; cap.textContent=IMGS[i].label; dl.href=IMGS[i].src;
  dl.download='FVE_'+(IMGS[i].label.replace(/[^A-Za-z0-9]+/g,'_')||'snimka')+'.jpg';
  [...tw.children].forEach((b,k)=>b.classList.toggle('active',k===i));
}
IMGS.forEach((im,i)=>{
  const b=document.createElement('button');
  b.innerHTML='<img src="'+im.src+'"><div class="tl">'+im.label+'</div>';
  b.onclick=()=>show(i); tw.appendChild(b);
});
show(0);
</script>
</body></html>'''


def build_pvprj_3d(pvprj_bytes, title="FVE projekt"):
    z = zipfile.ZipFile(io.BytesIO(pvprj_bytes))
    names = {n.replace("\\", "/"): n for n in z.namelist()}

    shots = []
    proj = None
    for k in sorted(names):
        base = k.rsplit("/", 1)[-1].lower()
        if not base.endswith((".jpg", ".jpeg", ".png")):
            continue
        if base.startswith("projscreenshot"):
            proj = k
        elif base.startswith("screenshot"):
            shots.append(k)
    ordered = ([proj] if proj else []) + [s for s in shots if s != proj]
    if not ordered:
        raise ValueError("V .pvprj nie su PV*SOL vizualizacie (ProjScreenShot/Screenshot)")

    imgs = []
    for k in ordered:
        data = z.read(names[k])
        mime = "png" if data[:4] == b"\x89PNG" else "jpeg"
        imgs.append({"src": "data:image/%s;base64,%s" % (mime, base64.b64encode(data).decode()),
                     "label": _label(k)})

    n_modules = _count_modules(z, names)
    subt = ("Vizualizacia FVE - %d modulov" % n_modules) if n_modules else "Vizualizacia fotovoltickej elektrarne"

    html = (_TEMPLATE
            .replace("__IMGS__", json.dumps(imgs))
            .replace("__SUBT__", subt)
            .replace("__TITLE__", (title or "").replace("<", "").replace(">", "")))

    render = z.read(names[proj]) if proj else z.read(names[ordered[0]])
    return {"html": html, "render": render, "n_tables": len(imgs), "n_modules": n_modules,
            "has_satellite": True, "calib": {"images": len(imgs)}}
