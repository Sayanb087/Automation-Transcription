

import json, sys, tempfile, os, threading, time
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = 5050

# Check SDK installed 
try:
    from sarvamai import SarvamAI
except ImportError:
    print(""" sarvamai SDK not found.""")
    sys.exit(1)

# Google Drive helpers 
import re as _re

_GDRIVE_FILE_RE = _re.compile(r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)")
_GDRIVE_ID_RE   = _re.compile(r"id=([a-zA-Z0-9_-]+)")

def _is_drive_url(s):
    return isinstance(s, str) and s.startswith("http") and "drive.google.com" in s

def _extract_drive_id(url):
    m = _GDRIVE_FILE_RE.search(url)
    if m: return m.group(1)
    m = _GDRIVE_ID_RE.search(url)
    if m: return m.group(1)
    return None

def _download_from_drive(url):
    """
    Download a public Google Drive audio file.
    Supports: /open?id=, /file/d/, /uc?id=

    Strategy order:
      1. gdown (id= form — works on all gdown versions)
      2. requests: confirm=t static bypass → cookie-based bypass → token-parse bypass
      3. urllib + CookieJar
    """
    file_id = _extract_drive_id(url)
    if not file_id:
        return None, None, "Could not extract file ID from the Drive URL."

    base_dl     = f"https://drive.google.com/uc?export=download&id={file_id}"
    confirm_url = base_dl + "&confirm=t"
    hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    def _extract_token(html):
        for pat in [
            r'confirm=([0-9A-Za-z_\-]{6,})',   # must be at least 6 chars (not just "t")
            r'"confirm","([^"]+)"',
            r'uuid=([0-9A-Za-z_\-]+)',
            r'&amp;confirm=([0-9A-Za-z_\-]{6,})',
        ]:
            m = _re.search(pat, html)
            if m:
                return m.group(1)
        return None

    def _filename_from_cd(cd, fallback):
        m = _re.search(r'filename[^=]*=([^;\n]+)', cd)
        if m:
            name = m.group(1).strip().strip('"').strip("'")
            name = _re.sub(r"^UTF-8'[^']*'", '', name)
            return name.strip() or fallback
        return fallback

    # ── Strategy 1: gdown (id= form, works on all versions) ──────────────────
    try:
        import gdown, tempfile as _tf, os as _os
        # Download into a temp DIRECTORY so gdown preserves the original filename
        tmpdir = _tf.mkdtemp()
        out = gdown.download(id=file_id, output=tmpdir + _os.sep, quiet=False)
        if out and _os.path.exists(out) and _os.path.getsize(out) > 1024:
            filename = _os.path.basename(out)          # e.g. "AUDIO 2.wav"
            with open(out, "rb") as f:
                raw = f.read()
            try:
                _os.unlink(out)
                _os.rmdir(tmpdir)
            except Exception:
                pass
            print(f"  gdown OK: {filename} ({len(raw)/1024/1024:.1f} MB)")
            return raw, filename, None
        print(f"  gdown returned empty/missing file, trying requests…")
    except ImportError:
        print("  gdown not installed (pip install gdown), trying requests…")
    except Exception as ex:
        print(f"  gdown failed ({ex}), trying requests…")

    # ── Strategy 2: requests — three-pass bypass ──────────────────────────────
    try:
        import requests

        session = requests.Session()
        session.headers.update(hdrs)

        def _do_request(dl_url):
            resp = session.get(dl_url, timeout=180, stream=True, allow_redirects=True)
            ct   = resp.headers.get("Content-Type", "")
            cd   = resp.headers.get("Content-Disposition", "")
            return resp, ct, cd

        # Pass A: confirm=t static bypass
        resp, ct, cd = _do_request(confirm_url)

        if "text/html" not in ct:
            raw = resp.content
            return raw, _filename_from_cd(cd, f"drive_{file_id[:10]}.wav"), None

        html = resp.text

        # Pass B: cookie-based (download_warning cookie set by Drive)
        token = None
        for ck in session.cookies:
            if "download_warning" in ck.name:
                token = ck.value
                break

        # Pass C: parse HTML for token
        if not token:
            token = _extract_token(html)

        if token:
            bypass_url    = base_dl + f"&confirm={token}"
            resp, ct, cd  = _do_request(bypass_url)
            if "text/html" not in ct:
                raw = resp.content
                return raw, _filename_from_cd(cd, f"drive_{file_id[:10]}.wav"), None

        # Pass D: try /uc?id= with &confirm=t (different endpoint)
        alt_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
        resp, ct, cd = _do_request(alt_url)
        if "text/html" not in ct and len(resp.content) > 1024:
            raw = resp.content
            print(f"  requests (usercontent) OK ({len(raw)/1024/1024:.1f} MB)")
            return raw, _filename_from_cd(cd, f"drive_{file_id[:10]}.wav"), None

        raise RuntimeError(
            "All request passes returned HTML. "
            "Make sure the file is shared as 'Anyone with the link can view'."
        )

    except ImportError:
        print("  requests not installed, trying urllib…")
    except Exception as ex:
        print(f"  requests failed ({ex}), trying urllib…")

    # ── Strategy 3: urllib + CookieJar ────────────────────────────────────────
    try:
        import urllib.request, http.cookiejar
        jar    = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

        # Try drive.usercontent.google.com first (newer, more permissive)
        for dl_url in [
            f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t",
            confirm_url,
        ]:
            req = urllib.request.Request(dl_url, headers=hdrs)
            with opener.open(req, timeout=180) as r:
                ct  = r.headers.get("Content-Type", "")
                cd  = r.headers.get("Content-Disposition", "")
                raw = r.read()

            if "text/html" not in ct and len(raw) > 1024:
                filename = _filename_from_cd(cd, f"drive_{file_id[:10]}.wav")
                print(f"  urllib OK: {filename} ({len(raw)/1024/1024:.1f} MB)")
                return raw, filename, None

            # Try cookie token from this response
            html  = raw.decode("utf-8", errors="ignore") if "text/html" in ct else ""
            token = _extract_token(html) if html else None
            if not token:
                for ck in jar:
                    if "download_warning" in ck.name:
                        token = ck.value
                        break
            if token:
                bypass = base_dl + f"&confirm={token}"
                req2   = urllib.request.Request(bypass, headers=hdrs)
                with opener.open(req2, timeout=180) as r2:
                    cd  = r2.headers.get("Content-Disposition", "")
                    raw = r2.read()
                if len(raw) > 1024:
                    filename = _filename_from_cd(cd, f"drive_{file_id[:10]}.wav")
                    print(f"  urllib+token OK: {filename} ({len(raw)/1024/1024:.1f} MB)")
                    return raw, filename, None

        return None, None, (
            "All download strategies failed. "
            "Check that the file is shared as 'Anyone with the link can view' "
            "and try installing gdown:  pip install gdown"
        )

    except Exception as ex:
        return None, None, f"All strategies failed. Last error: {ex}"



