# Plan: dodanie MotionBricks (NVIDIA) jako trzeciego backendu MMCP

**Cel:** serwować MotionBricks obok `kimodo-soma-rp` i `ardy-core-rp`, tak
by dało się go wybrać z dropdownu w Proscenium i wygenerować ruch.

---

## 0. Ustalenia z kodu i dokumentacji (fakty, nie założenia)

Zbadane repo: `NVlabs/GR00T-WholeBodyControl`, podkatalog `motionbricks/`
(sparse clone w `C:\_CODE\motionbricks-src`, stan na 2026-08-06).

1. **To jest robot, nie postać.** Jedyny szkielet w kodzie to
   `G1Skeleton34` (`motionlib/core/skeletons/g1.py`) — Unitree G1, 34 stawy
   (32 aktywne + 2 atrapy palców do detekcji kontaktu). Katalog
   `skeletons/` zawiera wyłącznie `base.py` i `g1.py`. Brak SMPL/SOMA/
   jakiegokolwiek szkieletu ludzkiego.
2. **Zapowiadane „pełne wydanie" nie wyszło.** Strona projektu mówi
   „preview release, full release ~miesiąc". Katalog `motionbricks/` ma
   ostatni commit **2026-04-30**, mimo że repo nadrzędne jest aktywne
   (commity z dziś). Od zapowiedzi minęły >3 miesiące.
