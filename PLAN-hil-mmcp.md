# Plan: HIL (Hybrid Imitation Learning) jako backend MMCP

**Wniosek na wstępie: rekomenduję NIE budować.** Nie z powodu trudności,
lecz dlatego, że HIL nie jest modelem generującym ruch — jest polityką
sterowania w symulacji fizycznej. To inna kategoria narzędzia niż kimodo
i ARDY, a MMCP nie ma dla niej kontraktu. Poniżej fakty, uczciwa wycena
„gdybyś jednak chciał" i to, co proponuję zamiast.

---

## 0. Ustalenia (fakty z papieru i z kodu)

Zbadane: strona projektu, arXiv 2505.12619v2, repo
`jiashunwang/Hybrid-Motion-Imitation` (płytki klon w `C:\_CODE\hil-src`,
525 MB, stan na 2026-08-07; ostatni push 2026-07-27, 98 gwiazdek).

1. **HIL to polityka RL w symulacji fizycznej**, nie generator klipów.
   Papier („HIL: Hybrid Imitation Learning for Dynamic Athletic Control",
   TOG 2026, CMU/NVIDIA/SFU): symulacja w **Isaac Gym 120 Hz**, polityka
   **30 Hz**, wyjściem są **docelowe pozycje stawów sterowane PD**
   (`τ = kp·(a−q) − kd·q̇`), a nie animacja. Papier wprost: **„no
   kinematic motion generation component"**.
2. **Wejściem jest stan i cel, nie prompt.** Obserwacje: stan postaci,
   **chmura 60 najbliższych punktów sceny**, cel zadania (pozycja docelowa
   dla parkouru, wektory kierunku i facingu dla heading). Tekstu nie ma.
3. **Kod jest nieoficjalny.** Repo autora opisuje się dosłownie jako
   „unofficial implementation and extension … **not the official code
   release** for either paper". Oficjalnego wydania HIL nie ma.
   *Sprawdzone też pod kątem ślepej plamki:* to samo repo obsługuje
   drugi papier tego autora — **GfR (RSS 2026)** — ale oferuje dokładnie
   dwa zadania, oba na G1 (`exp:g1-29dof-wbt-hybrid-climb`,
   `…-hybrid-object`). Nic po stronie GfR nie zmienia wniosków.
4. **Brak wag dla HIL.** Repo nie ma żadnych releases. Dołączone są
   wyłącznie polityki bazowego frameworka: `loco/g1_29dof`,
   `loco/t1_29dof`, `wbt/g1_29dof_dancing` (ONNX). **Polityk HIL
   (climb / object) nie ma — trzeba je wytrenować samodzielnie, dwuetapowo,
   przy konfiguracji 8192 równoległych środowisk.**
5. **Tylko roboty.** `data/robots/` zawiera `g1` i `t1` (Unitree G1
   29-DoF, Booster T1). Postaci SMPL z papieru w kodzie nie ma.
6. **SMPL występuje wyłącznie w retargetingu — i w złą stronę.**
   Mapowania to `("smplh","g1")`, `("smplx","g1")`, `("smplh","t1")`,
   czyli **człowiek → robot** (przygotowanie danych treningowych).
   Nie ma ścieżki robot → postać ludzka.
7. **Brak eksportu ruchu.** W `eval_agent.py` „export" dotyczy
   **eksportu polityki do ONNX**, nie zapisu animacji. Wyjście jest
   wyłącznie symulacyjne (podgląd w Viser na `localhost:8012`).
8. **Zależności:** zbudowane na **Holosoma** (Amazon FAR), które celuje w
   kilka symulatorów (`setup_isaacsim.sh`, `setup_isaacgym.sh`,
   `setup_mujoco.sh`), plus CUDA. Licencja Apache-2.0.
   *Precyzyjnie o platformie:* README nie deklaruje systemu, ale **całe
   oprzyrządowanie to skrypty `.sh`**, a symulator użyty w papierze
   (Isaac **Gym**, w odróżnieniu od Isaac **Sim** z instalatora) jest
   wyłącznie linuksowy. Traktuję to jako „Linux zdecydowanie zalecany",
   nie jako twardy wymóg repo.

## 1. Dlaczego to nie pasuje do MMCP

MMCP ma jeden kontrakt: *szkielet + segmenty + constrainty → klip glTF*.
Serwer dostaje pełną specyfikację z góry i zwraca skończoną animację.

HIL ma kontrakt pętli zamkniętej: *stan + cel + geometria sceny → momenty
w stawach*, krok po kroku, wewnątrz symulatora. Żeby zamienić to na klip,
trzeba **przeprowadzić pełną symulację fizyczną**, co wymaga rzeczy,
których w protokole nie ma i nie da się udawać:

| Czego wymaga HIL | Czy MMCP to ma |
|---|---|
| Geometria sceny (skrzynia do wejścia, przeszkody) | **Nie.** Protokół nie ma prymitywu sceny ani kolizji. Cała wartość HIL to ruch świadomy sceny — bez tego zostaje zwykła lokomocja |
| Stan początkowy w symulatorze (pozycje, prędkości, kontakty) | Nie. MMCP daje pozy kluczowe, nie stan dynamiczny |
| Symulator w procesie serwera | Isaac Sim, wielogigabajtowy, linuksowy |
| Wytrenowana polityka dla zadania | **Nie istnieje** dla HIL (pkt 4) |

To nie jest luka do załatania adapterem — to inna klasa systemu.
Dla porównania: ARDY i MotionBricks przynajmniej **generują klatki**;
HIL generuje **sterowanie**.

## 2. Gdybyś jednak chciał — uczciwa wycena

Ścieżka minimalna „HIL jako backend MMCP" wymaga po kolei:

| Krok | Koszt / ryzyko |
|---|---|
| Postawić Isaac Sim + Holosoma | Dni. **Linux praktycznie obowiązkowy** — obecny serwer stoi na Windowsie z venv dzielonym przez kimodo i ARDY; Isaac Sim tam nie wejdzie. To od razu wymusza osobną maszynę/proces |
| Wytrenować polityki HIL (2 etapy, 8192 środowisk) | **To jest zadanie klastrowe.** Na jednej RTX 3090 liczone w dniach–tygodniach, o ile w ogóle zmieści się pamięciowo. Bez tego nie ma czego serwować |
| Zdefiniować scenę w requeście | **Wymaga rozszerzenia protokołu MMCP** o prymityw sceny — zmiana specyfikacji, nie implementacji |
| Rollout symulacji → klip | Nowy kod: sterowanie epizodem, zbieranie qpos, konwersja na kwaterniony lokalne (jak w planie MotionBricks) |
| Wynik | Animacja **robota G1**, nie postaci |

Szkic implementacyjny — **wyłącznie na wypadek, gdybyś odrzucił
rekomendację** — jest w Załączniku A na końcu. Nie jest rozwinięty
w harmonogram, bo rozstrzyga o nim krok pierwszy (trening), a nie
kolejność plików.

Suma: tygodnie pracy plus trening klastrowy, żeby dostać ruch robota,
którego protokół i tak nie potrafi opisać (brak sceny). Przy takim
rachunku „nie budować" nie jest ostrożnością — jest jedyną obroną
uzasadnioną liczbami.

## 3. Co proponuję zamiast — jeśli motywacją jest fizyka

Podejrzewam, że za prośbą stoi realna potrzeba: **ruch wiarygodny
fizycznie** (bez ślizgania stóp, z poprawnymi kontaktami i równowagą).
Jeśli tak, kolejność wartości jest odwrotna niż „dodajmy HIL":

1. **Domknąć to, co już mamy, a nie działa.** Retargeting na rigi
   użytkownika (Mixamo/Rigify) jest zaimplementowany, ale lokalnie
   martwy — brakuje checkpointu `boneclassifier_best.pt`. To jedna
   brakująca paczka dzieli nas od funkcji, która **już jest napisana**
   i dotyczy każdego użytkownika. Największy zwrot z najmniejszego
   nakładu w całym projekcie.
2. **Włączyć prawdziwy enkoder tekstu.** Prompty są dziś ignorowane
   (`TEXT_ENCODER_MODE=dummy`) — token HF do Llama-3 odblokowuje
   sterowanie tekstem w obu działających modelach.
3. **Fizyczny post-processing klipów** — jeżeli chodzi o jakość fizyki,
   właściwym kierunkiem jest *tracker* poprawiający wygenerowany klip,
   a nie polityka generująca ruch od zera. Ten sam papier używa
   „physics-based motion tracker" do czyszczenia referencji przed
   treningiem, a Holosoma ma polityki whole-body tracking. To jednak
   osobny projekt badawczy, nie integracja — i w dostępnym kodzie
   również dotyczy robotów.
4. **Poczekać na oficjalne wydanie HIL** i wrócić do tematu, gdy pojawi
   się kod z postacią SMPL i wagami.

## 4. Warunki, które zmieniłyby tę rekomendację

Wracamy do tematu, jeśli **wszystkie trzy** będą spełnione:

1. Ukaże się **oficjalne** wydanie kodu HIL,
2. z **wagami** dla zadań z papieru,
3. dla **postaci ludzkiej** (SMPL), nie tylko robota.

Dodatkowo, niezależnie od powyższych: MMCP musiałby zyskać prymityw
sceny — inaczej serwujemy HIL pozbawiony tego, co czyni go ciekawym.

## 5. Ryzyka samego podejścia „zróbmy to mimo wszystko"

