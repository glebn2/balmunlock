# unmuda

[Русский](README.md) · **English**

A bootloader unlock utility for the BALMUDA Phone. The [glebn2/unmuda](https://github.com/glebn2/unmuda) fork is based on [E2r7hN07Fl47/unmuda](https://github.com/E2r7hN07Fl47/unmuda), adding validation, backups, recovery and native Windows support without WSL.

> **Unlocking erases user data.** Save required files separately. A `chkcode` backup restores a service partition; it does not contain your photographs or documents. The risk of device damage cannot be eliminated completely.

This is a CLI application. A portable Windows build is available through [GitHub Releases](https://github.com/glebn2/unmuda/releases): extract the ZIP and run `unmuda.exe` from PowerShell. Python and dependencies are included. Fork changes were written with assistance from **GPT-6.1 Sol**.

- [Compatibility](#compatibility)
- [Windows](#windows)
- [Linux](#linux)
- [Backups and recovery](#recovery)
- [Additional modes and diagnostics](#diagnostics)
- [How the utility works](#limitations)
- [Credits and license](#development)

<a id="compatibility"></a>
## Compatibility

| Parameter | Requirement |
| --- | --- |
| Model | A101BM or X01A |
| System | Stock Android 12, SDK 31 |
| Build | 1.220–1.280, including PO/MI suffixes such as `1.280PO.0686.a` |
| Security patch level | Earlier than 2024-06-01 |
| Connection | One phone and a working data-capable USB cable |

The utility checks these parameters automatically. They limit admission to the procedure but do not guarantee compatibility: a manufacturer may have backported the fix. CVE-2024-31317 was fixed in the [June 2024 Android Security Bulletin](https://source.android.com/docs/security/bulletin/2024-06-01).

Successful unlocking, restoration of the original `chkcode` and Android boot have been confirmed on A101BM, `1.280PO.0686.a`, SPL `2023-09-01`, Windows x64. The original project reports support for X01A and other builds. The new preliminary fastboot check has automated test coverage but has not been separately tested on a phone. This fork has not hardware-tested the modified Linux workflow or failure rollback scenarios.

<a id="setup"></a>
<a id="windows"></a>
## Windows

### Installation

1. Download `unmuda-v0.1.0-windows-x64.zip` and its `.sha256` file from [Releases](https://github.com/glebn2/unmuda/releases).
2. Extract the entire ZIP into a writable directory. Keep `_internal`, `platform-tools` and `tools` beside `unmuda.exe`.
3. Open PowerShell in that directory. No separate Python, PyUSB or Platform-Tools installation is needed.

To check the archive hash, run `Get-FileHash .\unmuda-v0.1.0-windows-x64.zip -Algorithm SHA256` and compare it with the published `.sha256`. This preview build is not digitally signed. The EXE and its helpers have been checked without a phone; this packaged build has not completed a separate hardware unlock.

Backups and receipts are stored beside the EXE regardless of PowerShell's current directory. Retain this directory until the procedure and any recovery have finished.

<details>
<summary>Running from source and building the EXE</summary>

1. Install [Python 3.13 x64](https://www.python.org/downloads/windows/) and [PowerShell 7](https://learn.microsoft.com/powershell/scripting/install/installing-powershell-on-windows). Open PowerShell 7 in the source directory.
2. Download [Android Platform-Tools for Windows](https://developer.android.com/tools/releases/platform-tools). Extract them into `host-tools/platform-tools`, containing `adb.exe` and `fastboot.exe`.
3. Create the Python environment and install dependencies:

```powershell
python -m venv .\host-tools\python
& .\host-tools\python\Scripts\python.exe -m pip install --require-hashes -r .\requirements-windows.txt
```

Tested configuration: Python 3.13 x64, PowerShell 7, Platform-Tools 37.0.1, PyUSB 1.3.1, libusb-package 1.0.30.0. The wrapper uses local tools, enables UTF-8 and sets PATH for its invocation only. No system PATH changes are needed. Dependencies and Platform-Tools are not included in the repository.

Source launch: `& .\tools\host-python.ps1 tools/unlock.py --lang en`. To build, install the pinned build dependencies and run:

```powershell
& .\host-tools\python\Scripts\python.exe -m pip install --require-hashes -r .\build-support\requirements.txt
& .\host-tools\python\Scripts\python.exe build-support/build_windows.py
```

The builder downloads pinned inputs with SHA-256 verification, checks the EXE without a phone and creates a ZIP plus checksum in `dist/`. GitHub Actions builds on push/PR; a manual run with `publish_release=true` publishes a preview release after Windows and Linux checks pass. Built binaries are not stored in Git.

</details>

### Phone

Enable developer options by tapping the build number in About phone. Enable **USB debugging** and **OEM unlocking**. Connect the phone, select file transfer, unlock the screen and authorize debugging from this computer. If the prompt appears while starting the utility, accept it; if the utility stops because authorization is missing, run it again.

<a id="drivers"></a>
### Drivers

Install drivers using [Zadig](https://zadig.akeo.ie/) when each mode appears. The utility switches modes and checks the connection automatically.

| Purpose | Device in Zadig | Driver |
| --- | --- | --- |
| Fastboot | `18D1:D00D` | WinUSB with the `tools/balmuda-fastboot.cfg` preset |
| DIAG | `0482:0A9D`, **Interface 0** | WinUSB |

**Fastboot:** during the initial check, use Zadig **Device → Load Preset Device → `tools/balmuda-fastboot.cfg`**, select WinUSB and install the driver. The utility waits up to five minutes for the connection, then returns the phone to Android. The preset specifies the Android interface GUID required by fastboot.

If fastboot remains unavailable after installation, bind the Zadig-generated `Android_Bootloader_Interface.inf` to `18D1:D00D` in Device Manager: Update driver → Browse my computer → Let me pick → Have Disk. See the [Zadig guide](https://github.com/pbatard/libwdi/wiki/Zadig#example-of-a-zadig-preset-device-config-file) and [USB driver installation](https://developer.android.com/studio/run/oem-usb). If the wait expires before the marker is written, return the phone to Android using its power button and rerun after configuring the driver.

**DIAG:** once `0482:0A9D` appears, enable **Options → List All Devices**, select **Interface 0** and install WinUSB. If the utility stopped because this driver was missing before backups were created, install it and repeat the normal launch. If it reports an existing session, use [recovery](#recovery).

<a id="usage"></a>
### Unlocking

```powershell
.\unmuda.exe --lang en
```

Enter `yes` and follow the messages. Manual ADB/fastboot commands and serial entry are unnecessary. The utility checks the phone and fastboot before writing the marker, creates backups and starts unlocking.

On the phone, select **Unlock the bootloader** with the volume buttons and confirm as instructed on screen. On the tested A101BM, the power button confirms the choice. Stay nearby: unlocking cannot finish without this action.

After confirmation, the utility waits for a verified `unlocked` state, restores the original `chkcode` and reboots. Wait for the Android welcome screen. USB debugging may need to be enabled again after the reset.

### Continuing recovery

If automatic rollback did not finish, restore the USB connection and run:

```powershell
.\unmuda.exe --recover --lang en
```

Keep backups and receipts. Fastboot recovery does not require working PyUSB/libusb. If portable application files are damaged, restore them from the same release archive while preserving backups and receipts beside the EXE.

<details>
<summary>Additional Windows diagnostics and tests</summary>

Check dependencies without accessing the phone:

```powershell
.\unmuda.exe --preflight --lang en
```

`--preflight` does not check installed drivers; the normal launch checks the connection.

For low-level operations, set `$env:ANDROID_SERIAL` to the original phone's serial. The main controller does this automatically. Before directly running a DIAG helper, stop the ADB server and restart it afterward, including after a failure; the controller coordinates this automatically. See the [libusb restrictions](https://github.com/libusb/libusb/wiki/Windows#known-restrictions).

If the service does not start because `sysprop` is empty, upstream describes an alternative that does not write that partition:

```powershell
.\unmuda.exe --internal-tool cve31317.py --cmd "setprop ctl.start fs_sys_call_diag ;" --check-prop init.svc.fs_sys_call_diag=running
```

This separate mode is not selected automatically and has not been hardware-tested by this fork.

Tests without a phone:

```powershell
& .\tools\host-python.ps1 -m unittest discover -s tests -v
```

</details>

<a id="linux"></a>
## Linux

### Installation

Python 3.9+, PyUSB, libusb-1.0, ADB and fastboot are required. Example for Debian/Ubuntu:

```bash
sudo apt install python3 python3-venv python3-pip libusb-1.0-0 adb fastboot
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r tools/requirements.txt
```

Configure your system's USB permissions/udev rules for DIAG VID `0482` and fastboot `18D1:D00D`. If `qcserial` holds DIAG, check its binding to the relevant interface. `shell.nix` is retained for Nix; this configuration has not been independently validated.

### Phone and launch

Enable developer options by tapping the build number in About phone, then enable **USB debugging** and **OEM unlocking**. Connect one phone, unlock its screen and authorize USB debugging.

In the activated environment, run:

```bash
python tools/unlock.py --lang en
```

Enter `yes`. The utility checks the phone and fastboot automatically, returns to Android, enables DIAG, saves backups and starts unlocking. On the screen, select **Unlock the bootloader** with the volume buttons and confirm as instructed; on the tested A101BM, use the power button. After `chkcode` restoration and reboot, wait for the Android welcome screen.

### Continuing recovery

```bash
python tools/unlock.py --recover --lang en
```

Retain original backups and receipts. This mode does not require PyUSB for fastboot recovery.

<details>
<summary>Additional Linux diagnostics and tests</summary>

Check dependencies without a phone:

```bash
python tools/unlock.py --preflight --lang en
```

The Bash entry points `bash unlock.sh` and `bash unlock.en.sh` are retained; they use the same controller.

For direct low-level calls, set `ANDROID_SERIAL` to the original phone. If DIAG must be re-enabled, use `tools/cve31317.py`, then `tools/kdiag_shell.py "setprop vendor.kc.diag.status start"`. The main workflow enables DIAG and checks the service automatically.

Upstream's alternative for an empty `sysprop`, without writing the partition:

```bash
python tools/cve31317.py --cmd "setprop ctl.start fs_sys_call_diag ;" --check-prop init.svc.fs_sys_call_diag=running
```

This mode is not selected automatically and has not been hardware-tested by this fork.

Tests without a phone:

```bash
python -m unittest discover -s tests -v
```

</details>

<a id="recovery"></a>
## Backups and recovery

The utility reads the entire `chkcode` twice, requires identical data of exactly **524288 bytes**, rejects an original containing the service marker and saves:

| Files | Purpose |
| --- | --- |
| `chkcode_backup.bin` + `chkcode_backup.sha256` | Original partition and SHA-256 |
| `chkcode_backup.recovery.bin` + `chkcode_backup.recovery.sha256` | Second verified copy |
| `chkcode_backup.device.json` | Original device, hash and operation phase |

Existing copies are never overwritten. Retain the complete set; two copies on one drive do not protect against losing that drive. An all-zero stock `chkcode` is acceptable, but recovery uses the actual original dump rather than a newly generated zero-filled image.

A failure after a possible marker write triggers bounded automatic rollback, waiting up to 120 seconds for the same phone. Accessible Android/DIAG allows restoration of the original eight bytes with full-partition verification; fastboot restores the verified original image. A locked bootloader requires a verified service marker in the receipt. This failure branch is based on upstream research and has not been hardware-tested by this fork.

Unknown lock state does not permit flashing. Failed flashes are not retried indefinitely or followed by an automatic reboot. If flashing completed but the reboot reply was lost, only reboot completion is retried.

Incomplete rollback retains `recovery_pending`; use the `--recover` command in your OS section. It does not start another unlock or restore erased user data. If `cve_cleanup.device.json` remains, recovery first restores the original phone's temporary hidden API/USAP settings and removes the payload; this can also run before a partition backup exists.

If one copy is damaged, the second is accepted only against the original receipt hash. A truncated dump, lost receipt or hash mismatch requires investigation; recreating a SHA file does not repair a backup. Do not delete session artifacts to enable a rerun.

Ctrl+C and handled signals are deferred until the write and receipt update finish. Forced process termination, power loss and physical USB disconnection cannot be repaired by the signal handler. Do not disconnect the cable during a write.

<a id="diagnostics"></a>
## Additional modes and diagnostics

The normal launch runs the complete procedure. Additional modes serve specific tasks:

| Mode | Action |
| --- | --- |
| `--preflight` | Host dependencies only, without ADB/USB |
| `--prepare` | DIAG and backups; no fastboot check, reboot, marker or unlock |
| `--unlock-prepared` | Unlock from a saved `prepared` state without recreating backups |
| `--recover` | Continue recovery of the original session |
| `--lang ru` / `--lang en` | Controller language; some low-level messages remain Russian |

The split `--prepare` / `--unlock-prepared` modes are unnecessary for a normal launch. They assume fastboot is already configured; rebooting a prepared phone may require separately re-enabling DIAG/the service.

| Message | Action |
| --- | --- |
| ADB offline / unauthorized | Unlock the screen, accept debugging authorization, check the cable and driver |
| Fastboot missing during the initial check | Configure the driver/USB permissions; the marker has not been written |
| DIAG `no langid` / Access denied | Check DIAG WinUSB and exclude concurrent ADB polling |
| `fs_sys_call_diag` not `running` / DIAG_BAD_CMD | Check service startup; an empty `sysprop` has a separate alternative command in each OS section |
| Backup or receipt already exists | Use the saved session or recovery; do not delete originals |
| Readback after a write not confirmed | The write result is uncertain; allow controller rollback |
| Repeated fastboot entry after unlocking started | A marker may remain; use the original receipt and `--recover` |

Low-level tools reside in `tools/`: `chkcode_backup.py` validates an image without USB; `backup_chkcode.py` creates backups; `unlock_chkcode.py` writes the marker; `restore_chkcode.py` performs limited DIAG rollback; `cve31317.py` / `cve_cleanup.py` manage preparation and cleanup; `diag_transport.py`, `fs_sys_call.py`, `kdiag_shell.py` implement transport and protocols.

Direct use bypasses some controller protections. The inherited `exfil.py` and `at_console.py` are outside the main workflow: the former has temporary-file cleanup limitations after failures; the latter lacks serial/multiple-device validation and accepts arbitrary AT commands. `fs_sys_call.py --close-slots` blindly closes the daemon's five fd slots and is never called automatically.

<a id="limitations"></a>
## How the utility works

The original method uses CVE-2024-31317 to execute a preparation command as uid=1000, switches USB to DIAG and exposes `chkcode` through `fs_sys_call_diag`. The `LOOTBFCK` marker enables the ABL service path `KcFastbootCheck()` and forces fastboot entry. After verified unlocking, the utility restores the original partition.

This fork adds serial binding across ADB/DIAG/fastboot, model/build checks, an early fastboot check, two backups, live-partition validation before writing, a state receipt, a process lock, cancellation handling and bounded automatic rollback. Temporary CVE settings are cleaned up with verification; unfinished cleanup retains a receipt.

The DIAG shell is subject to SELinux and is not universal root. During this procedure, fastboot only flashes the original `chkcode`. GSI, boot/system/super or GPT flashing, relocking, slot changes and manual erases are outside the procedure; upstream describes Virtual A/B corruption and slot-retry exhaustion risks.

Matching reads and SHA establish data consistency and local file integrity, not physical flash persistence. Successful fastboot flash is not independent readback; an accepted reboot does not prove completed Android boot. Automatic rollback is limited by available transport and confirmed state.

<a id="development"></a>
## Credits and license

Original project and Kyocera research: [E2r7hN07Fl47/unmuda](https://github.com/E2r7hN07Fl47/unmuda). Fork maintainer: [glebn2](https://github.com/glebn2). Fork changes dated 2026-10-17 cover validation and backups, recovery, Windows compatibility and consolidated documentation. These code changes were written with assistance from **GPT-6.1 Sol**.

Original project contributors:

- [E2r7hN07Fl47](https://github.com/E2r7hN07Fl47).
- Mamkin_Xakep — [email](mailto:mamkin@xakep.xyz), [Telegram](https://t.me/Mamkin_Xakep_bot).
- [radio_mudrec](https://4pda.to/forum/index.php?showuser=9245164) — Bash workflow fixes.
- [Stoobyy](https://github.com/Stoobyy) — English translation and fixes.

Upstream also credits Meta Red Team X, Google Android Security Team, JD Labs, agg23 and rabits for the discovery, fix, analysis and PoC of CVE-2024-31317.

The suite contains 107 automated tests: ADB/fastboot/USB are substituted; two checks use harmless local Python processes. The build additionally checks the EXE with an isolated PATH and Unicode paths. Checks do not access the phone. Backups, payloads, receipts, virtual environments and binaries are excluded from Git. Redact serial numbers and personal data before publishing logs; do not attach original partition dumps to public issues.

The [GNU GPL v3](LICENSE) license is retained from upstream. The utility is provided without warranties; see LICENSE for the complete terms.
