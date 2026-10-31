---
name: android-qa
description: Android en dispositivo o emulador. Úsala en toda prueba, QA o reproducción de un bug sobre un Android real o emulado, y antes de elegir entre la Android CLI (`android`) y adb.
---

# QA en Android

La Android CLI (`android`) maneja los dispositivos y
emuladores de toda prueba. adb queda para las tareas que la CLI no cubre.
Donde la skill oficial `android-cli` de Google enseña `adb shell input` con
coordenadas, manda esta skill.

## Antes de empezar

1. Corre en la máquina que diga el `AGENTS.md` del repositorio; si no dice
   nada, en la Mac Mini, como fija «Máquinas» del entrypoint global.
2. Comprueba la versión con `android -V`. Esta skill necesita 1.1.16717390
   o posterior, la que añade `android device hub`. `android input` llegó
   antes, en 1.0.16625370. Si la versión es anterior, `android update` instala la última en esa máquina;
   dilo en tu reporte.
3. Con más de un dispositivo conectado, pasa `--device <serial>`, con el
   serial que lista `adb devices`, a los comandos que actúan sobre un
   dispositivo: `install`, `run`, `layout`, `input`, `screen capture` y
   `device hub`. Los comandos `emulator` reciben el nombre del AVD.

Los flags de cada comando salen de `android <comando> -h`.

## Qué comando usar

| Tarea | Comando |
|---|---|
| Listar, arrancar y apagar emuladores | `android emulator list`, `android emulator start --headless <avd>`, `android emulator stop <avd>` |
| Instalar o lanzar un build | `android install`, `android run` |
| Leer la pantalla | `android layout`: JSON con los elementos interactivos y su `center`. `--full` añade el texto no interactivo. |
| Tocar, escribir, deslizar, desplazar | `android input tap`, `text`, `swipe` y `scroll`, con selectores `text=`, `id=`, `desc=` y `class=` |
| Teclas y botones del sistema | `android input key <tecla>`, como `back`, `home` o `enter`, o un keycode |
| Captura de evidencia | `android screen capture -o <archivo>.png` |
| Elemento sin nodo de accesibilidad (canvas, juego, imagen) | `android screen capture --annotate`, luego `android screen resolve` y `adb shell input tap` con las coordenadas resultantes |
| Ver el dispositivo en una ventana | `android device hub` |

## Reglas de interacción

- Toca, escribe y desliza por selector. `android input tap`, `text`,
  `swipe` y `scroll` salen con código distinto de cero si el elemento no
  existe o si coinciden varios. `adb shell input tap`
  devuelve 0 aunque no toque nada.
- Lee el código de salida de cada paso. Un código distinto de cero es un
  fallo del paso.
- Un selector de texto coincide con cualquier subcadena, sin distinguir
  mayúsculas: `text=Guardar` también encuentra «Guardar borrador». Para una
  coincidencia exacta usa una expresión regular, como `text=/^Guardar$/`.
- Espera con `--wait=<ms>` a que el elemento aparezca. Usa `--match=first`
  sólo cuando cualquiera de las coincidencias sirve.

## Choque con UiAutomation

`android layout` y `android input` dejan corriendo en el dispositivo el
proceso `com.android.cli.interact.instrumentation`, que ocupa UiAutomation.
Android admite un solo cliente de UiAutomation a la vez. Con la CLI 1.1 en un
emulador API 35, `adb shell uiautomator dump` murió con código 137 mientras
ese proceso vivía. Maestro, Appium y los tests de UiAutomator también usan
UiAutomation. Antes de usar uno de ellos, libera el dispositivo:

```bash
adb -s <serial> shell am force-stop com.android.cli.interact.instrumentation
```

El siguiente `android layout` o `android input` vuelve a levantarlo.

## Tareas de adb

La CLI 1.1 no cubre estas tareas. Usa adb con `-s <serial>`:

| Tarea | Comando |
|---|---|
| Logs | `adb logcat` |
| Metro y puertos locales | `adb reverse tcp:8081 tcp:8081` |
| Abrir una app ya instalada, deep links e intents | `adb shell am start` |
| Borrar datos y conceder permisos | `adb shell pm clear`, `adb shell pm grant` |
| Video de la sesión | `adb shell screenrecord` |
| Red lenta en el emulador | `adb emu network speed <perfil>` |

Antes de usar adb para otra tarea, busca en `android -h`. La CLI añade
comandos cada mes. Si ya cubre la tarea, usa la CLI.
