# Plan: ARDY sterowany w czasie rzeczywistym w MotionBuilderze (moduł nagrywania)

**Cel:** operator w MotionBuilderze steruje postacią generowaną przez ARDY
na żywo (kierunek, prędkość, heading; docelowo prompt), widzi ruch na szkielecie
w viewporcie i **nagrywa sesję do take'a** — jak przy sesji mocap, tylko
źródłem jest model zamiast aktora.

---

## 0. Ustalenia (fakty z kodu i z naszych wdrożeń)

1. **ARDY jest architektonicznie stworzony do tego zadania.** Autoregresja
   oknami + `autoregressive_step()` (`ardy_model.py:706`) do kontynuacji
   z historią — to jest „online prompting" z papieru. Nasz batchowy
   backend MMCP celowo z tego nie korzysta; tryb live to właśnie ta druga
   połowa modelu.
2. **Istnieje kompletny wzorzec pętli live**: `scripts/interactive_demo`
   w repo ardy (4 800 linii, GUI w viser). Kluczowe, sprawdzone w ich
   kodzie mechanizmy do przeniesienia:
   - replan z lockiem i `skip_if_busy` — trigger per klatka nie kolejkuje
     się za trwającą generacją (`generation.py:135-152`);
   - **`replan_buffer_size`** — odtwarzanie wyprzedza generację o bufor,
     co maskuje czas kroku;
   - historia przycinana do wielokrotności `num_frames_per_token`
     (`generation.py:170-171`);
   - **dyskowy cache embeddingów promptów** (`embedding_cache.py`) —
     presety promptów nie płacą latencji enkodera.
3. **Demo jest jednoprocesowe** (viser = web-GUI tego samego procesu).
   Rozdziału „serwer modelu ↔ klient DCC" nie ma — musimy go zbudować.
4. **Wariant `core8`** (horyzont 8 klatek = **0,4 s** @ 20 fps) istnieje
   w rejestrze ardy obok `core` (40 klatek = 2 s) — to on jest właściwy
   do sterowania na żywo; `core` zostaje dla trybu batch. [K]
5. **MMCP nie nadaje się jako transport live** — jest request/response.
   Tryb live musi być osobnym, jawnie niestandardowym kanałem
   (WebSocket) obok `/generate`. Protokół MMCP zostaje nietknięty.
6. **Prompty tekstowe dziś nie działają** (`TEXT_ENCODER_MODE=dummy`,
   embeddingi zerowe; bloker: token HF do gated Llama-3). Działają
   natomiast **constrainty** — kierunek/heading/ścieżka — zweryfikowane
   pomiarem (adherencja 4 cm w batchu). Sterowanie F1 opieramy na nich.
7. **MotionBuilder — fakty ogólne** (do potwierdzenia w S0 na Twojej
   instalacji): pełnoprawne urządzenie nagrywające (panel Record/Transport)
   wymaga pluginu C++ (Open Reality SDK, klasa `FBDevice`). Python
   (`pyfbsdk`) nie tworzy devices, ale pozwala na: wątek sieciowy w tle,
   aplikowanie transformów w callbacku głównego wątku i wstawianie kluczy
   — czyli pełny podgląd live + zapis do take'a po naszej stronie.
   API sceny **nie jest thread-safe** — mutacje wyłącznie z głównego wątku.
8. Ardy `core8` dzieli z `core` szkielet Core27 i motion_rep — nasza
   konwersja wyjścia (kwaterniony lokalne, `skeleton_to_mmcp`) działa bez
   zmian. VRAM: dziś oba modele = 3,9/24,5 GB [P]; drugi wariant ardy to
   ~+1,3 GB — mieści się z zapasem.

## 1. Architektura docelowa (Faza 1)

```
MotionBuilder (pyfbsdk)                     mmcp_server (istniejący proces)
┌──────────────────────────┐   WebSocket   ┌───────────────────────────────┐
│ wątek sieciowy → kolejka │ ⇄ /stream ⇄  │ ArdyStreamSession             │
│ OnUIIdle: aplikuj klatki │               │  · ardy core8 (ARDY_STREAM_   │
│ na szkielet Core27       │               │    MODEL, ładowany leniwie)   │
│ UI: kierunek/predk/Start │               │  · pętla autoregressive_step  │
│ Stop/Record              │               │    + historia (wzorce z demo) │
│ bufor nagrania → bake    │               │  · jedna sesja naraz (lock)   │
│ do take'a po Stop        │               └───────────────────────────────┘
└──────────────────────────┘
```

**Protokół `/stream` (rozszerzenie poza MMCP, jawnie oznaczone):**
- klient → serwer: `{type:"start", model_variant, fps, seed}`,
  `{type:"control", move_dir:[x,z], speed, facing?:[x,z], prompt?}`,
  `{type:"stop"}`;
