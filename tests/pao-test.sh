#!/bin/bash

# Cases and their helpers are invoked through the test-case dispatch loop.
# shellcheck disable=SC2329

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P) || exit 1
TEST_BASH=${PAO_TEST_BASH:-$BASH}
TEST_TMP=$(mktemp -d "${TMPDIR:-/tmp}/pao-test.XXXXXX") || exit 1
export HOME=$TEST_TMP/bootstrap-home
export XDG_STATE_HOME=$HOME/state
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
export GIT_AUTHOR_NAME=pao-test GIT_AUTHOR_EMAIL=pao-test@example.invalid
export GIT_COMMITTER_NAME=$GIT_AUTHOR_NAME GIT_COMMITTER_EMAIL=$GIT_AUTHOR_EMAIL
export MSYS=winsymlinks:nativestrict
unset PAO_MANIFEST PAO_NO_PLUGINS PAO_LOCK_PID PAO_UPDATE_ERROR PAO_UPDATE_WARN
mkdir -p "$HOME" "$TEST_TMP/guards"
trap 'rm -rf "$TEST_TMP"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for guarded in launchctl crontab schtasks claude codex; do
    cat > "$TEST_TMP/guards/$guarded" <<'GUARD'
#!/bin/bash
printf 'forbidden external command: %s\n' "$0" >&2
exit 99
GUARD
    chmod +x "$TEST_TMP/guards/$guarded"
done
printf '#!/bin/bash\nexit 0\n' > "$TEST_TMP/guards/osascript"
chmod +x "$TEST_TMP/guards/osascript"
REAL_RM=$(command -v rm)
cat > "$TEST_TMP/guards/rm" <<GUARD
#!/bin/bash
if [ -e "\$HOME/fail-rm" ]; then
    for arg in "\$@"; do case "\$arg" in *pao-readme) exit 1 ;; esac; done
fi
exec "$REAL_RM" "\$@"
GUARD
chmod +x "$TEST_TMP/guards/rm"
export PATH=$TEST_TMP/guards:$PATH

prepare_template() {
    git clone --quiet --no-local "$REPO" "$TEST_TMP/template" || return 1
    local path
    for path in bin tests manifest vendor .gitattributes .github README.md docs configs skills; do
        [ ! -e "$REPO/$path" ] || cp -R "$REPO/$path" "$TEST_TMP/template/" || return 1
    done
    git -C "$TEST_TMP/template" add -A || return 1
    git -C "$TEST_TMP/template" -c core.hooksPath=/dev/null commit --quiet --allow-empty -m 'test fixture' || return 1
    git -C "$TEST_TMP/template" checkout --quiet -B main || return 1
}

fixture() {
    local dir=${1:-$TEST_TMP/case-$CASE}
    mkdir -p "$dir/home space"
    export HOME=$dir/home\ space XDG_STATE_HOME=$dir/home\ space/state
    export PAO_ROOT=$dir/clone\ space
    git clone --quiet --bare "$TEST_TMP/template" "$dir/origin.git"
    git clone --quiet "$dir/origin.git" "$PAO_ROOT"
    PAO_ROOT=$(cd "$PAO_ROOT" && pwd -P)
    export PAO_MANIFEST=$PAO_ROOT/manifest
    STATE=$XDG_STATE_HOME/pao
}

pao() { "$TEST_BASH" "$PAO_ROOT/bin/pao" "$@"; }
install_pao() { pao install --no-schedule --no-plugins; }
assert() { "$@" || { printf 'assertion failed: %s\n' "$*" >&2; return 1; }; }

clean_install() {
    fixture
    install_pao
    local kind src dest _extra count=0
    while read -r kind src dest _extra; do
        case "$kind" in
            file)
                dest=$HOME/${dest#\~/}
                assert test -L "$dest"
                assert test "$(readlink "$dest")" = "$PAO_ROOT/$src"
                count=$((count + 1)) ;;
            skill)
                assert test "$(readlink "$HOME/.agents/skills/$dest")" = "$PAO_ROOT/$src"
                assert test "$(readlink "$HOME/.claude/skills/$dest")" = "$PAO_ROOT/$src"
                count=$((count + 2)) ;;
        esac
    done < "$PAO_MANIFEST"
    assert test "$count" = 53
    assert test ! -e "$HOME/.codex/opencode.json"
    assert test ! -e "$HOME/Library/LaunchAgents/com.github.cosmonaut-itc.pao.plist"
    assert grep -q '^overall ok$' "$STATE/status"
}

