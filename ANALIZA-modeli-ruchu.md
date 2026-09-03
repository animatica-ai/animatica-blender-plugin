# Analiza modeli ruchu: kimodo · ARDY · MotionBricks · HIL

**Fakty sprawdzone: 2026-08-10** · proscenium-blender `190a9c5` ·
motionmcp-kimodo-cloud `02eb0c3`

Każda wartość liczbowa ma oznaczenie źródła:

| | Znaczenie |
|---|---|
| **[P]** | **pomiar własny** — mamy skrypt i wynik |
| **[K]** | **odczyt z kodu** modelu lub jego konfiguracji |
| **[D]** | **deklaracja autorów** — niezweryfikowana |

Gdzie nie wiemy — pisze „nie badane". Nic nie jest szacowane.

---

## 1. Streszczenie decyzyjne

Mamy **dwa działające modele** serwowane z jednego serwera MMCP:
`kimodo-soma-rp` (dyfuzja całego klipu, 30 fps, do 30 s) i `ardy-core-rp`
(autoregresyjny, 20 fps, do 8 s). Oba sterują się identycznym zestawem
constraintów i oba trzymają się narysowanej ścieżki z dokładnością
**4 cm [P]**. Różnią się długością klipu, ziarnistością prompt-bloków
i tempem generacji.

**MotionBricks** zbadany, niewdrożony: w dostępnym kodzie to model
**robota Unitree G1**, sterowany komendami kierunku i stylu, bez tekstu.
Zapowiadane pełne wydanie nie ukazało się — katalog projektu nie był
ruszany od **kwietnia [K]**.

**HIL** zbadany, rekomendacja negatywna: to **polityka sterowania w
symulacji fizycznej**, nie generator ruchu. Kod jest nieoficjalny, wag
zadań z papieru **nie ma [K]**, a to, co jest, dotyczy robotów.

Najważniejsza luka nie dotyczy żadnego z nowych modeli: **prompty
tekstowe są dziś ignorowane** w obu wdrożonych modelach, bo serwer
działa na zerowym enkoderze tekstu.

## 2. Funkcje — przekrój

| | kimodo-soma-rp | ardy-core-rp | MotionBricks | HIL |
|---|---|---|---|---|
| **Kategoria** | generator klipów | generator klipów | sterownik ruchu robota, real-time | polityka RL w symulacji fizycznej |
| **Status u nas** | wdrożony, przetestowany | wdrożony, przetestowany | niewdrożony | niewdrożony (NO-GO) |
| **Embodiment** | postać ludzka (SOMA) [K] | postać ludzka (Core) [K] | robot G1 [K] | roboty G1/T1 [K] |
| **Warunkowanie tekstem** | tak (kod) | tak (kod) | **nie** [K] | **nie** [K] |
| **Ścieżka roota** | tak | tak | naturalnie (kierunek+facing) [K] | cel/heading [K] |
| **Piny efektorów** | tak | tak | Faza 2 (proxy keyframes) | n/d |
| **Pozy kluczowe** | tak | tak | Faza 2 | n/d |
| **Generacja pojedynczej pozy** | tak | tak | nie | n/d |
| **Retargeting na rig usera** | tak (kod) | tak (kod) | odradzany (robot) | n/d |
| **Świadomość sceny** | nie | nie | częściowa [D] | **tak — jego istota** [K] |

„n/d" przy HIL nie znaczy „gorszy" — znaczy, że pytanie nie ma sensu
dla tej kategorii. Patrz §6.

## 3. Sterowanie i workflow — co realnie widzi animator

Tabela dotyczy **wyłącznie dwóch wdrożonych modeli**; przy pozostałych
nie ma czego opisywać, bo nie są podłączone.

| Aspekt | kimodo-soma-rp | ardy-core-rp |
|---|---|---|
| **Granica prompt-bloków** | co do klatki [K] | zaokrąglana do okna **2 s** [K] |
| **Przejścia między blokami** | wygładzane, `transition_frames` domyślnie 5 [K] | brak parametru — ciągłość z historii autoregresyjnej [K] |
| **Seed per blok** | tak [K] | tak — po naszym patchu [P] |
| **Izolacja seedów** | nie badane | zmiana seeda bloku 2 **nie rusza** bloku 1 [P] |
| **Suwak jakości (kroki dyfuzji)** | presety 50/25/12 działają [K] | **bez efektu** — model ma 10 kroków bazowych, wszystko jest do nich przycinane [K] |
| **Pojedyncza poza** | kontekst 64 klatek [K] | kontekst 40 klatek [K] |
| **Siatka ciała w Blenderze** | tak (SOMA77) [K] | **nie** — inne proporcje szkieletu |
| **Maks. długość klipu** | 30 s, zalecane 12 s [K] | **8 s** [K] |

