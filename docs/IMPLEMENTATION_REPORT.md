# Raport implementacji

## Sprzęt i system

- Ubuntu 26.04.1 LTS, kernel 7.0.0-31, 8 CPU, 45 GiB RAM.
- FireWire VIA VT6306/7/8, `firewire_ohci`, Sony DCR-PC2E GUID `0800460101b2603d`, S100.
- Storage Kingston SKC400S371T 1 TB, SMART PASSED, ext4 w `/srv/minidv`; test 512 MiB direct I/O: 2,7 GB/s (wynik obejmuje cache kontrolera/SSD).

## Architektura

Hostowa usługa Python/systemd realizuje stan joba, AV/C, capture i processing. Statyczny responsywny UI korzysta z JSON API. Opcjonalny kontener nginx wystawia tę usługę na porcie 8088. Stan przechowywany jest atomowo logicznie w `/srv/minidv/state`; duże pliki nigdy nie trafiają na root NVMe.

Stan: `CREATED → CHECKING_STORAGE → CHECKING_CAMERA → REWINDING → CAPTURING → ANALYZING_DV → DETECTING_SCENES → COMPRESSING → VERIFYING_ARCHIVES → ENCODING_MP4 → VERIFYING_MP4 → COMPLETED`, z timestampami i logiem. Błąd przechodzi do `ERROR`, anulowanie do `CANCELLED`.

## Test rzeczywisty

Kontroler i kamera zostały wykryte. Bezpośrednie AV/C potwierdziło STOP i timecode, a PLAY/STOP wykonano na kasecie. Capture 15 s dał 375 pełnych klatek PAL, 54 000 000 B, timecode start `00:05:51:17`, recording datetime `2067-02-15 22:26:25` (błędny zegar kamery zachowany zgodnie z zasadą archiwalną). Offline autosplit wyprodukował strumień identyczny byte-for-byte: SHA-256 obu plików `a3a0d49b9c184191d926d3fd9d50a90eade17808139986134f7207168b4e9f9f`.

Pełny processing rzeczywistego pliku przeszedł pomyślnie: ZSTD 47 940 310 B, MP4 H.264 720×576/50p 29 571 591 B z dwiema ścieżkami audio AAC, JSON i thumbnail. `zstd -t` przeszedł, manifest `sha256sum -c` zwrócił OK dla wszystkich plików, a SHA-256 mastera po dekompresji jest identyczny ze źródłem: `a3a0d49b9c184191d926d3fd9d50a90eade17808139986134f7207168b4e9f9f`. API, UI, systemd oraz proxy Docker na portach 8080/8088 sprawdzono lokalnie i z innej maszyny LAN.

Ograniczenie sprzętowe: DCR-PC2E nie implementuje części zapytań AV/C oczekiwanych przez `dvcont`, więc zastosowano bezpośredni FCP. Po testach kamera przestała zgłaszać węzeł na magistrali (pozostał kontroler lokalny); aplikacja prawidłowo pokazuje `NO_CAMERA`. Zegar kamery wskazuje rok 2067. Dalsze usprawnienia: pakiet DVRescue dla jeszcze głębszej telemetrii DIF per-frame oraz uwierzytelnianie UI przy ekspozycji poza zaufany LAN.
