#!/usr/bin/env python3
"""
Notemaker MTC Emitter
Conecta Pro Tools con notemaker.hasanestudio.net via MIDI Timecode.

Instalación (una sola vez):
    pip3 install mido python-rtmidi requests
"""

import mido
import requests
import time
import sys

NOTEMAKER_URL = 'https://notemaker.hasanestudio.net'
STOP_TIMEOUT  = 0.2
FPS_MAP       = {0: '24', 1: '25', 2: '29.97', 3: '30'}

# ── Estado MTC ────────────────────────────────────────────────────────────────
mtc_qf        = [0] * 8
last_qf_time  = 0.0
is_playing    = False
last_tc       = None
mtc_cycle_count = 0  # ciclos completos desde el último play

def assemble_tc():
    frames   = mtc_qf[0] | ((mtc_qf[1] & 0x01) << 4)
    seconds  = mtc_qf[2] | ((mtc_qf[3] & 0x03) << 4)
    minutes  = mtc_qf[4] | ((mtc_qf[5] & 0x03) << 4)
    hours    = mtc_qf[6] | ((mtc_qf[7] & 0x01) << 4)
    fps_type = (mtc_qf[7] >> 1) & 0x03
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}', FPS_MAP.get(fps_type, '29.97')

def post_event(event, tc, fps):
    try:
        requests.post(f'{NOTEMAKER_URL}/tc',
                      json={'event': event, 'tc': tc, 'fps': fps},
                      timeout=3)
        print(f'→ {event:5s} {tc}')
    except Exception as e:
        print(f'Error POST: {e}')

def main():
    global is_playing, last_qf_time, last_tc

    ports = mido.get_input_names()
    if not ports:
        print('No hay puertos MIDI. Verifica el IAC Driver.')
        sys.exit(1)

    port_name = next((p for p in ports if 'IAC' in p), ports[0])
    print(f'Conectado a: {port_name}')
    print(f'Enviando a:  {NOTEMAKER_URL}')
    print('Escuchando MTC... (Ctrl+C para salir)\n')

    with mido.open_input(port_name) as port:
        while True:
            now = time.time()

            for msg in port.iter_pending():
                if msg.type == 'quarter_frame':
                    mtc_qf[msg.frame_type] = msg.frame_value
                    last_qf_time = now

                    if msg.frame_type == 7:
                        last_tc = assemble_tc()
                        if not is_playing:
                            mtc_cycle_count += 1
                            if mtc_cycle_count >= 2:
                                is_playing = True
                                mtc_cycle_count = 0
                                post_event('play', last_tc[0], last_tc[1])

                elif msg.type == 'sysex':
                    d = msg.data
                    if len(d) >= 8 and d[0] == 0x7F and d[2] == 0x01 and d[3] == 0x01:
                        hh, mm, ss, ff = d[4], d[5], d[6], d[7]
                        fps_type = (hh >> 5) & 0x03
                        hours    = hh & 0x1F
                        tc_str   = f'{hours:02d}:{mm:02d}:{ss:02d}:{ff:02d}'
                        last_tc  = (tc_str, FPS_MAP.get(fps_type, '29.97'))

            if (is_playing or mtc_cycle_count > 0) and (now - last_qf_time) > STOP_TIMEOUT:
                is_playing = False
                mtc_cycle_count = 0
                if last_tc:
                    tc, fps = last_tc
                    post_event('stop', tc, fps)

            time.sleep(0.005)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nEmitter cerrado.')