idempotent_install() {
    fixture
    install_pao
    install_pao > "$HOME/second"
    assert grep -qx unchanged "$HOME/second"
    assert test ! -e "$STATE/backups"
    assert grep -q '^overall ok$' "$STATE/status"
}

backup_real_files() {
    fixture
    mkdir -p "$HOME/.codex" "$HOME/.claude"
    cp "$PAO_ROOT/configs/codex/AGENTS.md" "$HOME/.codex/AGENTS.md"
    printf 'personal rules\n' > "$HOME/.claude/CLAUDE.md"
    install_pao
    local identical different
    identical=$(find "$STATE/backups" -path '*/.codex/AGENTS.md')
    different=$(find "$STATE/backups" -path '*/.claude/CLAUDE.md')
    assert cmp -s "$identical" "$PAO_ROOT/configs/codex/AGENTS.md"
    assert grep -qx 'personal rules' "$different"
    assert test -L "$HOME/.codex/AGENTS.md"
    assert test -L "$HOME/.claude/CLAUDE.md"
}

backup_real_directory() {
    fixture
    mkdir -p "$HOME/.agents/skills/felix-mode"
    printf 'preserve\n' > "$HOME/.agents/skills/felix-mode/local"
    install_pao
    local saved
    saved=$(find "$STATE/backups" -path '*/felix-mode/local')
    assert grep -qx preserve "$saved"
    assert test -L "$HOME/.agents/skills/felix-mode"
}

held_file() {
    fixture
    install_pao
    rm "$HOME/.codex/AGENTS.md"
    printf 'local\n' > "$HOME/.codex/AGENTS.md"
    pao sync
    assert test ! -L "$HOME/.codex/AGENTS.md"
    assert grep -qx local "$HOME/.codex/AGENTS.md"
    assert grep -q '^held' "$STATE/status"
    assert grep -q '^overall warn$' "$STATE/status"
    assert test ! -d "$STATE/backups"
}

retire_both() {
    fixture
    mkdir -p "$HOME/.agents/skills/tdd" "$HOME/.claude/skills" "$HOME/.agents/skills-retired/tdd"
    printf 'retired\n' > "$HOME/.agents/skills/tdd/local"
    ln -s "$PAO_ROOT/skills/felix-mode" "$HOME/.claude/skills/tdd"
    install_pao
    assert test ! -e "$HOME/.agents/skills/tdd"
    assert test ! -L "$HOME/.claude/skills/tdd"
    local saved
    saved=$(find "$HOME/.agents/skills-retired" -name local)
    assert grep -qx retired "$saved"
    assert test -d "$HOME/.agents/skills-retired/tdd"
}

prune_removed() {
    fixture
    install_pao
    printf '\nfile README.md ~/.custom/pao-readme\n' >> "$PAO_MANIFEST"
    install_pao
    sed '/^skill .* research$/d; /file README.md /d; /file docs\/agent-routing.md /d' "$PAO_MANIFEST" > "$HOME/manifest-altered"
    export PAO_MANIFEST=$HOME/manifest-altered
    ln -s "$HOME/outside" "$HOME/.agents/skills/unmanaged"
    ln -s "$PAO_ROOT/README.md" "$HOME/.codex/unmanaged-file"
    ln -s "$PAO_ROOT/../outside" "$HOME/.agents/skills/outside-via-dotdot"
    ln -s "$PAO_ROOT/missing" "$HOME/.agents/skills/stale"
    ln -s '../../../clone space/missing' "$HOME/.claude/skills/relative-stale"
    pao sync
    assert test ! -L "$HOME/.agents/skills/research"
    assert test ! -L "$HOME/.claude/skills/research"
    assert test ! -L "$HOME/.agents/docs/agent-routing.md"
    assert test ! -L "$HOME/.custom/pao-readme"
    assert test ! -L "$HOME/.agents/skills/stale"
    assert test ! -L "$HOME/.claude/skills/relative-stale"
    assert test -L "$HOME/.agents/skills/unmanaged"
    assert test -L "$HOME/.agents/skills/outside-via-dotdot"
    assert test -L "$HOME/.codex/unmanaged-file"
}

