"""Ollama Chat UI — supports text, images, CSV, JSON, PDF, and any text file."""
import base64
import csv
import io
import json
import os

import httpx
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
DEFAULT_MODEL = os.getenv("OLLAMA_MODEL", "llava")
MAX_CTX_CHARS = 8000  # max file content chars sent as prompt context

app = FastAPI(title="Ollama Chat")

# ── Embedded UI ───────────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ollama Chat</title>
<style>
:root {
  --bg: #0f0f1a; --surface: #1a1a2e; --border: #2a2a4a;
  --accent: #7c4dff; --accent2: #00b0ff;
  --text: #e0e0f0; --dim: #778; --error: #f55;
  --user-bg: #1e1e4a; --bot-bg: #132213;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font: 15px/1.5 system-ui,sans-serif;
       height: 100dvh; display: flex; flex-direction: column; overflow: hidden; }

/* header */
header { background: var(--surface); border-bottom: 1px solid var(--border);
         padding: 10px 16px; display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
header h1 { font-size: 17px; font-weight: 700; flex: 1; }
select { background: var(--bg); color: var(--text); border: 1px solid var(--border);
         border-radius: 6px; padding: 5px 9px; font-size: 13px; cursor: pointer; }
.hdr-btn { background: transparent; color: var(--dim); border: 1px solid var(--border);
           border-radius: 6px; padding: 5px 11px; cursor: pointer; font-size: 13px; }
.hdr-btn:hover { color: var(--text); }

/* file zone */
#file-zone { background: var(--surface); border-bottom: 1px solid var(--border);
             padding: 8px 16px; flex-shrink: 0; }
#file-zone.drag-over { background: #1a1a3e; }
#drop-label { display: flex; align-items: center; gap: 8px; color: var(--dim);
              font-size: 13px; cursor: pointer; }
#drop-label span { color: var(--accent2); text-decoration: underline; }
#drop-label input { display: none; }
#file-preview { display: none; align-items: flex-start; gap: 12px; margin-top: 8px; }
#prev-img { max-height: 72px; max-width: 110px; border-radius: 6px;
            border: 1px solid var(--border); object-fit: cover; display: none; }
.finfo .fname { font-weight: 600; color: var(--accent2); font-size: 13px; }
.finfo .fmeta { color: var(--dim); font-size: 11px; margin-top: 2px; }
.finfo pre { background: var(--bg); border: 1px solid var(--border); border-radius: 4px;
             padding: 5px 8px; font-size: 11px; max-height: 50px; overflow: hidden;
             color: var(--dim); white-space: pre-wrap; word-break: break-all; margin-top: 4px; }
.clr-file { background: transparent; color: var(--error); border: none;
            cursor: pointer; font-size: 18px; line-height: 1; padding: 0 4px; margin-left: auto; }

/* messages */
#msgs { flex: 1; overflow-y: auto; padding: 14px 16px; display: flex; flex-direction: column; gap: 10px; }
.msg { display: flex; flex-direction: column; max-width: 85%; }
.msg.user { align-self: flex-end; }
.msg.bot  { align-self: flex-start; }
.bubble { border-radius: 12px; padding: 9px 13px; font-size: 14px; line-height: 1.65;
          white-space: pre-wrap; word-break: break-word; }
.msg.user .bubble { background: var(--user-bg); border: 1px solid #3a3a6a; }
.msg.bot  .bubble { background: var(--bot-bg);  border: 1px solid #2a4a2a; }
.msg.err  .bubble { background: #2a1010; border-color: var(--error); color: var(--error); }
.bubble img { max-width: 180px; border-radius: 6px; margin-top: 6px; display: block; }
.typing::after { content: '▌'; animation: blink .8s step-end infinite; }
@keyframes blink { 50% { opacity: 0; } }

/* input */
#bar { background: var(--surface); border-top: 1px solid var(--border);
       padding: 10px 14px; display: flex; gap: 8px; flex-shrink: 0; align-items: flex-end; }
#q { flex: 1; background: var(--bg); color: var(--text); border: 1px solid var(--border);
     border-radius: 8px; padding: 9px 12px; font-size: 14px; font-family: inherit;
     resize: none; min-height: 42px; max-height: 140px; overflow-y: auto; }
#q:focus { outline: none; border-color: var(--accent); }
#send { background: var(--accent); color: #fff; border: none; border-radius: 8px;
        padding: 9px 16px; font-size: 14px; font-weight: 600; cursor: pointer; white-space: nowrap; }
#send:disabled { opacity: .45; cursor: default; }
#send:not(:disabled):hover { filter: brightness(1.15); }
</style>
</head>
<body>

<header>
  <h1>🦙 Ollama Chat</h1>
  <select id="model"><option>Loading…</option></select>
  <button class="hdr-btn" onclick="clearChat()">Clear chat</button>
</header>

