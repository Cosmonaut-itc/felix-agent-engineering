#!/bin/bash
# Cases are dispatched below.
# shellcheck disable=SC2329
set -uo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P) || exit 1
SCRIPT=$REPO/skills/felix-mode/scripts/pstack-refs
TEST_TMP=$(mktemp -d "${TMPDIR:-/tmp}/pstack-refs-test.XXXXXX") || exit 1
TEST_TMP=$(cd "$TEST_TMP" && pwd -P) || exit 1
export HOME=$TEST_TMP/home
mkdir -p "$HOME"
trap 'rm -rf "$TEST_TMP"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
assert() { "$@" || { printf 'Falló la comprobación: %s\n' "$*" >&2; exit 1; }; }
fixture() {
    ROOT=$TEST_TMP/$CASE/root\ space
    PSTACK=$TEST_TMP/$CASE/pstack\ space
    SKILL=$ROOT/skills/felix-mode
    mkdir -p "$SKILL/scripts" "$SKILL/references" "$PSTACK"
    printf 'playbook\tpaso\tancla\nShipping\t1\tResolve the forge.\n' > "$SKILL/scripts/pstack-refs.tsv"
    printf 'Consulta Shipping 1.\n' > "$SKILL/SKILL.md"
    cat > "$PSTACK/shipping.md" <<'BOOK'
### Shipping
1. **Resolve the forge.** Start here.
   An indented paragraph belongs to this step.
2. **Land safely.** Finish here.
**Reply:** stop.
BOOK
}
expect() {
    local expected=$1 key=$2 result=0
    shift 2
    "$SCRIPT" "$@" > "$TEST_TMP/output" 2>&1 || result=$?
    assert test "$result" = "$expected" || return 1
    assert grep -F -- "$key" "$TEST_TMP/output"
}
check() { expect "$1" "$2" --root "$ROOT" --pstack "$PSTACK"; }
all_green() { fixture; check 0 'ok: 1 citas; pstack ' && assert grep -F "$PSTACK" "$TEST_TMP/output"; }
missing_row() {
    fixture
    printf 'Ahora Shipping 2.\n' >> "$SKILL/references/merge.md"
    check 1 'references/merge.md:1 cita «Shipping 2»' &&
        assert grep -F $'Añade la fila: Shipping\t2\t<título del paso>' "$TEST_TMP/output"
}
unused_row() {
    fixture
    printf 'Shipping\t2\tLand safely.\n' >> "$SKILL/scripts/pstack-refs.tsv"
    check 1 'fila sin cita: Shipping 2'
}
renumbered() {
    fixture
    sed 's/^1\. /3. /' "$PSTACK/shipping.md" > "$TEST_TMP/book"
    mv "$TEST_TMP/book" "$PSTACK/shipping.md"
    check 1 'Shipping 1: ancla movida al paso 3'
}
lost_anchor() {
    fixture
    printf '### Shipping\n1. **Different task.**\n' > "$PSTACK/shipping.md"
    check 1 'Shipping 1: ancla desaparecida: Resolve the forge.'
}
missing_step() {
    fixture
    printf '### Shipping\n2. **Other task.**\n' > "$PSTACK/shipping.md"
    check 1 'Shipping 1: paso ausente; ancla desaparecida'
}
missing_book() {
    fixture
    mv "$PSTACK/shipping.md" "$PSTACK/other.md"
    check 1 'Shipping 1: playbook ausente: shipping.md'
}
registry_only() {
    fixture
    rm -rf "$PSTACK"
    expect 0 'ok: 1 citas; registro sin pstack' --registry-only --root "$ROOT"
}
not_installed() {
    fixture
    expect 2 'pstack no encontrado:' --root "$ROOT" --pstack "$TEST_TMP/absent"
    expect 2 'pstack@pstack-claude en installed_plugins.json' --root "$ROOT"
}
citation_forms() {
    fixture
    printf 'Shipping\t2\tLand safely.\n' >> "$SKILL/scripts/pstack-refs.tsv"
    printf 'Shipping 1; Shipping 1/2; Shipping, paso 1; paso 1 del playbook Shipping.\n' > "$SKILL/SKILL.md"
    check 0 'ok: 5 citas;'
}
split_line() {
    fixture
    printf 'Introducción.\nShipping\n\n  1.\n' > "$SKILL/SKILL.md"
    check 0 'ok: 1 citas;' || return 1
    printf 'Shipping\n  2.\n' > "$SKILL/SKILL.md"
    check 1 'SKILL.md:1 cita «Shipping 2»'
}
unregistered_playbook() {
    fixture
    printf 'Ver Bug fix 2.\n' >> "$SKILL/SKILL.md"
    expect 1 'SKILL.md:2 cita «Bug fix 2»' --registry-only --root "$ROOT"
}
slash_split() {
    fixture
    printf 'Shipping 1/\n2.\n' > "$SKILL/SKILL.md"
    check 1 'cita «Shipping 2»' || return 1
    printf 'Shipping 1 / 2.\n' > "$SKILL/SKILL.md"
    check 1 'cita «Shipping 2»'
}
crlf() {
    fixture
    local file
    for file in "$SKILL/SKILL.md" "$SKILL/scripts/pstack-refs.tsv" "$PSTACK/shipping.md"; do
        sed 's/$/\r/' "$file" > "$TEST_TMP/crlf"
        mv "$TEST_TMP/crlf" "$file"
    done
    check 0 'ok: 1 citas;'
}
local_docs() {
    fixture
    printf 'paso 6 de [Acumulador](acumulador.md), LocalShipping 2 y Shipping-extra 3.\n' >> "$SKILL/SKILL.md"
    check 0 'ok: 1 citas;'
}
new_title() {
    fixture
    printf '### New playbook\n1. **Start.**\n' > "$PSTACK/new-playbook.md"
    printf 'New playbook, paso 1.\n' >> "$SKILL/SKILL.md"
    check 1 'cita «New playbook 1»'
}
step_boundaries() {
    fixture
    printf 'playbook\tpaso\tancla\nShipping\t1\tAn indented paragraph belongs\n' > "$SKILL/scripts/pstack-refs.tsv"
    check 1 'ancla desaparecida: An indented paragraph belongs' || return 1
    printf 'playbook\tpaso\tancla\nShipping\t1\tstop.\n' > "$SKILL/scripts/pstack-refs.tsv"
    check 1 'ancla desaparecida: stop.' || return 1
    printf '### Shipping\n1. **Start.**\n# Heading\nstop.\n' > "$PSTACK/shipping.md"
    check 1 'ancla desaparecida: stop.'
}
tab_marker() {
    fixture
    printf '### Shipping\n1. **Start.**\n2.\t**Resolve the forge.**\n' > "$PSTACK/shipping.md"
    check 1 'Shipping 1: ancla movida al paso 2'
}
empty_marker() {
    fixture
    printf '### Shipping\n1. **Start.**\n2.\n   **Resolve the forge.**\n' > "$PSTACK/shipping.md"
    check 1 'Shipping 1: ancla movida al paso 2' || return 1
    printf '### Shipping\n1.\n   **Resolve the forge.**\n' > "$PSTACK/shipping.md"
    check 0 'ok: 1 citas;'
}
unnamed_playbook() {
    fixture
    printf '### Extra\n1. **Start.**\n' > "$PSTACK/extra.md"
    check 1 'pstack tiene extra.md'
}
installed_path() {
    fixture
    mkdir -p "$HOME/.claude/plugins" "$TEST_TMP/install/skills/poteto-mode"
    cp -R "$PSTACK" "$TEST_TMP/install/skills/poteto-mode/playbooks"
    # Include another plugin before pstack and a compact CRLF JSON document.
    printf '{"plugins":{"other":[{"installPath":"/wrong"}],"pstack@pstack-claude":[{"scope":"user","installPath":"%s"}]}}\r\n' \
        "$TEST_TMP/install" > "$HOME/.claude/plugins/installed_plugins.json"
    expect 0 "pstack $TEST_TMP/install/skills/poteto-mode/playbooks" --root "$ROOT"
    rm "$HOME/.claude/plugins/installed_plugins.json"
}
usage() { expect 2 'Uso:' --root; }
real_registry() { expect 0 'ok:' --registry-only --root "$REPO"; }
failed=0
for CASE in all_green missing_row unused_row renumbered lost_anchor missing_step missing_book registry_only not_installed citation_forms split_line unregistered_playbook slash_split crlf local_docs new_title step_boundaries tab_marker empty_marker unnamed_playbook installed_path usage real_registry; do
    if (set -e; "$CASE"); then printf 'ok %s\n' "$CASE"; else printf 'FAIL %s\n' "$CASE" >&2; failed=$((failed + 1)); fi
done
[ "$failed" = 0 ]