incoming() {
    local dir=$TEST_TMP/case-$CASE
    git clone --quiet "$dir/origin.git" "$dir/producer"
    printf '\nincoming change\n' >> "$dir/producer/docs/orchestration.md"
    printf 'new file\n' > "$dir/producer/docs/pao-new.md"
    printf '\nfile docs/pao-new.md ~/.agents/docs/pao-new.md\n' >> "$dir/producer/manifest"
    sed '$d; s/^main() {/main_before_update() {/' "$dir/producer/bin/pao" > "$dir/updated-pao"
    cat >> "$dir/updated-pao" <<'RELEASE'
main() {
    if [ "${1:-}" = sync ]; then printf 'new script\n' > "$HOME/new-script-ran"; fi
    main_before_update "$@"
}
main "$@"; exit $?
RELEASE
    cat "$dir/updated-pao" > "$dir/producer/bin/pao"
    git -C "$dir/producer" add .
    git -C "$dir/producer" -c core.hooksPath=/dev/null commit --quiet -m incoming
    git -C "$dir/producer" push --quiet origin main
}

update_clean() {
    fixture
    install_pao
    local before
    before=$(git -C "$PAO_ROOT" rev-parse HEAD)
    incoming
    pao update
    assert test "$(git -C "$PAO_ROOT" rev-parse HEAD)" != "$before"
    assert grep -q 'incoming change' "$HOME/.agents/docs/orchestration.md"
    assert grep -qx 'new script' "$HOME/new-script-ran"
    assert test "$(readlink "$HOME/.agents/docs/pao-new.md")" = "$PAO_ROOT/docs/pao-new.md"
    assert grep -q '^overall ok$' "$STATE/status"
    assert test ! -d "$STATE/lock"
}

update_conflict() {
    fixture
    install_pao
    printf '\nlocal change\n' >> "$PAO_ROOT/docs/orchestration.md"
    cp "$PAO_ROOT/docs/orchestration.md" "$HOME/expected"
    local before result
    before=$(git -C "$PAO_ROOT" rev-parse HEAD)
    incoming
    set +e
    pao update
    result=$?
    set -e
    assert test "$result" != 0
    assert test "$(git -C "$PAO_ROOT" rev-parse HEAD)" = "$before"
    assert cmp -s "$HOME/expected" "$PAO_ROOT/docs/orchestration.md"
    assert grep -q '^overall error$' "$STATE/status"
    assert grep -q 'docs/orchestration.md' "$STATE/status"
    assert test ! -d "$STATE/lock"
}

bad_manifest() {
    fixture
    local row
    for row in 'unknown whatever' 'skill skills/felix-mode other-name' 'retire felix-mode' 'file README.md ~/.codex/AGENTS.md' 'file ../README.md ~/.bad' 'file README.md /tmp/escape'; do
        cp "$PAO_ROOT/manifest" "$HOME/broken"
        printf '%s\n' "$row" >> "$HOME/broken"
        if PAO_MANIFEST=$HOME/broken pao check; then return 1; fi
    done
}

install_branch_guard() {
    fixture
    git -C "$PAO_ROOT" checkout -qb feature
    if install_pao; then return 1; fi
    assert test ! -e "$STATE/env"
    assert test ! -e "$HOME/.codex/AGENTS.md"
}

lock_behavior() {
    fixture
    install_pao
    sleep 300 &
    local owner=$!
    mkdir "$STATE/lock"
    printf '%s\n' "$owner" > "$STATE/lock/pid"
    pao update > "$HOME/locked"
    assert grep -qx locked "$HOME/locked"
    kill "$owner"
    wait "$owner" 2>/dev/null || true
    pao update
    assert test ! -d "$STATE/lock"
}