- serwer → klient: `{type:"frames", t0, fps, joints:[...],
  rotations:[[x,y,z,w]…], root:[…]}` — paczka klatek po każdym kroku AR,
  plus `{type:"session", skeleton:{…}}` na starcie (ten sam format co
  `canonical_skeleton` w MMCP — klient buduje szkielet identycznie jak
  addon Blenderowy).

**Nagrywanie F1 — decyzja upraszczająca:** klient buforuje wszystkie
odebrane klatki z timestampami; „Record" to znacznik początku, „Stop"
wyzwala **bake do take'a** (klucze co 1/20 s w FBTime, niezależnie od
fps sceny MB). Zero walki z transportem MB i synchronizacją — prawdziwy
panel Record przez C++ `FBDevice` to Faza 2.

## 2. Decyzje projektowe

| # | Decyzja | Wybór | Uzasadnienie |
|---|---------|-------|--------------|
| D1 | Transport | WebSocket na istniejącym FastAPI (`/stream/ardy`) | Model już siedzi w tym procesie; zero kopiowania wag. Endpoint opisany jako rozszerzenie niestandardowe — MMCP bez zmian |
| D2 | Wariant modelu | `core8` do live (env `ARDY_STREAM_MODEL`), ładowany **leniwie** przy pierwszej sesji | 0,4 s okna vs 2 s; leniwie — żeby batch-only deploymenty nie płaciły VRAM |
| D3 | Klient MB | Python/pyfbsdk w F1; C++ FBDevice dopiero w F2 | Rząd wielkości tańszy start; wystarcza do podglądu + bake. C++ tylko gdy potrzebny prawdziwy Record panel |
| D4 | Sterowanie F1 | **wyłącznie** kierunek+prędkość+heading (wektor z UI lub null-obiekt w scenie jako cel); bez tekstu | Ardy — inaczej niż MotionBricks — **nie ma enuma trybów ruchu**: „chód vs bieg" to u niego kwestia promptu albo prędkości implikowanej constraintem. Constrainty działają na dummy enkoderze [P w batchu]; charakter ruchu (skradanie, taniec…) wejdzie dopiero z tekstem w F2, po tokenie HF, bez zmian architektury (embedding cache zaplanowany) |
| D5 | Czas | klatki stemplowane czasem serwera (t0+i/20); klient aplikuje wg zegara, spóźnione dropuje na podglądzie, bake zawsze z pełnego bufora | Podgląd może gubić klatki, nagranie nigdy |
| D6 | Miejsce kodu | serwer: `stream_ardy.py` w motionmcp-kimodo-cloud; klient: **nowe repo `proscenium-motionbuilder`** (utworzenie — decyzja przy wdrożeniu) | Klient MB to osobny produkt jak proscenium-blender; nie zaśmiecać repo serwera |
| D7 | Sesje | jedna aktywna sesja streamu naraz (403 dla drugiej) | Model + historia są stanowe; wzorzec przetestowany w planie MotionBricks. Multi-sesja = osobny temat |
| D8 | Postprocessing | wyłączony w pętli live; opcjonalny przy bake'u | `post_process_motion` na oknie 8 klatek psułby ciągłość między oknami; przy bake'u działa na całości |

## 3. Kroki — serwer (motionmcp-kimodo-cloud)

### S0 — bramka pomiarowa (BLOKUJĄCA, ~0,5 dnia)
Skrypt w scratchpadzie, bez zmian w repo:
1. Załadować `core8`, zmierzyć **czas jednego `autoregressive_step`**
   (batch 1, 10 kroków dyfuzji, 3090) — i to samo dla `core`.
   **Progi:** < 150 ms → komfortowo (bufor 1 okna wystarczy);
   150–400 ms → działa z buforem 2 okien (dodatkowa latencja sterowania
   ~0,8 s — do zaakceptowania przez Ciebie); > 400 ms → generacja nie
   nadąża za odtwarzaniem 20 fps (8 klatek = 400 ms budżetu) — STOP
   i powrót z pomiarami.
2. Poprowadzić ręcznie 5 kroków AR z ciągłą historią poza demo
   (mini-pętla na wzór `generation.py`) — potwierdzić, że kontrolujemy
   pętlę bez visera i że przekazywanie constraintu kierunku między
   oknami daje spójny ruch (wizualnie: zapis do gltf i podgląd).
3. Zmierzyć VRAM z trzema modelami (kimodo + ardy core + core8).
4. **Pytania do Ciebie (bramka kliencka):** wersja MotionBuildera
   (2023+/Python 3?), czy na tej samej maszynie co serwer (localhost,
   czy sieć), czy masz gamepad/preferencję sterowania.

