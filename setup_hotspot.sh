#!/bin/sh
# Makes this machine start its own Wi-Fi network when no known Wi-Fi is in
# range, so the phone portal (web_portal.py) is reachable away from home at
# http://10.42.0.1:8080. Needs NetworkManager (Raspberry Pi OS default).
#
#   sudo ./setup_hotspot.sh "NetworkName" "password"      (password: 8+ characters)
#
# Run it again to change the name or password. To remove the hotspot:
#   sudo nmcli connection delete hosas-hotspot
set -e

SSID="$1"
PASSWORD="$2"
NAME=hosas-hotspot
DEVICE="${HOTSPOT_DEVICE:-wlan0}"

if [ -z "$SSID" ] || [ "${#PASSWORD}" -lt 8 ]; then
    echo "Usage: sudo $0 \"NetworkName\" \"password\"   (password: 8+ characters)" >&2
    exit 1
fi

nmcli connection delete "$NAME" >/dev/null 2>&1 || true

# 5 GHz (channel 36) keeps the hotspot off the 2.4 GHz band the Bluetooth
# link to the Switch uses - both run on the same radio. The priority is far
# below a normal Wi-Fi profile's 0, so any known network in range wins and
# the hotspot only comes up when there is none.
nmcli connection add type wifi ifname "$DEVICE" con-name "$NAME" ssid "$SSID" \
    802-11-wireless.mode ap 802-11-wireless.band a 802-11-wireless.channel 36 \
    wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PASSWORD" wifi-sec.proto rsn wifi-sec.pairwise ccmp \
    ipv4.method shared ipv6.method ignore \
    connection.autoconnect yes connection.autoconnect-priority -100

echo "Done. With no known Wi-Fi in range, join \"$SSID\" and open http://10.42.0.1:8080"
echo "Try it now:  sudo nmcli connection up $NAME     (back to normal Wi-Fi: sudo nmcli connection down $NAME)"
