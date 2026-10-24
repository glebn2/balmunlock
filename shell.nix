# shell.nix — зависимости Linux-входа balmunlock (анлок и TWRP).
#
# Использование:
#   nix-shell            # из корня репозитория
#   bash unlock.sh       # тот же Python-контроллер и меню, RU/EN автоматически
#   bash unlock.sh --lang en
#
# Доступ к USB настраивается отдельно через udev с учётом дистрибутива.
# Зависимости и настройка прав: README.md#linux.
# <nixpkgs> не закреплён: это среда зависимостей, не фиксированный build/release.

{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  name = "balmunlock";

  buildInputs = with pkgs; [
    # python + единственная зависимость скриптов tools/
    (python3.withPackages (ps: with ps; [
      pyusb
    ]))

    # бэкенд libusb для pyusb
    libusb1

    # adb + fastboot (нативные Linux)
    android-tools

    # диагностика USB (lsusb)
    usbutils

    # AT-порт как tty, если qcserial занял интерфейс (альтернатива at_console.py)
    picocom

    # вспомогательное для работы с образами разделов (не обязательно для анлока)
    e2fsprogs      # mke2fs/debugfs — ext4 (metadata)
    f2fs-tools     # mkfs.f2fs/sload.f2fs — userdata
    xz             # распаковка образов
  ];


}