## 4. Dane techniczne

| | kimodo-soma-rp | ardy-core-rp | MotionBricks | HIL |
|---|---|---|---|---|
| **Architektura** | dyfuzja całego klipu | dyfuzja autoregresyjna, okna 40 klatek [K] | modularny backbone latentny [D] | RL (imitacja hybrydowa) [D] |
| **Szkielet / stawy** | SOMA30 → SOMA77 [K] | Core27 [K] | G1Skeleton34 [K] | G1 29-DoF / T1 [K] |
| **fps** | 30 [K] | 20 [K] | nie badane | sym. 120 Hz / polityka 30 Hz [D] |
| **Dostępność wag** | HF, ~1,1 GB [K] | HF [K] | w repo (Git LFS), ~2,3 GB [K] | **brak wag zadań z papieru** [K] |
| **Licencja wag** | NVIDIA Open Model | NVIDIA Open Model | NVIDIA Open Model | n/d (brak wag) |
| **Licencja kodu** | Apache-2.0 | Apache-2.0 | Apache-2.0 | Apache-2.0, **nieoficjalny** [K] |
| **Wymaga patchowanego forka** | nie | **tak — 3 patche** [K] | nie badane | nie badane |
| **Ciężkie zależności** | — | — | MuJoCo [K] | Isaac Sim/Gym + Holosoma [K] |
| **Aktywność projektu** | — | — | brak zmian od **2026-04-30** [K] | push 2026-07-27 [K] |

## 5. Pomiary własne [P]

Wszystko na RTX 3090, `TEXT_ENCODER_MODE=dummy`, postprocessing włączony.
Kolumny MotionBricks i HIL są puste, bo **nie zostały uruchomione** —
brak danych jest tu informacją, nie przeoczeniem.

| Pomiar | kimodo-soma-rp | ardy-core-rp | MotionBricks | HIL |
|---|---|---|---|---|
| Odchyłka od ścieżki (L, 3 piny) | **4,0 cm** | **4,0 cm** | — | — |
| Przemieszczenie końcowe (cel 2,12 m) | 2,10 m | 2,05 m | — | — |
| Czas generacji | ~2,9–3,9 s (2 s klipu) | ~3,2 s (6 s klipu) | — | — |
| Multi-segment 2×80 klatek | n/d (natywne) | 4,3 s | — | — |
| Pojedyncza poza | testowana | 2,3 s | — | — |
| Powtarzalność seeda | bitowa | bitowa | — | — |
| Testy błędów i limitów | 7/7 | 7/7 | — | — |
| VRAM (oba modele naraz) | 3,9 GB / 24,5 GB | ↔ | — | — |

Metodyka: pinowana ścieżka w kształcie L, pomiar odległości roota od
pinów w wygenerowanym glTF; szczegóły w `RAPORT-nocny-2026-08-06.md`.

## 6. Kategorie, nie liga

Zestawienie czterech nazw w jednej tabeli kusi, by czytać je jak ranking.
To byłby błąd — należą do trzech różnych klas narzędzi:

- **Generatory klipów** (kimodo, ARDY) — dostają specyfikację i zwracają
  gotową animację. Tylko one pasują do kontraktu MMCP i do workflow
  w Blenderze.
- **Sterownik czasu rzeczywistego** (MotionBricks) — utrzymuje stan
  i reaguje na komendy klatka po klatce. Da się go opakować w generator
  wsadowy, ale to my dopisujemy pętlę, której model nie ma.
- **Polityka sterowania fizycznego** (HIL) — nie produkuje animacji,
  tylko momenty w stawach w symulatorze. Żeby dostać klip, trzeba
  przeprowadzić symulację fizyczną ze sceną i stanem początkowym.
  **MMCP nie ma prymitywu sceny**, a scena jest istotą tego modelu.

## 7. Charakterystyka

**kimodo-soma-rp** — model referencyjny, wokół którego zbudowany jest
addon. Denoisuje cały klip naraz, dzięki czemu granice prompt-bloków są
dokładne, przejścia wygładzane, a kompozycja spójna na całej długości.
Ma najbogatszy zestaw funkcji (pozy, siatka ciała, działający suwak
jakości) i najdłuższy limit. Kosztem jest czas rosnący z długością.

