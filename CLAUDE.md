# Hasan Notemaker — CLAUDE.md

## Qué es
Herramienta web para post-producción de audio (cine/TV). Permite:
1. **Logger**: reloj TC en tiempo real, capturar notas con timecode, exportar PDF
2. **Editor**: editar notas TC manualmente y generar AAF para Pro Tools

El AAF generado se importa a Pro Tools como regiones fantasma (ghost regions) con sus timecodes exactos.

## Archivos clave
| Archivo | Descripción |
|---|---|
| `api/index.py` | **App principal** — Flask + lógica AAF + HTML/CSS/JS inline. Corre en Vercel. |
| `notemaker_cloud.py` | Versión local/Railway con `http.server`. Referencia histórica. |
| `requirements.txt` | `pyaaf2` + `flask` |
| `vercel.json` | Redirige todo a `/api/index` |
| `Procfile` | `web: python3 notemaker_cloud.py` (Railway, ya no en uso) |

## Arquitectura actual (Vercel)
- **Flask** como servidor WSGI compatible con Vercel Serverless
- **GET /**: sirve el HTML completo (embebido como string en `api/index.py`)
- **POST /generate**: recibe JSON `{fps, start, dur, notes, filename}`, devuelve bytes `.aaf` + headers `X-Filename` y `X-Regions`
- **Librería core**: `pyaaf2` para construir el archivo AAF
- **Temp files**: `tempfile.NamedTemporaryFile` en `/tmp` (disponible en Vercel)

## Lógica de negocio
- `parse_notes()`: parsea texto con timecodes (`HH:MM:SS nombre` o `HH:MM:SS - HH:MM:SS nombre`)
- `build_aaf_bytes()`: construye el AAF con SourceMob + MasterMob + CompositionMob por región
- `tc_to_real()`: convierte TC a segundos reales, corrigiendo por fps (ej. 23.976 = 1001/1000)
- `minimal_wav()`: genera solo el header WAV con 0 muestras — mantiene el AAF liviano

## Decisiones de UX (no revertir sin consultar)
- El botón "Play from" se llama **"Save"** (no "Sync" — confunde a los usuarios haciéndoles pensar que sincroniza automáticamente con PT)
- La duración de las regiones en el AAF viene del campo **"Dur. default"** del menú (no del tiempo de escritura)
- El TC capturado con Shift+Enter **no expira** — se mantiene hasta que se guarda la nota

## Frame rates soportados
23.976, 24, 25, 29.97, 30, 48, 50, 59.94, 60

## Deployment
- **Vercel** — Flask serverless, `vercel.json` enruta todo a `api/index.py`
- Feature pendiente: sincronización con Pro Tools via MIDI Timecode (MTC)