### S1 — `stream_ardy.py`: sesja streamingu
- `ArdyStreamSession`: ładuje wariant z `ARDY_STREAM_MODEL` (leniwie,
  osobno od backbone'u batch), trzyma historię i stan pętli.
- Pętla generacji we własnym wątku sesji: co iteracja —
  zbierz najnowszy stan sterowania → zbuduj constraint kierunku
  (Root2D na horyzont okna; interpolacja headingu wygładza zakręty) →
  `autoregressive_step` z historią → konwersja okna na kwaterniony
  lokalne (reużycie ścieżki z `ardy_backbone`/`motion_rep.inverse`) →
  push paczki klatek do WebSocketu.
- **Zimny start**: pierwsze okno bez historii — `first_heading_angle=0`,
  postać rusza z originu twarzą w +Z (jak w batchu); klient ustawia
  szkielet w punkcie startu sceny offsetem obiektu.
- **Globalna translacja roota między oknami** — najłatwiejszy błąd tej
  pętli: `_recenter_history` recentruje historię co krok, a pozycję
  światową niesie akumulowane `global_transl` (`ardy_model.py`, pętla
  `__call__`). Sesja musi prowadzić ten akumulator identycznie, inaczej
  postać „teleportuje" na granicach okien. Wzorzec: demo `generation.py`
  + `translate_normalized_root_motion`.
- **Constrainty do okna**: budowane per okno w układzie okna (frame 0 =
  początek okna) albo cropowane wzorcem `crop_move(start, end)`
  z `ardy/constraints.py` — dokładnie po to istnieje w ich API.
- Zarządzanie historią: przycinanie do `default_history_frames` i do
  wielokrotności tokenów — kod przeniesiony wzorcem z demo
  (`generation.py:170-171`).
- Twardy limit iteracji bez odbiorcy (klient zniknął → sesja umiera po
  timeoucie, model zostaje w pamięci).

### S2 — endpoint `/stream/ardy` w `mmcp_server.py`
- WebSocket FastAPI; handshake → `session` (szkielet), potem pętla.
- Lock jednej sesji (D7); `/health` raportuje `stream: idle|active`.
- Wyraźny docstring: „rozszerzenie poza spec MMCP".

### S3 — testy serwera
- Jednostkowe: budowa constraintu kierunku z komend (czysta funkcja),
  przycinanie historii, protokół (pydantic-owe modele komunikatów).
- Integracyjny bez MB: **mock-klient w pytest** (websockets) — start,
  10 s sterowania skryptowego (jazda w kwadrat), zbiera klatki, mierzy:
  ciągłość czasów (brak dziur), opóźnienie komenda→pierwsza klatka
  z nowym kierunkiem, zgodność liczby klatek z czasem.
- Regresja batch: golden `/generate` bez zmian; pytest całości.

## 4. Kroki — klient MotionBuilder (nowe repo)

### K1 — szkielet sceny
Skrypt `build_skeleton.py`: buduje hierarchię `FBModelSkeleton` z dicta
szkieletu otrzymanego w handshake'u (ten sam format co w addonie
Blender — offsety rodzic-lokalne, MMCP Y-up → MB: **uwaga, MB jest
Y-up** jak MMCP, więc konwersja jest lżejsza niż w Blenderze — do
potwierdzenia na jednej kości w K2).

### K2 — odbiornik i podgląd live
`ardy_live_device.py` (skrypt, nie device): wątek `websocket-client` →
`queue`; rejestracja na `FBSystem().OnUIIdle`; w callbacku: pobierz
z kolejki wszystko, zastosuj najnowszą klatkę ≤ zegar (drop starszych),
ustaw lokalne rotacje kości + translację roota. Start/Stop z małego UI
(pyfbsdk `FBCreateUniqueTool` z przyciskami i sterowaniem
kierunkiem — strzałki/przyciski + suwak prędkości i przełącznik
„facing = kierunek ruchu / niezależny"; opcjonalnie null w scenie jako
cel, czytany co idle).
Dwie dyscypliny z góry: **sprzątanie callbacków** przy Stop/reload
(OnUIIdle.Remove — wzorzec purge znany z addonu Blenderowego, inaczej
duchy po przeładowaniu skryptu) oraz **zależności w Pythonie MB** —
pakiet WebSocket instalowany do interpretera MotionBuildera
(`mobupy -m pip install websocket-client`), co idzie do instrukcji K4.

### K3 — nagrywanie (bake do take'a)
Bufor wszystkich klatek od znacznika Record; Stop → utworzenie/wybór
take'a, `FBAnimationNode.KeyAdd` w FBTime co 1/20 s dla wszystkich kości,
plot. Sesja 60 s = 1200 kluczy × 28 węzłów — wciąż mało; bake w pętli
głównego wątku z paskiem postępu.

