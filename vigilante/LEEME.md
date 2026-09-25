# Vigilante de carpeta Nextcloud

Transcribe solo, sin que tengas que pedírselo, todo el audio que aparezca en una
carpeta de Nextcloud. Corre en el Umbrel, que es donde ya vive Nextcloud, así que
el audio no viaja a ninguna parte.

No reimplementa nada: la transcripción la hace `transcribe_audio.py` del proyecto
padre, tal cual.

## Cómo se usa

```
Transcribir/
├── entrada/    sueltas el audio aquí (desde el móvil, el PC o la web)
├── salida/     aparece <nombre>_transcripcion.txt
├── hechos/     se aparta el original
└── errores/    lo que falló, con su .log al lado
```

Las cuatro carpetas se crean solas al arrancar. El original **no se borra**: se
aparta a `hechos/` y se purga a los 30 días (`DIAS_RETENCION`). Así, si una
transcripción sale mal, el audio sigue ahí.

## Por qué WebDAV y no el disco

Escribir directamente en el directorio de datos de Nextcloud **no funciona**:
Nextcloud no se entera del fichero hasta que alguien lanza `occ files:scan`, y
además hay que cuadrar el uid de `www-data`. Hablando WebDAV el vigilante es un
cliente más, y Nextcloud indexa, sincroniza y gestiona la papelera él solo.

## Puesta en marcha

El único secreto es cosa tuya; yo no puedo generarlo:

1. **Contraseña de aplicación** de Nextcloud: Ajustes → Seguridad → Dispositivos
   y sesiones → Crear nueva contraseña de aplicación.
2. Rellenar el `.env`.

```bash
cd ~/transcriptor
cp vigilante/.env.ejemplo vigilante/.env
nano vigilante/.env
docker compose -f vigilante/compose.yml up -d --build
docker compose -f vigilante/compose.yml logs -f
```

El primer audio tarda algo más porque descarga el modelo de Whisper (~480 MB,
público, sin cuenta); queda en el volumen `modelos` y ya no se vuelve a bajar.

## Ajustes que importan

| Variable | Por defecto | Qué hace |
|---|---|---|
| `WHISPER_MODEL` | `small` | `medium` transcribe mejor pero multiplica el tiempo |
| `IDIOMA` | `es` | Vacío = detectar automáticamente |
| `INTERVALO` | `60` | Segundos entre vistazos a la carpeta |
| `DIAS_RETENCION` | `30` | Días antes de purgar `hechos/`. `0` = no purgar |

## Cuánto tarda

Medido por el proyecto padre en un i7-12700H: **10 min de audio en ~40 s**. El
Umbrel es un N100 de 4 núcleos con 3 disponibles, bastante más lento, así que
aquí hay que contar con algo del orden de **un tercio del tiempo real** (una
hora de audio, unos 20 minutos). Sin medir todavía en el propio N100.

Como nadie está esperando delante de la pantalla, eso importa poco: sueltas el
audio y recoges el `.txt` más tarde.

## Límites de recursos

El contenedor va topado a 3 de los 4 núcleos, prioridad de CPU baja y 6 GB de
memoria. Es para que Immich, Jellyfin y el propio Nextcloud sigan respondiendo
mientras hay una transcripción en marcha, y para que si algo se desmadra Docker
mate **este** contenedor y no la base de datos de otra app.
