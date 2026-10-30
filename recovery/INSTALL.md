# TWRP 3.7.1_12-BALMUDA_Phone (20261025)

Образ: `TWRP-3.7.1_12-BALMUDA_Phone-20261025.img`, 100663296 байт.
SHA-256: `9e4d0c0707d83a6a3d5ec3fcad4c2f3296996348603817163540ddad7a840702`.

Один образ для **A101BM и X01A**. Штатный Android 12: `1.280PO.0686.a`
для A101BM, `1.280MI.0648.a` для X01A. Загрузчик разблокирован; используется текущий активный слот a или b.
Нужны ADB/fastboot и разрешённая отладка по USB. Важные данные сохраните отдельно.
Root, Magisk, GSI, повторный анлок и сброс данных для установки не нужны.

## Установка вручную (Windows PowerShell)

Распакуйте архив, откройте терминал в его каталоге. Проверьте хеш:

```powershell
Get-FileHash .\TWRP-3.7.1_12-BALMUDA_Phone-20261025.img -Algorithm SHA256
adb shell getprop ro.build.version.incremental
$slot = (adb shell getprop ro.boot.slot_suffix).Trim()
if ($slot -notin @('_a', '_b')) { throw 'Unknown active slot' }
$recovery = "recovery$slot"
$backup = "$recovery.backup.img"
adb reboot bootloader
fastboot getvar unlocked
fastboot getvar is-userspace
fastboot getvar current-slot
fastboot getvar snapshot-update-status
```

Ожидается прошивка вашей модели из списка выше, `unlocked: yes`, `is-userspace: no`,
`current-slot` должен совпадать с сохранённым `$slot` без подчёркивания;
`snapshot-update-status: none`. Если ответы отличаются или
обновление OTA ещё не завершилось — остановитесь. Слот не переключайте.

Временный запуск и сохранение текущего recovery (при запросе — Keep Read Only):

```powershell
fastboot boot TWRP-3.7.1_12-BALMUDA_Phone-20261025.img
adb shell "dd if=/dev/block/by-name/$recovery of=/tmp/$backup bs=1048576 count=96 && sha256sum /tmp/$backup"
adb pull "/tmp/$backup" $backup
Get-FileHash $backup -Algorithm SHA256
```

Бэкап должен иметь размер 100663296 байт, хеш на ПК должен совпасть с телефонным.
Сохраните его отдельно. Если раньше стоял кастомный recovery, бэкап содержит его.
Перед записью снова проверьте загрузчик:

```powershell
adb reboot bootloader
fastboot getvar unlocked
fastboot getvar is-userspace
fastboot getvar current-slot
fastboot getvar snapshot-update-status
fastboot flash $recovery TWRP-3.7.1_12-BALMUDA_Phone-20261025.img
fastboot reboot recovery
adb shell sha256sum "/dev/block/by-name/$recovery"
```

К записи переходите только при тех же ожидаемых ответах. Хеш раздела после
записи должен совпасть с хешем образа. Записывается только `$recovery`; boot, dtbo, vbmeta
и другой слот не трогаем. Wipe/Format Data не нужны, TWRP App не устанавливаем.
В Android: Reboot → System. Следующий вход: `adb reboot recovery`.

## Возврат предыдущего recovery

Из того же разблокированного bootloader, при `is-userspace: no` и том же активном слоте,
сначала сверив размер и хеш своего бэкапа:

```powershell
fastboot flash $recovery $backup
fastboot reboot
```

Сбои записи не исправляйте случайными образами или переключением слота.
Для Linux команды ADB/fastboot те же; вместо Get-FileHash используйте `sha256sum`.

## Особенности сборки

Перечень изменений и проверок — `CHANGELOG-20261025.md`. Расшифровка LineageOS
требует stock-compatible версии и патча в заголовке boot: 11.0.0 / 2023-09.
Встроенный обход EROFS для MindTheGapps-17 срабатывает при Reboot → System
(также `twrp reboot system`), но не при `adb reboot`. Отключение:
`/sdcard/TWRP/.no_gmsfix`. Для штатной системы он ничего не меняет.

---

## English

The same image supports **A101BM and X01A** on stock Android 12:
`1.280PO.0686.a` for A101BM, `1.280MI.0648.a` for X01A. Use an unlocked
bootloader; use the recovery partition of the current active slot, a or b.
Extract the archive and use ADB/fastboot with USB debugging authorized.
Keep a separate copy of important data. Root, Magisk, GSI and a wipe are not
required to install this recovery.

The PowerShell commands above save the active suffix in `$slot`, derive `$recovery`
and `$backup`, and keep using that same partition. Check the image SHA-256 and
stock build, then all four fastboot variables above.
Proceed only with `unlocked: yes`, `is-userspace: no`, `current-slot` matching the saved `$slot` (without the underscore),
`snapshot-update-status: none`, and no pending OTA update. Never switch slots.
Temporarily boot the image and choose Keep Read Only if prompted. Pull the
active slot's recovery; it must be 100663296 bytes and have the same SHA-256 on
the phone and PC. Retain it separately; an existing custom recovery produces
a custom backup, not a factory image.

Return to bootloader and repeat all four checks before flashing `$recovery`.
After booting the installed TWRP, its partition SHA-256 must equal the pinned
image hash above. Do not modify boot, dtbo, vbmeta or the other slot. Do not wipe,
format data, or install the TWRP App. Reboot → System returns to Android;
`adb reboot recovery` enters TWRP again.

To restore your previous recovery, verify your own backup size/hash, enter
the same unlocked bootloader with `is-userspace: no`, the same active slot, and use the two
commands in the restore section. Do not retry failed writes with random
images or switch slots. On Linux use `sha256sum` instead of Get-FileHash.

See the bilingual changelog for tested functions and limitations. LineageOS
decryption requires the stock-compatible boot header OS/patch: 11.0.0 / 2023-09.
The MindTheGapps-17 EROFS workaround runs on Reboot → System or `twrp reboot
system`, not `adb reboot`; `/sdcard/TWRP/.no_gmsfix` disables it. Stock Android
is unaffected by this hook.