jobs = {}
jobs_lock = threading.Lock()

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Saaras Transcribe</title>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=Fira+Code:wght@300;400;500&display=swap" rel="stylesheet">
<style>
:root{--bg:#06080f;--s1:#0d1117;--s2:#131a24;--border:#1e2d42;--border2:#2a3d55;--saffron:#ff9933;--sdim:rgba(255,153,51,.12);--sglow:rgba(255,153,51,.25);--glit:#1db510;--gdim:rgba(19,136,8,.15);--blue:#4a9eff;--bdim:rgba(74,158,255,.12);--white:#f8f8f8;--muted:#5a6a80;--text:#d0dae8;--ff:'Plus Jakarta Sans',sans-serif;--fm:'Fira Code',monospace}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:var(--ff);min-height:100vh}
body::before{content:'';position:fixed;top:-200px;right:-200px;width:600px;height:600px;background:radial-gradient(circle,rgba(255,153,51,.04) 0%,transparent 65%);pointer-events:none;z-index:0}
body::after{content:'';position:fixed;bottom:-150px;left:-150px;width:500px;height:500px;background:radial-gradient(circle,rgba(19,136,8,.05) 0%,transparent 65%);pointer-events:none;z-index:0}
.wrap{max-width:820px;margin:0 auto;padding:40px 20px 80px;position:relative;z-index:1}
.header{display:flex;align-items:center;justify-content:space-between;margin-bottom:40px;flex-wrap:wrap;gap:12px}
.brand{display:flex;align-items:center;gap:14px}
.brand-mark{width:44px;height:44px;border-radius:12px;background:linear-gradient(135deg,var(--saffron) 0%,#ff6a00 100%);display:flex;align-items:center;justify-content:center;font-size:20px;box-shadow:0 0 24px var(--sglow)}
.brand-name{font-size:20px;font-weight:800;letter-spacing:-.3px;color:var(--white)}
.brand-sub{font-size:11px;color:var(--muted);font-family:var(--fm);margin-top:3px}
.tricolor-bar{height:3px;width:120px;border-radius:2px;background:linear-gradient(90deg,var(--saffron) 33%,white 33% 66%,var(--glit) 66%);opacity:.7}
.card{background:var(--s1);border:1px solid var(--border);border-radius:16px;padding:24px;margin-bottom:16px}
.card-title{font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-family:var(--fm);margin-bottom:16px;display:flex;align-items:center;gap:8px}
.card-title::before{content:'';display:block;width:3px;height:12px;background:var(--saffron);border-radius:2px}
.key-row{display:flex;gap:10px}
.key-input{flex:1;background:var(--s2);border:1px solid var(--border);border-radius:10px;padding:11px 14px;color:var(--white);font-family:var(--fm);font-size:13px;outline:none;transition:border-color .2s}
.key-input:focus{border-color:var(--saffron)}
.key-input::placeholder{color:var(--muted)}
.btn-eye{background:var(--s2);border:1px solid var(--border);border-radius:10px;padding:0 14px;color:var(--muted);cursor:pointer;font-size:15px;transition:all .2s}
.btn-eye:hover{border-color:var(--border2);color:var(--text)}
.lang-grid{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:6px}
.lang-chip{padding:6px 12px;border-radius:100px;font-size:12px;font-weight:500;cursor:pointer;border:1px solid var(--border);background:var(--s2);color:var(--muted);transition:all .15s;white-space:nowrap}
.lang-chip:hover{border-color:var(--saffron);color:var(--text)}
.lang-chip.active{background:var(--sdim);border-color:var(--saffron);color:var(--saffron);font-weight:600}
.lang-chip.auto.active{background:var(--bdim);border-color:var(--blue);color:var(--blue)}
.dropzone{background:var(--s2);border:1.5px dashed var(--border2);border-radius:14px;padding:36px 24px;text-align:center;cursor:pointer;transition:all .25s;margin-bottom:14px}
.dropzone:hover,.dropzone.drag{border-color:var(--saffron);background:var(--sdim)}
.dz-icon{font-size:32px;margin-bottom:10px}
.dz-title{font-size:15px;font-weight:700;color:var(--white);margin-bottom:4px}
.dz-sub{font-size:12px;color:var(--muted);font-family:var(--fm)}
#fi{display:none}
.file-pill{display:none;align-items:center;gap:12px;background:var(--s2);border:1px solid var(--border);border-radius:12px;padding:12px 16px;margin-bottom:14px}
.file-pill.show{display:flex}
.fpi{font-size:22px}
.fpin{flex:1}
.fpn{font-size:14px;font-weight:600;color:var(--white)}
.fpm{font-size:11px;color:var(--muted);font-family:var(--fm);margin-top:2px}
.fpr{background:none;border:none;color:var(--muted);cursor:pointer;font-size:16px;padding:4px;transition:color .2s}
.fpr:hover{color:#ff6060}
.mode-row{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:14px}
.mode-btn{padding:9px 6px;border-radius:10px;background:var(--s2);border:1px solid var(--border);color:var(--muted);font-family:var(--fm);font-size:11px;font-weight:500;cursor:pointer;text-align:center;transition:all .15s}
.mode-btn:hover{border-color:var(--border2);color:var(--text)}
.mode-btn.active{background:var(--sdim);border-color:var(--saffron);color:var(--saffron);font-weight:600}
.opts-row{display:flex;gap:10px;margin-bottom:20px;flex-wrap:wrap}
.opt-toggle{display:flex;align-items:center;gap:8px;padding:8px 14px;border-radius:10px;border:1px solid var(--border);background:var(--s2);cursor:pointer;transition:all .15s;user-select:none}
.opt-toggle:hover{border-color:var(--border2)}
.opt-toggle.active{border-color:var(--glit);background:var(--gdim)}
.tsw{width:28px;height:16px;background:var(--border2);border-radius:100px;position:relative;transition:background .2s;flex-shrink:0}
.tsw::after{content:'';position:absolute;left:2px;top:2px;width:12px;height:12px;background:white;border-radius:50%;transition:transform .2s}
.opt-toggle.active .tsw{background:var(--glit)}
.opt-toggle.active .tsw::after{transform:translateX(12px)}
.opt-label{font-size:12px;font-weight:600}
.num-row{display:flex;align-items:center;gap:10px;margin-bottom:20px}
.num-row label{font-size:12px;color:var(--muted);font-family:var(--fm)}
.num-row select{background:var(--s2);border:1px solid var(--border);border-radius:8px;padding:6px 10px;color:var(--text);font-family:var(--fm);font-size:12px;outline:none;cursor:pointer}
.btn-go{width:100%;padding:15px;border-radius:12px;border:none;background:linear-gradient(135deg,var(--saffron) 0%,#ff6a00 100%);color:white;font-family:var(--ff);font-size:15px;font-weight:700;cursor:pointer;transition:all .2s;box-shadow:0 4px 20px rgba(255,153,51,.2)}
.btn-go:hover:not(:disabled){transform:translateY(-1px);box-shadow:0 8px 28px rgba(255,153,51,.35)}
.btn-go:disabled{opacity:.4;cursor:not-allowed;transform:none;box-shadow:none}
.pc{display:none;background:var(--s1);border:1px solid var(--border);border-radius:16px;padding:24px;margin-bottom:16px}
.pc.show{display:block}
.steps{display:flex;flex-direction:column;gap:12px;margin-bottom:20px}
.step{display:flex;align-items:center;gap:12px;opacity:.35;transition:opacity .3s}
.step.active{opacity:1}.step.done{opacity:.6}
.sdot{width:28px;height:28px;border-radius:50%;border:2px solid var(--border2);display:flex;align-items:center;justify-content:center;font-size:11px;font-family:var(--fm);color:var(--muted);flex-shrink:0;transition:all .3s}
.step.active .sdot{border-color:var(--saffron);color:var(--saffron);background:var(--sdim);animation:dp 1.5s ease-in-out infinite}
.step.done .sdot{border-color:var(--glit);background:var(--gdim);color:var(--glit)}
@keyframes dp{0%,100%{box-shadow:0 0 0 0 var(--sglow)}50%{box-shadow:0 0 0 6px transparent}}
.stxt{font-size:13px;font-weight:500}
.step.active .stxt{color:var(--white)}
.pbo{background:var(--s2);border-radius:100px;height:4px;overflow:hidden}
.pbi{height:100%;background:linear-gradient(90deg,var(--saffron),#ff6a00);border-radius:100px;transition:width .5s ease;width:0}
.pbi.pulse{animation:bp 1.5s ease-in-out infinite;width:100%}
@keyframes bp{0%,100%{opacity:.4}50%{opacity:1}}
.pnote{font-size:11px;color:var(--muted);font-family:var(--fm);margin-top:10px;text-align:center}
.btn-cancel{background:transparent;border:1px solid rgba(255,80,80,.4);color:#ff8080;border-radius:8px;padding:8px 20px;font-family:var(--ff);font-size:12px;font-weight:600;cursor:pointer;transition:all .2s}
.btn-cancel:hover{background:rgba(255,80,80,.1);border-color:#ff6060;color:#ff6060}
.btn-cancel:disabled{opacity:.3;cursor:not-allowed}
.errbox{display:none;background:rgba(255,60,60,.07);border:1px solid rgba(255,60,60,.25);border-radius:12px;padding:14px 18px;margin-bottom:16px;font-family:var(--fm);font-size:12px;color:#ff8888;line-height:1.6}
.errbox.show{display:block}
.rc{display:none;background:var(--s1);border:1px solid var(--border);border-radius:16px;overflow:hidden;margin-bottom:16px}
.rc.show{display:block}
.rh{display:flex;align-items:center;justify-content:space-between;padding:16px 20px;border-bottom:1px solid var(--border);background:var(--s2);flex-wrap:wrap;gap:10px}
.rt{font-size:14px;font-weight:700;color:var(--white)}
.ra{display:flex;gap:8px}
.bsm{padding:7px 14px;border-radius:8px;font-family:var(--ff);font-size:12px;font-weight:600;cursor:pointer;transition:all .15s;border:none}
.bg{background:transparent;border:1px solid var(--border2);color:var(--text)}
.bg:hover{border-color:var(--saffron);color:var(--saffron)}
.bo{background:var(--saffron);color:white}
.bo:hover{background:#ff8800}
.tabs{display:flex;border-bottom:1px solid var(--border);background:var(--s2)}
.tab{padding:11px 18px;font-size:12px;font-weight:600;cursor:pointer;color:var(--muted);border-bottom:2px solid transparent;margin-bottom:-1px;transition:all .15s;font-family:var(--fm);letter-spacing:.05em;text-transform:uppercase}
.tab.active{color:var(--saffron);border-bottom-color:var(--saffron)}
.tab:hover:not(.active){color:var(--text)}
.tp{display:none;padding:20px}.tp.active{display:block}
.segs{display:flex;flex-direction:column;gap:16px}
.seg{display:flex;gap:14px;animation:si .3s ease}
@keyframes si{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
.smeta{width:120px;flex-shrink:0;text-align:right;padding-top:2px}
.sspk{display:inline-block;font-size:10px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;font-family:var(--fm);padding:3px 8px;border-radius:5px;margin-bottom:4px}
.stime{font-size:10px;color:var(--muted);font-family:var(--fm);display:block;text-align:right}
.sbody{flex:1;padding-left:14px;border-left:2px solid var(--border);padding-top:2px;font-size:14px;line-height:1.75}
.sp0{background:rgba(255,153,51,.12);color:#ffa94d;border:1px solid rgba(255,153,51,.25)}
.sp1{background:rgba(19,184,8,.12);color:#4cdb40;border:1px solid rgba(19,184,8,.25)}
.sp2{background:rgba(74,158,255,.12);color:#74b8ff;border:1px solid rgba(74,158,255,.25)}
.sp3{background:rgba(255,100,180,.12);color:#ff80c8;border:1px solid rgba(255,100,180,.25)}
.sp4{background:rgba(180,120,255,.12);color:#c08fff;border:1px solid rgba(180,120,255,.25)}
.sp5{background:rgba(255,220,50,.12);color:#ffe060;border:1px solid rgba(255,220,50,.25)}
.sp6{background:rgba(80,220,200,.12);color:#50dcc8;border:1px solid rgba(80,220,200,.25)}
.sp7{background:rgba(255,140,60,.12);color:#ffaa60;border:1px solid rgba(255,140,60,.25)}
.bl0{border-left-color:#ffa94d}.bl1{border-left-color:#4cdb40}.bl2{border-left-color:#74b8ff}.bl3{border-left-color:#ff80c8}.bl4{border-left-color:#c08fff}.bl5{border-left-color:#ffe060}.bl6{border-left-color:#50dcc8}.bl7{border-left-color:#ffaa60}
.rawpre{font-family:var(--fm);font-size:13px;line-height:1.8;color:var(--text);white-space:pre-wrap;max-height:480px;overflow-y:auto}
.rawpre::-webkit-scrollbar{width:5px}
.rawpre::-webkit-scrollbar-track{background:var(--s2)}
.rawpre::-webkit-scrollbar-thumb{background:var(--border2);border-radius:3px}
.sstrip{display:flex;border-top:1px solid var(--border);background:var(--s2)}
.si{flex:1;padding:14px 16px;border-right:1px solid var(--border)}
.si:last-child{border-right:none}
.sv{font-size:20px;font-weight:800;color:var(--saffron)}
.sl{font-size:10px;color:var(--muted);font-family:var(--fm);text-transform:uppercase;letter-spacing:.07em;margin-top:2px}
.lbadge{display:inline-block;background:var(--bdim);border:1px solid rgba(74,158,255,.3);color:var(--blue);font-family:var(--fm);font-size:11px;padding:3px 10px;border-radius:6px}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="brand">
      <div class="brand-mark">🎙</div>
      <div class="brand-text">
        <div class="brand-name">Saaras Transcribe</div>
        <div class="brand-sub">powered by Sarvam AI · saaras:v3</div>
      </div>
    </div>
    <div class="tricolor-bar"></div>
  </div>

  <!-- API Key (shared) -->
  <div class="card">
    <div class="card-title">Sarvam API Key</div>
    <div class="key-row">
      <input class="key-input" id="apiKey" type="password" placeholder="Paste your Sarvam API subscription key..." oninput="chk()"/>
      <button class="btn-eye" onclick="toggleKey()">👁</button>
    </div>
  </div>

  <!-- Mode tabs -->
  <div style="display:flex;gap:0;margin-bottom:16px;background:var(--s1);border:1px solid var(--border);border-radius:12px;overflow:hidden">
    <div id="tabSingle" onclick="switchTab('single')" style="flex:1;padding:12px;text-align:center;font-size:13px;font-weight:700;cursor:pointer;color:var(--saffron);border-bottom:2px solid var(--saffron);background:var(--sdim);font-family:var(--fm);transition:all .2s">🎵 Single File</div>
    <div id="tabBatch"  onclick="switchTab('batch')"  style="flex:1;padding:12px;text-align:center;font-size:13px;font-weight:700;cursor:pointer;color:var(--muted);border-bottom:2px solid transparent;font-family:var(--fm);transition:all .2s">⟳ Batch Drive Links</div>
  </div>

  <!-- ══ SINGLE TAB ══ -->
  <div id="paneSingle">
    <div class="card">
      <div class="card-title">Audio File</div>
      <div class="dropzone" id="dz" onclick="document.getElementById('fi').click()">
        <div class="dz-icon">🔊</div>
        <div class="dz-title">Drop your audio file here</div>
        <div class="dz-sub">WAV · MP3 · M4A · OGG · FLAC · AAC · WebM · AMR — up to 1 hour</div>
        <input type="file" id="fi" accept="audio/*" onchange="onFile(this.files[0])">
      </div>
      <div class="file-pill" id="fp">
        <div class="fpi">🎵</div>
        <div class="fpin"><div class="fpn" id="fn">—</div><div class="fpm" id="fm2">—</div></div>
        <button class="fpr" onclick="clrFile()">✕</button>
      </div>
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">
        <div style="flex:1;height:1px;background:var(--border)"></div>
        <span style="font-size:11px;color:var(--muted);font-family:var(--fm);letter-spacing:.1em">OR</span>
        <div style="flex:1;height:1px;background:var(--border)"></div>
      </div>
      <div style="display:flex;gap:10px;margin-bottom:14px">
        <input class="key-input" id="driveUrl" type="text" placeholder="Paste a single Google Drive link (Anyone with link can view)…" oninput="onDriveInput()" style="flex:1"/>
        <button class="btn-eye" onclick="clrDrive()" title="Clear">✕</button>
      </div>
      <div class="card-title" style="margin-top:4px">Language</div>
      <div class="lang-grid">
        <div class="lang-chip auto active" data-code="unknown" onclick="pickLang(this)">Auto-detect</div>
        <div class="lang-chip" data-code="hi-IN" onclick="pickLang(this)">Hindi</div>
        <div class="lang-chip" data-code="ta-IN" onclick="pickLang(this)">Tamil</div>
        <div class="lang-chip" data-code="te-IN" onclick="pickLang(this)">Telugu</div>
        <div class="lang-chip" data-code="kn-IN" onclick="pickLang(this)">Kannada</div>
        <div class="lang-chip" data-code="ml-IN" onclick="pickLang(this)">Malayalam</div>
        <div class="lang-chip" data-code="bn-IN" onclick="pickLang(this)">Bengali</div>
        <div class="lang-chip" data-code="mr-IN" onclick="pickLang(this)">Marathi</div>
        <div class="lang-chip" data-code="gu-IN" onclick="pickLang(this)">Gujarati</div>
        <div class="lang-chip" data-code="pa-IN" onclick="pickLang(this)">Punjabi</div>
        <div class="lang-chip" data-code="od-IN" onclick="pickLang(this)">Odia</div>
        <div class="lang-chip" data-code="as-IN" onclick="pickLang(this)">Assamese</div>
        <div class="lang-chip" data-code="ur-IN" onclick="pickLang(this)">Urdu</div>
        <div class="lang-chip" data-code="en-IN" onclick="pickLang(this)">English (IN)</div>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Transcription Mode</div>
      <div class="mode-row">
        <div class="mode-btn active" data-mode="transcribe" onclick="pickMode(this)">transcribe</div>
        <div class="mode-btn" data-mode="translate" onclick="pickMode(this)">translate</div>
        <div class="mode-btn" data-mode="verbatim" onclick="pickMode(this)">verbatim</div>
        <div class="mode-btn" data-mode="translit" onclick="pickMode(this)">translit</div>
        <div class="mode-btn" data-mode="codemix" onclick="pickMode(this)">codemix</div>
      </div>
      <div class="card-title">Options</div>
      <div class="opts-row">
        <div class="opt-toggle active" id="od" onclick="togOpt(this,'diarize')"><div class="tsw"></div><span class="opt-label">Speaker Diarization</span></div>
        <div class="opt-toggle active" id="ot" onclick="togOpt(this,'timestamps')"><div class="tsw"></div><span class="opt-label">Timestamps</span></div>
      </div>
      <div class="num-row" id="nsRow">
        <label>Number of speakers:</label>
        <select id="ns">
          <option value="0">Auto-detect</option>
          <option value="2" selected>2</option>
          <option value="3">3</option><option value="4">4</option>
          <option value="5">5</option><option value="6">6</option>
          <option value="7">7</option><option value="8">8</option>
        </select>
      </div>
    </div>
    <button class="btn-go" id="goBtn" onclick="run()" disabled>✦ Transcribe with Saaras v3</button>
    <div class="pc" id="pc">
      <div class="steps" id="stepsEl"></div>
      <div class="pbo"><div class="pbi" id="pb"></div></div>
      <div class="pnote" id="pn"></div>
      <div style="text-align:center;margin-top:16px">
        <button class="btn-cancel" id="cancelBtn" onclick="cancelJob()">✕ Cancel Transcription</button>
      </div>
    </div>
    <div class="errbox" id="eb"></div>
    <div class="rc" id="rc">
      <div class="rh">
        <div><div class="rt">Transcript — <span id='fnLabel' style='font-weight:500;color:var(--muted);font-size:12px'></span></div><div id="lb" style="margin-top:4px"></div></div>
        <div class="ra">
          <button class="bsm bg" onclick="dlTxt()">↓ TXT</button>
          <button class="bsm bg" onclick="dlJson()">↓ JSON</button>
          <button class="bsm bo" onclick="cpAll()" id="cpBtn">Copy</button>
        </div>
      </div>
      <div class="tabs">
        <div class="tab active" onclick="swTab('d')">Speakers</div>
        <div class="tab" onclick="swTab('r')">Full Text</div>
        <div class="tab" onclick="swTab('j')">JSON</div>
      </div>
      <div class="tp active" id="tpd"><div class="segs" id="segEl"></div></div>
      <div class="tp" id="tpr"><div class="rawpre" id="rawEl"></div></div>
      <div class="tp" id="tpj"><div style="display:flex;justify-content:flex-end;margin-bottom:10px"><button class="bsm bo" onclick="cpJson()" id="cpJsonBtn">Copy JSON</button></div><div class="rawpre" id="jsonEl"></div></div>
      <div class="sstrip">
        <div class="si"><div class="sv" id="ss">—</div><div class="sl">Speakers</div></div>
        <div class="si"><div class="sv" id="sg">—</div><div class="sl">Segments</div></div>
        <div class="si"><div class="sv" id="sw">—</div><div class="sl">Words</div></div>
        <div class="si"><div class="sv" id="sd">—</div><div class="sl">Duration</div></div>
      </div>
    </div>
  </div>

  <!-- ══ BATCH TAB ══ -->
  <div id="paneBatch" style="display:none">
    <div class="card">
      <div class="card-title">Google Drive Links — one per line</div>
      <textarea id="batchLinks" oninput="onBatchInput()" placeholder="https://drive.google.com/file/d/FILE1/view&#10;https://drive.google.com/file/d/FILE2/view&#10;https://drive.google.com/file/d/FILE3/view" style="width:100%;min-height:120px;background:var(--s2);border:1px solid var(--border);border-radius:10px;padding:12px 14px;color:var(--white);font-family:var(--fm);font-size:12px;outline:none;resize:vertical;line-height:1.8;transition:border-color .2s" onfocus="this.style.borderColor='var(--saffron)'" onblur="this.style.borderColor='var(--border)'"></textarea>
      <div id="batchCount" style="font-size:11px;color:var(--muted);font-family:var(--fm);margin-top:6px"></div>
      <div class="card-title" style="margin-top:16px">Language</div>
      <div class="lang-grid">
        <div class="lang-chip auto active" data-code="unknown" onclick="pickLang(this)">Auto-detect</div>
        <div class="lang-chip" data-code="hi-IN" onclick="pickLang(this)">Hindi</div>
        <div class="lang-chip" data-code="ta-IN" onclick="pickLang(this)">Tamil</div>
        <div class="lang-chip" data-code="te-IN" onclick="pickLang(this)">Telugu</div>
        <div class="lang-chip" data-code="kn-IN" onclick="pickLang(this)">Kannada</div>
        <div class="lang-chip" data-code="ml-IN" onclick="pickLang(this)">Malayalam</div>
        <div class="lang-chip" data-code="bn-IN" onclick="pickLang(this)">Bengali</div>
        <div class="lang-chip" data-code="mr-IN" onclick="pickLang(this)">Marathi</div>
        <div class="lang-chip" data-code="gu-IN" onclick="pickLang(this)">Gujarati</div>
        <div class="lang-chip" data-code="pa-IN" onclick="pickLang(this)">Punjabi</div>
        <div class="lang-chip" data-code="od-IN" onclick="pickLang(this)">Odia</div>
        <div class="lang-chip" data-code="as-IN" onclick="pickLang(this)">Assamese</div>
        <div class="lang-chip" data-code="ur-IN" onclick="pickLang(this)">Urdu</div>
        <div class="lang-chip" data-code="en-IN" onclick="pickLang(this)">English (IN)</div>
      </div>
      <div class="card-title" style="margin-top:16px">Transcription Mode</div>
      <div class="mode-row">
        <div class="mode-btn active" data-mode="transcribe" onclick="pickMode(this)">transcribe</div>
        <div class="mode-btn" data-mode="translate" onclick="pickMode(this)">translate</div>
        <div class="mode-btn" data-mode="verbatim" onclick="pickMode(this)">verbatim</div>
        <div class="mode-btn" data-mode="translit" onclick="pickMode(this)">translit</div>
        <div class="mode-btn" data-mode="codemix" onclick="pickMode(this)">codemix</div>
      </div>
      <div class="card-title" style="margin-top:16px">Options</div>
      <div class="opts-row">
        <div class="opt-toggle active" id="od2" onclick="togOpt(this,'diarize')"><div class="tsw"></div><span class="opt-label">Speaker Diarization</span></div>
        <div class="opt-toggle active" id="ot2" onclick="togOpt(this,'timestamps')"><div class="tsw"></div><span class="opt-label">Timestamps</span></div>
      </div>
      <div class="num-row" id="nsRow2">
        <label>Number of speakers:</label>
        <select id="ns2">
          <option value="0">Auto-detect</option>
          <option value="2" selected>2</option>
          <option value="3">3</option><option value="4">4</option>
          <option value="5">5</option><option value="6">6</option>
          <option value="7">7</option><option value="8">8</option>
        </select>
      </div>
    </div>
    <button class="btn-go" id="batchBtn" onclick="runBatch()" disabled style="background:linear-gradient(135deg,#4a9eff 0%,#1a6fd4 100%);box-shadow:0 4px 20px rgba(74,158,255,.2)">⟳ Start Batch Transcription</button>

    <!-- Batch progress -->
    <div id="batchPc" style="display:none;background:var(--s1);border:1px solid var(--border);border-radius:16px;padding:24px;margin-top:16px;margin-bottom:16px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
        <div style="font-size:13px;font-weight:700;color:var(--white)" id="batchProgressLabel">Batch Processing…</div>
        <button class="btn-cancel" id="batchCancelBtn" onclick="cancelBatch()">✕ Cancel Batch</button>
      </div>
      <div class="pbo" style="margin-bottom:6px"><div class="pbi" id="batchPb"></div></div>
      <div style="font-size:11px;color:var(--muted);font-family:var(--fm);text-align:center" id="batchNote"></div>
    </div>

    <!-- Batch report -->
    <div id="batchReport" style="display:none;background:var(--s1);border:1px solid var(--border);border-radius:16px;overflow:hidden;margin-top:16px;margin-bottom:16px">
      <div style="display:flex;align-items:center;justify-content:space-between;padding:16px 20px;border-bottom:1px solid var(--border);background:var(--s2);flex-wrap:wrap;gap:10px">
        <div style="font-size:14px;font-weight:700;color:var(--white)">Batch Report — <span id="batchSummary" style="font-weight:500;color:var(--muted);font-size:12px"></span></div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="bsm bg" onclick="downloadAll('txt')">↓ All TXT</button>
          <button class="bsm bg" onclick="downloadAll('json')">↓ All JSON</button>
        </div>
      </div>
      <div id="batchTable" style="padding:16px;display:flex;flex-direction:column;gap:8px"></div>
      <!-- Failed links box -->
      <div id="failedBox" style="display:none;margin:0 16px 16px;background:rgba(255,60,60,.07);border:1px solid rgba(255,60,60,.25);border-radius:10px;padding:14px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
          <div style="font-size:11px;font-weight:700;color:#ff8888;font-family:var(--fm);letter-spacing:.05em">❌ FAILED LINKS — paste these back to retry</div>
          <button class="bsm" onclick="copyFailed()" id="copyFailedBtn" style="background:rgba(255,80,80,.15);border:1px solid rgba(255,80,80,.3);color:#ff8888;font-size:11px;padding:4px 10px">⎘ Copy</button>
        </div>
        <div id="failedLinks" style="font-size:11px;font-family:var(--fm);color:#ffaaaa;line-height:1.8;white-space:pre-wrap;word-break:break-all"></div>
      </div>
    </div>
  </div>

</div>
<script>
let audioFile=null,lang='unknown',mode='transcribe',opts={diarize:true,timestamps:true},result=null;

/* ── Tab switching ── */
function switchTab(t){
  document.getElementById('paneSingle').style.display=t==='single'?'':'none';
  document.getElementById('paneBatch').style.display=t==='batch'?'':'none';
  const ts=document.getElementById('tabSingle'),tb=document.getElementById('tabBatch');
  if(t==='single'){
    ts.style.color='var(--saffron)';ts.style.borderBottom='2px solid var(--saffron)';ts.style.background='var(--sdim)';
    tb.style.color='var(--muted)';tb.style.borderBottom='2px solid transparent';tb.style.background='';
  } else {
    tb.style.color='var(--blue)';tb.style.borderBottom='2px solid var(--blue)';tb.style.background='var(--bdim)';
    ts.style.color='var(--muted)';ts.style.borderBottom='2px solid transparent';ts.style.background='';
  }
}

/* ── Drive input (single tab) ── */
function onDriveInput(){
  const v=document.getElementById('driveUrl').value.trim();
  if(v){audioFile=null;document.getElementById('fi').value='';document.getElementById('fp').classList.remove('show');document.getElementById('dz').style.display='none';}
  else{document.getElementById('dz').style.display='';}
  chk();
}
function clrDrive(){document.getElementById('driveUrl').value='';document.getElementById('dz').style.display='';chk();}
/* ── Restore saved settings ── */
(function(){
  const s=JSON.parse(localStorage.getItem('saaras_prefs')||'{}');
  if(s.apiKey)document.getElementById('apiKey').value=s.apiKey;
  if(s.lang){lang=s.lang;document.querySelectorAll('.lang-chip').forEach(c=>c.classList.toggle('active',c.dataset.code===lang));}
  if(s.mode){mode=s.mode;document.querySelectorAll('.mode-btn').forEach(b=>b.classList.toggle('active',b.dataset.mode===mode));}
  if(s.ns){document.getElementById('ns').value=s.ns;document.getElementById('ns2').value=s.ns;}
  if(s.diarize===false){opts.diarize=false;['od','od2'].forEach(id=>{const el=document.getElementById(id);if(el)el.classList.remove('active');});document.getElementById('nsRow').style.opacity='0.4';document.getElementById('nsRow2').style.opacity='0.4';}
  if(s.timestamps===false){opts.timestamps=false;['ot','ot2'].forEach(id=>{const el=document.getElementById(id);if(el)el.classList.remove('active');});}
})();
function savePrefs(){localStorage.setItem('saaras_prefs',JSON.stringify({apiKey:document.getElementById('apiKey').value,lang,mode,ns:document.getElementById('ns').value,diarize:opts.diarize,timestamps:opts.timestamps}));}
function chk(){const key=document.getElementById('apiKey').value.trim();document.getElementById('goBtn').disabled=!(key&&(audioFile||document.getElementById('driveUrl').value.trim()));savePrefs();}
function chkBatch(){const key=document.getElementById('apiKey').value.trim();document.getElementById('batchBtn').disabled=!(key&&parseBatchLinks().length>0);}
function toggleKey(){const i=document.getElementById('apiKey');i.type=i.type==='password'?'text':'password';}
function pickLang(el){document.querySelectorAll('.lang-chip').forEach(c=>c.classList.remove('active'));el.classList.add('active');lang=el.dataset.code;savePrefs();}
function pickMode(el){document.querySelectorAll('.mode-btn').forEach(b=>b.classList.remove('active'));el.classList.add('active');mode=el.dataset.mode;savePrefs();}
function togOpt(el,k){
  el.classList.toggle('active');opts[k]=el.classList.contains('active');
  if(k==='diarize'){document.getElementById('nsRow').style.opacity=opts.diarize?'1':'0.4';document.getElementById('nsRow2').style.opacity=opts.diarize?'1':'0.4';}
  savePrefs();
}
function onFile(f){if(!f)return;audioFile=f;document.getElementById('driveUrl').value='';document.getElementById('fn').textContent=f.name;document.getElementById('fm2').textContent=fmtSz(f.size)+' · '+f.type;document.getElementById('fp').classList.add('show');document.getElementById('dz').style.display='none';chk();}
function clrFile(){audioFile=null;document.getElementById('fi').value='';document.getElementById('fp').classList.remove('show');document.getElementById('dz').style.display='';chk();}
function fmtSz(b){return b>=1048576?(b/1048576).toFixed(1)+' MB':(b/1024).toFixed(0)+' KB';}
const dz=document.getElementById('dz');
dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('drag');});
dz.addEventListener('dragleave',()=>dz.classList.remove('drag'));
dz.addEventListener('drop',e=>{e.preventDefault();dz.classList.remove('drag');const f=e.dataTransfer.files[0];if(f&&f.type.startsWith('audio/'))onFile(f);});
function parseBatchLinks(){return document.getElementById('batchLinks').value.split('\n').map(l=>l.trim()).filter(l=>l.startsWith('http'));}
function onBatchInput(){const n=parseBatchLinks().length;document.getElementById('batchCount').textContent=n?n+' link'+(n>1?'s':'')+' ready':'';chkBatch();}

/* ── Steps (single file) ── */
const STEPS=[{id:'up',label:'Uploading audio to local server'},{id:'st',label:'SDK creating job & starting'},{id:'pr',label:'Saaras v3 processing audio…'},{id:'dl',label:'Fetching & parsing results'}];
function buildSteps(){document.getElementById('stepsEl').innerHTML=STEPS.map((s,i)=>`<div class="step" id="step-${s.id}"><div class="sdot">${i+1}</div><div class="stxt">${s.label}</div></div>`).join('');}
function actStep(id){STEPS.forEach(s=>{const e=document.getElementById('step-'+s.id);e.classList.remove('active','done');});const idx=STEPS.findIndex(s=>s.id===id);for(let i=0;i<idx;i++)document.getElementById('step-'+STEPS[i].id).classList.add('done');document.getElementById('step-'+id).classList.add('active');const pb=document.getElementById('pb');pb.classList.remove('pulse');pb.style.width=(idx/STEPS.length*100)+'%';if(id==='pr')pb.classList.add('pulse');}
function allDone(){STEPS.forEach(s=>document.getElementById('step-'+s.id).classList.add('done'));const pb=document.getElementById('pb');pb.classList.remove('pulse');pb.style.width='100%';}
function note(t){document.getElementById('pn').textContent=t;}

/* ── Single file run ── */
let activeJobId=null;
async function run(){
  const apiKey=document.getElementById('apiKey').value.trim();
  const driveUrl=document.getElementById('driveUrl').value.trim();
  if(!apiKey||(!audioFile&&!driveUrl))return;
  document.getElementById('rc').classList.remove('show');
  document.getElementById('eb').classList.remove('show');
  document.getElementById('pc').classList.add('show');
  document.getElementById('goBtn').disabled=true;
  buildSteps();result=null;
  try{
    actStep('up');note('Sending "'+(audioFile?audioFile.name:driveUrl)+'" to local server…');
    const ns=parseInt(document.getElementById('ns').value)||0;
    const fd=new FormData();
    fd.append('api_key',apiKey);fd.append('mode',mode);fd.append('language_code',lang);
    fd.append('with_diarization',opts.diarize?'true':'false');fd.append('with_timestamps',opts.timestamps?'true':'false');
    if(opts.diarize&&ns>0)fd.append('num_speakers',ns);
    if(audioFile)fd.append('file',audioFile,audioFile.name);
    else if(driveUrl)fd.append('drive_url',driveUrl);
    else throw new Error('No file or Drive link provided.');
    const r1=await fetch('/api/transcribe',{method:'POST',body:fd});
    const d1=await r1.json();
    if(!r1.ok)throw new Error(d1.error||'Upload failed');
    activeJobId=d1.job_id;
    actStep('st');note('Job '+d1.job_id+' created. SDK starting…');
    await sleep(2000);actStep('pr');note('Processing… polling every 5s.');
    const res=await poll(d1.job_id);
    actStep('dl');note('Parsing results…');activeJobId=null;
    result=parse(res);allDone();note('');
    setTimeout(()=>{document.getElementById('pc').classList.remove('show');render(result);},500);
  }catch(e){
    activeJobId=null;document.getElementById('pc').classList.remove('show');
    if(e.message==='__CANCELLED__')resetUI();
    else{showErr(e.message||String(e));document.getElementById('goBtn').disabled=false;}
  }
}
async function poll(jobId,max=600000){
  const t0=Date.now();
  while(Date.now()-t0<max){
    await sleep(5000);note('Checking… ('+(Math.round((Date.now()-t0)/1000))+'s)');
    const r=await fetch('/api/status/'+jobId);const d=await r.json();
    if(!r.ok)throw new Error(d.error||'Status failed');
    const st=(d.status||'').toLowerCase();
    if(st==='completed')return d.result;
    if(st==='failed')throw new Error('Job failed: '+(d.error||'unknown'));
    if(st==='cancelled')throw new Error('__CANCELLED__');
  }
  throw new Error('Timed out.');
}
async function cancelJob(){
  if(!activeJobId)return;
  const btn=document.getElementById('cancelBtn');btn.disabled=true;btn.textContent='Cancelling…';
  try{await fetch('/api/cancel/'+activeJobId,{method:'POST'});}catch(e){}
}

/* ── Batch run ── */
let batchResults=[],batchCancelled=false,batchActiveJobId=null;
async function runBatch(){
  const apiKey=document.getElementById('apiKey').value.trim();
  const links=parseBatchLinks();
  if(!apiKey||!links.length)return;
  batchResults=[];batchCancelled=false;
  document.getElementById('batchReport').style.display='none';
  document.getElementById('batchPc').style.display='block';
  document.getElementById('batchBtn').disabled=true;
  document.getElementById('batchCancelBtn').disabled=false;
  document.getElementById('batchCancelBtn').textContent='✕ Cancel Batch';
  const ns=parseInt(document.getElementById('ns2').value)||0;
  for(let i=0;i<links.length;i++){
    if(batchCancelled)break;
    const url=links[i];
    document.getElementById('batchProgressLabel').textContent='Processing '+(i+1)+' of '+links.length+'…';
    document.getElementById('batchNote').textContent=url.length>70?url.slice(0,67)+'…':url;
    document.getElementById('batchPb').style.width=Math.round(i/links.length*100)+'%';
    try{
      const fd=new FormData();
      fd.append('api_key',apiKey);fd.append('mode',mode);fd.append('language_code',lang);
      fd.append('with_diarization',opts.diarize?'true':'false');fd.append('with_timestamps',opts.timestamps?'true':'false');
      if(opts.diarize&&ns>0)fd.append('num_speakers',ns);
      fd.append('drive_url',url);
      const r1=await fetch('/api/transcribe',{method:'POST',body:fd});
      const d1=await r1.json();
      if(!r1.ok)throw new Error(d1.error||'Upload failed');
      const raw=await pollBatch(d1.job_id,i+1,links.length);
      const parsed=parse(raw);
      // Extra guard: if parse returned 0 segments and no full text, treat as failed
      if(!parsed.segs.length&&!parsed.full){
        throw new Error('Sarvam returned an empty transcript for this file.');
      }
      batchResults.push({url,status:'done',parsed,filename:parsed.filename||('file_'+(i+1))});
    }catch(e){
      if(e.message==='__BATCH_CANCELLED__')break;
      batchResults.push({url,status:'failed',error:e.message,filename:''});
    }
    renderBatchReport();
  }
  document.getElementById('batchPb').style.width='100%';
  document.getElementById('batchProgressLabel').textContent=batchCancelled?'Batch cancelled.':'Batch complete!';
  document.getElementById('batchNote').textContent='';
  setTimeout(()=>{document.getElementById('batchPc').style.display='none';},800);
  document.getElementById('batchBtn').disabled=false;
}
async function pollBatch(jobId,cur,total,max=600000){
  batchActiveJobId=jobId;const t0=Date.now();
  while(Date.now()-t0<max){
    if(batchCancelled)throw new Error('__BATCH_CANCELLED__');
    await sleep(5000);
    document.getElementById('batchNote').textContent='File '+cur+'/'+total+' — '+Math.round((Date.now()-t0)/1000)+'s elapsed…';
    const r=await fetch('/api/status/'+jobId);const d=await r.json();
    const st=(d.status||'').toLowerCase();
    if(st==='completed'){batchActiveJobId=null;return d.result;}
    if(st==='failed')throw new Error(d.error||'Job failed');
    if(st==='cancelled')throw new Error('__BATCH_CANCELLED__');
  }
  throw new Error('Timed out');
}
function cancelBatch(){
  batchCancelled=true;
  const btn=document.getElementById('batchCancelBtn');btn.disabled=true;btn.textContent='Cancelling…';
  if(batchActiveJobId)fetch('/api/cancel/'+batchActiveJobId,{method:'POST'}).catch(()=>{});
}
function renderBatchReport(){
  document.getElementById('batchReport').style.display='block';
  const done=batchResults.filter(r=>r.status==='done').length;
  const failed=batchResults.filter(r=>r.status==='failed').length;
  document.getElementById('batchSummary').textContent=done+' ✅ done · '+failed+' ❌ failed · '+batchResults.length+' total';
  const table=document.getElementById('batchTable');table.innerHTML='';
  batchResults.forEach((r,i)=>{
    const row=document.createElement('div');
    const fname=r.filename?r.filename.replace(/\.[^.]+$/,''):('file_'+(i+1));
    if(r.status==='done'){
      row.style.cssText='display:flex;align-items:center;gap:12px;background:var(--s2);border:1px solid var(--border);border-radius:10px;padding:10px 14px;';
      row.innerHTML=`<div style="font-size:16px">✅</div><div style="flex:1;min-width:0"><div style="font-size:13px;font-weight:600;color:var(--white);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(fname)}">${esc(fname)}</div><div style="font-size:11px;color:var(--muted);font-family:var(--fm);margin-top:2px">${r.parsed.segs.length} segments · ${r.parsed.wc.toLocaleString()} words</div></div><div style="display:flex;gap:6px;flex-shrink:0"><button class="bsm bg" onclick="batchDl(${i},'txt')">↓ TXT</button><button class="bsm bg" onclick="batchDl(${i},'json')">↓ JSON</button></div>`;
    } else {
      row.style.cssText='display:flex;align-items:center;gap:12px;background:rgba(255,60,60,.07);border:1px solid rgba(255,60,60,.25);border-radius:10px;padding:10px 14px;';
      row.innerHTML=`<div style="font-size:16px">❌</div><div style="flex:1;min-width:0"><div style="font-size:11px;font-family:var(--fm);color:#ffaaaa;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(r.url)}">${esc(r.url.length>70?r.url.slice(0,67)+'…':r.url)}</div><div style="font-size:11px;color:#ff8888;font-family:var(--fm);margin-top:2px">${esc(r.error||'Unknown error')}</div></div>`;
    }
    table.appendChild(row);
  });
  const failedList=batchResults.filter(r=>r.status==='failed');
  const fb=document.getElementById('failedBox');
  fb.style.display=failedList.length?'block':'none';
  if(failedList.length)document.getElementById('failedLinks').textContent=failedList.map(r=>r.url).join('\n');
  document.getElementById('batchReport').scrollIntoView({behavior:'smooth',block:'start'});
}
function batchDl(idx,type){
  const r=batchResults[idx];if(!r||r.status!=='done')return;
  const fname=r.filename?r.filename.replace(/\.[^.]+$/,''):('file_'+(idx+1));
  const prefix='Transcript ';
  if(type==='json'){const j=r.parsed.segs.map(s=>({start:s.startHMS,end:s.endHMS,speaker:s.speaker,text:s.text}));dl(JSON.stringify(j,null,2),prefix+fname+'.json','application/json');}
  else{const t=r.parsed.segs.map(s=>`[${s.speaker}]  ${s.startHMS} --> ${s.endHMS}\n${s.text}`).join('\n\n');dl(t,prefix+fname+'.txt','text/plain');}
}
async function downloadAll(type){
  const done=batchResults.filter(r=>r.status==='done');
  for(let i=0;i<done.length;i++){batchDl(batchResults.indexOf(done[i]),type);if(i<done.length-1)await sleep(400);}
}
function copyFailed(){
  const txt=document.getElementById('failedLinks').textContent;if(!txt)return;
  navigator.clipboard.writeText(txt).then(()=>{const b=document.getElementById('copyFailedBtn');b.textContent='✓ Copied!';setTimeout(()=>b.textContent='⎘ Copy',1800);});
}

/* ── Shared helpers ── */
function sleep(ms){return new Promise(r=>setTimeout(r,ms));}
function fmtHMS(s){if(!s&&s!==0)return'00:00:00.000';const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sc=Math.floor(s%60),ms=Math.round((s%1)*1000);return String(h).padStart(2,'0')+':'+String(m).padStart(2,'0')+':'+String(sc).padStart(2,'0')+'.'+String(ms).padStart(3,'0');}
function parse(raw){
  let rawSegs=[],full='',maxEnd=0,detLang='';
  if(raw?.full_transcript)full=raw.full_transcript;else if(raw?.transcript)full=raw.transcript;
  if(raw?.language_code||raw?.language_detected)detLang=raw.language_code||raw.language_detected;
  if(Array.isArray(raw?.segments)&&raw.segments.length){
    raw.segments.forEach(e=>{const end=parseFloat(e.end_time??e.end_time_seconds??0);if(end>maxEnd)maxEnd=end;rawSegs.push({speaker:e.speaker||'Speaker 1',start:parseFloat(e.start_time??e.start_time_seconds??0),end,text:(e.text||'').trim()});});
  } else if(raw?.diarized_transcript?.entries?.length){
    raw.diarized_transcript.entries.forEach(e=>{const end=parseFloat(e.end_time_seconds??0);if(end>maxEnd)maxEnd=end;rawSegs.push({speaker:'Speaker '+(parseInt(e.speaker_id??e.speaker??0)+1),start:parseFloat(e.start_time_seconds??0),end,text:(e.transcript||e.text||'').trim()});});
  }
  const merged=[];rawSegs.forEach(s=>{const last=merged[merged.length-1];if(last&&last.speaker===s.speaker){last.text+=' '+s.text;last.end=s.end;}else merged.push({...s});});
  const speakerMap={};let sc=0;merged.forEach(s=>{if(!(s.speaker in speakerMap))speakerMap[s.speaker]='speaker_'+String.fromCharCode(96+(++sc));});merged.forEach(s=>{s.speaker=speakerMap[s.speaker];});
  const speakerColor={};let ci=0;merged.forEach(s=>{if(!(s.speaker in speakerColor))speakerColor[s.speaker]=ci++%8;});
  const segs=merged.map(s=>({...s,si:speakerColor[s.speaker],startHMS:fmtHMS(s.start),endHMS:fmtHMS(s.end)}));
  if(!segs.length&&full)segs.push({speaker:'Speaker 1',si:0,start:0,end:0,startHMS:'00:00:00',endHMS:'00:00:00',text:full});
  const spk=new Set(segs.map(s=>s.speaker));
  const wc=segs.reduce((n,s)=>n+s.text.split(/\s+/).filter(Boolean).length,0);
  return{segs,full,lang:detLang,maxEnd,spk,wc,filename:raw?._filename||''};
}
function render(d){
  document.getElementById('lb').innerHTML=d.lang?`<span class="lbadge">Detected: ${d.lang}</span>`:'';
  document.getElementById('fnLabel').textContent=d.filename?d.filename:(audioFile?audioFile.name:'');
  const el=document.getElementById('segEl');el.innerHTML='';
  if(!d.segs.length){el.innerHTML='<div style="color:var(--muted);font-size:13px;font-family:var(--fm)">No diarized segments. Check Raw Text tab.</div>';}
  else{d.segs.forEach(s=>{const div=document.createElement('div');div.className='seg';div.innerHTML=`<div class="smeta"><span class="sspk sp${s.si}">${esc(s.speaker)}</span><span class="stime">${s.startHMS} → ${s.endHMS}</span></div><div class="sbody bl${s.si}">${esc(s.text)}</div>`;el.appendChild(div);});}
  document.getElementById('rawEl').textContent=d.segs.map(s=>`[${s.speaker}]  ${s.startHMS} → ${s.endHMS}\n${s.text}`).join('\n\n')||d.full;
  const jsonOut=d.segs.map(s=>({start:s.startHMS,end:s.endHMS,speaker:s.speaker,text:s.text}));
  document.getElementById('jsonEl').textContent=JSON.stringify(jsonOut,null,2);
  document.getElementById('ss').textContent=d.spk.size||'—';document.getElementById('sg').textContent=d.segs.length;
  document.getElementById('sw').textContent=d.wc.toLocaleString();document.getElementById('sd').textContent=d.maxEnd?fmtHMS(d.maxEnd):'—';
  document.getElementById('rc').classList.add('show');document.getElementById('goBtn').disabled=false;
  document.getElementById('rc').scrollIntoView({behavior:'smooth',block:'start'});
}
function esc(s){return(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function swTab(n){document.querySelectorAll('.tab').forEach((t,i)=>t.classList.toggle('active',['d','r','j'][i]===n));document.querySelectorAll('.tp').forEach(p=>p.classList.remove('active'));document.getElementById('tp'+n).classList.add('active');}
function dlTxt(){if(!result)return;const lines=result.segs.map(s=>`[${s.speaker}]  ${s.startHMS} --> ${s.endHMS}\n${s.text}`).join('\n\n');const base=result.filename?result.filename.replace(/\.[^.]+$/,''):(audioFile?audioFile.name.replace(/\.[^.]+$/,''):'transcript');dl(lines,'Transcript '+base+'.txt','text/plain');}
function dlJson(){if(!result)return;const j=result.segs.map(s=>({start:s.startHMS,end:s.endHMS,speaker:s.speaker,text:s.text}));const base=result.filename?result.filename.replace(/\.[^.]+$/,''):(audioFile?audioFile.name.replace(/\.[^.]+$/,''):'transcript');dl(JSON.stringify(j,null,2),'Transcript '+base+'.json','application/json');}
function dl(c,n,t){const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([c],{type:t}));a.download=n;a.click();}
function cpAll(){if(!result)return;const txt=result.segs.map(s=>`[${s.speaker}]  ${s.startHMS} --> ${s.endHMS}\n${s.text}`).join('\n\n');navigator.clipboard.writeText(txt).then(()=>{const b=document.getElementById('cpBtn');b.textContent='✓ Copied!';setTimeout(()=>b.textContent='Copy',1800);});}
function resetUI(){document.getElementById('goBtn').disabled=false;document.getElementById('eb').classList.remove('show');const btn=document.getElementById('cancelBtn');btn.disabled=false;btn.textContent='✕ Cancel Transcription';}
function cpJson(){const txt=document.getElementById('jsonEl').textContent;if(!txt)return;navigator.clipboard.writeText(txt).then(()=>{const b=document.getElementById('cpJsonBtn');b.textContent='✓ Copied!';setTimeout(()=>b.textContent='Copy JSON',1800);});}
function showErr(msg){const e=document.getElementById('eb');e.innerHTML='<strong>⚠ Error</strong><br>'+msg;e.classList.add('show');}
</script>
</body>
</html>"""


def obj_to_dict(obj):
    """Recursively convert SDK response objects to plain dicts/lists."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, list):
        return [obj_to_dict(i) for i in obj]
    if isinstance(obj, dict):
        return {k: obj_to_dict(v) for k, v in obj.items()}
    # SDK model object — try __dict__ or model_dump / to_dict
    if hasattr(obj, "model_dump"):
        return obj_to_dict(obj.model_dump())
    if hasattr(obj, "to_dict"):
        return obj_to_dict(obj.to_dict())
    if hasattr(obj, "__dict__"):
        return obj_to_dict({k: v for k, v in obj.__dict__.items() if not k.startswith("_")})
    return str(obj)


def find_transcript(obj):
    """
    Walk any nested structure and return the first dict that contains
    'transcript' or 'diarized_transcript'.
    """
    if isinstance(obj, dict):
        if "transcript" in obj or "diarized_transcript" in obj:
            return obj
        for v in obj.values():
            found = find_transcript(v)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_transcript(item)
            if found:
                return found
    return None


# ── Bengali post-processing ───────────────────────────────────────────────────

_DIGIT_MAP = str.maketrans('0123456789', '০১২৩৪৫৬৭৮৯')

# Dictionary: English word (lowercase key) → Bengali equivalent
# Case-insensitive lookup — covers common words in Bengali speech
_BN_DICT = {
    # Tech & internet
    "online": "অনলাইন", "offline": "অফলাইন", "internet": "ইন্টারনেট",
    "website": "ওয়েবসাইট", "app": "অ্যাপ", "apps": "অ্যাপস",
    "mobile": "মোবাইল", "phone": "ফোন", "laptop": "ল্যাপটপ",
    "computer": "কম্পিউটার", "software": "সফটওয়্যার", "hardware": "হার্ডওয়্যার",
    "download": "ডাউনলোড", "upload": "আপলোড", "password": "পাসওয়ার্ড",
    "email": "ইমেইল", "video": "ভিডিও", "audio": "অডিও",
    "screen": "স্ক্রিন", "keyboard": "কীবোর্ড", "mouse": "মাউস",
    "server": "সার্ভার", "data": "ডেটা", "wifi": "ওয়াইফাই",
    "bluetooth": "ব্লুটুথ", "charge": "চার্জ", "battery": "ব্যাটারি",
    "selfie": "সেলফি", "chat": "চ্যাট", "share": "শেয়ার",
    "post": "পোস্ট", "story": "স্টোরি", "reel": "রিল",
    "live": "লাইভ", "like": "লাইক", "comment": "কমেন্ট",
    "follow": "ফলো", "block": "ব্লক", "report": "রিপোর্ট",
    # Social media & brands
    "facebook": "ফেসবুক", "youtube": "ইউটিউব", "instagram": "ইনস্টাগ্রাম",
    "whatsapp": "হোয়াটসঅ্যাপ", "twitter": "টুইটার", "google": "গুগল",
    "amazon": "আমাজন", "shopify": "শপিফাই", "daraz": "দারাজ",
    "alibaba": "আলিবাবা", "netflix": "নেটফ্লিক্স", "zoom": "জুম",
    # Finance & banking
    "otp": "ওটিপি", "emi": "ইএমআই", "gst": "জিএসটি",
    "upi": "ইউপিআই", "atm": "এটিএম", "id": "আইডি",
    "pin": "পিন", "kyc": "কেওয়াইসি", "loan": "লোন",
    "bank": "ব্যাংক", "cash": "ক্যাশ", "card": "কার্ড",
    "credit": "ক্রেডিট", "debit": "ডেবিট", "payment": "পেমেন্ট",
    "account": "অ্যাকাউন্ট", "balance": "ব্যালেন্স",
    # E-commerce & business
    "order": "অর্ডার", "delivery": "ডেলিভারি", "courier": "কুরিয়ার",
    "product": "প্রোডাক্ট", "service": "সার্ভিস", "offer": "অফার",
    "discount": "ডিসকাউন্ট", "brand": "ব্র্যান্ড", "store": "স্টোর",
    "shop": "শপ", "market": "মার্কেট", "business": "বিজনেস",
    "marketing": "মার্কেটিং", "customer": "কাস্টমার", "client": "ক্লায়েন্ট",
    "company": "কোম্পানি", "startup": "স্টার্টআপ", "office": "অফিস",
    "meeting": "মিটিং", "team": "টিম", "project": "প্রজেক্ট",
    "target": "টার্গেট", "budget": "বাজেট", "profit": "প্রফিট",
    # Games & entertainment
    "ludo": "লুডো", "game": "গেম", "games": "গেমস",
    "play": "প্লে", "player": "প্লেয়ার", "players": "প্লেয়ারস",
    "board": "বোর্ড", "box": "বক্স", "bubble": "বাবল",
    "side": "সাইড", "rule": "রুল", "rules": "রুলস",
    "part": "পার্ট", "parts": "পার্টস", "level": "লেভেল",
    "score": "স্কোর", "point": "পয়েন্ট", "points": "পয়েন্টস",
    "team": "টিম", "match": "ম্যাচ", "cricket": "ক্রিকেট",
    "football": "ফুটবল", "tennis": "টেনিস", "chess": "চেস",
    "carrom": "ক্যারম",
    # Common English words in Bengali speech
    "ok": "ওকে", "okay": "ওকে", "yes": "হ্যাঁ", "no": "না",
    "hi": "হাই", "hello": "হ্যালো", "bye": "বাই",
    "please": "প্লিজ", "sorry": "সরি", "thanks": "ধন্যবাদ",
    "actually": "আসলে", "basically": "মূলত", "generally": "সাধারণত",
    "normally": "স্বাভাবিকভাবে", "specially": "বিশেষভাবে",
    "problem": "সমস্যা", "solution": "সমাধান", "idea": "আইডিয়া",
    "class": "ক্লাস", "school": "স্কুল", "college": "কলেজ",
    "university": "বিশ্ববিদ্যালয়", "exam": "পরীক্ষা", "result": "রেজাল্ট",
    "fee": "ফি", "form": "ফর্ম", "certificate": "সার্টিফিকেট",
    "job": "চাকরি", "work": "কাজ", "salary": "বেতন",
    "interview": "ইন্টারভিউ", "cv": "সিভি",
    # Transport & places
    "bus": "বাস", "train": "ট্রেন", "car": "গাড়ি",
    "bike": "বাইক", "taxi": "ট্যাক্সি", "hotel": "হোটেল",
    "hospital": "হাসপাতাল", "mall": "মল", "park": "পার্ক",
    # Misc
    "electricity": "ইলেকট্রিসিটি", "electric": "ইলেকট্রিক",
    "ac": "এসি", "fan": "ফ্যান", "light": "লাইট",
    "coat": "কোট", "shirt": "শার্ট", "pants": "প্যান্ট",
    "dress": "ড্রেস", "shoes": "জুতা",
    "foot": "ফুট", "put": "পুট", "cut": "কাট",
    "set": "সেট", "net": "নেট", "hit": "হিট",
    "fit": "ফিট", "kit": "কিট", "bit": "বিট",
    "hot": "হট", "not": "নট", "lot": "লট",
    "top": "টপ", "pop": "পপ", "cop": "কপ",
    "tip": "টিপ", "zip": "জিপ", "chip": "চিপ",
    "trip": "ট্রিপ", "grip": "গ্রিপ", "ship": "শিপ",
    "shop": "শপ", "stop": "স্টপ", "drop": "ড্রপ",
}

def _lookup_word(word):
    """
    Look up English word in dictionary (case-insensitive).
    Returns Bengali equivalent if found, else returns original word unchanged.
    """
    return _BN_DICT.get(word.lower(), word)

def bengali_postprocess(text):
    """
    1. Convert ASCII digits 0-9 → Bengali digits ০-৯
    2. Look up English words in dictionary → replace with Bengali
       Words NOT in dictionary are kept as-is (no garbled transliteration)
    Timestamps, speaker labels, and all other content are untouched.
    """
    import re
    if not isinstance(text, str):
        return text
    # Step 1: digits
    text = text.translate(_DIGIT_MAP)
    # Step 2: dictionary lookup only — no ITRANS, no garbling
    text = re.sub(r'[A-Za-z]+', lambda m: _lookup_word(m.group()), text)
    return text

def postprocess_result(data):
    """
    Walk only transcript text fields and apply bengali_postprocess.
    Timestamps and all other fields are never touched.
    """
    if not isinstance(data, dict):
        return data
    for key in ('transcript', 'full_transcript'):
        if key in data and isinstance(data[key], str):
            data[key] = bengali_postprocess(data[key])
    if isinstance(data.get('segments'), list):
        for seg in data['segments']:
            if isinstance(seg, dict) and 'text' in seg:
                seg['text'] = bengali_postprocess(seg['text'])
    dt = data.get('diarized_transcript')
    if isinstance(dt, dict) and isinstance(dt.get('entries'), list):
        for entry in dt['entries']:
            if isinstance(entry, dict):
                for key in ('transcript', 'text'):
                    if key in entry and isinstance(entry[key], str):
                        entry[key] = bengali_postprocess(entry[key])
    return data

# ─────────────────────────────────────────────────────────────────────────────


def run_job_thread(job_id, api_key, audio_path, tmp_original_path, mode, language_code,
                   with_diarization, with_timestamps, num_speakers, original_filename=""):
    try:
        client = SarvamAI(api_subscription_key=api_key)
        kwargs = dict(model="saaras:v3", mode=mode, language_code=language_code,
                      with_diarization=with_diarization, with_timestamps=with_timestamps)
        if with_diarization and num_speakers > 0:
            kwargs["num_speakers"] = num_speakers

        print(f"  [job {job_id}] Creating job with SDK…")
        job = client.speech_to_text_job.create_job(**kwargs)

        print(f"  [job {job_id}] Uploading {audio_path}…")
        job.upload_files(file_paths=[audio_path])

        print(f"  [job {job_id}] Starting job…")
        job.start()

        print(f"  [job {job_id}] Waiting for completion…")
        # Poll for cancel while waiting
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(job.wait_until_complete)
            while not future.done():
                time.sleep(2)
                with jobs_lock:
                    if jobs[job_id].get("cancel"):
                        print(f"  [job {job_id}] Cancelled by user.")
                        with jobs_lock:
                            jobs[job_id]["status"] = "cancelled"
                            jobs[job_id]["error"] = "Cancelled by user"
                        return
            future.result()  # re-raise any exception

        print(f"  [job {job_id}] Downloading output JSON files…")
        import glob, shutil
        out_dir = tempfile.mkdtemp(prefix=f"sarvam_{job_id}_")
        try:
            job.download_outputs(output_dir=out_dir)
        except Exception as de:
            print(f"  [job {job_id}] download_outputs error: {de}")

        json_files = sorted(glob.glob(os.path.join(out_dir, "*.json")))
        print(f"  [job {job_id}] Found output files: {json_files}")

        result_data = None
        for jf in json_files:
            try:
                with open(jf, "r", encoding="utf-8") as fh:
                    content = json.load(fh)
                print(f"  [job {job_id}] ══ {os.path.basename(jf)} ══")
                print(json.dumps(content, ensure_ascii=False, indent=2)[:3000])
                print(f"  [job {job_id}] ══ END ══\n")
                found = find_transcript(content)
                if found and result_data is None:
                    result_data = found
            except Exception as fe:
                print(f"  [job {job_id}] Could not read {jf}: {fe}")

        shutil.rmtree(out_dir, ignore_errors=True)

        if result_data is None:
            result_data = {"error": "No transcript data found in output files"}

        # ── Detect empty transcript — treat as failure so batch flags it ──
        is_empty = (
            "error" in result_data or
            (
                not result_data.get("transcript") and
                not result_data.get("full_transcript") and
                not result_data.get("segments") and
                not (result_data.get("diarized_transcript") or {}).get("entries")
            )
        )
        if is_empty:
            with jobs_lock:
                jobs[job_id]["status"] = "failed"
                jobs[job_id]["error"] = result_data.get("error", "Empty transcript returned by Sarvam")
            print(f"  [job {job_id}] ✗ Empty/missing transcript — marked as failed")
            return

        # ── Apply Bengali post-processing (digits + English word transliteration) ──
        result_data = postprocess_result(result_data)

        # ── Attach original filename so frontend can name downloads correctly ──
        if isinstance(result_data, dict) and original_filename:
            result_data["_filename"] = original_filename

        with jobs_lock:
            jobs[job_id]["status"] = "completed"
            jobs[job_id]["result"] = result_data

        print(f"  [job {job_id}] ✓ Done. result keys: {list(result_data.keys()) if isinstance(result_data, dict) else type(result_data)}")

    except Exception as e:
        import traceback
        print(f"  [job {job_id}] ✗ Error: {e}")
        traceback.print_exc()
        with jobs_lock:
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["error"] = str(e)
    finally:
        for f in set([audio_path, tmp_original_path]):
            try:
                os.unlink(f)
            except Exception:
                pass


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        status = args[1] if len(args) > 1 else "?"
        print(f"  {self.command:6s} {self.path}  →  {status}")

    def send_json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/status/"):
            job_id = self.path.split("/api/status/")[-1].strip("/")
            self._status(job_id)
        else:
            self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        if self.path == "/api/transcribe":
            self._transcribe()
        elif self.path.startswith("/api/cancel/"):
            job_id = self.path.split("/api/cancel/")[-1].strip("/")
            with jobs_lock:
                if job_id in jobs:
                    jobs[job_id]["cancel"] = True
                    self.send_json(200, {"status": "cancel_requested"})
                else:
                    self.send_json(404, {"error": "Unknown job_id"})
        else:
            self.send_json(404, {"error": "Not found"})

    def _read_multipart(self):
        import io, email
        ctype = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        # Parse multipart boundary
        boundary = None
        for part in ctype.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[len("boundary="):].strip().strip('"')
                break
        if not boundary:
            raise ValueError("No boundary found in Content-Type")

        fields, file_data, file_name, file_type = {}, None, "audio.mp3", "audio/mpeg"

        # Build a fake email message so email.parser can handle it
        raw = b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body
        msg = email.message_from_bytes(raw)

        for part in msg.walk():
            cd = part.get("Content-Disposition", "")
            if not cd:
                continue
            # Extract field name
            name = None
            fname = None
            for seg in cd.split(";"):
                seg = seg.strip()
                if seg.startswith("name="):
                    name = seg[5:].strip().strip('"')
                elif seg.startswith("filename="):
                    fname = seg[9:].strip().strip('"')
            if name is None:
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                payload = b""
            if fname:
                file_data = payload
                file_name = fname
                file_type = part.get_content_type() or "audio/mpeg"
            else:
                fields[name] = payload.decode("utf-8", errors="replace")

        return fields, file_data, file_name, file_type

    def _transcribe(self):
        try:
            fields, file_data, file_name, file_type = self._read_multipart()
        except Exception as e:
            self.send_json(400, {"error": "Parse error: " + str(e)})
            return

        api_key = fields.get("api_key", "").strip()
        mode = fields.get("mode", "transcribe")
        language_code = fields.get("language_code", "unknown")
        with_diarization = fields.get("with_diarization", "true").lower() == "true"
        with_timestamps = fields.get("with_timestamps", "true").lower() == "true"
        num_speakers = int(fields.get("num_speakers", 0) or 0)

        if not api_key:
            self.send_json(400, {"error": "Missing API key"})
            return

        drive_url = fields.get("drive_url", "").strip()

        # ── Source: Google Drive link ─────────────────────────────────────────
        if drive_url:
            if not file_data:
                print(f"  Downloading from Google Drive: {drive_url}")
                file_data, file_name, err = _download_from_drive(drive_url)
                if err:
                    self.send_json(400, {"error": f"Drive download failed: {err}"})
                    return
                print(f"  Drive download OK: {file_name} ({len(file_data)/1024/1024:.1f}MB)")

        # ── Source: uploaded file ─────────────────────────────────────────────
        if not file_data:
            self.send_json(400, {"error": "Missing audio file or Drive link"})
            return

        ext = Path(file_name).suffix or ".mp3"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
        tmp.write(file_data)
        tmp.close()
        tmp_original_path = tmp.name

        # Always normalize WAV to 16-bit / 16000Hz / mono before sending to Sarvam.
        # Uses ffmpeg directly — works on all Python versions including 3.13+
        audio_path = tmp.name
        if ext.lower() == ".wav":
            try:
                conv = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
                conv.close()
                import subprocess
                result = subprocess.run([
                    "ffmpeg", "-y",
                    "-i", tmp.name,
                    "-ar", "16000",
                    "-ac", "1",
                    "-sample_fmt", "s16",
                    conv.name
                ], capture_output=True, timeout=120)
                if result.returncode == 0:
                    audio_path = conv.name
                    new_size = os.path.getsize(audio_path)
                    print(f"  ffmpeg normalised -> 16bit/16000Hz/mono / {new_size/1024/1024:.1f}MB -> {audio_path}")
                else:
                    err_msg = result.stderr.decode(errors="ignore")[-200:]
                    print(f"  ffmpeg failed: {err_msg}, sending original file")
                    audio_path = tmp.name
            except Exception as ce:
                print(f"  normalisation error ({ce}), sending original file")
                audio_path = tmp.name

        import uuid
        job_id = str(uuid.uuid4())[:8]

        with jobs_lock:
            jobs[job_id] = {"status": "processing", "result": None, "error": None, "cancel": False}

        t = threading.Thread(
            target=run_job_thread,
            args=(job_id, api_key, audio_path, tmp_original_path, mode, language_code,
                  with_diarization, with_timestamps, num_speakers, file_name),
            daemon=True
        )
        t.start()

        self.send_json(200, {"job_id": job_id, "status": "processing"})

    def _status(self, job_id):
        with jobs_lock:
            job = jobs.get(job_id)
        if not job:
            self.send_json(404, {"error": "Unknown job_id"})
            return
        status = job["status"]
        if status == "completed":
            self.send_json(200, {"status": "completed", "result": job["result"]})
        elif status == "failed":
            self.send_json(200, {"status": "failed", "error": job["error"]})
        elif status == "cancelled":
            self.send_json(200, {"status": "cancelled", "error": "Cancelled by user"})
        else:
            self.send_json(200, {"status": "processing"})


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"""
     Open in browser:  http://localhost:{PORT}     
     Press Ctrl+C to stop                      
""")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
