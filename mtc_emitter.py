#!/usr/bin/env python3
"""
MTC Emitter — lee MIDI Timecode de Pro Tools y notifica al Notemaker.

Instalación (una sola vez):
    pip install mido python-rtmidi requests

Uso:
    NOTEMAKER_URL=https://tu-app.vercel.app python3 mtc_emitter.py

El script detecta automáticamente el IAC Driver. Envía un POST a /tc
cuando Pro Tools arranca (play) y cuando para (stop).
"""

import mido
import requests
import time
import os
import sys

NOTEMAKER_URL = os.environ.get('NOTEMAKER_URL', '').rstrip('/')
STOP_TIMEOUT  = 0.2   # segundos sin QF → PT paró
FPS_MAP       = {0: '24', 1: '25', 2: '29.97', 3: '30'}

# ── Estado MTC ────────────────────────────────────────────────────────────────
mtc_qf        = [0] * 8
last_qf_time  = 0.0
is_playing    = False
last_tc       = None   # (tc_str, fps_str)

def assemble_tc():
    frames  = mtc_qf[0] | ((mtc_qf[1] & 0x01) << 4)
    seconds = mtc_qf[2] | ((mtc_qf[3] & 0x03) << 4)
    minutes = mtc_qf[4] | ((mtc_qf[5] & 0x03) << 4)
    hours   = mtc_qf[6] | ((mtc_qf[7] & 0x01) << 4)
    fps_type = (mtc_qf[7] >> 1) & 0x03
    tc_str  = f'{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}'
    return tc_str, FPS_MAP.get(fps_type, '29.97')

def post_event(event, tc, fps):
    if not NOTEMAKER_URL:
        print(f'[sin URL] {event} {tc}')
        return
    try:
        requests.post(f'{NOTEMAKER_URL}/tc',
                      json={'event': event, 'tc': tc, 'fps': fps},
                      timeout=3)
        print(f'→ {event:5s} {tc}')
    except Exception as e:
        print(f'Error POST: {e}')

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    global is_playing, last_qf_time, last_tc

    if not NOTEMAKER_URL:
        print('⚠ Define NOTEMAKER_URL antes de correr el script.')
        print('  Ejemplo: NOTEMAKER_URL=https://tu-app.vercel.app python3 mtc_emitter.py')

    ports = mido.get_input_names()
    if not ports:
        print('No hay puertos MIDI disponibles. Verifica el IAC Driver.')
        sys.exit(1)

    # Preferir IAC Driver, si no tomar el primero disponible
    port_name = next((p for p in ports if 'IAC' in p), ports[0])
    print(f'Puertos disponibles: {ports}')
    print(f'Usando: {port_name}')
    print('Escuchando MTC... (Ctrl+C para salir)\n')

    with mido.open_input(port_name) as port:
        while True:
            now = time.time()

            for msg in port.iter_pending():
                if msg.type == 'quarter_frame':
                    mtc_qf[msg.frame_type] = msg.frame_value
                    last_qf_time = now

                    if not is_playing:
                        is_playing = True
                        tc, fps = assemble_tc()
                        post_event('play', tc, fps)

                    if msg.frame_type == 7:
                        last_tc = assemble_tc()

                elif msg.type == 'sysex':
                    # Full frame: F0 7F 7F 01 01 hh mm ss ff F7
                    d = msg.data
                    if len(d) >= 8 and d[0] == 0x7F and d[2] == 0x01 and d[3] == 0x01:
                        hh, mm, ss, ff = d[4], d[5], d[6], d[7]
                        fps_type = (hh >> 5) & 0x03
                        hours    = hh & 0x1F
                        tc_str   = f'{hours:02d}:{mm:02d}:{ss:02d}:{ff:02d}'
                        last_tc  = (tc_str, FPS_MAP.get(fps_type, '29.97'))

            # Detectar stop: sin QF por STOP_TIMEOUT segundos
            if is_playing and (now - last_qf_time) > STOP_TIMEOUT:
                is_playing = False
                if last_tc:
                    post_event('stop', *last_tc)

            time.sleep(0.005)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nEmitter cerrado.')