<div id="file-zone">
  <label id="drop-label">
    📎 Drop any file here or <span>click to upload</span>
    &nbsp;— image, CSV, JSON, PDF, code, text…
    <input type="file" id="finput" onchange="handleFile(this.files[0])">
  </label>
  <div id="file-preview">
    <img id="prev-img" alt="preview">
    <div class="finfo">
      <div class="fname" id="fname"></div>
      <div class="fmeta" id="fmeta"></div>
      <pre id="fpre"></pre>
    </div>
    <button class="clr-file" onclick="clearFile()" title="Remove file">✕</button>
  </div>
</div>

<div id="msgs">
  <div class="msg bot">
    <div class="bubble">👋 Hi! I'm powered by Ollama. Ask me anything, or upload a file (image, CSV, JSON, PDF, code…) and I'll answer questions about it.</div>
  </div>
</div>

<div id="bar">
  <textarea id="q" rows="1" placeholder="Type a message… (Enter = send, Shift+Enter = newline)"
    onkeydown="onKey(event)" oninput="resize(this)"></textarea>
  <button id="send" onclick="send()">Send</button>
</div>

<script>
let history = [];
let fileCtx  = null;   // {type:'image'|'text', b64?, content?, name}
let busy     = false;

// ── Models ──────────────────────────────────────────────────────────────────
async function loadModels() {
  try {
    const { models } = await (await fetch('/models')).json();
    const sel = document.getElementById('model');
    sel.innerHTML = models.map(m => `<option>${m}</option>`).join('');
  } catch {}
}
loadModels();

// ── Drag & drop ──────────────────────────────────────────────────────────────
const zone = document.getElementById('file-zone');
zone.addEventListener('dragover',  e => { e.preventDefault(); zone.classList.add('drag-over'); });
zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
zone.addEventListener('drop', e => {
  e.preventDefault(); zone.classList.remove('drag-over');
  const f = e.dataTransfer.files[0]; if (f) handleFile(f);
});

async function handleFile(file) {
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  try {
    const data = await (await fetch('/upload', { method: 'POST', body: fd })).json();
    if (data.type === 'error') { alert('File error: ' + data.error); return; }
    fileCtx = { ...data, name: file.name };
    renderFilePreview(file, data);
  } catch (e) { alert('Upload failed: ' + e); }
}

function renderFilePreview(file, data) {
  document.getElementById('file-preview').style.display = 'flex';
  document.getElementById('fname').textContent = file.name;
  const img  = document.getElementById('prev-img');
  const pre  = document.getElementById('fpre');
  const meta = document.getElementById('fmeta');

  if (data.type === 'image') {
    img.src = 'data:' + file.type + ';base64,' + data.b64;
    img.style.display = 'block';
    pre.style.display = 'none';
    meta.textContent = (file.size/1024).toFixed(1) + ' KB · image (vision model will analyse it)';
  } else {
    img.style.display = 'none';
    pre.style.display = 'block';
    pre.textContent = (data.content || '').slice(0, 320) + ((data.content || '').length > 320 ? '…' : '');
    const extra = [
      data.rows  ? data.rows + ' rows'  : null,
      data.pages ? data.pages + ' pages': null,
    ].filter(Boolean).join(' · ');
    meta.textContent = (file.size/1024).toFixed(1) + ' KB' + (extra ? ' · ' + extra : '') + ' · loaded as context';
  }
}

function clearFile() {
  fileCtx = null;
  document.getElementById('file-preview').style.display = 'none';
  document.getElementById('finput').value = '';
  document.getElementById('prev-img').src = '';
  document.getElementById('fpre').textContent = '';
}

// ── Chat ──────────────────────────────────────────────────────────────────────
function onKey(e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } }

function resize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 140) + 'px';
}

function scrollDown() {
  const m = document.getElementById('msgs');
  m.scrollTop = m.scrollHeight;
}

function addMsg(role, text, imgSrc) {
  const wrap   = document.createElement('div');
  wrap.className = 'msg ' + role;
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  if (imgSrc) {
    const img = document.createElement('img'); img.src = imgSrc;
    bubble.appendChild(img);
  }
  if (text) { const t = document.createElement('span'); t.textContent = text; bubble.appendChild(t); }
  wrap.appendChild(bubble);
  document.getElementById('msgs').appendChild(wrap);
  scrollDown();
  return bubble;
}

async function send() {
  if (busy) return;
  const input = document.getElementById('q');
  const q = input.value.trim(); if (!q) return;

  // Show user bubble (with image thumbnail on first image turn)
  const firstImageTurn = fileCtx?.type === 'image' && history.length === 0;
  addMsg('user', q, firstImageTurn ? ('data:image/jpeg;base64,' + fileCtx.b64) : null);
  input.value = ''; resize(input);

  const botBubble = addMsg('bot', '');
  botBubble.classList.add('typing');
  busy = true;
  document.getElementById('send').disabled = true;

  const fd = new FormData();
  fd.append('question', q);
  fd.append('model',    document.getElementById('model').value);
  fd.append('history',  JSON.stringify(history));
  if (fileCtx?.type === 'image' && fileCtx.b64) {
    fd.append('image_b64', fileCtx.b64);
  } else if (fileCtx?.type === 'text') {
    fd.append('file_context', fileCtx.content || '');
    fd.append('file_name',    fileCtx.name    || '');
  }

  let answer = '';
  try {
    const resp   = await fetch('/chat', { method: 'POST', body: fd });
    const reader = resp.body.getReader();
    const dec    = new TextDecoder();
    let   buf    = '';

    while (true) {
      const { done, value } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split('\n'); buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const chunk = JSON.parse(line.slice(6));
        answer += chunk.content || '';
        botBubble.textContent = answer; // update as tokens stream in
        scrollDown();
      }
    }
  } catch (e) {
    botBubble.textContent = 'Error: ' + e.message;
    botBubble.parentElement.classList.add('err');
  }

  botBubble.classList.remove('typing');
  busy = false;
  document.getElementById('send').disabled = false;

  history.push({ role: 'user',      content: q      });
  history.push({ role: 'assistant', content: answer });

  // Image sent once; clear b64 so we don't resend it every turn
  if (fileCtx?.type === 'image') fileCtx = { ...fileCtx, b64: null };
  input.focus();
}