| Ryzyko | Skutek |
|---|---|
| Trening nie zbiegnie się na jednej karcie (konfiguracja zakłada 8192 środowisk) | Tygodnie pracy bez wyniku; brak punktu odniesienia, bo nie ma wag referencyjnych do porównania |
| Nieoficjalna implementacja nie odtwarza wyników papieru | Nie da się odróżnić błędu integracji od różnicy implementacji — brak twardego oczekiwania |
| Isaac Sim wymusza rozdzielenie serwerów | Addon łączy się z jednym URL-em → użytkownik nie zobaczy wszystkich modeli w jednym dropdownie |
| Protokół bez sceny | Serwujemy model bez jego głównej zdolności; obiecujemy w capabilities coś, czego nie da się zamówić |

## 6. Jak sprawdzić moje twierdzenia

Rekomendacja „nie budować" jest mocna, więc podstawy mają być
weryfikowalne bez zaufania do mojego streszczenia:

```bash
cd C:\_CODE\hil-src

# (4) brak wag HIL — są tylko polityki bazowego Holosomy: loco + wbt dancing
find . -name "*.onnx" | sed 's|.*/models/||'
gh api repos/jiashunwang/Hybrid-Motion-Imitation/releases   # pusta lista

# (5) tylko roboty, brak postaci ludzkiej
ls src/holosoma/holosoma/data/robots            # g1, t1

# (6) retargeting wyłącznie człowiek → robot
grep -n '"smpl' src/holosoma_retargeting/holosoma_retargeting/config_types/data_type.py

# (7) "export" w ewaluacji dotyczy polityki ONNX, nie animacji
grep -n "export" src/holosoma/holosoma/eval_agent.py
```

Twierdzenia z papieru (symulacja 120 Hz, polityka 30 Hz, brak komponentu
kinematycznego, chmura 60 punktów sceny) pochodzą z arXiv 2505.12619v2,
sekcje o reprezentacji i o zadaniach.

**Porządki:** klon `C:\_CODE\hil-src` zajmuje 525 MB i po decyzji NO-GO
można go skasować — nic w naszych repozytoriach na niego nie wskazuje.

## Załącznik A — szkic implementacji, gdyby decyzja była „budujemy"

Umieszczony na końcu celowo: rekomendacja brzmi odwrotnie, ale plan ma
być kompletny, gdybyś ją odrzucił.

**Warunek wstępny (bramka, rozstrzyga o całości):** wytrenować politykę
HIL dla jednego zadania (`hybrid-climb` albo `hybrid-object`) i pokazać,
że robot wykonuje zadanie w symulatorze. Dopóki to nie zadziała, reszta
nie ma czego serwować. Kryterium: powtarzalne wejście na skrzynię
w `eval_agent`. Bez maszyny linuksowej z Isaac Sim i budżetem GPU
na dwuetapowy trening — nie zaczynać.

**Pliki i kroki po przejściu bramki** (wzorowane na integracji ARDY,
której architekturę opisuje `BACKBONES.md` w repo serwera):

1. `pyproject.toml` — extra `hil` (Holosoma + Isaac). **Prawie na pewno
   nie zmieści się w venv z kimodo/ardy** → osobny proces i osobny port,
   z konsekwencją UX: addon widzi jeden serwer naraz.
2. `hil_backbone.py` — podklasa `RetargetingCloudBackbone`;
   `supports_retargeting=False`, `supported_segments=["unconditioned"]`,
   `supported_constraints=["root_path"]` (heading/goal to jedyne, co
   protokół potrafi wyrazić), szkielet kanoniczny G1 przez
   `skeleton_to_mmcp`.
3. `hil_translate.py` — `root_path` → cel/heading polityki; rollout
   epizodu w symulatorze; qpos → kwaterniony lokalne (identyczny problem
   konwersji jak w `PLAN-motionbricks-mmcp.md` §S4 — tam opisany
   dokładniej wraz ze ścieżką zapasową przez `mj_forward`).
4. `mmcp_server.py` — rejestracja w `_enabled_backbones()` z gracefulnym
   `ImportError`, jak dla ARDY.
5. Klient: **żadnych zmian poza K1 z planu MotionBricks** (bramkowanie UI
   constraintów po capabilities) — import szkieletu i bake są
   szkieletowo-agnostyczne.

**Weryfikacja:** `/capabilities` z trzema modelami; request z
niekanonicznym szkieletem → `retargeting_unsupported`; w Blenderze
import szkieletu G1 i generacja po ścieżce; dwa requesty pod rząd
startujące z tego samego stanu (symulacja jest stanowa — ten sam problem
co w MotionBricks). Odchyłkę od ścieżki mierzyć tym samym skryptem
porównawczym co dla kimodo/ardy.

**Czego ten szkic i tak nie rozwiązuje:** braku prymitywu sceny w MMCP.
Bez niego serwujemy HIL bez zdolności, dla której powstał — i to jest
najmocniejszy argument z §1, niezależny od kosztu implementacji.
