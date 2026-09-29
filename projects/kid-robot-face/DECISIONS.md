# DECISIONS — Kid-Robot-Face

## Decision Record Template

| Field | Description |
|-------|-------------|
| Decision ID | Unique identifier |
| Date | When decided |
| Subject | What is being decided |
| Status | PROPOSED / APPROVED / REJECTED / SUPERSEDED |
| Decision | The choice made |
| Reason | Why this choice |
| Alternatives Considered | Other options + why rejected |
| Impact | Who/what is affected |
| Affected Requirements | REQ-IDs impacted |
| Affected Tasks | TASK-IDs impacted |
| Affected Tests | TEST-IDs impacted |
| Owner | Who made the call |
| Follow-up | Any pending actions |

---

## DEC-001: Microcontroller Selection (ESP32-C3 vs ESP32 Classic)

**Date:** 2026-09-15  
**Status:** APPROVED  
**Subject:** Which ESP32 variant for the robot face  

### Decision
Use **ESP32-C3** instead of ESP32 Classic (C3 is the standard going forward).

### Reason
- **Size & Power:** ESP32-C3 is physically smaller (~50% area) and draws less power than Classic
- **Sufficient:** WiFi + BLE both present; no need for dual-core overhead
- **Modern:** C3 is actively maintained; Classic is legacy
- **Cost:** C3 slightly cheaper (~¥300 less)
- **Ecosystem:** Better documentation and examples for IoT projects

### Alternatives Considered
1. **ESP32 Classic** — REJECTED: Larger, more power, overkill for this application
2. **nRF52840** — REJECTED: Better BLE, but no WiFi; overkill complexity for this project
3. **STM32 + external WiFi module** — REJECTED: More PCB complexity, slower development

### Impact
- Smaller physical footprint → smaller enclosure
- Lower power draw → longer battery life
- Reduced feature set is acceptable (no need for dual Xtensa cores)

### Affected Requirements
- REQ-008: Microcontroller choice
- REQ-012: Low power (helps)
- REQ-014: Offline capable (WiFi sufficient)

### Affected Tasks
- TASK-020: BOM (different part number)
- TASK-025: Firmware environment (MicroPython on C3 has excellent support)
- TASK-040: Integration testing

### Affected Tests
- None (no test changes, just different hardware)

### Owner
Alireza Goudarzinemati (Architecture decision)

### Follow-up
- Verify MicroPython C3 build available (confirmed as of 2026-09)
- Confirm GPIO pin count sufficient (26 available, 8-10 needed; OK)

---

## DEC-002: OLED Display Size (0.96" vs 1.3")

**Date:** 2026-09-16  
**Status:** APPROVED  
**Subject:** OLED display resolution and physical size

### Decision
Use **0.96" SSD1306** (128×64 pixels) instead of 1.3" (128×64 or 240×136).