function clearChat() {
  history = [];
  document.getElementById('msgs').innerHTML =
    '<div class="msg bot"><div class="bubble">Chat cleared. Ask me anything!</div></div>';
}
</script>
</body>
</html>"""


# ── API endpoints ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return _HTML


@app.get("/models")
async def list_models():
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            r.raise_for_status()
            models = [m["name"] for m in r.json().get("models", [])]
            return {"models": models or [DEFAULT_MODEL]}
    except Exception:
        return {"models": [DEFAULT_MODEL]}


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """Extract text or base64 from any uploaded file."""
    content_type = file.content_type or ""
    filename = file.filename or "file"
    data = await file.read()

    # ── Image ────────────────────────────────────────────────────────────────
    if content_type.startswith("image/"):
        return {"type": "image", "b64": base64.b64encode(data).decode(), "filename": filename}

    # ── PDF ──────────────────────────────────────────────────────────────────
    if content_type == "application/pdf" or filename.lower().endswith(".pdf"):
        try:
            from pypdf import PdfReader  # type: ignore
            reader = PdfReader(io.BytesIO(data))
            text = "\n\n".join(p.extract_text() or "" for p in reader.pages)
            return {"type": "text", "content": text, "filename": filename, "pages": len(reader.pages)}
        except ImportError:
            return {"type": "error", "error": "pypdf not installed — PDF not supported"}
        except Exception as exc:
            return {"type": "error", "error": f"PDF read failed: {exc}"}

    # ── CSV ──────────────────────────────────────────────────────────────────
    if content_type in ("text/csv", "application/csv") or filename.lower().endswith(".csv"):
        try:
            text = data.decode("utf-8", errors="replace")
            rows = list(csv.reader(io.StringIO(text)))
            preview = "\n".join(",".join(r) for r in rows[:500])
            return {"type": "text", "content": preview, "filename": filename, "rows": len(rows)}
        except Exception as exc:
            return {"type": "error", "error": f"CSV read failed: {exc}"}

    # ── JSON ─────────────────────────────────────────────────────────────────
    if content_type == "application/json" or filename.lower().endswith(".json"):
        try:
            parsed = json.loads(data)
            text = json.dumps(parsed, indent=2, ensure_ascii=False)
            return {"type": "text", "content": text, "filename": filename}
        except Exception as exc:
            return {"type": "error", "error": f"JSON parse failed: {exc}"}

    # ── Everything else: try UTF-8 text (code, markdown, yaml, xml…) ─────────
    try:
        text = data.decode("utf-8", errors="replace")
        return {"type": "text", "content": text, "filename": filename}
    except Exception as exc:
        return {"type": "error", "error": f"Cannot read file as text: {exc}"}


@app.post("/chat")
async def chat(
    question: str = Form(...),
    model: str = Form(default=DEFAULT_MODEL),
    history: str = Form(default="[]"),
    file_context: str = Form(default=""),
    file_name: str = Form(default=""),
    image_b64: str = Form(default=""),
):
    prev = json.loads(history)

    if image_b64:
        # Vision: attach image to the user turn
        messages = [*prev, {"role": "user", "content": question, "images": [image_b64]}]
    elif file_context:
        # Text file: inject as system context so it persists across turns
        system = {
            "role": "system",
            "content": (
                f"The user has uploaded a file named '{file_name}'.\n\n"
                f"File content (may be truncated at {MAX_CTX_CHARS} chars):\n"
                f"```\n{file_context[:MAX_CTX_CHARS]}\n```\n\n"
                "Use this content to answer the user's questions accurately."
            ),
        }
        messages = [system, *prev, {"role": "user", "content": question}]
    else:
        messages = [*prev, {"role": "user", "content": question}]

    payload = {"model": model, "messages": messages, "stream": True}

    async def generate():
        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
                async with client.stream(
                    "POST", f"{OLLAMA_BASE_URL}/api/chat", json=payload
                ) as resp:
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        chunk = json.loads(line)
                        content = chunk.get("message", {}).get("content", "")
                        done = chunk.get("done", False)
                        yield f"data: {json.dumps({'content': content, 'done': done})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'content': f'\\n\\n[Error: {exc}]', 'done': True})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