update_feature_branch() {
    fixture
    install_pao
    git -C "$PAO_ROOT" checkout -qb feature
    local before
    before=$(git -C "$PAO_ROOT" rev-parse HEAD)
    incoming
    pao update
    assert test "$(git -C "$PAO_ROOT" rev-parse HEAD)" = "$before"
    assert grep -q '^overall warn$' "$STATE/status"
}

uninstall_safe() {
    fixture
    mkdir -p "$HOME/.codex"
    printf 'saved\n' > "$HOME/.codex/AGENTS.md"
    install_pao
    ln -s "$HOME/outside" "$HOME/.agents/skills/unmanaged"
    pao uninstall
    assert test ! -L "$HOME/.codex/AGENTS.md"
    assert test ! -L "$HOME/.agents/skills/felix-mode"
    assert test -L "$HOME/.agents/skills/unmanaged"
    local saved
    saved=$(find "$STATE/backups" -path '*/.codex/AGENTS.md')
    assert grep -qx saved "$saved"
}


sync_invalid_manifest() {
    fixture
    install_pao
    printf 'unknown broken\n' >> "$PAO_MANIFEST"
    if pao sync; then return 1; fi
    assert grep -q '^overall error$' "$STATE/status"
    assert grep -q 'manifest' "$STATE/status"
    assert test -L "$HOME/.codex/AGENTS.md"
}

update_divergence() {
    fixture
    install_pao
    printf 'local commit\n' > "$PAO_ROOT/local.txt"
    git -C "$PAO_ROOT" add local.txt
    git -C "$PAO_ROOT" -c core.hooksPath=/dev/null commit --quiet -m local
    local before
    before=$(git -C "$PAO_ROOT" rev-parse HEAD)
    incoming
    if pao update; then return 1; fi
    assert test "$(git -C "$PAO_ROOT" rev-parse HEAD)" = "$before"
    assert grep -qx 'local commit' "$PAO_ROOT/local.txt"
    assert grep -q '^overall error$' "$STATE/status"
    assert test ! -d "$STATE/lock"
}

update_fetch_failure() {
    fixture
    install_pao
    git -C "$PAO_ROOT" remote set-url origin "$HOME/missing-origin"
    rm "$HOME/.codex/AGENTS.md"
    if pao update; then return 1; fi
    assert test -L "$HOME/.codex/AGENTS.md"
    assert grep -q '^overall error$' "$STATE/status"
    assert grep -q fetch "$STATE/status"
    assert test ! -d "$STATE/lock"
}

update_fetch_failure_recent() {
    fixture
    install_pao
    git -C "$PAO_ROOT" remote set-url origin "$HOME/missing-origin"
    date +%s > "$STATE/last-fetch"
    pao update
    assert grep -q '^overall warn$' "$STATE/status"
    assert grep -q fetch "$STATE/status"
}

install_locked() {
    fixture
    sleep 300 &
    local owner=$! result=0
    mkdir -p "$STATE/lock"
    printf '%s\n' "$owner" > "$STATE/lock/pid"
    if install_pao 2> "$HOME/err"; then result=1; fi
    kill "$owner"
    wait "$owner" 2>/dev/null || true
    [ "$result" = 0 ] || return 1
    assert grep -q 'en curso' "$HOME/err"
    assert test ! -e "$HOME/.codex/AGENTS.md"
    assert test -d "$STATE/lock"
}

uninstall_locked() {
    fixture
    install_pao
    sleep 300 &
    local owner=$! result=0
    mkdir "$STATE/lock"
    printf '%s\n' "$owner" > "$STATE/lock/pid"
    if pao uninstall 2> "$HOME/err"; then result=1; fi
    kill "$owner"
    wait "$owner" 2>/dev/null || true
    [ "$result" = 0 ] || return 1
    assert grep -q 'en curso' "$HOME/err"
    assert test -L "$HOME/.codex/AGENTS.md"
}

