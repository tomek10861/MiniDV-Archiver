# Metadata DV i granice scen

`dvgrab -autosplit` analizuje flagę nowego nagrania i nieciągłości timecode większe niż 1 s lub cofające się. Operuje na pełnym, surowym capture po zakończeniu odbioru, więc kolejność `scene001.dv`, `scene002.dv` jest fizyczną kolejnością taśmy i nigdy nie zależy od zegara kamery.

Recording datetime pierwszej klatki każdej sceny jest wydobywany z DV VAUX przez ograniczony do jednej klatki przebieg `dvgrab -timestamp`. Brak lub uszkodzenie daje `UNKNOWN-DATE`. `ffprobe` dostarcza standard, rozdzielczość, fps, proporcje, format pikseli, audio i początkowy timecode. JSON zapisuje też liczbę klatek i nieciągłości źródła.

Ważne rozróżnienie: błędy `frame dropped` w logu capture oznaczają utratę przy odbiorze; komunikaty offline autosplit są zapisywane jako `source_discontinuities` i mogą oznaczać przerwę/timecode na samej taśmie.

