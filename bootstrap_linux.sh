#!/usr/bin/env bash
#
# Tianlai's minimal Linux bootstrap.
#
# This installs the Python runtime and MCP extra only. It deliberately does not
# download multi-gigabyte third-party audio assets or install FluidSynth.

set -Eeuo pipefail

usage() {
    cat <<'EOF'
Usage: bash ./bootstrap_linux.sh [options]

Options:
  --python PATH          Use a specific interpreter when creating .venv.
  --skip-smoke           Skip the self-contained first-sound render.
  --portable-tests       Install development dependencies and run the portable
                         test suite (no external audio assets required).
  -h, --help             Show this help.

Supported runtime: 64-bit CPython 3.11-3.14.
EOF
}

fail() {
    printf 'Tianlai Linux bootstrap failed: %s\n' "$*" >&2
    exit 2
}

run() {
    printf '+'
    printf ' %q' "$@"
    printf '\n'
    "$@"
}

skip_smoke=0
portable_tests=0
requested_python=''

while (($# > 0)); do
    case "$1" in
        --python)
            (($# >= 2)) || fail '--python requires an interpreter path'
            requested_python=$2
            shift 2
            ;;
        --python=*)
            requested_python=${1#*=}
            [[ -n "$requested_python" ]] ||
                fail '--python requires an interpreter path'
            shift
            ;;
        --skip-smoke)
            skip_smoke=1
            shift
            ;;
        --portable-tests)
            portable_tests=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "unknown option: $1"
            ;;
    esac
done

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
venv_root="$root/.venv"
venv_python="$venv_root/bin/python"

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
export PIP_DISABLE_PIP_VERSION_CHECK=1

[[ -f "$root/pyproject.toml" ]] ||
    fail 'the source root is incomplete: pyproject.toml is missing'
[[ -f "$root/乐器/测试工具/参考振荡器/乐器.json" ]] ||
    fail 'the source root is incomplete: the reference oscillator is missing'

python_facts() {
    "$1" -c \
        'import struct,sys; print(f"{sys.implementation.name}|{sys.version_info.major}.{sys.version_info.minor}|{struct.calcsize(chr(80))*8}")'
}

facts_are_supported() {
    local facts=$1
    local implementation=${facts%%|*}
    local version_and_bits=${facts#*|}
    local version=${version_and_bits%%|*}
    local bits=${version_and_bits#*|}
    [[ "$implementation" == 'cpython' ]] || return 1
    case "$version" in
        3.11|3.12|3.13|3.14) ;;
        *) return 1 ;;
    esac
    [[ "$bits" == '64' ]]
}

describe_unsupported_facts() {
    local label=$1
    local facts=$2
    local implementation=${facts%%|*}
    local version_and_bits=${facts#*|}
    local version=${version_and_bits%%|*}
    local bits=${version_and_bits#*|}
    if [[ "$implementation" != 'cpython' ]]; then
        printf '%s: %s is unsupported; CPython is required' \
            "$label" "$implementation"
    elif [[ "$version" != '3.11' &&
          "$version" != '3.12' &&
          "$version" != '3.13' &&
          "$version" != '3.14' ]]; then
        printf '%s: Python %s is outside the validated 3.11-3.14 range' \
            "$label" "$version"
    elif [[ "$bits" != '64' ]]; then
        printf '%s: %s-bit Python is unsupported' "$label" "$bits"
    else
        printf '%s: unsupported interpreter' "$label"
    fi
}

find_python() {
    local -a candidates=()
    local -a observations=()
    local candidate resolved facts
    if [[ -n "$requested_python" ]]; then
        candidates=("$requested_python")
    else
        candidates=(
            python3.14
            python3.13
            python3.12
            python3.11
            python3
            python
        )
    fi

    for candidate in "${candidates[@]}"; do
        resolved=$(command -v "$candidate" 2>/dev/null || true)
        if [[ -z "$resolved" ]]; then
            observations+=("$candidate: not found")
            continue
        fi
        if ! facts=$(python_facts "$resolved" 2>/dev/null); then
            observations+=("$candidate: could not read version/architecture")
            continue
        fi
        if facts_are_supported "$facts"; then
            printf '%s\n' "$resolved"
            return 0
        fi
        observations+=("$(describe_unsupported_facts "$candidate" "$facts")")
    done

    printf 'No supported 64-bit Python 3.11-3.14 was found.\n' >&2
    if ((${#observations[@]} > 0)); then
        printf '  - %s\n' "${observations[@]}" >&2
    fi
    printf 'Install python3 and its venv package, then rerun this script.\n' >&2
    return 1
}

validate_venv_python() {
    local facts
    if ! facts=$(python_facts "$venv_python" 2>/dev/null); then
        fail "cannot inspect the existing virtual environment: $venv_python"
    fi
    facts_are_supported "$facts" ||
        fail "the existing .venv is unsupported ($(describe_unsupported_facts '.venv' "$facts")); move it aside and rerun"
}

if [[ -e "$venv_root" && ! -x "$venv_python" ]]; then
    if [[ -f "$venv_root/Scripts/python.exe" ]]; then
        fail 'the existing .venv is a Windows environment; Linux needs its own .venv (move the Windows one aside or use a separate checkout)'
    fi
    fail 'the existing .venv is incomplete or is not a Linux virtual environment; move it aside and rerun'
fi

if [[ ! -x "$venv_python" ]]; then
    launcher=$(find_python) || exit 2
    launcher_facts=$(python_facts "$launcher")
    launcher_version_and_bits=${launcher_facts#*|}
    printf 'Creating the project virtual environment with Python %s...\n' \
        "${launcher_version_and_bits%%|*}"
    if ! run "$launcher" -m venv "$venv_root"; then
        fail 'could not create .venv; on Debian/Ubuntu install the matching python3-venv package'
    fi
fi

validate_venv_python

if ! "$venv_python" -m pip --version >/dev/null 2>&1; then
    if ! run "$venv_python" -m ensurepip --upgrade; then
        fail 'pip is unavailable in .venv; install the matching python3-venv package and recreate .venv'
    fi
fi

cd -- "$root"

printf 'Checking the Python build tools...\n'
run "$venv_python" -m pip install 'setuptools>=77'

install_target="${root}[mcp]"
if ((portable_tests)); then
    install_target="${root}[mcp,dev]"
fi

printf 'Installing Tianlai core and the MCP entry point (without large audio assets)...\n'
run "$venv_python" -m pip install --no-build-isolation -e "$install_target"
run "$venv_python" -m pip check

printf 'Checking the runtime layout, catalogue and installed resource state...\n'
run "$venv_python" -m tianlai.doctor --start "$root"

if ((!skip_smoke)); then
    smoke_directory="$root/output/首次出声"
    smoke_wave="$smoke_directory/参考振荡器.wav"
    mkdir -p -- "$smoke_directory"
    printf 'Rendering the self-contained reference oscillator...\n'
    run "$venv_python" -m tianlai render \
        --instrument "$root/乐器/测试工具/参考振荡器/乐器.json" \
        --events "$root/examples/c_major.events.json" \
        --output "$smoke_wave"
    [[ -f "$smoke_wave" ]] ||
        fail "the render command returned without creating $smoke_wave"
    "$venv_python" - "$smoke_wave" <<'PY'
from pathlib import Path
import soundfile as sf
import sys

path = Path(sys.argv[1])
info = sf.info(path)
if info.frames <= 0 or info.channels <= 0 or info.samplerate <= 0:
    raise SystemExit(f"invalid first-sound WAV metadata: {info}")
PY
    printf 'First-sound smoke passed: %s\n' "$smoke_wave"
fi

if ((portable_tests)); then
    printf 'Running the portable test contract (external assets/listening excluded)...\n'
    run "$venv_python" -m pytest -q \
        -m 'not external_assets and not listening'
fi

cat <<EOF

Tianlai's minimal Linux environment is ready.

CLI:
  "$venv_python" -m tianlai --help

MCP client configuration:
  command: $venv_python
  args:    ["-m", "tianlai.mcp_entry"]
  cwd:     $root

Optional large-resource plan (no download):
  "$venv_python" -m tianlai.resource_restore --home "$root" plan

7z-backed resource families additionally require bsdtar/libarchive-tools.
Large third-party audio assets remain a separate install and validation layer.
EOF
