#!/usr/bin/env python3
"""
Notemaker — TC Logger + AAF Generator para Pro Tools.
Corre en el navegador via localhost:8765.

USO:
  python3 notemaker.py   (o alias: notemaker)

DEPENDENCIAS:
  pip3 install pyaaf2
"""

import sys, re, struct, json, threading, webbrowser, tempfile, os
from fractions import Fraction
from pathlib import Path
from io import BytesIO
from datetime import date
from http.server import HTTPServer, BaseHTTPRequestHandler

try:
    import aaf2
except ImportError:
    import subprocess
    print("Instalando pyaaf2...")
    subprocess.run([sys.executable, '-m', 'pip', 'install', 'pyaaf2'])
    import aaf2

PORT        = int(os.environ.get('PORT', 8765))
SAMPLE_RATE = 48000
EDIT_RATE   = Fraction(SAMPLE_RATE, 1)
TC_RE       = re.compile(r'(\d{1,2}):(\d{2}):(\d{2})(?:[:;]\d{1,2})?')

FPS_TABLE = {
    '23.976': Fraction(1001, 1000),
    '24':     Fraction(1, 1),
    '25':     Fraction(1, 1),
    '29.97':  Fraction(1001, 1000),
    '30':     Fraction(1, 1),
    '48':     Fraction(1, 1),
    '50':     Fraction(1, 1),
    '59.94':  Fraction(1001, 1000),
    '60':     Fraction(1, 1),
}

# ── Lógica ────────────────────────────────────────────────────────────────────
def tc_to_real(h, m, s, multiplier, offset):
    return float((int(h)*3600 + int(m)*60 + int(s)) * multiplier) - offset

def parse_start(start_str):
    parts = re.split(r'[:;]', start_str.strip())
    if len(parts) < 3: raise ValueError(f"Start TC inválido: {start_str}")
    return int(parts[0])*3600 + int(parts[1])*60 + int(parts[2])

def parse_notes(text, default_dur, fps_str, start_str):
    if fps_str not in FPS_TABLE:
        raise ValueError(f"FPS no reconocido: {fps_str}")
    multiplier = FPS_TABLE[fps_str]
    offset     = float(parse_start(start_str) * multiplier)
    regions    = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'): continue
        matches = list(TC_RE.finditer(line))
        if len(matches) >= 2:
            start = tc_to_real(*matches[0].groups()[:3], multiplier, offset)
            end   = tc_to_real(*matches[1].groups()[:3], multiplier, offset)
            dur   = max(end - start, 1.0)
            name  = line[matches[1].end():].strip().lstrip('- ').strip()
        elif len(matches) == 1:
            start = tc_to_real(*matches[0].groups()[:3], multiplier, offset)
            dur   = float(default_dur)
            name  = line[matches[0].end():].strip().lstrip('- ').strip()
        else:
            continue
        if not name: name = f"Region_{int(start):06d}"
        if start < 0: continue
        regions.append((name, start, dur))
    return sorted(regions, key=lambda r: r[1])

def silence_wav(dur_sec):
    n = int(round(dur_sec * SAMPLE_RATE))
    dsize = n * 2
    buf = BytesIO()
    buf.write(b'RIFF'); buf.write(struct.pack('<I', 36 + dsize))
    buf.write(b'WAVE')
    buf.write(b'fmt '); buf.write(struct.pack('<I', 16))
    buf.write(struct.pack('<HHIIHH', 1, 1, SAMPLE_RATE, SAMPLE_RATE*2, 2, 16))
    buf.write(b'data'); buf.write(struct.pack('<I', dsize))
    buf.write(b'\x00' * dsize)
    return buf.getvalue()

def sr(sec): return int(round(sec * SAMPLE_RATE))

