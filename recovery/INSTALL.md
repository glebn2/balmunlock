# TWRP 3.7.1_12-BALMUDA_Phone (20261024)

Образ: `TWRP-3.7.1_12-BALMUDA_Phone-20261024.img`, 100663296 байт.
SHA-256: `9d19aeaac7b6f96d00fa4745f7cdc3642b2a06988124c8859d2f59f0d604a6f7`.

Проверен на A101BM. X01A не проверен. Инструкция ниже — для штатного
A101BM, Android 12 `1.280PO.0686.a`, разблокированный загрузчик, слот b.
Нужны ADB/fastboot и разрешённая отладка по USB. Важные данные сохраните отдельно.
Root, Magisk, GSI, повторный анлок и сброс данных для установки не нужны.

## Установка вручную (Windows PowerShell)

Распакуйте архив, откройте терминал в его каталоге. Проверьте хеш:

```powershell
Get-FileHash .\TWRP-3.7.1_12-BALMUDA_Phone-20261024.img -Algorithm SHA256
adb shell getprop ro.build.version.incremental
adb reboot bootloader
fastboot getvar unlocked
fastboot getvar is-userspace
fastboot getvar current-slot
fastboot getvar snapshot-update-status
```

Ожидается: `1.280PO.0686.a`, `unlocked: yes`, `is-userspace: no`,
`current-slot: b`, `snapshot-update-status: none`. Если ответы отличаются или
обновление OTA ещё не завершилось — остановитесь. Слот не переключайте.

Временный запуск и сохранение текущего recovery (при запросе — Keep Read Only):

```powershell
fastboot boot TWRP-3.7.1_12-BALMUDA_Phone-20261024.img
adb shell "dd if=/dev/block/by-name/recovery_b of=/tmp/recovery_b.backup.img bs=1048576 count=96 && sha256sum /tmp/recovery_b.backup.img"
adb pull /tmp/recovery_b.backup.img recovery_b.backup.img
Get-FileHash .\recovery_b.backup.img -Algorithm SHA256
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
fastboot flash recovery_b TWRP-3.7.1_12-BALMUDA_Phone-20261024.img
fastboot reboot recovery
adb shell sha256sum /dev/block/by-name/recovery_b
```

К записи переходите только при тех же ожидаемых ответах. Хеш раздела после
записи должен совпасть с хешем образа. Только `recovery_b`; boot, dtbo, vbmeta
и слот a не трогаем. Wipe/Format Data не нужны, TWRP App не устанавливаем.
В Android: Reboot → System. Следующий вход: `adb reboot recovery`.

## Возврат предыдущего recovery

Из того же разблокированного bootloader, при `is-userspace: no` и слоте b,
сначала сверив размер и хеш своего бэкапа:

```powershell
fastboot flash recovery_b recovery_b.backup.img
fastboot reboot
```

Сбои записи не исправляйте случайными образами или переключением слота.
Для Linux команды ADB/fastboot те же; вместо Get-FileHash используйте `sha256sum`.

## Особенности сборки

Перечень изменений и проверок — `CHANGELOG-20261024.md`. Расшифровка LineageOS
требует stock-compatible версии и патча в заголовке boot: 11.0.0 / 2023-09.
Встроенный обход EROFS для MindTheGapps-17 срабатывает при Reboot → System
(также `twrp reboot system`), но не при `adb reboot`. Отключение:
`/sdcard/TWRP/.no_gmsfix`. Для штатной системы он ничего не меняет.

---

## English

Hardware-tested on A101BM; X01A remains untested. The commands above apply to
stock A101BM Android 12 `1.280PO.0686.a`, unlocked bootloader, active slot b.
Extract the archive and use ADB/fastboot with USB debugging authorized.
Keep a separate copy of important data. Root, Magisk, GSI and a wipe are not
required to install this recovery.

Check the image SHA-256 and stock build, then all four fastboot variables above.
Proceed only with `unlocked: yes`, `is-userspace: no`, `current-slot: b`,
`snapshot-update-status: none`, and no pending OTA update. Never switch slots.
Temporarily boot the image and choose Keep Read Only if prompted. Pull the
current recovery_b; it must be 100663296 bytes and have the same SHA-256 on
the phone and PC. Retain it separately; an existing custom recovery produces
a custom backup, not a factory image.

Return to bootloader and repeat all four checks before flashing recovery_b.
After booting the installed TWRP, its partition SHA-256 must equal the pinned
image hash above. Do not modify boot, dtbo, vbmeta or slot a. Do not wipe,
format data, or install the TWRP App. Reboot → System returns to Android;
`adb reboot recovery` enters TWRP again.

To restore your previous recovery, verify your own backup size/hash, enter
the same unlocked bootloader with `is-userspace: no`, slot b, and use the two
commands in the restore section. Do not retry failed writes with random
images or switch slots. On Linux use `sha256sum` instead of Get-FileHash.

See the bilingual changelog for tested functions and limitations. LineageOS
decryption requires the stock-compatible boot header OS/patch: 11.0.0 / 2023-09.
The MindTheGapps-17 EROFS workaround runs on Reboot → System or `twrp reboot
system`, not `adb reboot`; `/sdcard/TWRP/.no_gmsfix` disables it. Stock Android
is unaffected by this hook.
