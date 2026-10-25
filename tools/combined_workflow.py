"""Unlock followed by the selected recovery, with one serial and one process lock."""
from pathlib import Path
import time

from recovery_image import file_hash, image_path, inspect_image
from recovery_install import RecoveryError, RecoveryInstaller
from session_lock import session_lock
from ui_language import error_message


def combined(root, args, workflow_factory, banner_shown=False):
    recovery = RecoveryInstaller(root, args.lang, interactive=not args.non_interactive,
                                 verbose=args.verbose)
    flow = None
    try:
        with session_lock(Path(root) / ".unlock.lock"):
            # Reject unsupported phones and missing artifacts BEFORE an unlock can erase data.
            source = inspect_image(image_path(root, args.recovery_image))
            selected_sha256 = file_hash(source)
            recovery.select_android(require_stock=True)
            expected_serial = recovery.serial
            flow = workflow_factory(args.lang, interactive=not args.non_interactive,
                                    verbose=args.verbose)
            flow.introduced = banner_shown
            flow.initial_serial = expected_serial
            flow.env["ANDROID_SERIAL"] = expected_serial
            flow.say("Сначала разблокировка и возврат служебного раздела, затем TWRP. "
                     "Разблокировка стирает данные; после сброса понадобится снова разрешить USB-отладку.",
                     "Unlock and restore the service partition first, then install TWRP. "
                     "Unlocking erases data; USB debugging must be authorized again after the reset.")
            with flow.signals:
                if not flow.route_session(args):
                    return 0
                if flow.serial not in (None, expected_serial):
                    raise RecoveryError("Unlock session belongs to another phone", "Сеанс анлока принадлежит другому телефону")
                result = flow.guided(args)
            if result != 0:
                return result
            complete = (flow.already_unlocked_observed or
                        flow.state.get("phase") in ("reboot_submitted", "already_unlocked"))
            if (not complete or flow.serial != expected_serial or flow.marker_possible
                    or flow.state.get("reboot_pending") or (flow.root / "cve_cleanup.device.json").exists()):
                recovery.say("Переход к TWRP отменён: успешное завершение анлока и возврат исходного раздела не подтверждены.",
                             "TWRP skipped: completed unlock and original service-partition restore were not confirmed.")
                return 0
            if flow.unlocked_this_run:
                recovery.say("Анлок завершён. На том же телефоне завершите первоначальную настройку Android, "
                             "снова включите отладку по USB и разрешите этот компьютер. "
                             "Не закрывайте программу. После подготовки введите yes; Enter — остановиться перед TWRP.",
                             "Unlock finished. Complete Android setup on the same phone, enable USB debugging again, "
                             "and authorize this computer. Keep this window open. Enter yes when ready; Enter to stop before TWRP.")
                if input().strip().lower() != "yes":
                    return 0
            with recovery.signals:
                # The instance retains the original serial; another phone is refused, not selected.
                recovery.wait("device", 180)
                deadline = time.monotonic() + 180
                while recovery.android_identity().get("sys.boot_completed") != "1":
                    if time.monotonic() >= deadline:
                        raise RecoveryError("Android boot not confirmed", "Загрузка Android не подтверждена")
                    time.sleep(2)
                # execute rechecks the image, device identity and unlock receipts, and asks before installation.
                return recovery.execute(source, expected_sha256=selected_sha256)
    except (OSError, ValueError, RuntimeError, KeyboardInterrupt, EOFError) as error:
        message = error_message(error, args.lang)
        if flow is not None:
            flow.record_error(error)
            flow.recovery_notice()
        if recovery.folder is not None:
            try:
                recovery.save("stopped", stopped_phase=recovery.state.get("phase"), error=message, technical_error=str(error))
            except OSError:
                pass
        recovery.say(f"Связанный сценарий остановлен: {message}. Бэкапы сохранены; автоматического повторного анлока/flash нет.",
                     f"Combined workflow stopped: {message}. Backups retained; no automatic repeat unlock/flash.")
        if args.verbose:
            print(str(error), flush=True)
        return 1
