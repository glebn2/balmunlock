# TWRP 3.7.1_12-BALMUDA_Phone-20261024 — изменения относительно 20261020

**Образ:** `TWRP-3.7.1_12-BALMUDA_Phone-20261024.img`
**sha256:** `9d19aeaac7b6f96d00fa4745f7cdc3642b2a06988124c8859d2f59f0d604a6f7`

## Новое

- **Встроенный GMS fix для MindTheGapps-17.** Штатное ядро не поддерживает EROFS. Из-за этого Google Play services из MTG-17 не запускаются, и мастер Google показывает чёрный экран.
  - Теперь при Reboot → System TWRP сам переносит Play services в систему; подпись Google не меняется. Отдельный zip больше не нужен: Lineage → MindTheGapps → Reboot.
  - Срабатывает, только если ядро без EROFS и установлен именно такой пакет gapps. Другие прошивки и gapps не затрагивает.
  - Выключается файлом `/sdcard/TWRP/.no_gmsfix`. При перезагрузке через `adb reboot` не срабатывает.
- **Расшифровка /data LineageOS.** Работает с PIN, паролем и узором, если boot-образ прошивки собран с версией и патчем в заголовке 11.0.0 / 2023-09, как у стока. Сборки LineageOS для BALMUDA Phone так и собираются.

## Исправлено

- **Format Data больше не отключает системные разделы.** Раньше gapps, Magisk и прошивки после форматирования падали с «Could not mount /mnt/system», пока не перезагрузишь recovery.
- **TWRP не трогает базу блокировки экрана.** Раньше он мог создать пустую `/data/system/locksettings.db`, и система после этого не загружалась.
- **Имя телефона.** В Проводнике Windows, по MTP и в Linux — «BALMUDA Phone», модель — «Phone». Кодовое имя (A101BM или X01A) берётся из установленной системы, поэтому zip-установщики проверяют модель правильно. Если Windows показывает старое имя — удалите телефон в Диспетчере устройств и подключите заново.
- **Батарея:**
  - показывается реальный процент заряда, а не 0;
  - по таймауту экран гаснет полностью, кнопка питания его будит. Раньше гасла только подсветка, и за ночь батарея садилась.
- **Вернулись файлы, пропавшие из образа:**
  - Advanced → Fix Contexts снова работает;
  - часовой пояс применяется (раньше время всегда было в UTC);
  - nano и настройки bash снова на месте.
- **Логи:**
  - пропал поток ошибок keymaster раз в секунду в fastbootd;
  - строки о батарее пишутся только при изменениях.

## Проверено на A101BM

- Расшифровка: сток (без PIN и с PIN), LineageOS (PIN, узор).
- MTP, ADB, sideload, установка zip, OTG.
- Бэкап и восстановление.
- Wipe: Dalvik, Cache, Factory reset, Format Data.
- Mount, Fix Contexts, перезагрузки, fastbootd.

На X01A эта сборка не проверялась.

---

# TWRP 3.7.1_12-BALMUDA_Phone-20261024 — changes since 20261020

**Image:** `TWRP-3.7.1_12-BALMUDA_Phone-20261024.img`
**sha256:** `9d19aeaac7b6f96d00fa4745f7cdc3642b2a06988124c8859d2f59f0d604a6f7`

## New

- **Built-in GMS fix for MindTheGapps-17.** The stock kernel has no EROFS support. Because of that, Google Play services from MTG-17 never start, and Google setup shows a black screen.
  - On Reboot → System, TWRP now moves Play services into the system; the Google signature is untouched. No extra zip is needed: Lineage → MindTheGapps → Reboot.
  - It only runs when the kernel lacks EROFS and this exact gapps package is installed. Other ROMs and gapps are not affected.
  - To disable it, create `/sdcard/TWRP/.no_gmsfix`. It does not run on `adb reboot`.
- **LineageOS /data decryption.** Works with PIN, password and pattern, as long as the ROM's boot image header carries the stock version and patch level 11.0.0 / 2023-09. LineageOS builds for BALMUDA Phone are built this way.

## Fixed

- **Format Data no longer unmaps the system partitions.** Previously gapps, Magisk and ROM zips failed after a format with "Could not mount /mnt/system" until recovery was rebooted.
- **TWRP no longer touches the lock screen database.** Previously it could create an empty `/data/system/locksettings.db`, and the system then failed to boot.
- **Device name.** Windows Explorer, MTP and Linux show "BALMUDA Phone"; the model is "Phone". The codename (A101BM or X01A) comes from the installed system, so zip installers still check the model correctly. If Windows shows the old name, remove the phone in Device Manager and reconnect it.
- **Battery:**
  - the real charge level is shown instead of 0;
  - the screen fully turns off on timeout and the power button wakes it. Previously only the backlight went off and the battery drained overnight.
- **Files missing from the image are back:**
  - Advanced → Fix Contexts works again;
  - the time zone is applied (the clock used to be always UTC);
  - nano and the bash config are back.
- **Logs:**
  - no more keymaster error spam every second in fastbootd;
  - battery lines are logged only on change.

## Tested on A101BM

- Decryption: stock (no PIN and with PIN), LineageOS (PIN, pattern).
- MTP, ADB, sideload, zip install, OTG.
- Backup and restore.
- Wipe: Dalvik, Cache, Factory reset, Format Data.
- Mount, Fix Contexts, reboots, fastbootd.

This build has not been tested on X01A.