### Reason
- **Perfect for face:** 128×64 is enough pixels for 8×8 sprite grid; adds character with constraints
- **Size fit:** Fits on desk without taking up much space (child won't accidentally knock over a large display)
- **Cost:** ~¥800–¥1,200 vs ¥2,500+ for larger displays (stays within budget)
- **Power:** Lower current draw (0.96" ~40mA vs 1.3" ~100mA)
- **Simplicity:** SSD1306 is battle-tested; extremely mature ecosystem

### Alternatives Considered
1. **1.3" OLED** — REJECTED: Overkill for cute 8×8 faces; too large for small desk robot
2. **TFT LCD (1.8")** — REJECTED: Higher power, requires SPI driver optimization
3. **7-segment LED** — REJECTED: Not expressive enough for character

### Impact
- Pixel limitations force creative cute design (8×8 constraint is a feature, not a bug)
- Display sits comfortably on a desk near keyboard
- Hardware cost stays within budget
- Driver maturity = faster firmware development

### Affected Requirements
- REQ-005: OLED face display
- REQ-006: Cute character animations (8×8 forces cute aesthetic)
- REQ-011: Low latency (smaller display = faster pixel updates)

### Affected Tasks
- TASK-035: Face animation library (design is 8×8 grid)
- TASK-040: Integration (display update timing straightforward)

### Affected Tests
- TEST-DISPLAY-001: Verify 128×64 OLED working
- TEST-ANIMATION-001: Verify sprite rendering < 50ms

### Owner
Alireza Goudarzinemati (Hardware + UI decision)

### Follow-up
- Sprite design phase: start with reference cute robot faces (e.g., Marty the robot, Sphero)

---

## DEC-003: LLM Backend (Ollama on Home Server vs Cloud APIs)

**Date:** 2026-09-17  
**Status:** APPROVED  
**Subject:** Where the LLM inference runs

### Decision
Run **Ollama locally on home server** (192.168.0.x) instead of cloud APIs (OpenAI, Google, Claude).

### Reason
- **Privacy:** No external API calls; conversation stays on home network
- **Offline capable:** If internet drops, robot still works (Ollama is self-contained)
- **Cost:** No per-request billing; Ollama is free/open-source
- **Latency:** Same-network latency (~10–50ms) vs internet round-trip (200–500ms)
- **Control:** Can fine-tune model behavior without external platform constraints
- **No secrets:** No API keys to manage on ESP32

### Alternatives Considered
1. **OpenAI GPT-4 API** — REJECTED: High latency; recurring cost (~¥1–5/day); child data leaves home
2. **Google PaLM** — REJECTED: Same concerns as OpenAI
3. **Claude API** — REJECTED: Same concerns; Anthropic's terms require explicit data handling agreement for child use
4. **On-device Gemma (ESP32)** — REJECTED: Gemma 7B too large for 4MB ESP32 Flash; would need offline model compression (out of scope)

### Impact
- Requires home server with Ollama running 24/7 (power cost ~10–20W continuous)
- Setup slightly more complex (Ollama configuration, WiFi bridging)
- Home network is a dependency (scope: REQ-014 explicitly assumes local network)
- Model selection: Using Gemma (lightweight, fast) instead of LLaMA 2 (more capable, slower)

### Affected Requirements
- REQ-003: LLM chat response (fulfilled by Ollama)
- REQ-014: Offline capable (fully supported)
- REQ-015: Easy config via Web UI (FastAPI bridge provides this)

### Affected Tasks
- TASK-030: FastAPI bridge (must route to Ollama 11434)
- TASK-025: Firmware (must know home server IP + WiFi credentials)
- TASK-020: BOM (no API costs, but assumes home server exists)

### Affected Tests
- TEST-LLM-LATENCY: Measure end-to-end Whisper → Ollama → Piper time

### Owner
Alireza Goudarzinemati (Architecture + privacy decision)

### Follow-up
- Document home server setup (Ollama installation, model download, port exposure)
- Child content policy: decide what topics Ollama should refuse

---

## DEC-004: Speech-to-Text & Text-to-Speech (Whisper + Piper vs Commercial APIs)

**Date:** 2026-09-18  
**Status:** APPROVED  
**Subject:** How to handle speech input/output

### Decision
Use **Whisper (OpenAI open-source) for STT** and **Piper for TTS**, both running on home server, instead of cloud APIs.

### Reason
- **Open-source:** No vendor lock-in; can run offline
- **Maturity:** Whisper is battle-tested; Piper is Mozilla's proven TTS
- **Quality:** Whisper handles accents + background noise better than many lightweight models
- **Speed:** Piper TTS ~500ms for typical response; fast enough for < 1s latency budget
- **Cost:** Free; no per-request billing
- **Privacy:** Audio doesn't leave home network

### Alternatives Considered
1. **Google Cloud Speech-to-Text** — REJECTED: Cloud-dependent; latency; cost
2. **Amazon Polly** — REJECTED: Same concerns
3. **SpeechRecognition (Google offline)** — REJECTED: Less accurate than Whisper; smaller language support
4. **Lightweight on-device models (TinySpeak)** — REJECTED: Not mature enough; poor accuracy for child's voice + home noise

### Impact
- Home server must run Whisper + Piper (CPU-intensive; ~2–4 cores beneficial)
- Audio latency: Whisper ~1–2s on CPU (can be optimized with GPU or smaller model)
- Piper latency: ~300–500ms for typical 2–3 second response
- Model download: Whisper base ~140MB, Piper voice ~100MB (one-time)

### Affected Requirements
- REQ-002: Speech-to-text (Whisper)
- REQ-004: Text-to-speech (Piper)
- REQ-011: Zero latency < 1 sec (achievable with optimization)

### Affected Tasks
- TASK-030: FastAPI bridge (provide /transcribe + /synthesize endpoints)
- TASK-040: Integration (measure real-world latency with typical inputs)

### Affected Tests
- TEST-STT-ACCURACY: Whisper accuracy on child voice + background noise
- TEST-TTS-QUALITY: Piper voice naturalness + child comprehension

### Owner
Alireza Goudarzinemati (Technical decision)

### Follow-up
- Whisper model size: evaluate tiny vs base vs small (trade-off speed vs accuracy)
- Piper voice selection: test child-friendly voices
- Latency optimization: measure and document bottleneck (Whisper vs Piper vs network)

---

## DEC-005: Face Design Aesthetic (Cute 8×8 Sprites vs Realistic Rendering)

**Date:** 2026-09-19  
**Status:** APPROVED  
**Subject:** Visual style of robot face expressions

### Decision
Use **cute, playful 8×8 pixel sprite-based characters** instead of attempting realistic face rendering.

### Reason
- **Age-appropriate:** Cute sprites are more engaging for 5-year-old than realistic/uncanny valley faces
- **Technical fit:** 128×64 OLED naturally limits to small sprites; forces creativity within constraints
- **Memorable:** Simple character design (like Pac-Man, space invaders) is more iconic than realistic
- **Easier animation:** Fewer pixels = faster updates, less compute
- **Personality:** Allows for funny/exaggerated expressions (tongue out, dizzy eyes)
- **Safeguard:** Less realistic = less chance of disturbing or "scary" appearance

### Alternatives Considered
1. **Realistic face rendering** — REJECTED: 128×64 insufficient; would look blurry/creepy; not age-appropriate
2. **Analog face (physical eyeballs + servos)** — REJECTED: Mechanical complexity; noise; single point of failure
3. **Minimalist (3–4 pixel dots)** — REJECTED: Too simple; not expressive enough to convey emotion

### Impact
- Face animation library design (TASK-035) focuses on pixel-art sprites
- Personality: robot will have a distinct character (design decision during sprite creation)
- Easier for child to understand expressions (exaggeration is a feature)
- Potential merchandising value (cute character can be marketed)

### Affected Requirements
- REQ-006: Cute character animations (defined as 8×8 sprites)
- REQ-007: Emotion-driven reactions (expressions mapped to emotions)

### Affected Tasks
- TASK-035: Face animation library (sprite design phase critical)
- TASK-070: Documentation (include character design rationale)

### Affected Tests
- TEST-ANIMATION-QUALITY: Subjective: child finds the robot fun/not scary
- TEST-EMOTION-ACCURACY: Expressions correctly map to intended emotion

### Owner
Alireza Goudarzinemati (UX/design decision)

### Follow-up
- Sprite design reference: collect cute robot references (Marty, Pepper, Wall-E inspired)
- Accessibility: ensure expressions are clear enough for vision-impaired perception (if relevant)

---

## DEC-006: Firmware Language (MicroPython vs C/Arduino)

**Date:** 2026-09-20  
**Status:** APPROVED  
**Subject:** Which programming language for ESP32 firmware

### Decision
Use **MicroPython** instead of C/C++ (Arduino or esp-idf).

### Reason
- **Development speed:** MicroPython development cycle is ~10× faster (no compile step)
- **Debugging:** REPL (read-eval-print loop) makes troubleshooting instant
- **Readability:** Code is more readable than C for someone picking up the project later
- **Libraries:** Excellent MicroPython ecosystem for OLED, I2C, I2S, WiFi
- **Safety:** Python's memory management reduces pointer bugs (important for long-running robot)
- **Fit:** This isn't a performance-critical project; latency budget is comfortable

### Alternatives Considered
1. **C/Arduino** — REJECTED: Slower iteration; pointer debugging; overkill for this use case
2. **C/esp-idf** — REJECTED: Even steeper learning curve; necessary only for advanced features we don't need
3. **Rust/embedded-rs** — REJECTED: Overkill; learning curve excessive for a hobby project

### Impact
- Firmware is completely readable + maintainable by non-embedded developers
- MicroPython on ESP32-C3 has excellent support (official Espressif builds)
- Runtime overhead: acceptable (robot doesn't have hard real-time requirements)
- OTA updates: can be scripted easily in Python

### Affected Requirements
- REQ-009: MicroPython firmware (becomes mandatory choice)

### Affected Tasks
- TASK-025: Firmware skeleton (fast iteration expected)
- TASK-040: Integration (fewer low-level gotchas)

### Affected Tests
- TEST-MEMORY-LEAK: Important to verify MicroPython doesn't leak memory over long uptime

### Owner
Alireza Goudarzinemati (Technical + productivity decision)

### Follow-up
- Document MicroPython setup (environment, toolchain, REPL access)
- Performance profiling: measure actual vs expected latency with MicroPython overhead

---

## DEC-007: API Architecture (FastAPI Bridge vs Direct ESP32 APIs)

**Date:** 2026-09-21  
**Status:** APPROVED  
**Subject:** How ESP32 firmware communicates with Ollama/Whisper/Piper

### Decision
Use a **FastAPI bridge server** (localhost:8000) running on home server to proxy requests from ESP32 to Ollama/Whisper/Piper, instead of ESP32 calling external services directly.

### Reason
- **Separation of concerns:** ESP32 doesn't know about Ollama/Whisper/Piper details
- **Error handling:** Centralized timeout + retry logic (easier to debug)
- **Security:** ESP32 only needs to know one IP/port; API keys/details hidden on server
- **Testing:** FastAPI server can be tested independently without hardware
- **Flexibility:** Easy to add new backends (e.g., swap Whisper for AssemblyAI later)
- **Load balancing:** Future: could route multiple robots to same server

### Alternatives Considered
1. **Direct ESP32 → Ollama/Whisper** — REJECTED: ESP32 would need to handle all models; firmware bloat; harder to debug
2. **Lightweight proxy (nginx)** — REJECTED: Can't add custom logic (emotion mapping, error handling)
3. **MQTT pub/sub** — REJECTED: Overkill complexity for single-user device

### Impact
- Home server must run FastAPI server in addition to Ollama/Whisper/Piper
- Network dependency: if FastAPI bridge down, robot can't respond (acceptable given privacy trade-off)
- Latency: +10–20ms for FastAPI routing (acceptable)
- Development: can develop/test server code independently of ESP32

### Affected Requirements
- REQ-003: LLM chat response (via FastAPI)
- REQ-002: Speech-to-text (via FastAPI)
- REQ-004: Text-to-speech (via FastAPI)
- REQ-015: Easy config via Web UI (FastAPI provides admin panel)

### Affected Tasks
- TASK-030: FastAPI bridge (core task; can run in parallel with firmware)
- TASK-025: Firmware (simpler; just HTTP calls)
- TASK-040: Integration (test via HTTP, very straightforward)

### Affected Tests
- TEST-API-ENDPOINTS: All FastAPI endpoints tested with pytest
- TEST-ERROR-HANDLING: Timeouts, malformed requests, service down scenarios

### Owner
Alireza Goudarzinemati (Architecture decision)

### Follow-up
- FastAPI documentation: include OpenAPI schema for future API consumers
- Config file: make Ollama host/port configurable (support multiple home setups)

---

## Summary Table

| ID | Decision | Status | Impact |
|-----|----------|--------|--------|
| DEC-001 | ESP32-C3 microcontroller | APPROVED | Smaller, lower power, modern |
| DEC-002 | 0.96" OLED display | APPROVED | Perfect for cute sprites, budget-friendly |
| DEC-003 | Ollama home server (not cloud APIs) | APPROVED | Privacy, offline, no cost, latency savings |
| DEC-004 | Whisper + Piper (not commercial APIs) | APPROVED | Open-source, privacy, cost-free |
| DEC-005 | Cute 8×8 sprites (not realistic rendering) | APPROVED | Age-appropriate, memorable, technical fit |
| DEC-006 | MicroPython (not C/Arduino) | APPROVED | Faster development, maintainability |
| DEC-007 | FastAPI bridge (not direct ESP32 APIs) | APPROVED | Separation of concerns, easier testing |

---

**Last Updated:** 2026-09-21  
**All Decisions Status:** APPROVED (7/7)  
**Next Review:** After architecture finalization (TASK-010)