def build_aaf_bytes(regions, seq_name):
    with tempfile.NamedTemporaryFile(suffix='.aaf', delete=False) as tmp:
        tmp_path = tmp.name
    try:
        total = sr(regions[-1][1] + regions[-1][2] + 2.0)
        with aaf2.open(tmp_path, 'w') as f:
            comp = f.create.CompositionMob()
            comp.name = seq_name
            f.content.mobs.append(comp)
            tslot = comp.create_timeline_slot(edit_rate=EDIT_RATE)
            tslot.name = "A1"; tslot.slot_id = 1
            seq = f.create.Sequence(media_kind='sound')
            components = []; cursor = 0
            for name, start_sec, dur_sec in regions:
                ss, ds = sr(start_sec), sr(dur_sec)
                if ss > cursor:
                    components.append(f.create.Filler(media_kind='sound', length=ss - cursor))
                src_mob = f.create.SourceMob()
                src_mob.name = name
                desc = f.create.PCMDescriptor()
                desc['SampleRate'].value        = EDIT_RATE
                desc['AudioSamplingRate'].value = EDIT_RATE
                desc['Channels'].value          = 1
                desc['QuantizationBits'].value  = 16
                desc['BlockAlign'].value        = 2
                desc['AverageBPS'].value        = SAMPLE_RATE * 2
                desc['Length'].value            = ds
                src_mob['EssenceDescription'].value = desc
                f.content.mobs.append(src_mob)
                wav = silence_wav(dur_sec)
                result = src_mob.create_essence(1, 'sound', 'wave', EDIT_RATE)
                ess = result[0] if isinstance(result, tuple) else result
                if hasattr(ess, 'write'): ess.write(wav)
                if hasattr(ess, 'close'): ess.close()
                master = f.create.MasterMob()
                master.name = name
                mslot = master.create_timeline_slot(edit_rate=EDIT_RATE)
                mslot.slot_id = 1
                mc = f.create.SourceClip(media_kind='sound', length=ds)
                mc['SourceID'].value        = src_mob.mob_id
                mc['SourceMobSlotID'].value = 1
                mc['StartTime'].value       = 0
                mslot.segment = mc
                f.content.mobs.append(master)
                clip = f.create.SourceClip(media_kind='sound', length=ds)
                clip['SourceID'].value        = master.mob_id
                clip['SourceMobSlotID'].value = 1
                clip['StartTime'].value       = 0
                components.append(clip); cursor = ss + ds
            if cursor < total:
                components.append(f.create.Filler(media_kind='sound', length=total - cursor))
            seq.components.extend(components)
            tslot.segment = seq
        with open(tmp_path, 'rb') as f:
            return f.read()
    finally:
        os.unlink(tmp_path)

# ── HTML ──────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hasan Notemaker</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg:      #0f0f0f;
  --surface: #181818;
  --border:  #2a2a2a;
  --amber:   #f5a623;
  --amber2:  #ffcc66;
  --fg:      #e8e8e8;
  --fg2:     #888;
  --green:   #4caf7d;
  --red:     #e05252;
  --blue:    #4a9eff;
  --mono:    'IBM Plex Mono', monospace;
  --sans:    'IBM Plex Sans', sans-serif;
}
body {
  background: var(--bg);
  color: var(--fg);
  font-family: var(--sans);
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 32px 20px 60px;
}
header {
  width: 100%; max-width: 700px;
  display: flex; align-items: baseline; gap: 16px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 18px; margin-bottom: 28px;
}
header h1 { font-family: var(--mono); font-size: 26px; color: var(--amber); }
header span { font-size: 13px; color: var(--fg2); font-weight: 300; }

