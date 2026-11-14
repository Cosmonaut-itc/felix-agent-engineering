# Runner de GitHub Actions en la Mini

La organización SEN-HASO está en el plan gratuito de GitHub: 2,000 minutos al
mes de runners de GitHub para repositorios privados. Los gates rápidos de Jale
corren en Boat, pero dos trabajos no pueden ir ahí: el guard que decide qué
recibe Boat y las unitarias que leen datos excluidos de Boat. Con unos 190
pushes al día, esos dos trabajos agotarían los minutos en menos de una semana.
Este runner los corre en la Mini, sin costo.

## Qué instala

- El runner oficial `actions/runner` 2.338.2 para macOS arm64, en
  `~/actions-runner-jale`. `instalar.sh` fija su sha256 y no extrae un paquete
  que no coincida.
- Un registro de repositorio en `SEN-HASO/Jale` con el nombre `mini-jale` y la
  etiqueta `jale-ligero`. Los trabajos lo piden con
  `runs-on: [self-hosted, jale-ligero]`.
- El LaunchAgent `local.pao.gha-runner-jale`, con log en
  `~/Library/Logs/pao-gha-runner-jale.log`.

El runner toma un trabajo a la vez. GitHub guarda la fila.

## Instalar

Desde la MacBook, con el checkout de PAO de la Mini al día:

```sh
gh api -X POST repos/SEN-HASO/Jale/actions/runners/registration-token -q .token \
  | ssh mini 'bash ~/.local/share/personal-agent-orchestration/fleet/gha-runner/instalar.sh'
```

El token de registro dura una hora y viaja por stdin. `instalar.sh` se lo pasa
a `config.sh` por entorno, nunca por argumentos. Termina cuando el runner
escribe `Listening for Jobs` en su log; si no lo hace en 60 s, sale con 1.

`instalar.sh --seco` imprime los pasos sin escribir nada.

Comprueba el registro desde la MacBook:

```sh
gh api repos/SEN-HASO/Jale/actions/runners \
  -q '.runners[] | [.name, .status, (.labels | map(.name) | join(","))] | @tsv'
```

Debe imprimir `mini-jale`, `online` y la etiqueta `jale-ligero`.

Volver a correr `instalar.sh` no descarga ni registra de nuevo y no pide token.
Reescribe el LaunchAgent y reinicia el servicio: un trabajo en curso se pierde y
hay que relanzarlo en GitHub.

## Retirar

```sh
gh api -X POST repos/SEN-HASO/Jale/actions/runners/remove-token -q .token \
  | ssh mini 'bash ~/.local/share/personal-agent-orchestration/fleet/gha-runner/instalar.sh --retirar'
```

Descarga el servicio, da de baja el registro y renombra el directorio y el
plist a `.retirado-<fecha>`. No borra nada.

## Cambiar de versión

El runner se actualiza solo: GitHub deja de asignar trabajos a una versión
vieja. `version` y `sha256` de `instalar.sh` sólo cuentan en una instalación
nueva. Al cambiarlos, copia el sha256 de `osx-arm64` de las notas de la versión
en `github.com/actions/runner/releases`.

## Límites

- **Quién ejecuta código en la Mini.** Un trabajo corre el código de la rama
  como el usuario de la Mini, igual que `remote-run`. Con el runner, basta
  permiso de escritura en `SEN-HASO/Jale` para ejecutar código ahí; sin él
  hacía falta `ssh`. El 2026-11-14 el único colaborador es el dueño. Antes de
  dar escritura a otra persona o a una app, retira el runner o pásalo a un
  usuario de macOS sin acceso al home.
- **Fuera de Pueue.** Los trabajos no pasan por los grupos `suites` ni
  `rapidas` y la guardia de la Mini no los pausa. La carga queda acotada por el
  propio runner: un trabajo a la vez, y sólo los dos trabajos ligeros lo piden.
  Un trabajo pesado no va aquí; va a Boat o a `remote-run`.
- **Servicios de la Mini a la vista.** Un trabajo alcanza los puertos locales,
  como el Postgres de pruebas en `127.0.0.1:5435`. Un workflow que corre aquí
  no debe apuntar a ellos.
- **Mini apagada.** Los trabajos esperan en GitHub hasta 24 h y después fallan.
  En Jale, sin el guard no arranca ningún trabajo de Boat.

## Pruebas

`python3 -I -m unittest discover -s fleet/gha-runner/tests -v`. Usan dobles de
`curl`, `shasum`, `launchctl` y `uname`; no tocan la red ni launchd.