vendor_sync_dirty() {
    fixture
    printf 'local edit\n' >> "$PAO_ROOT/vendor/mattpocock/grilling/SKILL.md"
    if pao vendor-sync 2> "$HOME/err"; then return 1; fi
    assert grep -q 'cambios locales' "$HOME/err"
    assert grep -q 'local edit' "$PAO_ROOT/vendor/mattpocock/grilling/SKILL.md"
}

update_ignored_file() {
    fixture
    install_pao
    local dir=$TEST_TMP/case-$CASE
    git clone --quiet "$dir/origin.git" "$dir/producer"
    mkdir -p "$dir/producer/node_modules"
    printf 'upstream\n' > "$dir/producer/node_modules/pao-ignored"
    git -C "$dir/producer" add -f node_modules/pao-ignored
    git -C "$dir/producer" -c core.hooksPath=/dev/null commit --quiet -m ignored
    git -C "$dir/producer" push --quiet origin main
    mkdir -p "$PAO_ROOT/node_modules"
    printf 'local\n' > "$PAO_ROOT/node_modules/pao-ignored"
    if pao update; then return 1; fi
    assert grep -qx local "$PAO_ROOT/node_modules/pao-ignored"
    assert grep -q '^overall error$' "$STATE/status"
}

prune_retry() {
    fixture
    printf '\nfile README.md ~/.custom/pao-readme\n' >> "$PAO_MANIFEST"
    install_pao
    sed '/file README.md /d' "$PAO_MANIFEST" > "$HOME/manifest-altered"
    export PAO_MANIFEST=$HOME/manifest-altered
    touch "$HOME/fail-rm"
    pao sync || true
    rm "$HOME/fail-rm"
    assert test -L "$HOME/.custom/pao-readme"
    pao sync
    assert test ! -L "$HOME/.custom/pao-readme"
}

vendor_sync_ignored() {
    fixture
    mkdir -p "$PAO_ROOT/vendor/mattpocock/grilling/node_modules"
    printf 'local\n' > "$PAO_ROOT/vendor/mattpocock/grilling/node_modules/pao-local"
    if pao vendor-sync 2> "$HOME/err"; then return 1; fi
    assert grep -q 'cambios locales' "$HOME/err"
    assert test -f "$PAO_ROOT/vendor/mattpocock/grilling/node_modules/pao-local"
}

minimal_path() {
    fixture
    install_pao
    env PATH=/nonexistent "$TEST_BASH" "$PAO_ROOT/bin/pao" sync > "$HOME/out"
    assert grep -q '^overall ok$' "$STATE/status"
}

held_directory_and_foreign_link() {
    fixture
    install_pao
    rm "$HOME/.agents/skills/felix-mode" "$HOME/.claude/skills/felix-mode"
    mkdir "$HOME/.agents/skills/felix-mode"
    printf 'local\n' > "$HOME/.agents/skills/felix-mode/local"
    ln -s "$HOME/missing" "$HOME/.claude/skills/felix-mode"
    pao sync
    assert grep -qx local "$HOME/.agents/skills/felix-mode/local"
    assert test ! -L "$HOME/.agents/skills/felix-mode"
    assert test "$(readlink "$HOME/.claude/skills/felix-mode")" = "$PAO_ROOT/skills/felix-mode"
    assert grep -q '^overall warn$' "$STATE/status"
}

status_and_duplicate_warning() {
    fixture
    mkdir -p "$HOME/.claude"
    printf '{"enabledPlugins":{"mattpocock-skills@demo":\n true,"mattpocock-skills@off":false}}\n' > "$HOME/.claude/settings.json"
    cp "$HOME/.claude/settings.json" "$HOME/settings-original"
    install_pao
    assert grep -q '^overall warn$' "$STATE/status"
    assert grep -q 'claude plugin uninstall mattpocock-skills@demo' "$STATE/status"
    assert cmp -s "$HOME/settings-original" "$HOME/.claude/settings.json"
    printf 'dirty\n' >> "$PAO_ROOT/README.md"
    pao status > "$HOME/status-output"
    assert grep -q '^branch main$' "$HOME/status-output"
    assert grep -q '^ahead 0 behind 0$' "$HOME/status-output"
    assert grep -q 'README.md' "$HOME/status-output"
    assert grep -q '^job absent$' "$HOME/status-output"
}

