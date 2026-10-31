#!/usr/bin/env bash
# install_driver.sh — VM only, opt-in (bash preflight.sh --install-driver).
# Installs the NVIDIA driver Ubuntu recommends for this GPU, then asks for one reboot.
# Container GPU hosts ship the driver in the image; never run this there.
set -Eeuo pipefail

SUDO="${SUDO:-}"
if [[ $EUID -ne 0 && -z "$SUDO" ]]; then
  command -v sudo >/dev/null && SUDO="sudo" || { echo "  ✗ need root (or sudo) to install the driver" >&2; exit 1; }
fi
[[ "$SUDO" == "__none__" ]] && { echo "  ✗ need root (or sudo) to install the driver" >&2; exit 1; }
command -v apt-get >/dev/null || { echo "  ✗ --install-driver needs apt (Ubuntu)" >&2; exit 1; }
# shellcheck disable=SC1091
OS_ID="$(. /etc/os-release 2>/dev/null && echo "${ID:-}")"
[[ "$OS_ID" == "ubuntu" ]] || { echo "  ✗ --install-driver uses ubuntu-drivers, which is Ubuntu only (this is ${OS_ID:-unknown});" \
  "install the NVIDIA driver with your distribution's own instructions, then re-run bash preflight.sh" >&2; exit 1; }

printf '\n\033[1;34m==> Installing the recommended NVIDIA driver\033[0m\n'
${SUDO:+$SUDO} apt-get update -qq
${SUDO:+$SUDO} env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ubuntu-drivers-common
${SUDO:+$SUDO} ubuntu-drivers install
printf '\033[1mDriver installed. Reboot (sudo reboot), then run: bash preflight.sh\033[0m\n'
echo "  (If the driver doesn't load after the reboot, check Secure Boot / MOK enrollment.)"
