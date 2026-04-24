# Nubly – Home Assistant Integration

Custom Home Assistant integration for Nubly devices.

## Overview

This repo contains **only** the Home Assistant custom integration.
The device firmware lives in a separate repository.

## Status

Discovers Nubly devices via mDNS (zeroconf) and publishes retained config
to `nubly/devices/<device_id>/config` over the user's Home Assistant MQTT
broker. Requires HA's MQTT integration to be configured.

## Installation (HACS)

1. Open HACS in your Home Assistant instance.
2. Go to **Integrations** → three-dot menu (top right) → **Custom repositories**.
3. Add this repository URL and select **Integration** as the category.
4. Find **Nubly** in the HACS integration list and click **Install**.
5. Restart Home Assistant.
6. Go to **Settings → Devices & Services → Add Integration** and search for **Nubly**.

### Manual installation

Copy the `custom_components/nubly` folder into your Home Assistant
`config/custom_components/` directory and restart Home Assistant.

## Releases

Versioning follows [semantic versioning](https://semver.org/):

- **patch** (`0.1.0` → `0.1.1`) — bug fixes, no behavior change for users
- **minor** (`0.1.0` → `0.2.0`) — new features, backwards compatible
- **major** (`0.x.y` → `1.0.0`) — breaking changes (config schema, topic names, etc.)

### Cutting a new release

1. Bump `"version"` in [custom_components/nubly/manifest.json](custom_components/nubly/manifest.json).
2. Commit the change to `main`: `git commit -am "Release vX.Y.Z"`.
3. Tag the commit and push the tag:
   ```sh
   git tag vX.Y.Z
   git push origin main --tags
   ```
4. On GitHub, open **Releases → Draft a new release**, pick the `vX.Y.Z` tag,
   write release notes, and publish.
5. Within a few minutes HACS will show an update available on every instance
   that installed this integration. Users click **Update** in HACS and restart
   Home Assistant to pick up the new version.

### How HACS detects the version

- **Currently installed version:** HACS reads `manifest.json["version"]` from
  the copy in the user's `config/custom_components/nubly/` folder.
- **Latest available version:** HACS reads the latest **GitHub release tag**
  from this repository.

The tag name (minus the leading `v`) and the manifest `version` field **must
match** — e.g. tag `v0.2.0` ↔ `"version": "0.2.0"`. If they diverge, HACS will
either not detect the update or install the wrong version.
