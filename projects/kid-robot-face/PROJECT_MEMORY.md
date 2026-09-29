# PROJECT_MEMORY — Kid-Robot-Face

## Project Details

**Project Name:** Kid-Robot-Face  
**Project ID:** PROJECT-KID-ROBOT-001  
**Version:** 0.1.0  
**Current Phase:** REQUIREMENTS → ARCHITECTURE  
**Owner:** Alireza Goudarzinemati  
**Target User:** 5-year-old son  

## Goal

Build a voice-reactive robot face that listens, understands, and responds with expressive character animations. The system should be engaging, fun, and safe for a young child.

## Key Requirements (Summary)

| REQ-ID | Title | Status |
|--------|-------|--------|
| REQ-001 | Voice Input Capture | APPROVED |
| REQ-002 | Speech-to-Text | APPROVED |
| REQ-003 | LLM Chat Response | APPROVED |
| REQ-004 | Text-to-Speech Output | APPROVED |
| REQ-005 | OLED Face Display (0.96") | APPROVED |
| REQ-006 | Cute Character Animations | APPROVED |
| REQ-007 | Emotion-Driven Reactions | APPROVED |
| REQ-008 | ESP32-C3 Microcontroller | APPROVED |
| REQ-009 | MicroPython Firmware | APPROVED |
| REQ-010 | Audio Out (Speaker) | APPROVED |
| REQ-011 | Zero Latency < 1 sec | APPROVED |
| REQ-012 | Low Power (Battery) | APPROVED |
| REQ-013 | Child-Safe Code | APPROVED |
| REQ-014 | Offline Capable | APPROVED |
| REQ-015 | Easy Config via Web UI | APPROVED |

## Current Architecture (Approved)

### Hardware Stack
- **Microcontroller:** ESP32-C3 (built-in WiFi, BLE, ≥4MB Flash)
- **Display:** 0.96" SSD1306 OLED (128x64 px, I2C)
- **Audio Input:** INMP441 MEMS microphone (I2S)
- **Audio Output:** MAX98357A I2S amplifier + speaker
- **Power:** USB-C or battery (18650 + boost converter)
- **Communication:** WiFi (ESP32 native) + optional BLE

### Software Stack
- **ESP32 Firmware:** MicroPython (optimized build)
- **Display Driver:** SSD1306 library (MicroPython)
- **Audio Capture:** I2S driver (built-in) + MEMS mic firmware
- **LLM Chat:** Ollama running on Home Server (192.168.x.x:11434)
- **STT:** OpenAI Whisper (Python backend)
- **TTS:** Piper TTS (Python backend)
- **API Bridge:** FastAPI server (localhost:8000) routing ESP32 → Ollama/Whisper/Piper

### Data Flow

```
Microphone (I2S)
    ↓
ESP32 (I2S capture + audio buffer)
    ↓
POST /transcribe → FastAPI
    ↓
Whisper (STT)
    ↓
LLM Response + Emotion Tag → Ollama Gemma
    ↓
Piper (TTS) → MP3 bytes
    ↓
ESP32 (I2S playback) → MAX98357A → Speaker
    ↓
Animation API: GET /animate?emotion=happy → JSON face config
    ↓
SSD1306 OLED renders face
```

## Important Decisions

| DEC-ID | Decision | Status | Reason |
|--------|----------|--------|--------|
| DEC-001 | Use ESP32-C3 over ESP32 Classic | APPROVED | Smaller, less power, WiFi sufficient |
| DEC-002 | OLED 0.96" (128x64) not 1.3" | APPROVED | Size/cost fit for small desk robot |
| DEC-003 | Ollama backend on home server | APPROVED | No internet required; privacy; offline capable |
| DEC-004 | Whisper + Piper vs commercial APIs | APPROVED | Open-source; offline; no recurring costs |
| DEC-005 | Cute 8x8 face sprites vs realistic | APPROVED | Safe, playful, easy to animate for young child |
| DEC-006 | MicroPython not C/Arduino | APPROVED | Faster iteration; easier debugging |
| DEC-007 | FastAPI bridge vs direct ESP32 APIs | APPROVED | Separation of concerns; easier testing |

## Known Constraints

- **Size:** Desktop robot, fits in 150×150×200mm enclosure (est.)
- **Power:** ≤2W average (WiFi + OLED + speaker); ~4 hours on 5000mAh 18650
- **Latency:** Must respond within 1 second (Whisper + Gemma + Piper + ESP32 render)
- **Audio Quality:** Acceptable quality STT (Whisper tiny/base model); fast TTS (Piper)
- **Safety:** No sharp edges, no toxic materials, all code reviewed for child-safe outputs
- **Connectivity:** WiFi only (no cellular); assumes home network available

## Known Risks

| RISK-ID | Risk | Probability | Impact | Mitigation |
|---------|------|-------------|--------|-----------|
| RISK-001 | Whisper latency > 1s | MEDIUM | Slow response feels broken | Use tiny model; pre-load models |
| RISK-002 | OLED contrast/sunlight | LOW | Display hard to see | Position away from direct light |
| RISK-003 | Audio echo/feedback | MEDIUM | Poor STT accuracy | Separate mic/speaker; mute during playback |
| RISK-004 | Child drops/breaks | MEDIUM | Safety hazard | Plastic enclosure; impact padding |
| RISK-005 | Inappropriate LLM output | LOW | Child upset | Instruction prompt filters; content review |
| RISK-006 | Battery overcharge | LOW | Fire risk | BMS IC + firmware limits |
| RISK-007 | WiFi dropouts | LOW | System hangs | Auto-reconnect logic; timeout handling |

## Current Blockers

None. All research complete; architecture approved.

## Recently Completed

- ✓ Requirements capture (REQ-001 to REQ-015 approved)
- ✓ Research: ESP32 variants, OLED selection, audio processors, LLM options
- ✓ Architecture design: hardware block diagram, data flow, API interfaces
- ✓ Decision log: 7 major decisions, all approved

## Current Tasks (In Flight)

- TASK-005: Finalize all requirements (acceptance criteria review)
- TASK-010: Lock architecture (finalize block diagram + pin mapping)
- TASK-012: Research MicroPython OLED drivers (driver evaluation + PoC)

## Immediate Next Tasks

1. **TASK-005:** Finalize Requirements → acceptance criteria signed off
2. **TASK-010:** Finalize Architecture → pin mapping + schematic notes
3. **TASK-015:** Build Task Dependency Graph → create TASKS.yaml
4. **TASK-020:** Hardware BOM & Procurement → verify availability
5. **TASK-025:** Firmware Skeleton → ESP32-C3 MicroPython template

## Open Human Decisions

None at this phase. Will need approval before:
- Purchasing hardware (budget ~¥8,000–¥12,000)
- Deploying TTS/Whisper to production home server
- Publishing source code or design

## Technical Notes for Resume

- **Ollama Location:** Assumed home server on 192.168.0.x network; can be overridden
- **FastAPI Bridge:** Runs on localhost:8000; routes to Ollama (11434) + Whisper + Piper
- **ESP32 WiFi:** SSID + password stored in config file (not in source); optional BLE pairing
- **Face Animations:** 8×8 sprite library (32 base expressions × 4 emotion variants)
- **Audio:** I2S protocol (GPIO pins TBD in final hardware design)

---

**Last Updated:** 2026-09-29  
**Next Review:** After TASK-005 + TASK-010 complete
