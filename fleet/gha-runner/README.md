# Runners de GitHub Actions en la Mini

La organización SEN-HASO está en el plan gratuito de GitHub: 2,000 minutos al
mes de runners de GitHub para repositorios privados. Los gates rápidos de Jale
corren en Boat, pero dos trabajos no pueden ir ahí: el guard que decide qué
recibe Boat y las unitarias que leen datos excluidos de Boat. Con unos 190
pushes al día, esos dos trabajos agotarían los minutos en menos de una semana.
Dos runners en la Mini los corren sin costo.

## Qué instala

`instalar.sh` instala un runner por papel:

| Papel | Trabajo de Jale | Etiqueta | Nombre |
|---|---|---|---|
| `guard` | «Política de exportación a Boat» | `jale-guard` | `mini-jale-guard` |
| `datos` | «Unitarias con datos o recursos excluidos de Boat» | `jale-datos` | `mini-jale-datos` |

Cada runner toma un trabajo a la vez y GitHub guarda su fila. Van separados
porque los trabajos de Boat esperan al guard, que tarda segundos: con un solo
runner, el guard haría fila detrás de unitarias de varios minutos.

Por cada papel instala:

- El runner oficial `actions/runner` 2.338.2 para macOS arm64, en
  `~/actions-runner-jale-<papel>`. `instalar.sh` fija su sha256 y no extrae un
  paquete que no coincida.
- Un registro de repositorio en `SEN-HASO/Jale`. Los trabajos lo piden con
  `runs-on: [self-hosted, jale-<papel>]`.
- El LaunchAgent `local.pao.gha-runner-jale-<papel>`, con log en
  `~/Library/Logs/pao-gha-runner-jale-<papel>.log`.

## Instalar

Desde la MacBook, con el checkout de PAO de la Mini al día:

```sh
gh api -X POST repos/SEN-HASO/Jale/actions/runners/registration-token -q .token \
  | ssh mini 'bash ~/.local/share/personal-agent-orchestration/fleet/gha-runner/instalar.sh guard'
```

Repite el comando con `datos`. Cada instalación pide su propio token.

El token de registro dura una hora y viaja por stdin. `instalar.sh` se lo pasa
a `config.sh` por entorno, nunca por argumentos. Termina cuando el runner
escribe `Listening for Jobs` en su log; si no lo hace en 60 s, sale con 1.

`instalar.sh --seco guard` imprime los pasos sin escribir nada.

Comprueba el registro desde la MacBook:

```sh
gh api repos/SEN-HASO/Jale/actions/runners \
  -q '.runners[] | [.name, .status, (.labels | map(.name) | join(","))] | @tsv'
```

Debe imprimir cada runner con `online` y su etiqueta.

Volver a correr `instalar.sh <papel>` no descarga ni registra de nuevo y no pide token.
Reescribe el LaunchAgent y reinicia el servicio: un trabajo en curso se pierde y
hay que relanzarlo en GitHub.

## Retirar

```sh
gh api -X POST repos/SEN-HASO/Jale/actions/runners/remove-token -q .token \
  | ssh mini 'bash ~/.local/share/personal-agent-orchestration/fleet/gha-runner/instalar.sh --retirar guard'
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
  hacía falta `ssh`. Eso incluye a cada colaborador y a cada app de GitHub con
  escritura en el repositorio; `gh api orgs/SEN-HASO/installations` lista las
  apps. Para acotarlo, quita la escritura a quien no deba tenerla o pasa los
  runners a un usuario de macOS sin acceso al home del dueño.
- **Fuera de Pueue.** Los trabajos no pasan por los grupos `suites` ni
  `rapidas` y la guardia de la Mini no los pausa. La carga queda acotada por
  los propios runners: un trabajo a la vez cada uno, y sólo esos dos trabajos
  los piden. Un trabajo pesado no va aquí; va a Boat o a `remote-run`.
- **Servicios de la Mini a la vista.** Un trabajo alcanza los puertos locales,
  como el Postgres de pruebas en `127.0.0.1:5435`. Un workflow que corre aquí
  no debe apuntar a ellos.
- **Mini apagada.** Los trabajos esperan en GitHub hasta 24 h y después fallan.
  En Jale, sin el guard no arranca ningún trabajo de Boat.

## Pruebas

`python3 -I -m unittest discover -s fleet/gha-runner/tests -v`. Usan dobles de
`curl`, `shasum`, `launchctl` y `uname`; no tocan la red ni launchd.
