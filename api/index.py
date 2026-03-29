#!/usr/bin/env python3
"""
Notemaker — TC Logger + AAF Generator para Pro Tools.
Vercel Serverless (Flask).
"""

import re, struct, json, tempfile, os, time
from fractions import Fraction
from io import BytesIO
from datetime import date

import aaf2
from flask import Flask, request, Response
from upstash_redis import Redis

def get_redis():
    url   = os.environ.get('KV_REST_API_URL')
    token = os.environ.get('KV_REST_API_TOKEN')
    if not url or not token: return None
    return Redis(url=url, token=token)

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

def minimal_wav():
    """WAV header con 0 muestras — mantiene el AAF liviano."""
    buf = BytesIO()
    buf.write(b'RIFF'); buf.write(struct.pack('<I', 36))
    buf.write(b'WAVE')
    buf.write(b'fmt '); buf.write(struct.pack('<I', 16))
    buf.write(struct.pack('<HHIIHH', 1, 1, SAMPLE_RATE, SAMPLE_RATE*2, 2, 16))
    buf.write(b'data'); buf.write(struct.pack('<I', 0))
    return buf.getvalue()

def sr(sec): return int(round(sec * SAMPLE_RATE))

def build_aaf_bytes(regions, seq_name):
    with tempfile.NamedTemporaryFile(suffix='.aaf', delete=False, dir='/tmp') as tmp:
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
                wav = minimal_wav()
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
            data = f.read()
        return data
    finally:
        os.unlink(tmp_path)

# ── HTML ──────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hasan Notemaker</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600&family=DM+Serif+Display:ital@0;1&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg:          #F6F4F0;
  --surface:     #FFFFFF;
  --border:      #E4E4E4;
  --border-soft: rgba(245,166,35,0.18);
  --amber:       #F5A623;
  --amber2:      #D9880A;
  --amber-pale:  #FEF5E7;
  --fg:          #1C1C1C;
  --fg2:         #8A8A8A;
  --green:       #4caf7d;
  --red:         #e05252;
  --mono:        'IBM Plex Mono', monospace;
  --sans:        'DM Sans', sans-serif;
  --serif:       'DM Serif Display', serif;
}
body {
  background: var(--bg); color: var(--fg); font-family: var(--sans);
  min-height: 100vh; display: flex; flex-direction: column;
  align-items: center; padding: 0 12px 60px;
}
header {
  width: 100%; max-width: 700px;
  display: flex; align-items: baseline; gap: 12px;
  border-bottom: 2.5px solid var(--amber);
  padding: 18px 0 14px; margin-bottom: 20px;
  flex-wrap: wrap;
}
header h1 { font-family: var(--serif); font-style: italic; font-size: 24px; color: var(--fg); }
header span { font-size: 12px; color: var(--fg2); font-weight: 300; }

