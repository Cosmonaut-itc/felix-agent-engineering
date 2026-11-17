# Runners de GitHub Actions en la Mini y el desk

La organización SEN-HASO está en el plan gratuito de GitHub: 2,000 minutos al
mes de runners de GitHub para repositorios privados. Los gates rápidos de Jale
corren en Boat, pero dos trabajos no pueden ir ahí: el guard que decide qué
recibe Boat y las unitarias que leen datos excluidos de Boat. Con unos 190
pushes al día, esos dos trabajos agotarían los minutos en menos de una semana.
Dos runners en la Mini los corren sin costo.

## Qué instala

`instalar.sh` instala un runner por papel:

| Papel | Trabajo | Etiqueta | Nombre |
|---|---|---|---|
| `guard` | «Política de exportación a Boat» | `jale-guard` | `mini-jale-guard` |
| `datos` | «Unitarias con datos o recursos excluidos de Boat» | `jale-datos` | `mini-jale-datos` |
| `hasito-noche` | Gate completo nocturno de Hasito sobre `main` | `hasito-noche` | `mini-hasito-noche` |
| `hasito-ci` | Lints de los PR de Hasito | `hasito-ci` | `mini-hasito-ci` o `desk-hasito-ci` |

Cada runner toma un trabajo a la vez y GitHub guarda su fila. Van separados
porque los trabajos de Boat esperan al guard, que tarda segundos: con un solo
runner, el guard haría fila detrás de unitarias de varios minutos.

Para los papeles de Jale instala:

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
plist a `.retirado-<fecha>`. No borra nada. Si GitHub ya no tiene el registro,
avisa y termina el retiro local. Si no hay registro local, porque una
instalación falló a medias, también avisa: borra entonces el runner en GitHub
o vuelve a instalarlo, que reemplaza el registro.

GitHub borra el registro de un runner que pasa 14 días sin conectarse. Para
recuperarlo, retíralo e instálalo de nuevo.

## Gate nocturno de Hasito

El tercer runner ejecuta el gate completo nocturno sobre la punta de `main`
de `SEN-HASO/Hasito`. Usa la etiqueta `hasito-noche`, el directorio
`~/actions-runner-hasito-noche`, el LaunchAgent
`local.pao.gha-runner-hasito-noche` y el log
`~/Library/Logs/pao-gha-runner-hasito-noche.log`.

`solo-main.sh` sólo acepta `GITHUB_REPOSITORY=SEN-HASO/Hasito`, el ref exacto
`SEN-HASO/Hasito/.github/workflows/gate-noche.yml@refs/heads/main` y los eventos
`schedule` o `workflow_dispatch`. Con cualquier otro valor o variable ausente
imprime una línea que identifica el valor rechazado y termina al proceso
`Runner.Worker` del trabajo. Salir con error no basta: el runner marca el
trabajo como fallido y sigue con los pasos `if: always()`. Con el proceso
terminado no corre ningún paso y GitHub muestra el trabajo como fallido. Si el
hook no encuentra ese proceso entre sus ancestros, lo dice en una segunda línea
y sale con 1.

El hook impide ramas, otros workflows y otros eventos. Dos límites quedan fuera:

- Quien pueda cambiar ese workflow en `main` de Hasito sí ejecuta código en la
  Mini.
- Un workflow reutilizable hereda la identidad de quien lo llama. Si
  `gate-noche.yml` llamara a uno, sus trabajos pasarían el hook. Por eso
  `gate-noche.yml` define sus trabajos en el propio archivo y no llama a
  workflows reutilizables.

GitHub restringe un grupo de runners a workflows concretos sólo en sus planes
Enterprise, así que el hook es la única barrera de este runner.

Para instalar, con el checkout de la Mini al día:

```sh
gh api -X POST repos/SEN-HASO/Hasito/actions/runners/registration-token -q .token \
  | ssh mini 'bash ~/.local/share/personal-agent-orchestration/fleet/gha-runner/instalar.sh hasito-noche'
```

