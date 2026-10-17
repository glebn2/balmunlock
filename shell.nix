# shell.nix — окружение для разблокировки BALMUDA Phone (A101BM)
# Часть репозитория A101BM-unlock.
#
# Использование:
#   nix-shell            # из корня репозитория
#   cd tools && python3 cve31317.py ...
#
# Доступ к USB настраивается отдельно через udev с учётом дистрибутива.
# Зависимости и настройка прав: README.md#setup.

{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  name = "a101bm-unlock";

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
