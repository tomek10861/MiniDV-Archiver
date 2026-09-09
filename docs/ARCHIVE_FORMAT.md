# Format archiwum

Każda scena ma identyfikator `NNNN_YYYY-MM-DD_HH-MM-SS` albo `NNNN_UNKNOWN-DATE` i pliki `.dv.zst`, `.mp4`, `.json`. `tape.json` zachowuje fizyczną kolejność, `tape.sha256` kontroluje pliki, `capture.log` dokumentuje odbiór, a `thumbnails/` zawiera JPEG.

Przed usunięciem roboczego `.dv` program liczy SHA-256, kompresuje ZSTD poziomem 6, wykonuje `zstd -t`, strumieniowo dekompresuje archiwum i ponownie liczy SHA-256. Źródło jest kasowane wyłącznie po identycznym wyniku. MP4 to H.264 CRF 16 + AAC; materiał wykryty jako przeplatany jest przekształcany `bwdif` do pełnej częstotliwości pól. Wszystkie wykryte ścieżki audio są zachowane jako osobne ścieżki MP4.

