#!/bin/bash

# Los casos se invocan mediante el bucle de despacho al final.
# shellcheck disable=SC2329
set -uo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P) || exit 1
TOOL=$REPO/bin/pao-worktree
TEST_TMP=$(mktemp -d "${TMPDIR:-/tmp}/pao-worktree-test.XXXXXX") || exit 1
TEST_TMP=$(cd "$TEST_TMP" && pwd -P) || exit 1
trap 'rm -rf "$TEST_TMP"' EXIT
export XDG_STATE_HOME=$TEST_TMP/state GIT_CONFIG_GLOBAL=$TEST_TMP/gitconfig GIT_CONFIG_NOSYSTEM=1
git config --file "$GIT_CONFIG_GLOBAL" user.name prueba
git config --file "$GIT_CONFIG_GLOBAL" user.email prueba@example.invalid
git config --file "$GIT_CONFIG_GLOBAL" init.defaultBranch main

# Twig real necesita APFS o Btrfs; el doble registra desde dónde lo llaman y crea un worktree normal.
mkdir -p "$TEST_TMP/bin"
cat > "$TEST_TMP/bin/git-twig" <<'FAKE'
#!/bin/bash
[ "$1" = --version ] && { echo 'git-twig 0.3.0'; exit 0; }
[ "$1" = new ] || exit 9
shift
git rev-parse --show-toplevel > "$TWIG_LOG"
exec git worktree add --quiet "$@"
FAKE
chmod +x "$TEST_TMP/bin/git-twig"
export PATH=$TEST_TMP/bin:$PATH TWIG_LOG=$TEST_TMP/twig-cwd

assert() { "$@" || { printf 'Falló la comprobación: %s\n' "$*" >&2; return 1; }; }

fixture() {
    local dir=$TEST_TMP/$1
    git init --quiet --bare "$dir/origen.git"
    git clone --quiet "$dir/origen.git" "$dir/principal" 2>/dev/null
    git -C "$dir/principal" commit --quiet --allow-empty -m uno
    git -C "$dir/principal" push --quiet origin main
    git -C "$dir/principal" remote set-head origin main >/dev/null
    printf '%s\n' "$dir"
}

publica() {
    git -C "$1/principal" commit --quiet --allow-empty -m "$2"
    git -C "$1/principal" push --quiet origin main
    git -C "$1/principal" rev-parse HEAD
}

crea_desde_base_limpia() {
    local dir esperado base
    dir=$(fixture crea)
    esperado=$(git -C "$dir/principal" rev-parse HEAD)
    printf 'cambio local\n' > "$dir/principal/sucio.txt"
    "$TOOL" new "$dir/principal" "$dir/wt" -b rama-nueva
    base=$(cat "$TWIG_LOG")
    assert test "$(git -C "$dir/wt" rev-parse HEAD)" = "$esperado"
    assert test "$(git -C "$dir/wt" branch --show-current)" = rama-nueva
    assert test "$base" != "$dir/principal"
    assert test -z "$(git -C "$base" status --porcelain)"
    git -C "$dir/principal" worktree list --porcelain | grep -A4 "^worktree $base\$" | grep -q '^locked'
}

avanza_la_base() {
    local dir nuevo
    dir=$(fixture avanza)
    "$TOOL" new "$dir/principal" "$dir/wt1" --detach
    nuevo=$(publica "$dir" dos)
    git -C "$dir/principal" update-ref -d refs/remotes/origin/main
    "$TOOL" new "$dir/principal" "$dir/wt2" --detach
    assert test "$(git -C "$dir/wt2" rev-parse HEAD)" = "$nuevo"
    assert test "$(git -C "$(cat "$TWIG_LOG")" rev-parse HEAD)" = "$nuevo"
}

rechaza_base_sucia() {
    local dir base codigo=0
    dir=$(fixture sucia)
    "$TOOL" new "$dir/principal" "$dir/wt1" --detach
    base=$(cat "$TWIG_LOG")
    printf 'x\n' > "$base/basura.txt"
    "$TOOL" new "$dir/principal" "$dir/wt2" --detach 2>/dev/null || codigo=$?
    assert test "$codigo" = 3
    assert test ! -e "$dir/wt2"
}

ruta_relativa_al_llamador() {
    local dir
    dir=$(fixture relativa)
    (cd "$dir" && "$TOOL" new principal sub/wt --detach)
    assert test -d "$dir/sub/wt/.git" -o -f "$dir/sub/wt/.git"
}

errores_de_uso() {
    local dir codigo
    dir=$(fixture uso)
    codigo=0; "$TOOL" new "$dir/principal" "$dir/wt" 2>/dev/null || codigo=$?
    assert test "$codigo" = 2
    mkdir "$dir/existe"
    codigo=0; "$TOOL" new "$dir/principal" "$dir/existe" --detach 2>/dev/null || codigo=$?
    assert test "$codigo" = 2
    codigo=0; PATH=/usr/bin:/bin "$TOOL" new "$dir/principal" "$dir/wt" --detach 2>/dev/null || codigo=$?
    assert test "$codigo" = 4
}

TEST_CASES=(crea_desde_base_limpia avanza_la_base rechaza_base_sucia ruta_relativa_al_llamador errores_de_uso)
CASE=0 FAILURES=0
for test_case in "${TEST_CASES[@]}"; do
    CASE=$((CASE + 1))
    (set -e; "$test_case") > "$TEST_TMP/$test_case.log" 2>&1
    result=$?
    if [ "$result" = 0 ]; then
        printf 'PASS %s\n' "$test_case"
    else
        printf 'FAIL %s\n' "$test_case"; cat "$TEST_TMP/$test_case.log"; FAILURES=$((FAILURES + 1))
    fi
done
printf '%s casos, %s fallos\n' "$CASE" "$FAILURES"
exit "$FAILURES"