/* Tabs */
.tabs {
  width: 100%; max-width: 700px;
  display: flex; gap: 2px; margin-bottom: 20px;
}
.tab {
  padding: 9px 22px;
  font-family: var(--mono); font-size: 12px; font-weight: 500;
  letter-spacing: 0.8px; text-transform: uppercase;
  border: 1px solid var(--border); border-radius: 3px;
  cursor: pointer; background: var(--surface); color: var(--fg2);
  transition: all 0.15s;
}
.tab.active { background: var(--amber); color: #000; border-color: var(--amber); }
.tab:not(.active):hover { color: var(--fg); border-color: #444; }

/* Panels */
.panel { display: none; width: 100%; max-width: 700px; }
.panel.active { display: block; }

/* Card */
.card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 4px; margin-bottom: 14px; overflow: hidden;
}
.card-header {
  padding: 11px 18px; border-bottom: 1px solid var(--border);
  font-family: var(--mono); font-size: 10px; font-weight: 500;
  color: var(--fg2); letter-spacing: 1.5px; text-transform: uppercase;
}
.card-body { padding: 18px; }

/* Fields */
.config-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 14px; }
.field label {
  display: block; font-size: 10px; font-weight: 500; color: var(--fg2);
  letter-spacing: 0.8px; text-transform: uppercase; margin-bottom: 7px;
  font-family: var(--mono);
}
.field select, .field input[type=text], .field input[type=number] {
  width: 100%; background: var(--bg); border: 1px solid var(--border);
  border-radius: 3px; color: var(--fg); font-family: var(--mono);
  font-size: 14px; padding: 8px 11px; outline: none;
  transition: border-color 0.15s; appearance: none; -webkit-appearance: none;
}
.field select {
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%23888' stroke-width='1.5' fill='none' stroke-linecap='round'/%3E%3C/svg%3E");
  background-repeat: no-repeat; background-position: right 10px center; padding-right: 30px; cursor: pointer;
}
.field select:focus, .field input:focus { border-color: var(--amber); }
.field-full { margin-bottom: 14px; }
.field-full input {
  width: 100%; background: var(--bg); border: 1px solid var(--border);
  border-radius: 3px; color: var(--fg); font-family: var(--mono);
  font-size: 14px; padding: 8px 11px; outline: none; transition: border-color 0.15s;
}
.field-full input:focus { border-color: var(--amber); }

/* Textarea */
textarea {
  width: 100%; background: var(--bg); border: 1px solid var(--border);
  border-radius: 3px; color: var(--fg); font-family: var(--mono);
  font-size: 13px; line-height: 1.8; padding: 12px 14px;
  resize: vertical; min-height: 220px; outline: none; transition: border-color 0.15s;
}
textarea:focus { border-color: var(--amber); }
.hint { font-size: 11px; color: var(--fg2); font-family: var(--mono); margin-top: 9px; line-height: 2; }
.hint code { color: var(--amber2); background: rgba(245,166,35,0.08); padding: 1px 5px; border-radius: 2px; }

/* Preview list */
.preview-list { list-style: none; }
.preview-list li {
  display: flex; align-items: center; gap: 14px;
  padding: 9px 18px; border-bottom: 1px solid var(--border); font-size: 13px;
}
.preview-list li:last-child { border-bottom: none; }
.tc { font-family: var(--mono); color: var(--amber); font-size: 13px; min-width: 75px; }
.dur { font-family: var(--mono); color: var(--fg2); font-size: 11px; min-width: 48px; }
.rname { color: var(--fg); flex: 1; }

/* Acciones */
.actions { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin-top: 4px; }
#status, #status2 { font-family: var(--mono); font-size: 12px; color: var(--fg2); flex: 1; }
.ok  { color: var(--green) !important; }
.err { color: var(--red)   !important; }