/* Tabs */
.tabs { width: 100%; max-width: 700px; display: flex; gap: 6px; margin-bottom: 16px; }
.tab {
  flex: 1; padding: 9px 10px; text-align: center;
  font-family: var(--sans); font-size: 12px; font-weight: 500;
  border: 1.5px solid var(--border); border-radius: 8px;
  cursor: pointer; background: var(--surface); color: var(--fg2); transition: all 0.15s;
}
.tab.active { background: var(--amber); color: #fff; border-color: var(--amber); }
.tab:not(.active):hover { color: var(--fg); border-color: #bbb; }

/* Panels */
.panel { display: none; width: 100%; max-width: 700px; }
.panel.active { display: block; }

/* Card */
.card {
  background: var(--surface); border: 1px solid var(--border-soft);
  border-radius: 10px; margin-bottom: 12px; overflow: hidden;
  box-shadow: 0 2px 18px rgba(0,0,0,0.07);
}
.card-header {
  padding: 10px 16px; border-bottom: 1px solid var(--border);
  font-family: var(--sans); font-size: 10px; font-weight: 600;
  color: var(--fg2); letter-spacing: 1.2px; text-transform: uppercase;
}
.card-body { padding: 14px 16px; }

/* Config grid */
.config-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; }
@media (max-width: 480px) { .config-grid { grid-template-columns: 1fr 1fr; } }
.field label {
  display: block; font-size: 10px; font-weight: 600; color: var(--fg2);
  letter-spacing: 0.8px; text-transform: uppercase; margin-bottom: 6px; font-family: var(--sans);
}
.field select, .field input[type=text], .field input[type=number] {
  width: 100%; background: var(--surface); border: 1.5px solid var(--border);
  border-radius: 7px; color: var(--fg); font-family: var(--mono);
  font-size: 14px; padding: 8px 10px; outline: none;
  transition: border-color 0.15s, box-shadow 0.15s;
  appearance: none; -webkit-appearance: none;
}
@media (max-width: 480px) {
  .field select, .field input[type=text], .field input[type=number] { font-size: 16px; }
}
.field select {
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%238A8A8A' stroke-width='1.5' fill='none' stroke-linecap='round'/%3E%3C/svg%3E");
  background-repeat: no-repeat; background-position: right 10px center; padding-right: 28px; cursor: pointer;
}
.field select:focus, .field input:focus { border-color: var(--amber); box-shadow: 0 0 0 3px rgba(245,166,35,0.12); }
.field-full { margin-bottom: 12px; }
.field-full input {
  width: 100%; background: var(--surface); border: 1.5px solid var(--border);
  border-radius: 7px; color: var(--fg); font-family: var(--sans);
  font-size: 14px; padding: 8px 10px; outline: none;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.field-full input:focus { border-color: var(--amber); box-shadow: 0 0 0 3px rgba(245,166,35,0.12); }

/* Textarea */
textarea {
  width: 100%; background: var(--surface); border: 1.5px solid var(--border);
  border-radius: 7px; color: var(--fg); font-family: var(--mono);
  font-size: 13px; line-height: 1.8; padding: 12px 14px;
  resize: vertical; min-height: 200px; outline: none;
  transition: border-color 0.15s, box-shadow 0.15s;
}
textarea:focus { border-color: var(--amber); box-shadow: 0 0 0 3px rgba(245,166,35,0.12); }
.hint { font-size: 11px; color: var(--fg2); font-family: var(--mono); margin-top: 9px; line-height: 2; }
.hint code { color: var(--amber2); background: var(--amber-pale); padding: 1px 5px; border-radius: 4px; }

/* Preview list */
.preview-list { list-style: none; }
.preview-list li {
  display: flex; align-items: center; gap: 12px;
  padding: 8px 16px; border-bottom: 1px solid var(--border); font-size: 12px;
}
.preview-list li:last-child { border-bottom: none; }
.tc { font-family: var(--mono); color: var(--amber2); font-size: 12px; min-width: 70px; }
.dur { font-family: var(--mono); color: var(--fg2); font-size: 11px; min-width: 44px; }
.rname { color: var(--fg); flex: 1; font-size: 12px; }

/* Acciones */
.actions { display: flex; align-items: center; gap: 10px; margin-top: 4px; flex-wrap: wrap; }
.actions-left { display: flex; align-items: center; gap: 10px; flex: 1; min-width: 0; }
.actions-right { display: flex; gap: 8px; flex-shrink: 0; }
#status, #status2 { font-family: var(--mono); font-size: 11px; color: var(--fg2); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ok  { color: var(--green) !important; }
.err { color: var(--red)   !important; }

/* Botones */
.btn {
  border: none; border-radius: 8px; font-family: var(--sans); font-size: 13px;
  font-weight: 500; padding: 10px 20px; cursor: pointer;
  transition: background 0.15s, transform 0.1s; white-space: nowrap;
}
.btn:active { transform: scale(0.97); }
.btn:disabled { background: var(--border) !important; color: var(--fg2) !important; cursor: not-allowed; transform: none; }
.btn-primary { background: var(--amber); color: #fff; box-shadow: 0 3px 10px rgba(245,166,35,0.3); }
.btn-primary:hover:not(:disabled) { background: var(--amber2); }
.btn-pdf { background: var(--surface); color: var(--fg2); border: 1.5px solid var(--border); }
.btn-pdf:hover:not(:disabled) { color: var(--fg); border-color: #bbb; }
.btn-ghost { background: var(--surface); color: var(--fg2); border: 1.5px solid var(--border); }
.btn-ghost:hover:not(:disabled) { color: var(--fg); border-color: #bbb; }
.btn-danger { background: var(--red); color: #fff; }

/* ── RELOJ ── */
.clock-display {
  text-align: center; padding: 24px 16px 20px;
  font-family: var(--mono); font-weight: 500; color: var(--fg2);
  letter-spacing: 3px; border-bottom: 1px solid var(--border); transition: color 0.2s;
  font-size: clamp(28px, 8vw, 52px);
}
.clock-display.running { color: var(--green); }
.clock-display.stopped { color: var(--fg2); }

.clock-controls {
  display: flex; gap: 8px; padding: 12px 16px;
  border-bottom: 1px solid var(--border); align-items: center; flex-wrap: wrap;
}
.clock-hint { font-size: 11px; color: var(--fg2); font-family: var(--mono); margin-left: auto; text-align: right; }
@media (max-width: 480px) { .clock-hint { width: 100%; margin-left: 0; margin-top: 6px; } }

/* Sync wrap */
.sync-wrap {
  display: flex; align-items: center; gap: 8px; padding: 10px 16px;
  border-bottom: 1px solid var(--border); flex-wrap: wrap;
}
.sync-label { font-family: var(--sans); font-size: 10px; font-weight: 600; color: var(--fg2); text-transform: uppercase; letter-spacing: 0.8px; white-space: nowrap; }
.sync-wrap input {
  background: var(--surface); border: 1.5px solid var(--border); border-radius: 7px;
  color: var(--fg); font-family: var(--mono); font-size: 14px;
  padding: 7px 10px; outline: none; width: 130px;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.sync-wrap input:focus { border-color: var(--amber); box-shadow: 0 0 0 3px rgba(245,166,35,0.12); }
.btn-sync {
  background: var(--surface); color: var(--fg2); border: 1.5px solid var(--border);
  border-radius: 7px; font-family: var(--sans); font-size: 12px; font-weight: 500;
  padding: 7px 14px; cursor: pointer; transition: all 0.15s; white-space: nowrap;
}
.btn-sync:hover { color: var(--fg); border-color: #bbb; }
.btn-sync.synced { background: var(--green); color: #fff; border-color: var(--green); }
.sync-status { font-family: var(--mono); font-size: 11px; color: var(--fg2); }

/* MTC */
.mtc-wrap {
  display: flex; align-items: center; gap: 8px; padding: 10px 16px;
  border-bottom: 1px solid var(--border); flex-wrap: wrap;
}
.mtc-label { font-family: var(--sans); font-size: 10px; font-weight: 600; color: var(--fg2); text-transform: uppercase; letter-spacing: 0.8px; white-space: nowrap; }
.mtc-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: var(--border); border: 1px solid #bbb; transition: background 0.2s; }
.mtc-dot.live { background: var(--green); border-color: var(--green); }
.mtc-dot.idle { background: var(--amber); border-color: var(--amber); }
.mtc-dot.err  { background: var(--red);   border-color: var(--red); }
.mtc-status { font-family: var(--mono); font-size: 11px; color: var(--fg2); flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mtc-select {
  background: var(--surface); border: 1.5px solid var(--border); border-radius: 7px;
  color: var(--fg); font-family: var(--mono); font-size: 12px;
  padding: 6px 8px; outline: none; max-width: 200px; cursor: pointer;
  appearance: none; -webkit-appearance: none; transition: border-color 0.15s;
}
.mtc-select:focus { border-color: var(--amber); }
.mtc-select:disabled { opacity: 0.4; cursor: not-allowed; }

/* Input de nota */
.note-input-wrap {
  padding: 12px 16px; border-bottom: 1px solid var(--border);
  display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
}
.note-input-wrap input {
  flex: 1; min-width: 0; background: var(--surface); border: 1.5px solid var(--border);
  border-radius: 7px; color: var(--fg); font-family: var(--sans);
  font-size: 15px; padding: 10px 12px; outline: none;
  transition: border-color 0.15s, box-shadow 0.15s;
}
@media (max-width: 480px) { .note-input-wrap input { font-size: 16px; } }
.note-input-wrap input:focus { border-color: var(--amber); box-shadow: 0 0 0 3px rgba(245,166,35,0.12); }
.note-input-wrap input:disabled { opacity: 0.4; background: var(--bg); }

/* Botón capturar TC */
.btn-capture {
  background: var(--amber); color: #fff; border: none; border-radius: 7px;
  font-family: var(--sans); font-size: 12px; font-weight: 500;
  padding: 10px 14px; cursor: pointer; white-space: nowrap; flex-shrink: 0;
  transition: background 0.15s;
}
.btn-capture:hover { background: var(--amber2); }
.btn-capture:disabled { background: var(--border); color: var(--fg2); cursor: not-allowed; }
.btn-capture.captured { background: var(--green); color: #fff; }

/* Lista log */
.log-list { list-style: none; max-height: 260px; overflow-y: auto; }
.log-list li {
  display: flex; align-items: center; gap: 12px;
  padding: 8px 16px; border-bottom: 1px solid var(--border);
  font-size: 12px; animation: fadeIn 0.2s ease;
}
.log-list li:last-child { border-bottom: none; }
@keyframes fadeIn { from { opacity:0; transform: translateY(-3px); } to { opacity:1; transform: none; } }
.log-tc { font-family: var(--mono); color: var(--amber2); font-size: 12px; min-width: 80px; flex-shrink: 0; }
.log-name { color: var(--fg); flex: 1; font-size: 13px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.log-del { background: none; border: none; color: var(--border); cursor: pointer; font-size: 16px; padding: 0 4px; transition: color 0.15s; flex-shrink: 0; }
.log-del:hover { color: var(--red); }
.log-empty { padding: 20px 16px; text-align: center; color: var(--fg2); font-family: var(--sans); font-size: 12px; }

@keyframes flash { 0%{background:var(--amber-pale)} 100%{background:transparent} }
.flash { animation: flash 0.4s ease; }

/* ── PDF PRINT ── */
@media print {
  body { background: #fff; color: #000; padding: 20px; }
  .tabs, .actions, .clock-controls, .note-input-wrap, .sync-wrap, .mtc-wrap, button, header span { display: none; }
  header h1 { color: #000; font-size: 18px; font-style: normal; }
  .card { border: 1px solid #ccc; box-shadow: none; }
  .card-header { color: #333; border-bottom: 1px solid #ccc; }
  .log-tc { color: #333; }
  .log-name { color: #000; }
  .log-del { display: none; }
  #panel-logger { display: block !important; }
  #panel-editor { display: none !important; }
}
</style>
</head>
<body>

<header>
  <h1>hasan notemaker</h1>
  <span>TC Logger + AAF Generator for Pro Tools — Hasan Estudio</span>
</header>

<div class="tabs">
  <div class="tab active" onclick="switchTab('logger')">① Logger</div>
  <div class="tab" onclick="switchTab('editor')">② Editor</div>
</div>

<!-- ══ LOGGER ════════════════════════════════════════════════════════════ -->
<div class="panel active" id="panel-logger">

  <div class="card">
    <div class="card-header">Proyecto</div>
    <div class="card-body">
      <div class="field-full">
        <label style="display:block;font-size:10px;font-weight:600;color:var(--fg2);letter-spacing:.8px;text-transform:uppercase;margin-bottom:6px;font-family:var(--sans)">Nombre del proyecto</label>
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
          <input type="text" id="l-start" value="00:00:00:00">
        </div>
        <div class="field">
          <label>Dur. default (s)</label>
          <input type="number" id="l-dur" value="5" min="1" max="300">
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="clock-display stopped" id="clock">00:00:00:00</div>
    <div class="clock-controls">
      <button class="btn btn-primary" id="btn-start" onclick="startClock()">▶ Start</button>
      <button class="btn btn-ghost" id="btn-stop" onclick="stopClock()" disabled>■ Stop</button>
      <button class="btn btn-ghost" id="btn-reset" onclick="resetClock()">↺ Reset</button>
      <span class="clock-hint" id="clock-hint">Configura el TC y dale Start</span>
    </div>
    <div class="sync-wrap">
      <span class="sync-label">Play from:</span>
      <input type="text" id="sync-input" placeholder="01:05:32:14" maxlength="14">
      <button class="btn-sync" id="btn-sync" onclick="syncTC()">Save</button>
      <button class="btn-sync" id="btn-sync-pt" onclick="syncFromPT()">↻ Sync PT</button>
      <span class="sync-status" id="sync-status"></span>
    </div>
    <div class="note-input-wrap">
      <button class="btn-capture" id="btn-capture" onclick="captureTC()" disabled>⏱ Capturar TC</button>
      <input type="text" id="note-input" placeholder="Escribe la nota y presiona Enter..." disabled
             onkeydown="handleNoteKey(event)">
    </div>
    <div style="padding:6px 16px 10px;font-size:10px;color:var(--fg2);font-family:var(--sans)" id="note-capture-hint">
      Toca Capturar TC (o Shift+Enter) → escribe la nota → Enter para guardar
    </div>
  </div>

  <div class="card">
    <div class="card-header" id="log-header">Notas capturadas</div>
    <div id="log-empty" class="log-empty">Ninguna nota aún.</div>
    <ul class="log-list" id="log-list"></ul>
  </div>

  <div class="actions">
    <div class="actions-left">
      <button class="btn btn-ghost" onclick="clearLog()">Limpiar</button>
      <span id="status2"></span>
    </div>
    <div class="actions-right">
      <button class="btn btn-pdf" onclick="exportPDF()">PDF</button>
      <button class="btn btn-primary" onclick="generateFromLog()">AAF →</button>
    </div>
  </div>
</div>

<!-- ══ EDITOR ════════════════════════════════════════════════════════════ -->
<div class="panel" id="panel-editor">
  <div class="card">
    <div class="card-header">Proyecto</div>
    <div class="card-body">
      <div class="field-full">
        <label style="display:block;font-size:10px;font-weight:600;color:var(--fg2);letter-spacing:.8px;text-transform:uppercase;margin-bottom:6px;font-family:var(--sans)">Nombre del proyecto</label>
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
        <code>HH:MM:SS - HH:MM:SS Nombre</code> → dur. exacta
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
    <div class="actions-left"><span id="status"></span></div>
    <div class="actions-right">
      <button class="btn btn-pdf" onclick="exportPDFEditor()">PDF</button>
      <button class="btn btn-primary" onclick="generateFromEditor()">AAF →</button>
    </div>
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

// ── Reloj ─────────────────────────────────────────────────────────────────────
let clockInterval = null, elapsedMs = 0, lastTick = null, clockRunning = false;

function getStartOffsetMs() {
  const start = document.getElementById('l-start').value;
  const parts = start.split(/[:;]/);
  const h=parseInt(parts[0]||0), m=parseInt(parts[1]||0), s=parseInt(parts[2]||0);
  return (h*3600 + m*60 + s) * 1000;
}

function msToTC(ms) {
  const fps = parseFloat(document.getElementById('l-fps').value);
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
  const now = Date.now();
  elapsedMs += now - lastTick;
  lastTick = now;
  document.getElementById('clock').textContent = msToTC(getStartOffsetMs() + elapsedMs);
}

function startClock() {
  if (clockRunning) return;
  clockRunning = true; lastTick = Date.now();
  clockInterval = setInterval(updateClock, 50);
  document.getElementById('clock').className     = 'clock-display running';
  document.getElementById('btn-start').disabled  = true;
  document.getElementById('btn-stop').disabled   = false;
  document.getElementById('note-input').disabled = false;
  document.getElementById('btn-capture').disabled = false;
  document.getElementById('note-input').focus();
  document.getElementById('clock-hint').textContent = 'Corriendo';
}

function stopClock() {
  if (!clockRunning) return;
  clockRunning = false; clearInterval(clockInterval);
  document.getElementById('clock').className     = 'clock-display stopped';
  document.getElementById('btn-start').disabled  = false;
  document.getElementById('btn-stop').disabled   = true;
  document.getElementById('note-input').disabled = true;
  document.getElementById('btn-capture').disabled = true;
  document.getElementById('clock-hint').textContent = 'Pausado';
}

function resetClock() {
  stopClock(); elapsedMs = 0;
  document.getElementById('clock').textContent = msToTC(getStartOffsetMs());
  document.getElementById('clock').className   = 'clock-display stopped';
  document.getElementById('clock-hint').textContent = 'Configura el TC y dale Start';
  document.getElementById('sync-status').textContent = '';
}

function syncTC() {
  const val   = document.getElementById('sync-input').value.trim();
  if (!val) { alert('Escribe el TC de PT antes de sincronizar.'); return; }
  const parts = val.split(/[:;]/);
  let h=0, m=0, s=0;
  if (parts.length >= 3) { h=parseInt(parts[0]); m=parseInt(parts[1]); s=parseInt(parts[2]); }
  else if (parts.length === 2) { m=parseInt(parts[0]); s=parseInt(parts[1]); }
  else { alert('Formato inválido. Usa HH:MM:SS o MM:SS'); return; }

  const syncTcMs  = (h*3600 + m*60 + s) * 1000;
  const startMs   = getStartOffsetMs();
  const newElapsed = syncTcMs - startMs;

  if (newElapsed < 0) {
    alert('El TC ingresado es anterior al Start TC configurado.'); return;
  }

  elapsedMs = newElapsed;
  if (clockRunning) lastTick = Date.now();

  const btn = document.getElementById('btn-sync');
  btn.classList.add('synced'); btn.textContent = '✓ Saved';
  document.getElementById('sync-status').textContent = `desde ${val}`;
  document.getElementById('sync-status').style.color = 'var(--green)';
  setTimeout(() => { btn.classList.remove('synced'); btn.textContent = 'Save'; }, 2000);
}

// ── Captura de notas ──────────────────────────────────────────────────────────
let logEntries = [], capturedTC = null;

function captureTC() {
  const now          = Date.now();
  const extra        = clockRunning ? (now - lastTick) : 0;
  const totalElapsed = elapsedMs + extra;
  const displayMs    = getStartOffsetMs() + totalElapsed;
  capturedTC = { tc_str: msToTC(displayMs), elapsed_ms: totalElapsed };
  const btn = document.getElementById('btn-capture');
  btn.classList.add('captured');
  btn.textContent = '✓ ' + capturedTC.tc_str;
  document.getElementById('note-capture-hint').textContent = 'TC capturado — escribe la nota y presiona Enter';
  document.getElementById('note-capture-hint').style.color = 'var(--amber)';
  document.getElementById('note-input').focus();
  setTimeout(() => { btn.classList.remove('captured'); btn.textContent = '⏱ Capturar TC'; }, 3000);
}

function handleNoteKey(e) {
  if (e.key === 'Enter' && e.shiftKey) {
    e.preventDefault(); captureTC(); return;
  }
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    const input = document.getElementById('note-input');
    const name  = input.value.trim();
    if (!name) return;
    if (!capturedTC) {
      const now = Date.now(), extra = clockRunning ? (now - lastTick) : 0;
      const totalElapsed = elapsedMs + extra;
      capturedTC = { tc_str: msToTC(getStartOffsetMs() + totalElapsed), elapsed_ms: totalElapsed };
    }
    logEntries.push({ tc_str: capturedTC.tc_str, elapsed_ms: capturedTC.elapsed_ms, name });
    input.value = ''; capturedTC = null;
    document.getElementById('note-capture-hint').textContent = 'Toca Capturar TC (o Shift+Enter) → escribe la nota → Enter para guardar';
    document.getElementById('note-capture-hint').style.color = 'var(--fg2)';
    renderLog();
    const row = document.getElementById('log-list').lastElementChild;
    if (row) row.classList.add('flash');
  }
}

function renderLog() {
  const list = document.getElementById('log-list');
  const empty = document.getElementById('log-empty');
  document.getElementById('log-header').textContent = logEntries.length
    ? `Notas capturadas (${logEntries.length})` : 'Notas capturadas';
  list.innerHTML = '';
  empty.style.display = logEntries.length ? 'none' : 'block';
  logEntries.forEach((e, i) => {
    const li = document.createElement('li');
    li.innerHTML = `<span class="log-tc">${e.tc_str}</span><span class="log-name">${e.name}</span><button class="log-del" onclick="deleteEntry(${i})">×</button>`;
    list.appendChild(li);
  });
}

function deleteEntry(i) { logEntries.splice(i, 1); renderLog(); }
function clearLog() {
  if (logEntries.length && !confirm('¿Limpiar todas las notas?')) return;
  logEntries = []; renderLog();
}

function logToText() {
  return logEntries.map(e => {
    const parts = e.tc_str.split(':');
    return `${parts.slice(0,3).join(':')} ${e.name}`;
  }).join('\n');
}

function syncEditorFromLog() {
  if (!logEntries.length) return;
  document.getElementById('e-notes').value  = logToText();
  document.getElementById('e-fps').value    = document.getElementById('l-fps').value;
  document.getElementById('e-start').value  = document.getElementById('l-start').value;
  document.getElementById('e-dur').value    = document.getElementById('l-dur').value;
  const name = document.getElementById('l-filename').value;
  if (name) document.getElementById('e-filename').value = name;
}

// ── PDF ───────────────────────────────────────────────────────────────────────
function exportPDF() {
  if (!logEntries.length) { alert('No hay notas para exportar.'); return; }
  const project = document.getElementById('l-filename').value || 'Notas';
  const today   = new Date().toLocaleDateString('es-MX', {year:'numeric',month:'long',day:'numeric'});
  const rows    = logEntries.map(e =>
    `<tr><td class="tc-col">${e.tc_str}</td><td>${e.name}</td></tr>`
  ).join('');
  printDoc(project, today, rows);
}

function exportPDFEditor() {
  const notes   = document.getElementById('e-notes').value.trim();
  const project = document.getElementById('e-filename').value || 'Notas';
  const today   = new Date().toLocaleDateString('es-MX', {year:'numeric',month:'long',day:'numeric'});
  if (!notes) { alert('No hay notas para exportar.'); return; }
  const rows = notes.split('\n').filter(l => l.trim() && !l.startsWith('#')).map(l => {
    const m = l.match(/^(\d{1,2}:\d{2}:\d{2}(?::\d{1,2})?)/);
    const tc   = m ? m[1] : '';
    const name = m ? l.slice(m[0].length).replace(/^\s*-\s*/, '').trim() : l;
    return `<tr><td class="tc-col">${tc}</td><td>${name}</td></tr>`;
  }).join('');
  printDoc(project, today, rows);
}

function printDoc(project, today, rows) {
  const win = window.open('', '_blank');
  win.document.write(`<!DOCTYPE html><html><head><meta charset="UTF-8">
  <title>${project}</title>
  <style>
    body { font-family: 'Courier New', monospace; font-size: 12px; color: #000; padding: 32px; }
    h1 { font-size: 18px; margin-bottom: 4px; }
    .sub { color: #666; font-size: 11px; margin-bottom: 24px; }
    table { width: 100%; border-collapse: collapse; }
    tr { border-bottom: 1px solid #e0e0e0; }
    td { padding: 7px 8px; vertical-align: top; }
    .tc-col { color: #333; width: 110px; white-space: nowrap; font-weight: bold; }
    @media print { body { padding: 16px; } }
  </style></head><body>
  <h1>${project}</h1>
  <div class="sub">${today} — Hasan Estudio</div>
  <table>${rows}</table>
  <script>window.onload=()=>{window.print();}<\/script>
  </body></html>`);
  win.document.close();
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
      method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload)
    });
    if (!res.ok) { const err = await res.json(); throw new Error(err.error); }
    const regions = JSON.parse(res.headers.get('X-Regions') || '[]');
    const fname   = res.headers.get('X-Filename') || 'notas.aaf';
    const blob    = await res.blob();
    const url     = URL.createObjectURL(blob);
    const a       = document.createElement('a');
    a.href = url; a.download = fname; a.click(); URL.revokeObjectURL(url);
    statusEl.textContent = `✓ ${fname} — ${regions.length} región(es)`;
    statusEl.className   = 'ok';
    return regions;
  } catch(e) {
    statusEl.textContent = `✗ ${e.message}`; statusEl.className = 'err'; return null;
  }
}

async function generateFromLog() {
  if (!logEntries.length) {
    document.getElementById('status2').textContent = '✗ No hay notas';
    document.getElementById('status2').className   = 'err'; return;
  }
  await doGenerate({
    fps: document.getElementById('l-fps').value, start: document.getElementById('l-start').value,
    dur: document.getElementById('l-dur').value, notes: logToText(),
    filename: document.getElementById('l-filename').value.trim() || 'notas'
  }, document.getElementById('status2'));
}

// ── Sync manual desde PT ──────────────────────────────────────────────────────
async function syncFromPT() {
  const btn = document.getElementById('btn-sync-pt');
  try {
    const res = await fetch('/tc');
    if (!res.ok) throw new Error();
    const data = await res.json();
    if (!data.tc) { btn.textContent = '✗ Sin datos'; setTimeout(() => btn.textContent = '↻ Sync PT', 2000); return; }
    const parts = data.tc.split(':');
    const h = parseInt(parts[0])||0, m = parseInt(parts[1])||0, s = parseInt(parts[2])||0;
    const newElapsed = (h*3600 + m*60 + s)*1000 - getStartOffsetMs();
    if (newElapsed >= 0) { elapsedMs = newElapsed; if (clockRunning) lastTick = Date.now(); document.getElementById('clock').textContent = msToTC(getStartOffsetMs() + elapsedMs); }
    btn.classList.add('synced'); btn.textContent = '✓ Synced';
    setTimeout(() => { btn.classList.remove('synced'); btn.textContent = '↻ Sync PT'; }, 2000);
  } catch(e) { btn.textContent = '✗ Error'; setTimeout(() => btn.textContent = '↻ Sync PT', 2000); }
}

// ── TC Sync (polling) ─────────────────────────────────────────────────────────
let lastSyncTs = 0;
async function pollTC() {
  try {
    const res = await fetch('/tc');
    if (!res.ok) return;
    const data = await res.json();
    if (!data.ts || data.ts <= lastSyncTs) return;
    lastSyncTs = data.ts;
    const parts = (data.tc || '').split(':');
    const h = parseInt(parts[0])||0, m = parseInt(parts[1])||0, s = parseInt(parts[2])||0;
    const newElapsed = (h*3600 + m*60 + s)*1000 - getStartOffsetMs();
    if (newElapsed >= 0) { elapsedMs = newElapsed; if (clockRunning) lastTick = Date.now(); }
    if (data.event === 'play') startClock();
    else if (data.event === 'stop') stopClock();
  } catch(e) {}
}
setInterval(pollTC, 500);

async function generateFromEditor() {
  const regions = await doGenerate({
    fps: document.getElementById('e-fps').value, start: document.getElementById('e-start').value,
    dur: document.getElementById('e-dur').value, notes: document.getElementById('e-notes').value,
    filename: document.getElementById('e-filename').value.trim() || 'notas'
  }, document.getElementById('status'));
  if (regions && regions.length) {
    document.getElementById('e-preview-title').textContent = `${regions.length} región(es) generada(s)`;
    document.getElementById('e-preview-list').innerHTML = regions.map(r =>
      `<li><span class="tc">${fmtSec(Math.round(r.start))}</span><span class="dur">${r.dur.toFixed(1)}s</span><span class="rname">${r.name}</span></li>`
    ).join('');
    document.getElementById('e-preview').style.display = 'block';
  }
}
</script>
</body>
</html>"""

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route('/')
def index():
    return Response(HTML, mimetype='text/html; charset=utf-8')

@app.route('/generate', methods=['POST'])
def generate():
    body      = request.get_json(force=True)
    fps_str   = body.get('fps', '23.976')
    start_str = body.get('start', '00:00:00:00')
    notes_txt = body.get('notes', '')
    filename  = body.get('filename', 'notas').strip() or 'notas'
    try:
        default_dur = float(body.get('dur', 5))
    except Exception:
        default_dur = 5.0

    today    = date.today().strftime('%Y-%m-%d')
    aaf_name = f"{filename}_{today}.aaf"
    seq_name = f"{filename} — {today}"

    try:
        regions   = parse_notes(notes_txt, default_dur, fps_str, start_str)
        if not regions:
            raise ValueError("No se encontraron notas con timecode válido.")
        aaf_bytes = build_aaf_bytes(regions, seq_name)
    except Exception as e:
        return Response(
            json.dumps({'error': str(e)}),
            status=400,
            mimetype='application/json'
        )

    rjson = json.dumps([{'name': r[0], 'start': r[1], 'dur': r[2]} for r in regions])
    resp  = Response(aaf_bytes, mimetype='application/octet-stream')
    resp.headers['Content-Disposition']          = f'attachment; filename="{aaf_name}"'
    resp.headers['X-Filename']                   = aaf_name
    resp.headers['X-Regions']                    = rjson
    resp.headers['Access-Control-Expose-Headers'] = 'X-Filename, X-Regions'
    return resp

@app.route('/tc', methods=['GET'])
def get_tc():
    r = get_redis()
    if not r: return Response('{}', mimetype='application/json')
    val = r.get('tc')
    return Response(val or '{}', mimetype='application/json')

@app.route('/tc', methods=['POST'])
def post_tc():
    r = get_redis()
    if not r: return Response('{}', mimetype='application/json')
    body = request.get_json(force=True)
    body['ts'] = time.time()
    r.set('tc', json.dumps(body))
    return Response('{}', mimetype='application/json')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8765)))
