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

Successful unlocking, restoration of the original `chkcode` and Android boot have been confirmed on A101BM, `1.280PO.0686.a`, SPL `2023-09-01`, Windows x64. On the same phone, the EXE was verified to refuse an already unlocked bootloader before writing the marker. A complete unlock using the packaged build, the modified Linux workflow and hardware failure rollback remain unverified by this fork. Normal USB (0482:0A74) and bootloader USB (18D1:D00D) were observed on X01A without unlocking it. X01A DIAG and unlocking remain unverified by this fork; the original project reports support for X01A and other builds.

<a id="setup"></a>
<a id="windows"></a>
## Windows

### Installation

1. Download `unmuda-v0.3.0-windows-x64.zip` and its `.sha256` file from [Releases](https://github.com/glebn2/unmuda/releases).
2. Extract the entire ZIP into a writable directory. Keep `_internal`, `platform-tools` and `tools` beside `unmuda.exe`.
3. Open PowerShell in that directory. No separate Python, PyUSB or Platform-Tools installation is needed.

To check the archive hash, run `Get-FileHash .\unmuda-v0.3.0-windows-x64.zip -Algorithm SHA256` and compare it with the published `.sha256`. The EXE is not digitally signed. The EXE and its helpers have automated checks; refusal of an already unlocked bootloader was verified on a phone. A complete unlock using this packaged build remains unverified.

Each phone gets a separate `sessions/device-.../` directory beside the EXE, regardless of PowerShell's current directory. It contains only that phone's backups and receipts; the selected path is printed. Retain the entire `sessions/` directory for recovery. Legacy 0.1/0.2 files beside the EXE are recognized and kept in place.

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

The builder downloads pinned inputs with SHA-256 verification, checks the EXE without a phone and creates a ZIP plus checksum in `dist/`. GitHub Actions builds on push/PR; a manual run with `publish_release=true` publishes a release after Windows and Linux checks pass. Built binaries are not stored in Git.

</details>

### Phone

Enable developer options by tapping the build number in About phone. Enable **USB debugging** and **OEM unlocking**. Connect the phone, select file transfer, unlock the screen and authorize debugging from this computer. If the prompt appears while starting the utility, accept it; enter `yes` to check again in the same window.

<a id="drivers"></a>
### Drivers

Install drivers using [Zadig](https://zadig.akeo.ie/) when each mode appears. The utility switches modes and checks the connection automatically.

| Purpose | Device in Zadig | Driver |
| --- | --- | --- |
| Fastboot | `18D1:D00D` | WinUSB with the `tools/balmuda-fastboot.cfg` preset |
| DIAG | `0482:0A9D`, **Interface 0** | WinUSB |

Windows may require binding the same preset again for another physical phone, even when VID/PID match. Follow the program's prompts for the attached phone.

**Fastboot:** during the initial check, use Zadig **Device → Load Preset Device → `tools/balmuda-fastboot.cfg`**, select WinUSB and install the driver. The utility waits up to five minutes for the connection, then returns the phone to Android. The preset specifies the Android interface GUID required by fastboot.

If fastboot remains unavailable after installation, bind the Zadig-generated `Android_Bootloader_Interface.inf` to `18D1:D00D` in Device Manager: Update driver → Browse my computer → Let me pick → Have Disk. See the [Zadig guide](https://github.com/pbatard/libwdi/wiki/Zadig#example-of-a-zadig-preset-device-config-file) and [USB driver installation](https://developer.android.com/studio/run/oem-usb). If the wait expires, the program offers another check in the same window: finish installing the driver and enter `yes`. Keep the phone in the bootloader; no application restart is needed.

**DIAG:** the program first checks the connection without partition writes. If the driver is missing, it stays open and shows the steps: Zadig → **Options → List All Devices** → **0482:0A9D, Interface 0** → **WinUSB**. After installation enter `yes` in the same window. Only the connection check is retried; unlocking continues after it succeeds.

<a id="usage"></a>
### Unlocking

Open PowerShell in the directory extracted from the ZIP, then run:

```powershell
.\unmuda.exe --lang en
```

A normal terminal enables guided prompts. Enter `yes` when ready for the proposed action. If ADB is unauthorized, accept the phone prompt and enter `yes` to check again in the same window. Manual ADB/fastboot commands and serial entry are unnecessary. The utility checks the phone and fastboot before writing the marker, creates backups and starts unlocking.

On the phone, select **Unlock the bootloader** with the volume buttons and confirm as instructed on screen. On the tested A101BM, the power button confirms the choice. Stay nearby: unlocking cannot finish without this action.

After confirmation, the utility waits for a verified `unlocked` state, restores the original `chkcode` and reboots. Wait for the Android welcome screen. USB debugging may need to be enabled again after the reset.

### Continuing recovery

A normal launch recognizes saved sessions and offers the appropriate continuation. If automatic rollback is unfinished, fix the connection/driver and confirm another recovery attempt in the same window. After closing the program, launch normally or explicitly select recovery:

```powershell
.\unmuda.exe --recover --lang en
```

Keep backups and receipts. Fastboot recovery does not require working PyUSB/libusb. If portable application files are damaged, restore them from the same release archive while preserving the entire `sessions/` directory and any legacy backups/receipts beside the EXE.

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

One installation supports any number of phones, one at a time. A normal launch lists ADB/fastboot devices and selects a session by serial. A new phone gets its own directory; a known phone resumes its own session. Another phone's completed or interrupted session does not block a new unlock, and its files are neither used nor changed.

If no phone is detected, the program offers saved-session selection. For a new phone, connect it and relaunch. Leave only one phone connected. `--list-sessions` lists saved sessions without device access; `--session SERIAL_OR_KEY` selects an existing session without automatic discovery. `--session legacy` selects older files in the application directory. Selection itself never flashes or reboots the phone.

After selection, the program validates saved files and explains the session:

| State | Offered action |
| --- | --- |
| Preparation saved; marker not written | Continue unlocking with the original backups after checking the phone and confirming data erasure |
| Interrupted after a possible marker write | Restore the original; no new unlock is started |
| Original restored; reboot pending | Finish reboot only, without flashing again |
| Rollback completed | If needed, check the phone and retry unlocking with the same originals |
| Unlock already completed | After selection, report completion without further phone operations or repeat writes |
| Backups exist without a receipt | Rebuild a receipt only after two full live reads match the backup, without partition writes |
| Receipt damaged, backups invalid or device mismatched | Explain the cause and retain files/error details; confirmation cannot override validation |

Recovery is limited by available transport. A failure after a possible marker write starts one bounded automatic rollback attempt, waiting up to 120 seconds for the same phone. Another attempt requires a separate `yes`. Unknown bootloader mode/lock state does not permit flashing.

Android/DIAG restores the original eight-byte prefix with full-partition verification; fastboot flashes the verified original image. A locked device additionally requires a verified service marker in the receipt. This hardware failure branch has not been tested by this fork. If only reboot remains, the image is not flashed again.

Backup creation reads all of `chkcode` twice and requires identical **524288 bytes**, a valid SHA-256 and no service marker in the original:

| Files | Purpose |
| --- | --- |
| `session.json` | Binds a new session directory to its device serial |
| `chkcode_backup.bin` + `chkcode_backup.sha256` | Original partition and SHA-256 |
| `chkcode_backup.recovery.bin` + `chkcode_backup.recovery.sha256` | Second verified copy |
| `chkcode_backup.device.json` | Original device, hash and operation phase |
| `cve_cleanup.device.json`, if retained | Restore temporary Android settings and remove the payload on the original phone |
| `unmuda-last-error.txt`, if created | Last error details for diagnosis |

Originals are never deleted or overwritten. If one copy is damaged, the other is accepted only against the original receipt hash. Regenerating SHA cannot make a truncated file valid. Two copies on one drive do not protect against losing that drive; retain the complete set separately. Redact serial numbers and personal data before publishing logs.

**Upgrading:** extract the complete new archive and retain the original directory. Copy legacy 0.1/0.2 session files (`chkcode_backup*` and `cve_cleanup.device.json`, if present) beside the new EXE. If a `sessions/` directory already exists, copy it intact as well. Older receipts remain compatible and are processed in place. Never merge different phones' files into one session directory. For the next phone, connect it and run the same EXE again.

Ctrl+C and handled signals are deferred until the write and receipt update finish. Forced termination, power loss and physical USB disconnection cannot be repaired by the signal handler. Do not disconnect the cable during a write.

<a id="diagnostics"></a>
## Additional modes and diagnostics

The normal launch runs the complete procedure. Additional modes serve specific tasks:

| Mode | Action |
| --- | --- |
| `--preflight` | Host dependencies only, without ADB/USB |
| `--list-sessions` | List saved serials, phases and keys without ADB/USB |
| `--session SERIAL_OR_KEY` | Select a saved session by serial or key; `legacy` selects older files in the application directory |
| `--prepare` | DIAG and backups; no fastboot check, reboot, marker or unlock |
| `--unlock-prepared` | Unlock from a saved `prepared` state without recreating backups |
| `--recover` | Continue recovery of the original session |
| `--guided` | Force guided prompts, including with redirected input |
| `--non-interactive` | Stop on errors without offering retries |
| `--lang ru` / `--lang en` | Controller language; some low-level messages remain Russian |

The split `--prepare` / `--unlock-prepared` modes are unnecessary for a normal launch. Guided continuation reuses existing backups, re-enables DIAG/the service when needed and compares the live partition with the original before the preliminary reboot.

| Message | Action |
| --- | --- |
| ADB offline / unauthorized | Unlock the screen, accept debugging authorization, check the cable and driver |
| Fastboot missing during the initial check | Configure the driver/USB permissions; the marker has not been written |
| DIAG `no langid` / Access denied | Check DIAG WinUSB and exclude concurrent ADB polling |
| `fs_sys_call_diag` not `running` / DIAG_BAD_CMD | Check service startup; an empty `sysprop` has a separate alternative command in each OS section |
| A saved session is found | The program explains its phase and offers continuation/recovery; originals are retained |
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

The suite contains 175 automated tests: ADB/fastboot/USB are substituted; two checks use harmless local Python processes. The build additionally checks the EXE with an isolated PATH, Unicode paths, two independent sessions and legacy receipts. Checks do not access the phone. Backups, payloads, receipts, virtual environments and binaries are excluded from Git. Redact serial numbers and personal data before publishing logs; do not attach original partition dumps to public issues.

The [GNU GPL v3](LICENSE) license is retained from upstream. The utility is provided without warranties; see LICENSE for the complete terms.