**ardy-core-rp** — autoregresyjny, generuje oknami po 2 s kontynuującymi
historię. Stąd wyraźnie szybszy na sekundę animacji i naturalne
zastosowanie do szybkich iteracji, ale też twardy limit 8 s i prompt
zmieniający się dopiero na granicy okna. Wymaga naszego forka: trzy
addytywne patche (harmonogram promptów i seedów per okno, zerowy enkoder
tekstu, flaga kolizji `motion_correction`).

**MotionBricks** — w materiałach prezentowany jako uniwersalny backbone
ruchu; w dostępnym kodzie jest modelem robota G1 sterowanym czterema
trybami chodu i wektorami kierunku. Dobra wiadomość: przestrzeń ruchu
jest identyczna z MMCP (Y-up, Z-forward), a klasa szkieletu ma ten sam
interfejs co kimodo i ARDY, więc publikacja szkieletu byłaby niemal
darmowa. Zła: bez tekstu, z API strumieniowym i bez postaci ludzkiej.

**HIL** — najciekawszy naukowo i najmniej użyteczny produktowo w obecnym
stanie. Papier opisuje postać opartą na SMPL, ale wydany kod dotyczy
robotów; wag dla zadań z papieru nie ma i trzeba by je trenować
dwuetapowo przy 8192 równoległych środowiskach. Retargeting w tym
repo prowadzi **z człowieka do robota**, nie odwrotnie.

## 8. Rekomendacje

| Sytuacja | Model |
|---|---|
| Sceny dłuższe niż 8 s | **kimodo** — ARDY ma twardy limit |
| Precyzyjne granice bloków, wiele akcji po kolei | **kimodo** — ziarnistość klatki, wygładzane przejścia |
| Praca na siatce ciała bez własnej postaci | **kimodo** — jedyny z siatką |
| Szybkie iteracje, blocking, krótkie akcje | **ARDY** — szybszy na sekundę animacji |
| Regulacja jakości krokami dyfuzji | **kimodo** — u ARDY suwak nic nie zmienia |
| Ruch robota G1 | dziś: żaden wdrożony; plan MotionBricks |
| Fizycznie wiarygodny ruch | dziś: żaden; patrz §9 |

**Co dalej — kolejność wartości.** Nie „kolejny model", tylko domknięcie
tego, co już napisane: (1) brakujący checkpoint `boneclassifier_best.pt`
odblokowuje retargeting na rigi użytkownika — funkcję gotową w kodzie
i dotyczącą każdego; (2) token HF do Llama-3 włącza prompty w obu
modelach; (3) dopiero potem sensowna jest rozmowa o trzecim modelu.

## 9. Czego nie wiemy

- **Jakość podążania za promptem — nieprzetestowana dla obu wdrożonych
  modeli.** Serwer działa na `TEXT_ENCODER_MODE=dummy`, gdzie prompty
  kodują się do zera i są ignorowane. Wszystko w §3 i §5 dotyczy
  sterowania constraintami. Odblokowuje to token HF do gated Llama-3.
- **Retargeting na rigi użytkownika nietestowany lokalnie** — brak
  checkpointu `boneclassifier_best.pt`. Kod istnieje, ścieżka kanoniczna
  działa.
- **Jakość ruchu nie była oceniana.** Mierzyliśmy zgodność z
  constraintami, czasy i poprawność protokołu — nie „czy wygląda dobrze".
  Żadne zdanie w tym dokumencie nie orzeka, który model generuje ładniej.
- **MotionBricks i HIL nieuruchamiane** — zero pomiarów własnych; ich
  wiersze opierają się na kodzie i deklaracjach.
- **fps MotionBricks niesprawdzony** — czytany z konfiguracji w runtime,
  a modelu nie uruchamialiśmy.
- Rozbieżność u MotionBricks: strona reklamuje style (injured, zombie,
  skipping…), a kod wymienia **cztery** tryby [K]. Nierozstrzygnięte.

## 10. Źródła szczegółów

| Dokument | Co zawiera |
|---|---|
| `BACKBONES.md` (repo serwera) | architektura wieloserwerowa, kontrakt backendu, pułapki ARDY |
| `PLAN-ardy-mmcp.md` | plan integracji ARDY (zrealizowany) |
| `PLAN-motionbricks-mmcp.md` | badanie MotionBricks + plan warunkowy |
| `PLAN-hil-mmcp.md` | badanie HIL + uzasadnienie NO-GO + załącznik implementacyjny |
| `RAPORT-nocny-2026-08-06.md` | metodyka i pełne wyniki pomiarów |
| `DEVLOG-local-windows.md` (repo serwera) | środowisko lokalne i pułapki uruchomieniowe |