/* Botones */
.btn {
  border: none; border-radius: 3px; font-family: var(--mono); font-size: 13px;
  font-weight: 500; padding: 11px 24px; cursor: pointer;
  transition: background 0.15s, transform 0.1s; white-space: nowrap;
}
.btn:active { transform: scale(0.98); }
.btn:disabled { background: #2a2a2a !important; color: #555 !important; cursor: not-allowed; transform: none; }
.btn-primary  { background: var(--amber); color: #000; }
.btn-primary:hover:not(:disabled)  { background: var(--amber2); }
.btn-danger   { background: var(--red); color: #fff; }
.btn-danger:hover:not(:disabled)   { background: #f06060; }
.btn-ghost    { background: transparent; color: var(--fg2); border: 1px solid var(--border); }
.btn-ghost:hover:not(:disabled)    { color: var(--fg); border-color: #555; }

/* ── LOGGER ── */
.clock-display {
  text-align: center; padding: 32px 20px 24px;
  font-family: var(--mono); font-size: 56px; font-weight: 500;
  color: var(--amber); letter-spacing: 4px;
  border-bottom: 1px solid var(--border);
  transition: color 0.2s;
}
.clock-display.running { color: var(--green); }
.clock-display.stopped { color: var(--fg2); }

.clock-controls {
  display: flex; gap: 10px; padding: 16px 18px;
  border-bottom: 1px solid var(--border); align-items: center;
}
.clock-hint { font-size: 11px; color: var(--fg2); font-family: var(--mono); margin-left: auto; }

/* Input de nota en tiempo real */
.note-input-wrap {
  padding: 14px 18px; border-bottom: 1px solid var(--border);
  display: flex; gap: 10px; align-items: center;
}
.note-input-wrap input {
  flex: 1; background: var(--bg); border: 1px solid var(--border);
  border-radius: 3px; color: var(--fg); font-family: var(--mono);
  font-size: 15px; padding: 10px 14px; outline: none;
  transition: border-color 0.15s;
}
.note-input-wrap input:focus { border-color: var(--amber); }
.note-input-wrap input:disabled { opacity: 0.3; }
.enter-hint { font-size: 11px; color: var(--fg2); font-family: var(--mono); white-space: nowrap; }

/* Lista de notas capturadas */
.log-list { list-style: none; max-height: 280px; overflow-y: auto; }
.log-list li {
  display: flex; align-items: center; gap: 14px;
  padding: 9px 18px; border-bottom: 1px solid var(--border);
  font-size: 13px; animation: fadeIn 0.2s ease;
}
.log-list li:last-child { border-bottom: none; }
@keyframes fadeIn { from { opacity:0; transform: translateY(-4px); } to { opacity:1; transform: none; } }
.log-tc { font-family: var(--mono); color: var(--amber); font-size: 13px; min-width: 90px; }
.log-name { color: var(--fg); flex: 1; }
.log-del {
  background: none; border: none; color: #444; cursor: pointer;
  font-size: 16px; padding: 0 4px; transition: color 0.15s;
}
.log-del:hover { color: var(--red); }
.log-empty { padding: 24px 18px; text-align: center; color: var(--fg2); font-family: var(--mono); font-size: 12px; }

/* Flash en captura */
@keyframes flash { 0%{background:rgba(245,166,35,0.15)} 100%{background:transparent} }
.flash { animation: flash 0.4s ease; }
</style>
</head>
<body>

<header>
  <h1>hasan notemaker</h1>
  <span>TC Logger + AAF Generator for Pro Tools — Hasan Estudio</span>
</header>

<!-- Tabs -->
<div class="tabs">
  <div class="tab active" onclick="switchTab('logger')">① Logger en vivo</div>
  <div class="tab" onclick="switchTab('editor')">② Editor + Export</div>
</div>

<!-- ══ PANEL: LOGGER ══════════════════════════════════════════════════════ -->
<div class="panel active" id="panel-logger">

  <!-- Config -->
  <div class="card">
    <div class="card-header">Configuración</div>
    <div class="card-body">
      <div class="field-full">
        <label style="display:block;font-size:10px;font-weight:500;color:var(--fg2);letter-spacing:.8px;text-transform:uppercase;margin-bottom:7px;font-family:var(--mono)">Nombre del proyecto</label>
        <input type="text" id="l-filename" placeholder="El Eternauta">
      </div>
      <div class="config-grid">
        <div class="field">
          <label>Frame rate</label>
          <select id="l-fps">
            <option value="23.976" selected>23.976</option>
            <option value="24">24</option>
            <option value="25">25</option>
            <option value="29.97">29.97</option>
            <option value="30">30</option>
            <option value="48">48</option>
            <option value="50">50</option>
            <option value="59.94">59.94</option>
            <option value="60">60</option>
          </select>
        </div>
        <div class="field">
          <label>Start TC</label>
          <input type="text" id="l-start" value="00:00:00:00" placeholder="01:00:00:00">
        </div>
        <div class="field">
          <label>Dur. default (s)</label>
          <input type="number" id="l-dur" value="5" min="1" max="300">
        </div>
      </div>
    </div>
  </div>

  <!-- Reloj -->
  <div class="card">
    <div class="clock-display stopped" id="clock">00:00:00:00</div>
    <div class="clock-controls">
      <button class="btn btn-primary" id="btn-start" onclick="startClock()">▶ Start</button>
      <button class="btn btn-ghost" id="btn-stop" onclick="stopClock()" disabled>■ Stop</button>
      <button class="btn btn-ghost" id="btn-reset" onclick="resetClock()">↺ Reset</button>
      <span class="clock-hint" id="clock-hint">Configura el TC y dale Start cuando empiece la proyección</span>
    </div>
    <div class="note-input-wrap">
      <input type="text" id="note-input" placeholder="Escribe la nota y presiona Enter para capturar el TC..." disabled
             onkeydown="handleNoteKey(event)">
      <span class="enter-hint" id="note-capture-hint" style="font-size:11px;color:var(--fg2);font-family:var(--mono);max-width:300px;text-align:right">Shift+Enter para capturar TC → escribe la nota → Enter para guardar</span>
    </div>
  </div>

  <!-- Log de notas capturadas -->
  <div class="card">
    <div class="card-header" id="log-header">Notas capturadas</div>
    <div id="log-empty" class="log-empty">Ninguna nota aún — dale Start y empieza a capturar.</div>
    <ul class="log-list" id="log-list"></ul>
  </div>

  <!-- Acciones -->
  <div class="actions">
    <span id="status2"></span>
    <button class="btn btn-ghost" onclick="clearLog()" style="margin-right:auto">Limpiar</button>
    <button class="btn btn-primary" onclick="generateFromLog()">Generar AAF →</button>
  </div>
</div>

<!-- ══ PANEL: EDITOR ══════════════════════════════════════════════════════ -->
<div class="panel" id="panel-editor">

  <div class="card">
    <div class="card-header">Proyecto</div>
    <div class="card-body">
      <div class="field-full">
        <label style="display:block;font-size:10px;font-weight:500;color:var(--fg2);letter-spacing:.8px;text-transform:uppercase;margin-bottom:7px;font-family:var(--mono)">Nombre del proyecto</label>
        <input type="text" id="e-filename" placeholder="El Eternauta">
      </div>
      <div class="config-grid">
        <div class="field">
          <label>Frame rate</label>
          <select id="e-fps">
            <option value="23.976" selected>23.976</option>
            <option value="24">24</option>
            <option value="25">25</option>
            <option value="29.97">29.97</option>
            <option value="30">30</option>
            <option value="48">48</option>
            <option value="50">50</option>
            <option value="59.94">59.94</option>
            <option value="60">60</option>
          </select>
        </div>
        <div class="field">
          <label>Start TC</label>
          <input type="text" id="e-start" value="00:00:00:00">
        </div>
        <div class="field">
          <label>Dur. default (s)</label>
          <input type="number" id="e-dur" value="5" min="1">
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-header">Notas</div>
    <div class="card-body">
      <textarea id="e-notes" placeholder="01:00:10 Explosion coche&#10;01:00:25 - 01:01:05 Ambiente ciudad">01:00:10 Explosion coche
01:00:25 - 01:01:05 Ambiente ciudad
01:01:15 Foley pasos</textarea>
      <div class="hint">
        <code>HH:MM:SS Nombre</code> → dur. default &nbsp;|&nbsp;
        <code>HH:MM:SS - HH:MM:SS Nombre</code> → dur. exacta &nbsp;|&nbsp;
        <code># comentario</code> → ignorado
      </div>
    </div>
  </div>

  <div id="e-preview" style="display:none">
    <div class="card">
      <div class="card-header" id="e-preview-title">Regiones</div>
      <ul class="preview-list" id="e-preview-list"></ul>
    </div>
  </div>

  <div class="actions">
    <span id="status"></span>
    <button class="btn btn-primary" onclick="generateFromEditor()">Generar AAF →</button>
  </div>
</div>

<script>
// ── Tabs ──────────────────────────────────────────────────────────────────────
function switchTab(tab) {
  document.querySelectorAll('.tab').forEach((t,i) => {
    t.classList.toggle('active', (i===0&&tab==='logger')||(i===1&&tab==='editor'));
  });
  document.getElementById('panel-logger').classList.toggle('active', tab==='logger');
  document.getElementById('panel-editor').classList.toggle('active', tab==='editor');
  if (tab==='editor') syncEditorFromLog();
}

// ── Reloj TC ──────────────────────────────────────────────────────────────────
let clockInterval = null;
let elapsedMs     = 0;
let lastTick      = null;
let clockRunning  = false;

const FPS_MULT = {
  '23.976': 1000/1000, '24': 1, '25': 1,
  '29.97': 1000/1000,  '30': 1, '48': 1,
  '50': 1, '59.94': 1000/1000, '60': 1
};

function getStartOffsetMs() {
  const start = document.getElementById('l-start').value;
  const parts = start.split(/[:;]/);
  const h=parseInt(parts[0]||0), m=parseInt(parts[1]||0), s=parseInt(parts[2]||0);
  return (h*3600 + m*60 + s) * 1000;
}

function msToTC(ms) {
  const fps    = parseFloat(document.getElementById('l-fps').value);
  const totalS = ms / 1000;
  const h  = Math.floor(totalS / 3600);
  const m  = Math.floor((totalS % 3600) / 60);
  const s  = Math.floor(totalS % 60);
  const fr = Math.floor((totalS % 1) * fps);
  return `${pad(h)}:${pad(m)}:${pad(s)}:${pad(fr)}`;
}

function pad(n) { return String(Math.floor(n)).padStart(2,'0'); }

function updateClock() {
  if (!clockRunning) return;
  const now  = Date.now();
  elapsedMs += now - lastTick;
  lastTick   = now;
  const displayMs = getStartOffsetMs() + elapsedMs;
  document.getElementById('clock').textContent = msToTC(displayMs);
}

function startClock() {
  if (clockRunning) return;
  clockRunning = true;
  lastTick     = Date.now();
  clockInterval = setInterval(updateClock, 50);
  document.getElementById('clock').className      = 'clock-display running';
  document.getElementById('btn-start').disabled   = true;
  document.getElementById('btn-stop').disabled    = false;
  document.getElementById('note-input').disabled  = false;
  document.getElementById('note-input').focus();
  document.getElementById('clock-hint').textContent = 'Escribe la nota y presiona Enter para capturar el TC';
}

function stopClock() {
  if (!clockRunning) return;
  clockRunning = false;
  clearInterval(clockInterval);
  document.getElementById('clock').className      = 'clock-display stopped';
  document.getElementById('btn-start').disabled   = false;
  document.getElementById('btn-stop').disabled    = true;
  document.getElementById('note-input').disabled  = true;
  document.getElementById('clock-hint').textContent = 'Pausado — dale Start para continuar';
}

function resetClock() {
  stopClock();
  elapsedMs = 0;
  const startMs = getStartOffsetMs();
  document.getElementById('clock').textContent   = msToTC(startMs);
  document.getElementById('clock').className     = 'clock-display stopped';
  document.getElementById('clock-hint').textContent = 'Configura el TC y dale Start cuando empiece la proyección';
}

// ── Captura de notas ───────────────────────────────────────────────────────
let logEntries = []; // [{tc_str, elapsed_ms, name}]

let capturedTC = null; // TC capturado con Shift+Enter

function handleNoteKey(e) {
  // Shift+Enter → captura TC ahora
  if (e.key === 'Enter' && e.shiftKey) {
    e.preventDefault();
    const now          = Date.now();
    const extra        = clockRunning ? (now - lastTick) : 0;
    const totalElapsed = elapsedMs + extra;
    const displayMs    = getStartOffsetMs() + totalElapsed;
    capturedTC = { tc_str: msToTC(displayMs), elapsed_ms: totalElapsed };

    // Feedback visual en el hint
    document.getElementById('note-capture-hint').textContent = `TC capturado: ${capturedTC.tc_str} — escribe la nota y presiona Enter`;
    document.getElementById('note-capture-hint').style.color = 'var(--amber)';
    return;
  }

  // Enter solo → guardar nota con TC capturado
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    const input = document.getElementById('note-input');
    const name  = input.value.trim();
    if (!name) return;

    // Si no hay TC capturado, usar el TC actual
    if (!capturedTC) {
      const now          = Date.now();
      const extra        = clockRunning ? (now - lastTick) : 0;
      const totalElapsed = elapsedMs + extra;
      const displayMs    = getStartOffsetMs() + totalElapsed;
      capturedTC = { tc_str: msToTC(displayMs), elapsed_ms: totalElapsed };
    }

    logEntries.push({ tc_str: capturedTC.tc_str, elapsed_ms: capturedTC.elapsed_ms, name });
    input.value = '';
    capturedTC  = null;
    document.getElementById('note-capture-hint').textContent = 'Shift+Enter para capturar TC → escribe la nota → Enter para guardar';
    document.getElementById('note-capture-hint').style.color = 'var(--fg2)';
    renderLog();

    // Flash
    const row = document.getElementById('log-list').lastElementChild;
    if (row) row.classList.add('flash');
  }
}

function renderLog() {
  const list   = document.getElementById('log-list');
  const empty  = document.getElementById('log-empty');
  const header = document.getElementById('log-header');
  list.innerHTML = '';
  if (logEntries.length === 0) {
    empty.style.display  = 'block';
    header.textContent   = 'Notas capturadas';
    return;
  }
  empty.style.display = 'none';
  header.textContent  = `Notas capturadas (${logEntries.length})`;
  logEntries.forEach((e, i) => {
    const li = document.createElement('li');
    li.innerHTML = `
      <span class="log-tc">${e.tc_str}</span>
      <span class="log-name">${e.name}</span>
      <button class="log-del" onclick="deleteEntry(${i})" title="Eliminar">×</button>
    `;
    list.appendChild(li);
  });
}

function deleteEntry(i) {
  logEntries.splice(i, 1);
  renderLog();
}

function clearLog() {
  if (logEntries.length && !confirm('¿Limpiar todas las notas?')) return;
  logEntries = [];
  renderLog();
}

// Convierte el log a texto plano para el editor
function logToText() {
  return logEntries.map(e => {
    // tc_str es HH:MM:SS:FF — lo convertimos a HH:MM:SS
    const parts = e.tc_str.split(':');
    const hms   = parts.slice(0,3).join(':');
    return `${hms} ${e.name}`;
  }).join('\n');
}

function syncEditorFromLog() {
  if (logEntries.length === 0) return;
  document.getElementById('e-notes').value  = logToText();
  document.getElementById('e-fps').value    = document.getElementById('l-fps').value;
  document.getElementById('e-start').value  = document.getElementById('l-start').value;
  document.getElementById('e-dur').value    = document.getElementById('l-dur').value;
  const name = document.getElementById('l-filename').value;
  if (name) document.getElementById('e-filename').value = name;
}

// ── Generar AAF ───────────────────────────────────────────────────────────────
function fmtSec(sec) {
  const h=Math.floor(sec/3600), r=sec%3600, m=Math.floor(r/60), s=Math.floor(r%60);
  return `${pad(h)}:${pad(m)}:${pad(s)}`;
}

async function doGenerate(payload, statusEl) {
  statusEl.textContent = 'Generando...'; statusEl.className = '';
  try {
    const res = await fetch('/generate', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(payload)
    });
    if (!res.ok) { const err = await res.json(); throw new Error(err.error); }
    const regions = JSON.parse(res.headers.get('X-Regions') || '[]');
    const fname   = res.headers.get('X-Filename') || 'notas.aaf';
    const blob    = await res.blob();
    const url     = URL.createObjectURL(blob);
    const a       = document.createElement('a');
    a.href = url; a.download = fname; a.click();
    URL.revokeObjectURL(url);
    statusEl.textContent = `✓ ${fname} — ${regions.length} región(es)`;
    statusEl.className   = 'ok';
    return regions;
  } catch(e) {
    statusEl.textContent = `✗ ${e.message}`;
    statusEl.className   = 'err';
    return null;
  }
}

async function generateFromLog() {
  if (logEntries.length === 0) {
    document.getElementById('status2').textContent = '✗ No hay notas capturadas';
    document.getElementById('status2').className   = 'err';
    return;
  }
  const payload = {
    fps:      document.getElementById('l-fps').value,
    start:    document.getElementById('l-start').value,
    dur:      document.getElementById('l-dur').value,
    notes:    logToText(),
    filename: document.getElementById('l-filename').value.trim() || 'notas'
  };
  await doGenerate(payload, document.getElementById('status2'));
}

async function generateFromEditor() {
  const regions = await doGenerate({
    fps:      document.getElementById('e-fps').value,
    start:    document.getElementById('e-start').value,
    dur:      document.getElementById('e-dur').value,
    notes:    document.getElementById('e-notes').value,
    filename: document.getElementById('e-filename').value.trim() || 'notas'
  }, document.getElementById('status'));

  if (regions && regions.length) {
    const list  = document.getElementById('e-preview-list');
    const title = document.getElementById('e-preview-title');
    title.textContent = `${regions.length} región${regions.length>1?'es':''} generada${regions.length>1?'s':''}`;
    list.innerHTML = regions.map(r =>
      `<li><span class="tc">${fmtSec(Math.round(r.start))}</span>
           <span class="dur">${r.dur.toFixed(1)}s</span>
           <span class="rname">${r.name}</span></li>`
    ).join('');
    document.getElementById('e-preview').style.display = 'block';
  }
}
</script>
</body>
</html>"""

# ── Servidor ──────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(HTML.encode('utf-8'))

    def do_POST(self):
        if self.path != '/generate':
            self.send_error(404); return
        length = int(self.headers.get('Content-Length', 0))
        body   = json.loads(self.rfile.read(length))
        fps_str   = body.get('fps', '23.976')
        start_str = body.get('start', '00:00:00:00')
        notes_txt = body.get('notes', '')
        filename  = body.get('filename', 'notas').strip() or 'notas'
        try: default_dur = float(body.get('dur', 5))
        except: default_dur = 5.0

        today    = date.today().strftime('%Y-%m-%d')
        aaf_name = f"{filename}_{today}.aaf"
        seq_name = f"{filename} — {today}"

        try:
            regions   = parse_notes(notes_txt, default_dur, fps_str, start_str)
            if not regions: raise ValueError("No se encontraron notas con timecode válido.")
            aaf_bytes = build_aaf_bytes(regions, seq_name)
        except Exception as e:
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())
            return

        rjson = json.dumps([{'name':r[0],'start':r[1],'dur':r[2]} for r in regions])
        self.send_response(200)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Disposition', f'attachment; filename="{aaf_name}"')
        self.send_header('Content-Length', str(len(aaf_bytes)))
        self.send_header('X-Filename', aaf_name)
        self.send_header('X-Regions', rjson)
        self.send_header('Access-Control-Expose-Headers', 'X-Filename, X-Regions')
        self.end_headers()
        self.wfile.write(aaf_bytes)

def main():
    server = HTTPServer(('0.0.0.0', PORT), Handler)
    url    = f'http://localhost:{PORT}'
    print(f"\n🎬 Notemaker corriendo en {url}")
    print(f"   Ctrl+C para cerrar.\n")
    if os.environ.get('RAILWAY_ENVIRONMENT') is None:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: print("\nNotemaker cerrado.")

if __name__ == '__main__':
    main()