### K4 — dokumentacja klienta
README: instalacja (skrypt w `PythonStartup` MB), konfiguracja URL,
znane ograniczenia F1 (podgląd może dropować klatki — nagranie nie;
jeden strumień naraz; brak promptów do czasu tokena HF).

## 5. Faza 2 (poza zakresem F1 — świadomie)
- C++ `FBDevice` → prawdziwy panel Record/Transport, wejście w standardowy
  workflow mocap (wymaga toolchainu MSVC pod konkretną wersję MB).
- Characterization HIK szkieletu Core27 (nazwy mixamo-podobne → mapping
  niemal 1:1) → retarget live na dowolny rig w MB.
- Sterowanie promptem (po tokenie HF): presety pre-enkodowane przez
  `embedding_cache` (wzorzec z demo), pole tekstowe z ostrzeżeniem
  o latencji enkodera.
- Gamepad (XInput w wątku klienta).
- Multi-sesja / multi-postać.

## 6. Kolejność i zależności

```
S0 (pomiary + Twoje odpowiedzi o MB) ──► GO/NO-GO
   │ GO
   ├─► S1 ──► S2 ──► S3 (mock-klient; serwer DONE bez MB)
   └─► K1 ──► K2 ──► K3 ──► K4  (wymaga instancji MB — Twojej)
E2E: dopiero S3 + K2 razem; potem K3.
```
Serwer jest w pełni testowalny bez MotionBuildera (S3-mock). Klient
wymaga MB — jego kroki wykonujemy z Tobą przy maszynie albo dostarczam
kod + instrukcję, a Ty odpalasz.

## 7. Weryfikacja końcowa (E2E)

1. **Latencja sterowania**: zmiana kierunku w UI → widoczna zmiana na
   szkielecie < 1 s (core8: 0,4 s okna + krok + bufor). Pomiar w logach
   obu stron (serwer stempluje generację, klient aplikację).
2. **Ciągłość**: 60 s jazdy → zero dziur czasowych w buforze nagrania;
   złączenia okien bez skoków (wizualnie + max delta rotacji między
   klatkami na granicach okien porównywalna z wewnątrz okna).
3. **Nagranie**: bake → take odtwarza się identycznie jak podgląd;
   eksport FBX z MB otwiera się w Blenderze (kontrola krzyżowa szkieletu).
4. **Odporność**: zabicie klienta w trakcie → sesja serwera umiera po
   timeoucie, `/health` wraca do `idle`; restart sesji działa bez
   restartu serwera; `/generate` (batch) działa **w trakcie** aktywnego
   streamu albo zwraca czysty błąd zajętości (decyzja w S1 — lock GPU).
5. **Regresja**: golden batch bez zmian; cała suita pytest zielona.

## 8. Ryzyka

| # | Ryzyko | Prawdop. | Mitygacja |
|---|--------|----------|-----------|
| R1 | Krok AR za wolny na 20 fps (budżet 400 ms/okno core8) | średnie | S0 mierzy z twardymi progami; bufor okien maskuje do granicy; NO-GO z liczbami zamiast „gąbczastego" produktu |
| R2 | Sterowanie tekstem oczekiwane, a zablokowane (dummy enkoder) | pewne w F1 | Jawnie w zakresie: F1 = kierunek/prędkość/heading; tekst wchodzi po tokenie HF bez zmian architektury |
| R3 | pyfbsdk nie jest thread-safe → crashe MB | średnie | Żelazna zasada: sieć tylko do kolejki, scena tylko z OnUIIdle; code review pod tym kątem |
| R4 | Szwy między oknami AR widoczne w ruchu | średnie | To samo ryzyko co multi-segment w batchu — tam ciągłość z historii wystarczyła [P dla batch]; E2E #2 mierzy jawnie; ewent. krótki cross-fade przy bake'u |
| R5 | `/generate` i stream biją się o GPU | pewne przy równoległości | D7 + decyzja w S1: albo wspólny lock (batch czeka), albo 409; nigdy ciche spowolnienie obu |
| R6 | Nie mam dostępu do MB — klienta nie przetestuję sam | pewne | Rozdział S3-mock (serwer domknięty beze mnie przy MB); kroki K z Tobą; kod klienta z trybem „replay z pliku" do testów poza MB |
| R7 | Konwersja osi MMCP→MB błędna (obrócony/lustrzany szkielet) | niskie–średnie | MB jest Y-up jak MMCP; K1 weryfikuje na T-pose porównaniem world-pozycji 4 kości z wartościami z capabilities |

## 9. Czego NIE robimy w F1
- Żadnych zmian w spec MMCP ani w addonie Blenderowym.
- Bez C++ SDK, bez HIK retargetu, bez gamepada, bez promptów (F2).
- Bez multi-sesji.
- Nie tworzę repo `proscenium-motionbuilder` przed Twoją decyzją.
