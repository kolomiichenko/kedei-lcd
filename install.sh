#!/bin/bash
# Installs the KeDei LCD driver.
#
#   sudo ./install.sh ui         driver + touch status UI (default)
#   sudo ./install.sh desktop    driver mirroring /dev/fb0 + X11 desktop
#   sudo ./install.sh uninstall  remove everything (calibration is kept)
set -euo pipefail

MODE=${1:-ui}
SRC=$(cd "$(dirname "$0")" && pwd)
DESKTOP_USER=${SUDO_USER:-pi}
VIDEO_ARG="video=HDMI-A-1:480x320M@60D"
XWRAPPER_MARK="# managed by kedei-lcd"

BOOT=/boot/firmware
[ -d "$BOOT" ] || BOOT=/boot
CONFIG_TXT=$BOOT/config.txt
CMDLINE_TXT=$BOOT/cmdline.txt

REBOOT=0

log() { echo "==> $*"; }

need_root() {
	if [ "$(id -u)" -ne 0 ]; then
		echo "run as root: sudo $0 $MODE" >&2
		exit 1
	fi
}

apt_install() {
	DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "$@" >/dev/null
}

# The driver talks to SPI0 directly, so the kernel SPI driver must stay off.
disable_kernel_spi() {
	if grep -qE '^\s*dtparam=spi=on' "$CONFIG_TXT"; then
		log "disabling kernel SPI in $CONFIG_TXT"
		sed -i -E 's/^(\s*dtparam=spi=on)/#\1  # disabled by kedei-lcd/' "$CONFIG_TXT"
		REBOOT=1
	fi
}

cmdline_add_video() {
	if ! grep -q "$VIDEO_ARG" "$CMDLINE_TXT"; then
		log "adding $VIDEO_ARG to $CMDLINE_TXT"
		sed -i "1s|\$| $VIDEO_ARG|" "$CMDLINE_TXT"
		REBOOT=1
	fi
}

cmdline_remove_video() {
	if grep -q "$VIDEO_ARG" "$CMDLINE_TXT"; then
		log "removing $VIDEO_ARG from $CMDLINE_TXT"
		sed -i "s| $VIDEO_ARG||" "$CMDLINE_TXT"
		REBOOT=1
	fi
}

install_driver() {
	log "installing build dependencies"
	apt_install gcc libc6-dev
	log "building kedei-lcd"
	gcc -O2 -Wall -Wextra -o /usr/local/bin/kedei-lcd "$SRC/src/kedei-lcd.c"
	install -m 644 "$SRC/systemd/kedei-lcd.service" /etc/systemd/system/
	disable_kernel_spi
}

install_ui() {
	log "installing kedei-ui"
	apt_install python3-pil fonts-dejavu-core
	install -m 755 "$SRC/ui/kedei-ui.py" /usr/local/bin/kedei-ui
	install -m 644 "$SRC/systemd/kedei-ui.service" /etc/systemd/system/
}

remove_desktop_mode() {
	systemctl disable --now kedei-desktop.service 2>/dev/null || true
	rm -f /etc/systemd/system/kedei-desktop.service
	rm -f /etc/systemd/system/kedei-lcd.service.d/mirror.conf
	rmdir /etc/systemd/system/kedei-lcd.service.d 2>/dev/null || true
	rm -f /etc/X11/xorg.conf.d/99-kedei.conf
	if grep -qF "$XWRAPPER_MARK" /etc/X11/Xwrapper.config 2>/dev/null; then
		rm -f /etc/X11/Xwrapper.config
	fi
	cmdline_remove_video
}

mode_ui() {
	install_driver
	install_ui
	remove_desktop_mode
	systemctl daemon-reload
	systemctl enable kedei-lcd.service kedei-ui.service
	systemctl restart kedei-lcd.service
	systemctl restart kedei-ui.service
}

mode_desktop() {
	install_driver
	log "installing X11 desktop packages"
	apt_install xserver-xorg-core xserver-xorg-video-fbdev xserver-xorg-input-libinput \
		xserver-xorg-legacy xinit x11-xserver-utils openbox tint2 pcmanfm xterm \
		adwaita-icon-theme fonts-dejavu-core
	install -d /etc/X11/xorg.conf.d /etc/systemd/system/kedei-lcd.service.d
	install -m 644 "$SRC/x11/99-kedei.conf" /etc/X11/xorg.conf.d/
	if [ ! -e /etc/X11/Xwrapper.config ] || grep -qF "$XWRAPPER_MARK" /etc/X11/Xwrapper.config; then
		printf '%s\nallowed_users=anybody\nneeds_root_rights=yes\n' "$XWRAPPER_MARK" >/etc/X11/Xwrapper.config
	fi
	home=$(getent passwd "$DESKTOP_USER" | cut -d: -f6)
	if [ ! -e "$home/.xinitrc" ]; then
		install -m 644 -o "$DESKTOP_USER" -g "$DESKTOP_USER" "$SRC/x11/xinitrc" "$home/.xinitrc"
	fi
	install -m 644 "$SRC/systemd/kedei-lcd-mirror.conf" /etc/systemd/system/kedei-lcd.service.d/mirror.conf
	sed "s/@USER@/$DESKTOP_USER/" "$SRC/systemd/kedei-desktop.service" >/etc/systemd/system/kedei-desktop.service
	cmdline_add_video
	systemctl disable --now kedei-ui.service 2>/dev/null || true
	systemctl daemon-reload
	systemctl enable kedei-lcd.service kedei-desktop.service
	if [ "$REBOOT" -eq 0 ]; then
		systemctl restart kedei-lcd.service
		systemctl restart kedei-desktop.service
	fi
}

mode_uninstall() {
	systemctl disable --now kedei-ui.service kedei-lcd.service 2>/dev/null || true
	remove_desktop_mode
	rm -f /etc/systemd/system/kedei-ui.service /etc/systemd/system/kedei-lcd.service
	rm -f /usr/local/bin/kedei-lcd /usr/local/bin/kedei-ui
	systemctl daemon-reload
	log "removed (calibration kept in /etc/kedei-touch.conf, packages left installed)"
}

need_root
case "$MODE" in
ui) mode_ui ;;
desktop) mode_desktop ;;
uninstall) mode_uninstall ;;
*)
	echo "usage: $0 [ui|desktop|uninstall]" >&2
	exit 2
	;;
esac

if [ "$REBOOT" -eq 1 ]; then
	log "boot configuration changed: reboot to apply (sudo reboot)"
else
	log "done"
fi
