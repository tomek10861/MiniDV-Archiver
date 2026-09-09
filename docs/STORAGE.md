# Storage

Dedykowany SSD: Kingston SKC400S371T, serial `50026B72670515AA`, 1 024 209 543 168 B. Dysk systemowy to osobny Lexar NVMe. Kingston nie był root, boot, swap, RAID ani zamontowanym filesystemem; zawierał pusty thin-pool LVM `SSD-DATA` bez thin volumes.

Utworzono GPT, jedną partycję ext4 z etykietą `MINIDV_ARCHIVE`, UUID `5d4f9391-bb58-495a-8cd4-751e60bb69b3`, montowaną w `/srv/minidv` przez `/etc/fstab` z `defaults,noatime,nofail,x-systemd.device-timeout=30`. Ext4 wybrano ze względu na stabilność, fsck/recovery i dobrą obsługę dużych plików. Rezerwa root wynosi 0%, bo to dedykowany wolumen danych.

Katalogi: `tapes`, `working`, `failed`, `logs`, `tmp`, `state`. Przed capture wymagane jest domyślnie 80 GiB wolnego. UI pokazuje pojemność i konserwatywną liczbę godzin DV. Recovery: zatrzymać usługę, sprawdzić SMART, odmontować i wykonać `e2fsck -f /dev/disk/by-label/MINIDV_ARCHIVE`; niedokończone capture pozostają w `working/`.

