# TWRP 3.7.1_12-BALMUDA_Phone-20261025

**Образ:** `TWRP-3.7.1_12-BALMUDA_Phone-20261025.img`
**sha256:** `9e4d0c0707d83a6a3d5ec3fcad4c2f3296996348603817163540ddad7a840702`

## Изменения относительно 20261024

- **Понятное сообщение, если /data не расшифровывается.** Раньше было только «Failed to mount '/data'» с кодом 6. Теперь TWRP объясняет причину: скорее всего, /data создана прошивкой, у которой версия Android и патч в заголовке boot-образа отличаются от стоковых 11.0.0 / 2023-09. И подсказывает, что Wipe → Format Data очистит /data вместе с внутренней памятью.
- **Сообщение, если отложенная установка не выполнена.** Это задел для обновлений LineageOS «одной кнопкой». Если при установке по команде из системы ввод PIN отменён, TWRP теперь пишет, что команды не выполнены, и не пытается выполнить их снова.

Полный список изменений относительно 20261020 — в `CHANGELOG-20261024.md`; все его пункты действуют и в этой сборке.

## Проверено на A101BM

- Расшифровка LineageOS (без PIN), MTP «BALMUDA Phone».
- Сообщение о сбое расшифровки — на тестовой сборке с изменённым заголовком. Ключ шифрования не меняется.
- Остальное — без изменений относительно 20261024.

На X01A эта сборка не проверялась.

---

# TWRP 3.7.1_12-BALMUDA_Phone-20261025

**Image:** `TWRP-3.7.1_12-BALMUDA_Phone-20261025.img`
**sha256:** `9e4d0c0707d83a6a3d5ec3fcad4c2f3296996348603817163540ddad7a840702`

## Changes since 20261024

- **Clear message when /data cannot be decrypted.** Previously users only saw "Failed to mount '/data'" with error 6. TWRP now explains the likely cause: /data was created by a ROM whose boot image header carries a different Android version / security patch than stock (11.0.0 / 2023-09). It also points out that Wipe → Format Data clears /data, including internal storage.
- **Message when a pending install did not run.** This prepares one-tap LineageOS updates. If PIN entry is cancelled during an install requested by the system, TWRP now says the commands were not run and does not retry them.

For the full list of changes since 20261020, see `CHANGELOG-20261024.md`; all of it applies to this build too.

## Tested on A101BM

- LineageOS decryption (no PIN), MTP "BALMUDA Phone".
- Decryption failure message, on a test build with a modified header. The encryption key is left untouched.
- Everything else is unchanged from 20261024.

This build has not been tested on X01A.
