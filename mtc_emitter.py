#!/usr/bin/env python3
"""
Notemaker MTC Emitter
Conecta Pro Tools con notemaker.hasanestudio.net via MIDI Timecode.

Instalación (una sola vez):
    pip3 install mido python-rtmidi requests
"""

import time, json
from threading import Thread
import tkinter as tk

NOTEMAKER_URL = 'https://notemaker.hasanestudio.net'
STOP_TIMEOUT  = 0.2
FPS_MAP       = {0: '24', 1: '25', 2: '29.97', 3: '30'}

# ── Estado MTC ────────────────────────────────────────────────────────────────
mtc_qf       = [0] * 8
last_qf_time = 0.0
is_playing   = False
last_tc      = None

def assemble_tc():
    frames   = mtc_qf[0] | ((mtc_qf[1] & 0x01) << 4)
    seconds  = mtc_qf[2] | ((mtc_qf[3] & 0x03) << 4)
    minutes  = mtc_qf[4] | ((mtc_qf[5] & 0x03) << 4)
    hours    = mtc_qf[6] | ((mtc_qf[7] & 0x01) << 4)
    fps_type = (mtc_qf[7] >> 1) & 0x03
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}', FPS_MAP.get(fps_type, '29.97')

def post_event(event, tc, fps):
    try:
        import requests
        requests.post(f'{NOTEMAKER_URL}/tc',
                      json={'event': event, 'tc': tc, 'fps': fps},
                      timeout=3)
    except Exception as e:
        print(f'Error POST: {e}')

def run_emitter(on_status):
    global is_playing, last_qf_time, last_tc
    import mido

    on_status('waiting', '--:--:--:--')

    ports = mido.get_input_names()
    if not ports:
        on_status('error', 'Sin puertos MIDI')
        return

    port_name = next((p for p in ports if 'IAC' in p), ports[0])
    on_status('idle', '--:--:--:--')

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
                        Thread(target=post_event, args=('play', tc, fps), daemon=True).start()
                        on_status('playing', tc)

                    if msg.frame_type == 7:
                        last_tc = assemble_tc()
                        on_status('playing', last_tc[0])

                elif msg.type == 'sysex':
                    d = msg.data
                    if len(d) >= 8 and d[0] == 0x7F and d[2] == 0x01 and d[3] == 0x01:
                        hh, mm, ss, ff = d[4], d[5], d[6], d[7]
                        fps_type = (hh >> 5) & 0x03
                        hours    = hh & 0x1F
                        tc_str   = f'{hours:02d}:{mm:02d}:{ss:02d}:{ff:02d}'
                        last_tc  = (tc_str, FPS_MAP.get(fps_type, '29.97'))
                        on_status('idle', tc_str)

            if is_playing and (now - last_qf_time) > STOP_TIMEOUT:
                is_playing = False
                if last_tc:
                    tc, fps = last_tc
                    Thread(target=post_event, args=('stop', tc, fps), daemon=True).start()
                    on_status('idle', tc)

            time.sleep(0.005)

# ── GUI ───────────────────────────────────────────────────────────────────────
def run_gui():
    COLORS = {
        'waiting': '#444444',
        'idle':    '#f5a623',
        'playing': '#4caf7d',
        'error':   '#e05252',
    }
    STATUS_TEXT = {
        'waiting': 'Buscando MIDI...',
        'idle':    'PT listo',
        'playing': 'PT corriendo',
        'error':   'Error — sin puertos MIDI',
    }

    root = tk.Tk()
    root.title('Notemaker MTC')
    root.geometry('300x115')
    root.resizable(False, False)
    root.configure(bg='#0f0f0f')

    # Dot
    canvas = tk.Canvas(root, width=14, height=14, bg='#0f0f0f', highlightthickness=0)
    dot = canvas.create_oval(1, 1, 13, 13, fill='#444444', outline='')
    canvas.place(x=18, y=18)

    # Status
    status_var = tk.StringVar(value='Iniciando...')
    tk.Label(root, textvariable=status_var, bg='#0f0f0f', fg='#888888',
             font=('Helvetica', 11)).place(x=40, y=14)

    # TC
    tc_var = tk.StringVar(value='--:--:--:--')
    tc_lbl = tk.Label(root, textvariable=tc_var, bg='#0f0f0f', fg='#f5a623',
                      font=('Courier New', 30, 'bold'))
    tc_lbl.place(x=14, y=42)

    # URL
    tk.Label(root, text=NOTEMAKER_URL.replace('https://', ''),
             bg='#0f0f0f', fg='#333333', font=('Helvetica', 9)).place(x=18, y=94)

    def on_status(state, tc):
        color = COLORS.get(state, '#444444')
        root.after(0, lambda: canvas.itemconfig(dot, fill=color))
        root.after(0, lambda: status_var.set(STATUS_TEXT.get(state, state)))
        root.after(0, lambda: tc_var.set(tc))
        root.after(0, lambda: tc_lbl.configure(
            fg='#4caf7d' if state == 'playing' else '#f5a623'))

    Thread(target=run_emitter, args=(on_status,), daemon=True).start()
    root.mainloop()

if __name__ == '__main__':
    run_gui()