El token llega por stdin y se pasa a `config.sh` sólo por entorno, igual que
para Jale. `instalar.sh --seco hasito-noche` anuncia todos los pasos sin escribir.
El instalador copia el hook con modo 0755 y reemplazo atómico a
`~/.local/lib/pao-gha-runner/solo-main.sh`, fuera del directorio del runner.
Conserva las demás líneas de `.env` y deja una sola línea
`ACTIONS_RUNNER_HOOK_JOB_STARTED` con la ruta absoluta de esa copia antes de
arrancar el servicio. Si falla la copia o la configuración, no carga launchd.
Una reinstalación actualiza el hook y reinicia el servicio: `.env` se lee al
arrancar. Los papeles `guard` y `datos` no reciben hook.

Comprueba en la Mini que la copia es ejecutable y que `.env` apunta a ella:

```sh
test -x "$HOME/.local/lib/pao-gha-runner/solo-main.sh"
grep '^ACTIONS_RUNNER_HOOK_JOB_STARTED=' "$HOME/actions-runner-hasito-noche/.env"
launchctl print "gui/$(id -u)/local.pao.gha-runner-hasito-noche"
```

Después de instalar o reinstalar, el log debe mostrar un nuevo
`Listening for Jobs`. Comprueba también que `mini-hasito-noche` aparece
`online` con etiqueta `hasito-noche` en
`gh api repos/SEN-HASO/Hasito/actions/runners`. Un despacho manual del workflow
desde `main` debe pasar el hook. Desde otra rama debe fallar sin ejecutar ningún
paso, tampoco uno con `if: always()`.

Para retirar:

```sh
gh api -X POST repos/SEN-HASO/Hasito/actions/runners/remove-token -q .token \
  | ssh mini 'bash ~/.local/share/personal-agent-orchestration/fleet/gha-runner/instalar.sh --retirar hasito-noche'
```

Descarga el servicio y da de baja el registro como en Jale. Conserva el
runner, el plist y la copia del hook renombrándolos a `.retirado-<fecha>`.

## CI de los PR de Hasito

El papel `hasito-ci` ejecuta los lints de los PR de `SEN-HASO/Hasito`.
Se instala una vez en cada host. Comparten la etiqueta `hasito-ci` y el
directorio `~/actions-runner-hasito-ci`. Cada runner toma un trabajo a la vez.
No lleva hook de inicio de trabajo.

Cualquier PR de Hasito que pida esa etiqueta ejecuta código con el usuario del host.
El límite de confianza es el mismo que el de `jale-guard`.

Los workflows eligen esta ruta con las variables del repositorio de Hasito
`CI_RUNS_ON_LINT` y `CI_RUNS_ON_LINT_TS`, cada una con el valor
`["self-hosted","hasito-ci"]`. Sin variable, el trabajo sigue en Boat.

Para instalar en la Mini, con su checkout de PAO al día:

```sh
gh api -X POST repos/SEN-HASO/Hasito/actions/runners/registration-token -q .token \
  | ssh mini 'bash ~/.local/share/personal-agent-orchestration/fleet/gha-runner/instalar.sh hasito-ci'
```

Para instalar en el desk, con su checkout de PAO al día:

```sh
gh api -X POST repos/SEN-HASO/Hasito/actions/runners/registration-token -q .token \
  | ssh desk 'bash ~/.local/share/personal-agent-orchestration/fleet/gha-runner/instalar.sh hasito-ci'
```

Cada instalación requiere un token propio. Viaja por stdin y se pasa a
`config.sh` por entorno. `instalar.sh --seco hasito-ci` anuncia los pasos sin
escribir. El sistema detectado elige el paquete: macOS arm64 en la Mini;
Linux x64 en el desk. Un papel exclusivo de la Mini falla en Linux.

La Mini usa el LaunchAgent `local.pao.gha-runner-hasito-ci` y el log
`~/Library/Logs/pao-gha-runner-hasito-ci.log`. El desk usa la unidad de usuario
`local.pao.gha-runner-hasito-ci.service` y el log
`~/.local/state/pao-gha-runner/hasito-ci.log`. No usa `sudo` ni `svc.sh`.
El usuario necesita un systemd de usuario activo con linger.

Los trabajos del desk corren fuera de Pueue. La unidad usa `Nice=10` y
`CPUWeight=10`, frente al nice 0 y peso de CPU por defecto (100) de Pueue.
El systemd de usuario del desk delega CPU, memoria y PIDs, pero no E/S.
El disco usa el planificador `none`, que no aplica la prioridad de `ionice`.
No se fijan `IOWeight` ni `IOSchedulingClass`: hoy sólo baja la prioridad de
CPU. Dar prioridad de E/S a Pueue requiere ajustar el host con permisos de
administrador. Windows Update puede reiniciar el desk. WSL vuelve a arrancar
al entrar por ssh; mientras está apagado, el runner de la Mini puede recibir
los trabajos.