3. **Brak jakiegokolwiek warunkowania tekstem.** `generate_new_frames`
   (`motion_backbone/demo/full_agent.py:109`) przyjmuje:
   `mode` ∈ **{walk, run, idle, slow_walk}** (int), `movement_direction`
   i `facing_direction` (wektory 3D w układzie MuJoCo), kontekst
   (`context_mujoco_qpos` albo pozycje/rotacje stawów) i opcjonalnie
   `specific_target_positions` / `specific_target_headings` (to są
   „proxy keyframes"). Prompt tekstowy nie istnieje w API.
   *Uwaga:* strona reklamuje style (injured, zombie, skipping, strafing,
   crouching), a docstring kodu wymienia tylko 4 tryby — rozbieżność do
   zweryfikowania (krok S0).
4. **API jest strumieniowe i stanowe**, nie wsadowe: agent trzyma bufor
   klatek, `generate_new_frames()` dogenerowuje okno, `get_next_frame()`
   wydaje kolejną klatkę. Istnieje ścieżka headless (demo bez viewera),
   ale wymaga `mj_model`/`mj_data` MuJoCo.
5. **Wyjście:** `mujoco_qpos` `[B, T, 36]` = translacja roota (3) +
   kwaternion roota wxyz (4) + **29 kątów zawiasów** (1 DOF każdy), oraz
   `model_features` (418 dim, zawierają globalne rotacje stawów).
   MMCP potrzebuje kwaternionów **lokalnych względem rodzica** dla 34
   stawów — konwersja jest do napisania (krok S4).
6. **Układ współrzędnych ruchu: Y-up, Z-forward, prawoskrętny** — czyli
   dokładnie MMCP `right_handed_y_up`. MuJoCo jest Z-up/X-forward, a repo
   ma gotowy `mujoco_qpos_converter`. To zdejmuje największe ryzyko
   „obróconej postaci".
7. **Szkielet ma ten sam interfejs co kimodo/ardy**: `SkeletonBase`
   (`skeletons/base.py`) wystawia `bone_order_names_with_parents`,
   `neutral_joints`, `joint_parents`, `bone_index`. Wspólny rodowód →
   **`kimodo.mmcp.capabilities.skeleton_to_mmcp()` powinno zadziałać
   wprost** (do potwierdzenia w S0).
8. **Checkpointy leżą w repo przez Git LFS** (`out/`): vqvae 273 MB,
   pose 1.6 GB, root 391 MB, `G1-clip.ckpt`. Brak repozytorium na HF.
   Licencja: kod Apache-2.0, wagi **NVIDIA Open Model License**.
9. **Zależności:** Python 3.10+, CUDA, **MuJoCo**, Git LFS.

## 1. Wniosek strategiczny i rekomendacja

MotionBricks **nie jest dziś modelem do animacji postaci** — jest
sterownikiem lokomocji robota G1 z API czasu rzeczywistego. Proscenium to
addon do animacji humanoidalnych postaci; w planie ARDY świadomie
odrzuciliśmy wariant G1 argumentem „addon jest humanoidalny". Ten sam
argument stosuje się tu do **całego** modelu.

**Rekomendacja: zrobić, ale w wąskim, uczciwie nazwanym zakresie** —
backend „robot navigation", sterowany **ścieżką roota**, bez tekstu, bez
retargetingu, jako `motionbricks-g1`. Uzasadnienie:

- Mapowanie `root_path` → `movement_direction`/`facing_direction` jest
  naturalne: to dokładnie to, co robi ich kontroler nawigacyjny. Dostajemy
  realną funkcję (previz ruchu robota po narysowanej ścieżce w Blenderze),
  a nie atrapę.
- Ekosystem ma już „pas robotyczny" (`kimodo-g1-rp`, warianty `g1` w ARDY),
  więc G1 nie jest ciałem obcym.
- Instalacja hydrauliki teraz = gdy wyjdzie pełne wydanie (jeśli przyniesie
  szkielet ludzki), zostaje podmiana szkieletu i włączenie tekstu.

**Alternatywa, którą odrzucam:** czekać na pełne wydanie. Odrzucam, bo
termin minął ponad 3 miesiące temu i nie ma sygnału, że nadejdzie; koszt
Fazy 1 jest umiarkowany, a hydraulika i tak będzie potrzebna.

### Pytanie produktowe, na które musisz odpowiedzieć przed startem

Rekomendacja „zrobić" jest **warunkowa** i uczciwość wymaga to nazwać:
dla dzisiejszego użytkownika Proscenium — animatora postaci — wartość
tej integracji jest **bliska zeru**, bo dostanie robota, nie postać.
Praca ma sens tylko wtedy, gdy prawdziwa jest przynajmniej jedna z tez:

1. **Jest odbiorca na ruch robota** (previz G1, robotyka, demo dla
   NVIDII/klienta) — wtedy Faza 1 dowozi realną funkcję.
2. **Traktujemy to jako inwestycję w hydraulikę** pod pełne wydanie
   MotionBricks — wtedy świadomie płacimy dziś za gotowość jutro,
   akceptując, że pełne wydanie może nie przyjść wcale.

Jeśli obie są fałszywe, poprawną decyzją jest **nie robić** — i wtedy
warto wyjąć z planu sam krok K1, bo jest wartościowy niezależnie.

**Bramka techniczna (przed S1):** jeżeli po kroku S0 okaże się, że
(a) checkpoint nie ładuje się bez pełnego środowiska GR00T, albo
(b) headless bez viewera nie działa, albo
(c) `skeleton_to_mmcp` nie przechodzi na G1Skeleton34 —
zatrzymać się i wrócić z rekomendacją „czekamy na pełne wydanie".
Koszt bramki: ~pół dnia. Bez niej ryzykujemy tygodniem pracy w ślepą uliczkę.

## 2. Decyzje projektowe

| # | Decyzja | Wybór | Uzasadnienie |
|---|---------|-------|--------------|
| D1 | Gdzie | Trzeci backend w `motionmcp-kimodo-cloud` | `BACKBONES.md` opisuje dokładnie tę ścieżkę; rejestr, `MMCP_MODELS`, `/health` gotowe |
| D2 | Id na drucie | `motionbricks-g1` | Nazwa mówi wprost, że to G1; miejsce na `motionbricks-<skel>` po pełnym wydaniu |
| D3 | Retargeting | **`supports_retargeting=False`** | Retarget rigu ludzkiego na 34-stawowego robota z zawiasami jest semantycznie bez sensu, a boneclassifier oczekuje humanoidalnych efektorów. SDK sam odrzuci niezgodny szkielet (`retargeting_unsupported`) |
| D4 | Segmenty | `["unconditioned"]` + `"text"` jako **tokeny stylu** | Model nie ma enkodera tekstu. Prompt mapujemy słowem kluczowym na `mode` i **dokumentujemy, że to nie jest język naturalny** |
| D5 | Kto ustala `mode` | **Tekst wygrywa, prędkość jest fallbackiem** | Dwa mechanizmy piszą do tego samego pola. Reguła: jeśli prompt segmentu zawiera nazwę trybu — użyj jej; w przeciwnym razie wyprowadź z prędkości implikowanej przez ścieżkę. Bez tej reguły implementacja jest niedookreślona |
| D6 | Constrainty Fazy 1 | tylko `root_path` | Jedyne mapowanie 1:1 na API. `pose_keyframe`/`effector_target` → Faza 2 (przez `specific_target_positions`) |
| D7 | Request bez `root_path` | Idle w miejscu przez `duration_frames` | Schemat MMCP dopuszcza request z samym segmentem. Bez ścieżki nie ma kierunku ruchu → `mode=idle`, zerowy `movement_direction`, facing z pozy startowej. Alternatywa („idź prosto") byłaby zgadywaniem intencji |
| D8 | `limits.max_duration_seconds` | **20 s** (wstępnie, potwierdzić w S0) | Limit jest praktyczny (czas generacji i dryf), nie architektoniczny — model nie ma okna treningowego jak ARDY. Wartość ustalić pomiarem w S0 i wpisać zmierzoną |
| D9 | Źródło pakietu | Prywatny fork **tylko jeśli potrzebne patche** | Wzorzec z ardy; najpierw próbujemy kompozycji bez forka (S3) |
| D10 | Klasa bazowa | `RetargetingCloudBackbone` z jedną poprawką | Reuse pipeline'u; poprawka: etap 0 (T-pose) ma się nie odpalać, gdy backend nie wspiera retargetingu (S2) |

## 3. Zmiany serwerowe

### S0 — bramka wykonalności (BLOKUJĄCA, ~0,5 dnia)
Skrypt jednorazowy w scratchpadzie, bez dotykania repo:
1. `git lfs pull` checkpointów w `C:\_CODE\motionbricks-src\motionbricks`
   (~2,3 GB); `pip install -e motionbricks` w osobnym venv (**nie** w venv
   serwera — patrz R1).
2. Załadować agenta headless (bez `mujoco.viewer`), wygenerować ~100 klatek
   przy stałym `movement_direction`/`facing_direction`, zapisać `qpos`.
3. Sprawdzić: `motion_rep.fps` (spodziewane 30), **rzeczywistą listę
   trybów** (docstring mówi 4, strona reklamuje więcej), czy
   `skeleton_to_mmcp(G1Skeleton34())` zwraca poprawny dict.
4. Zweryfikować konwersję wyjścia (S4) na jednej klatce: qpos → globalne
   rotacje → lokalne kwaterniony → FK z powrotem, błąd pozycji < 1e-3 m.
5. **Determinizm:** wygenerować 2× z tym samym `manual_seed` i porównać
   bitowo. Wynik decyduje, czy `supports_segment_seed`/powtarzalność w
   ogóle deklarujemy (nie zgadywać w `_build_spec`).
6. **Czas generacji** dla 10 s ruchu — model reklamuje 15 000 FPS, więc
   spodziewamy się ułamka sekundy. Pomiar ustala realne
   `max_duration_seconds` (D7b) i wyłapie sytuację, w której narzut
   replanowania dominuje.

**Weryfikacja:** działający NPZ/JSON z 100 klatkami + wypisany fps, tryby,
determinizm, czas i szkielet w formacie MMCP. Bez tego dalej nie idziemy.

### S1 — środowisko i zależność
- Domyślnie: **extra w tym samym venv** — `motionbricks = ["motionbricks @ …"]`
  w `pyproject.toml`, obok `ardy`. Osobny proces wchodzi w grę dopiero,
  gdy poniższy krok pokaże konflikt nie do pogodzenia (R1).
- **Konflikt do rozstrzygnięcia:** MotionBricks ciągnie MuJoCo, PyTorch
  Lightning i własne piny; kimodo+ardy mają już napięty zestaw
  (`transformers==5.8.1`, `numpy>=2`). Rozstrzygnięcie: `uv lock --extra
  motionbricks` **na sucho** (bez `sync`) i przegląd rezolucji.
  Kryterium przejścia: lock rozwiązuje się bez zmiany wersji `torch`,
  `numpy` i `transformers` używanych dziś przez kimodo/ardy. Zmiana
  którejkolwiek = ścieżka „osobny proces", bo oznacza rewalidację obu
  działających modeli.
- Pakiet leży w **podkatalogu monorepo**, więc dep ma postać
  `motionbricks @ git+https://github.com/NVlabs/GR00T-WholeBodyControl#subdirectory=motionbricks`
  (przypiąć commit — repo nadrzędne jest aktywne i nie chcemy, by
  niepowiązany merge zmienił nam wersję pod ręką).
- Checkpointy: nie wchodzą przez pip (LFS w repo) → env
  `MOTIONBRICKS_CKPT_DIR` wskazujący katalog `out/`.

**Weryfikacja:** `uv run python -c "import kimodo, ardy, motionbricks"` w
jednym venv, albo świadoma decyzja o rozdzieleniu procesów (R1).

### S2 — poprawka klasy bazowej (mała, z regresją)
W `cloud_backbone.generate()` etap 0 (normalizacja T-pose) odpala się
bezwarunkowo, a służy wyłącznie klasyfikatorowi kości przy retargetingu.
Dla backendu z `supports_retargeting=False` jest w najlepszym razie
bezużyteczny, w najgorszym przekrzywi szkielet G1, jeśli któraś nazwa
stawu przypadkiem wpadnie w heurystykę A-pose.
- Zmiana: policzyć `needs_retarget` **przed** etapem 0 i pominąć
  normalizację, gdy retargetu nie ma.
- **Przy okazji zamyka utajony błąd.** Inwersja normalizacji
  (`cloud_backbone.py`, etap 5) jest zagnieżdżona wewnątrz
  `if needs_retarget:`, a sama normalizacja odpala się bezwarunkowo.
  Zarazem `_request_matches_canonical` porównuje wyłącznie **nazwy i
  kolejność** stawów, nie pozę spoczynkową. Klient, który wyśle kanoniczne
  nazwy z rigiem w A-pose, dostaje więc transformację **jednostronną** —
  znormalizowaną w tę stronę i nieodwróconą z powrotem. Dziś jest to
  nieosiągalne tylko dlatego, że addon kopiuje `canonical_skeleton`
  dosłownie. Poprawka usuwa tę pułapkę.
- **Ryzyko regresji na kimodo/ardy: znikome** — dla szkieletów
  kanonicznych (T-pose) `tpose_q` jest puste, więc etap 0 i tak nic nie
  robi; ścieżka z retargetem zostaje bez zmian. Wymaga potwierdzenia
  goldenem — patrz weryfikacja.

**Weryfikacja:** golden `/generate` (stały seed, dummy encoder) dla kimodo
i ardy, przed i po — **bitowo identyczny** dla żądania na szkielecie
kanonicznym; dla żądania z retargetem (rig Mixamo) również bez zmian.

### S3 — `motionbricks_backbone.py`
`MotionBricksCloudBackbone(RetargetingCloudBackbone)`:
- `_load()`: zbudować agenta headless raz, na `MOTIONBRICKS_CKPT_DIR`.
  **Uwaga na stanowość:** agent trzyma bufor i pozycję w świecie, więc
  `_run_model` musi go resetować (`full_agent.reset()`) na starcie każdego
  requestu — inaczej drugi request zacznie tam, gdzie skończył pierwszy.
  **Konsekwencja dla współbieżności:** jeden agent na proces + reset
  oznacza, że dwa równoległe requesty pomieszają sobie bufory. Kimodo i
  ARDY są bezstanowe, więc ten problem u nich nie istnieje. Rozwiązanie
  Fazy 1: `threading.Lock` wokół `_run_model` (serializacja żądań na ten
  model) — prostsze i wystarczające przy pojedynczym GPU. Agent per request
  odpada: ładowanie stanu jest kosztowne.
- `_build_spec()`: `id="motionbricks-g1"`, `fps` z `motion_rep.fps`,
  `canonical_skeleton=skeleton_to_mmcp(G1Skeleton34)`,
  `supports_retargeting=False`, `supported_segments=["unconditioned"]`
  (+`"text"` wg D4), `supported_constraints=["root_path"]`,
  `predicted_contact_joints`: **pusta lista** w Fazie 1 —
  `_resolve_foot_contact_joints` ma tabelę kandydatów humanoidalnych i nie
  zna nazw G1 (`left_ankle_roll_skel`, `left_toe_base`); pusta lista to
  poprawny sygnał „ten model nie emituje kontaktów", a foot-lock po stronie
  klienta i tak nie dotyczy robota. Rozszerzenie tabeli — Faza 2.
- `_run_model()`: pętla „replan" — dla każdego kroku sterowania wyliczyć
  z `root_path` kierunek ruchu i heading, wywołać `generate_new_frames`,
  zebrać klatki aż do `num_frames`. Trzy konkrety implementacyjne:
  - `generate_new_frames` ma **własną heurystykę częstotliwości
    replanowania** i potrafi zwrócić bufor bez generowania
    (`if self._current_frame_idx < controller_dt * self._fps: return`).
    W trybie wsadowym krok kontrolujemy sami → `force_generation=True`.
  - kontekst podajemy jako `context_mujoco_qpos` (kod komentuje
    „should always use this if possible"), nie jako cechy ruchu.
  - pętla musi mieć **twardy limit iteracji** niezależny od `num_frames`:
    gdyby model zwrócił zero nowych klatek, inaczej dostajemy pętlę
    nieskończoną w wątku obsługującym request.
- `skeleton`/`output_skeleton`/`fps` jako property.
- Wyjątki: `ProtocolError`, mapowanie OOM jak w pozostałych backendach.

### S4 — `motionbricks_translate.py`
Dwie strony konwersji, obie nietrywialne:

**Wejście (MMCP → sygnały sterujące).** `root_path` daje punkty XZ w
klatkach + opcjonalny heading. Model chce, **co krok replanowania**,
kierunku ruchu i facingu w układzie MuJoCo:
- próbkować ścieżkę co `controller_dt`,
- `movement_direction` = znormalizowany wektor do następnego punktu,
- `facing_direction` = z `heading_radians`, a gdy go brak — styczna ścieżki
  (ta sama konwencja co w addonie),
- `mode` wyprowadzić z **implikowanej prędkości** (dystans/czas): idle /
  slow_walk / walk / run — to uczciwe i nie wymaga tekstu. **Progów nie
  zgadywać:** w S0 wygenerować po klipie w każdym trybie przy stałym
  kierunku, zmierzyć uzyskaną prędkość roota i ustawić progi na środkach
  między zmierzonymi wartościami. Inaczej ścieżka narysowana „na spacer"
  wyjdzie biegiem albo odwrotnie,
- konwersja MMCP (Y-up, Z-fwd) → MuJoCo (Z-up, X-fwd) wg tabeli z
  `docs/motion_representation.md` (MotionX=MjY, MotionY=MjZ, MotionZ=MjX).

**Wyjście (qpos → MMCP).** Ścieżka podstawowa: z `model_features`
(zawierają globalne rotacje stawów) → rotacje lokalne przez `joint_parents`
→ kwaterniony `(x,y,z,w)`. Ścieżka zapasowa: `mj_forward` na każdej klatce
i odczyt `xquat` ciał. Wybór po pomiarze błędu w S0.

### S5 — rejestracja i konfiguracja
- Dopisać backend do `_enabled_backbones()` z tym samym gracefulnym
  `ImportError` co ARDY (brak pakietu → serwuj resztę).
- Env: `MOTIONBRICKS_CKPT_DIR`, wpis w `MMCP_MODELS`.
- `BACKBONES.md`: wiersz w tabeli modeli + sekcja „MotionBricks specifics"
  (stanowość agenta, brak tekstu, brak retargetingu, replan loop).

### S6 — deployment
Dockerfile/modal: MuJoCo (headless — `MUJOCO_GL=egl`), Git LFS dla
checkpointów, opcjonalny vendored `motionbricks_src/`. Analogicznie do ARDY.

### Faza 2 (osobne zadania, po działającej Fazie 1)
Zbiorczo to, co plan wielokrotnie odsyła „na później":
- `pose_keyframe` / `effector_target` przez `specific_target_positions`
  + `specific_target_headings` (proxy keyframes, `BYPASS_SPRING_MODEL`).
- Rozszerzenie `FOOT_CONTACT_CANDIDATES` o nazwy G1 → foot contacts na drucie.
- Pełna lista trybów, jeśli S0 wykaże więcej niż 4 (style ze strony
  projektu) — wtedy mapowanie tekst→styl staje się użyteczne.
- Podmiana szkieletu na ludzki, gdy NVIDIA wyda pełną wersję (wtedy też
  rewizja D3 — retargeting może nabrać sensu).

## 4. Zmiany klienckie (Proscenium)

### K1 — bramkowanie UI constraintów po capabilities (**realna luka**)
Dziś `supported_constraints` jest używane wyłącznie jako etykieta w
preferencjach (`properties.py:465`), a lista stawów do pinowania to
**zahardkodowana** czwórka SOMA (`constants.END_EFFECTOR_JOINTS`,
`constraints_ui.py:413`). Przy modelu G1 użytkownik dostanie do wyboru
stawy, których ten szkielet nie ma → request odrzucony przez serwer
(`unknown_joint`).
- Ukrywać przyciski dodania pinu / ścieżki, gdy model nie deklaruje
  danego typu w `supported_constraints`.
- Listę stawów efektorów wyprowadzić z capabilities; utrzymać obecną
  czwórkę jako domyślną, gdy model nic nie deklaruje (kompatybilność).
- **Constrainty osierocone przy przełączeniu modelu.** Scena może już
  zawierać piny utworzone dla poprzedniego modelu. Po przełączeniu na
  model, który ich nie wspiera, `request_builder` musi je pominąć
  z ostrzeżeniem w panelu — dziś wysłałby je i dostał `unsupported_constraint`
  albo `unknown_joint` dopiero z serwera, po opłaceniu round-tripu.
- **Ten krok ma wartość niezależnie od MotionBricks** — jest poprawką
  spójności klienta z protokołem i można go wdrożyć nawet przy decyzji
  NO-GO na bramce.

### K2 — dokumentacja
`docs/limitations.md`: model robota, sterowany wyłącznie ścieżką, bez
promptów, bez siatki ciała (bramka K1 z poprzedniego planu załatwia to
automatycznie — `"soma" not in "motionbricks-g1"`).

### Poza zakresem klienta
Nic więcej. Import szkieletu kanonicznego, bake i NLA są szkieletowo-
agnostyczne i zadziałają na 34 kościach G1 bez zmian.

## 5. Kolejność i zależności

```
S0 (bramka wykonalności) ──► decyzja GO/NO-GO
        │ GO
        ├─► S1 (środowisko) ──► S3+S4 (backend + translacja) ──► S5 ──► E2E
        └─► S2 (poprawka bazy + golden) ─────────────────────────┘
K1 (bramkowanie UI) — niezależne, wartościowe samo w sobie, można równolegle
K2, S6 — po działającym E2E
```

## 6. Weryfikacja końcowa (E2E)

1. `/capabilities` pokazuje 3 modele; `motionbricks-g1` ma 34 stawy,
   `supports_retargeting=false`, `supported_constraints=["root_path"]`.
2. Request z **niekanonicznym** szkieletem → `retargeting_unsupported`
   (czysty błąd, nie 500).
3. Blender: import szkieletu G1 (34 kości, **bez** siatki ciała),
   narysowana ścieżka L, Generate → Accept; robot idzie po ścieżce.
   Zmierzyć odchyłkę od pinów tym samym testem co dla kimodo/ardy.
   **Uwaga organizacyjna:** `n5_path_adherence.py` żyje dziś w
   scratchpadzie sesji (efemeryczny). Przenieść go do repo serwera jako
   `scripts/path_adherence.py` z parametrem `--model`, zanim posłuży za
   miarę porównawczą trzech modeli.
4. Dwa requesty pod rząd → drugi zaczyna od pozycji startowej, nie od
   końca pierwszego (test stanowości agenta). Dodatkowo **dwa requesty
   równolegle** — jeśli agent jest jeden na proces, trzeba go objąć
   blokadą albo tworzyć per request; inaczej równoległe żądania zmieszają
   sobie bufory.
5. Powtarzalność wg wyniku S0 punkt 5 — deklaracja w `_build_spec` ma
   odpowiadać zmierzonemu zachowaniu, nie życzeniom.
6. Regresja kimodo i ardy po S2 — golden bitowo identyczny.
7. Playblast porównawczy: trzy modele na tej samej ścieżce.

## 7. Ryzyka

| Ryzyko | Prawdop. | Skutek | Mitygacja |
|--------|----------|--------|-----------|
| **R1** Konflikt zależności (MuJoCo/Lightning vs kimodo+ardy) | **wysokie** | Nie da się trzymać 3 modeli w jednym venv | S1 rozstrzyga na sucho wg kryterium przejścia. Plan B: osobny proces MMCP na innym porcie. **Uczciwie o koszcie:** addon łączy się z jednym URL-em naraz, więc przy rozdzieleniu użytkownik nie zobaczy trzech modeli w jednym dropdownie — musiałby przełączać adres serwera w preferencjach. To wyraźna regresja UX i argument, by najpierw poważnie powalczyć o wspólne środowisko |
| **R2** Konwersja qpos → lokalne kwaterniony błędna (skręcone kończyny) | średnie | Bezużyteczny wynik | S0 punkt 4: round-trip FK z progiem błędu, zanim powstanie backend |
| **R3** Stanowość agenta przecieka między requestami | średnie | Drugi request startuje w złym miejscu | Reset w `_run_model`; test E2E #4 |
| **R4** Model nie umie „iść dokładnie tędy" (sterowanie prędkością, nie pozycją; jest model sprężynowy) | **wysokie** | Odchyłka od ścieżki dużo większa niż 4 cm u kimodo/ardy | Zmierzyć w E2E #3. **Reguła decyzyjna:** < 0,25 m — akceptujemy i dokumentujemy realną dokładność; 0,25–1,0 m — próbujemy `specific_target_positions` z `BYPASS_SPRING_MODEL`; > 1,0 m — `root_path` nie jest uczciwym mapowaniem, wracamy do właściciela produktu z rekomendacją zawężenia do sterowania kierunkiem/stylem bez obietnicy trafiania w ścieżkę |
| **R5** Preview zniknie/zmieni API przy pełnym wydaniu | średnie | Przepisanie translacji | Trzymać całą wiedzę o modelu w `motionbricks_translate.py`; nie rozlewać po backendzie |
| **R6** Licencja wag (NVIDIA Open Model License) w produkcji chmurowej | niskie | Blokada wdrożenia | Przeczytać licencję przed S6; odnotować w ATTRIBUTIONS |
| **R7** Wartość dla dzisiejszego użytkownika Proscenium bliska zeru (to robot) | pewne | Praca bez odbiorcy | **Nie jest to ryzyko techniczne, tylko warunek startu** — rozstrzygany pytaniem produktowym w §1, przed jakąkolwiek pracą. Zostawiony w tabeli, żeby nie zginął |

## 8. Czego NIE robimy

- Nie udajemy, że MotionBricks rozumie prompty. Jeśli mapujemy tekst, to
  wyłącznie na skończony zbiór trybów i tak to nazywamy w dokumentacji.
- Nie włączamy retargetingu na rigi użytkownika (D3).
- Nie ruszamy `pose_keyframe`/`effector_target` w Fazie 1 (D5).
- Nie forkujemy repo NVIDII, dopóki kompozycja wystarcza (D6).
- Nie dokładamy MotionBricks do obrazu produkcyjnego, zanim nie
  rozstrzygniemy R1 i R6.