uninstall_rm_failure() {
    fixture
    printf '\nfile README.md ~/.custom/pao-readme\n' >> "$PAO_MANIFEST"
    install_pao
    touch "$HOME/fail-rm"
    if pao uninstall > "$HOME/out" 2> "$HOME/err"; then return 1; fi
    rm "$HOME/fail-rm"
    assert grep -q 'pendiente: .*pao-readme' "$HOME/err"
    assert test ! -s "$HOME/out"
    assert test -L "$HOME/.custom/pao-readme"
    assert test ! -L "$HOME/.agents/skills/felix-mode"
    pao uninstall
    assert test ! -L "$HOME/.custom/pao-readme"
}

windows_job_backup() {
    # schtasks caps /TR at 261 characters, so this case needs a short root.
    # short stays global: the EXIT trap runs after the function returns.
    local saved
    short=$(mktemp -d /tmp/pao.XXXXXX)
    trap 'rm -rf "$short"' EXIT
    fixture "$short"
    install_pao
    mkdir "$short/stubs"
    cat > "$short/stubs/uname" <<'STUB'
#!/bin/bash
printf 'MINGW64_NT-10.0\n'
STUB
    cat > "$short/stubs/cygpath" <<'STUB'
#!/bin/bash
printf '%s\n' "$2"
STUB
    cat > "$short/stubs/schtasks" <<'STUB'
#!/bin/bash
case "$*" in
    *Create*) printf '%s\n' "$*" > "$HOME/task" ;;
    *XML*) [ ! -e "$HOME/xml-fail" ] || exit 1; [ -e "$HOME/xml-empty" ] || printf '<Task>%s</Task>\n' "$(cat "$HOME/task")" ;;
    *) [ -e "$HOME/task" ] ;;
esac
STUB
    chmod +x "$short/stubs/uname" "$short/stubs/cygpath" "$short/stubs/schtasks"
    export PATH=$short/stubs:$PATH
    printf 'foreign\n' > "$HOME/task"
    touch "$HOME/xml-fail"
    if pao install --no-plugins; then return 1; fi
    assert grep -q 'no se pudo respaldar la tarea' "$STATE/status"
    assert grep -qx foreign "$HOME/task"
    rm "$HOME/xml-fail"
    touch "$HOME/xml-empty"
    if pao install --no-plugins; then return 1; fi
    assert grep -qx foreign "$HOME/task"
    rm "$HOME/xml-empty"
    pao install --no-plugins
    assert grep -q Create "$HOME/task"
    saved=$(find "$STATE/backups" -name pao-update.xml)
    assert grep -qx '<Task>foreign</Task>' "$saved"
}

if ! prepare_template > "$TEST_TMP/setup.log" 2>&1; then cat "$TEST_TMP/setup.log"; exit 1; fi
FAILURES=0 CASE=0
for test_case in clean_install idempotent_install backup_real_files backup_real_directory held_file retire_both prune_removed update_clean update_conflict bad_manifest install_branch_guard lock_behavior update_feature_branch uninstall_safe sync_invalid_manifest update_divergence update_fetch_failure update_fetch_failure_recent install_locked uninstall_locked vendor_sync_dirty vendor_sync_ignored update_ignored_file prune_retry minimal_path held_directory_and_foreign_link status_and_duplicate_warning uninstall_rm_failure windows_job_backup; do
    CASE=$((CASE + 1))
    (set -e; "$test_case") > "$TEST_TMP/$test_case.log" 2>&1
    result=$?
    if [ "$result" = 0 ]; then printf 'PASS %s\n' "$test_case";
    else printf 'FAIL %s\n' "$test_case"; cat "$TEST_TMP/$test_case.log"; FAILURES=$((FAILURES + 1)); fi
done
printf '%s cases, %s failures\n' "$CASE" "$FAILURES"
exit "$FAILURES"