Antes de registrar en Ubuntu 24.04, el instalador consulta `dpkg-query` y
`ldconfig -p`: requiere `libc6`, `libgcc-s1`, `libstdc++6`,
`libgssapi-krb5-2`, `libkrb5-3`, `libicu74`, `libssl3t64` y `zlib1g`.
Si falta algún paquete o biblioteca, sale con la lista.
No ejecuta `bin/installdependencies.sh`: requiere `sudo`. Un administrador
instala los paquetes que falten antes de reintentar. La lista no incluye
`liblttng-ust1t64`: ese paquete sólo sirve a las trazas de .NET, `config.sh`
no lo comprueba y la imagen oficial del runner tampoco lo instala.
Los runtimes de los lints los preparan los propios workflows.

El PATH de los trabajos es fijo. En la Mini es
`/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin`; en el desk,
`/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin`.
Una reinstalación no descarga ni registra de nuevo. Reescribe el servicio y
`.path`, y reinicia el runner. Un trabajo en curso se pierde; hay que
relanzarlo en GitHub. Si la instalación falla, el servicio queda detenido.

Comprueba desde la MacBook que ambos nombres aparecen `online` con la etiqueta:

```sh
gh api repos/SEN-HASO/Hasito/actions/runners \
  -q '.runners[] | [.name, .status, (.labels | map(.name) | join(","))] | @tsv'
```

Comprueba el servicio y un nuevo `Listening for Jobs` en cada host:

```sh
ssh mini 'launchctl print "gui/$(id -u)/local.pao.gha-runner-hasito-ci"; tail -n 20 ~/Library/Logs/pao-gha-runner-hasito-ci.log'
ssh desk 'systemctl --user status local.pao.gha-runner-hasito-ci.service; tail -n 20 ~/.local/state/pao-gha-runner/hasito-ci.log'
```

Para retirar cada runner:

```sh
gh api -X POST repos/SEN-HASO/Hasito/actions/runners/remove-token -q .token \
  | ssh mini 'bash ~/.local/share/personal-agent-orchestration/fleet/gha-runner/instalar.sh --retirar hasito-ci'
gh api -X POST repos/SEN-HASO/Hasito/actions/runners/remove-token -q .token \
  | ssh desk 'bash ~/.local/share/personal-agent-orchestration/fleet/gha-runner/instalar.sh --retirar hasito-ci'
```

El retiro detiene el servicio y da de baja el registro. En Linux también
lo deshabilita. Conserva el directorio y el plist o unidad renombrados a
`.retirado-<fecha>`. No borra nada.

## Cambiar de versión

El runner se actualiza solo: GitHub deja de asignar trabajos a una versión
vieja. `version` y `sha256` de `instalar.sh` sólo cuentan en una instalación
nueva. Al cambiarlos, copia los sha256 de `osx-arm64` y `linux-x64` de las notas de
la versión en `github.com/actions/runner/releases`.

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
  los propios runners: un trabajo a la vez cada uno. Los de Jale se reservan
  para esos dos trabajos; los pesados van a Boat o a `remote-run`. El tercer
  runner `hasito-noche` se reserva para el gate nocturno completo de Hasito.
- **Servicios de la Mini a la vista.** Un trabajo alcanza los puertos locales,
  como el Postgres de pruebas en `127.0.0.1:5435`. Un workflow que corre aquí
  no debe apuntar a ellos.
- **Mini apagada.** Los trabajos esperan en GitHub hasta 24 h y después fallan.
  En Jale, sin el guard no arranca ningún trabajo de Boat.

## Pruebas

`python3 -I -m unittest discover -s fleet/gha-runner/tests -v`. Usan dobles de
`curl`, `shasum`, `launchctl`, `systemctl`, `dpkg-query`, `ldconfig` y `uname`;
no descargan ni registran runners, ni tocan servicios reales. Ejecútalas
con `remote-run` en el desk, desde el checkout de trabajo: este repositorio
no corre pruebas en la MacBook.
