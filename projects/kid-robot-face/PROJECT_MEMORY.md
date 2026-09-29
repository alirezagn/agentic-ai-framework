# PROJECT_MEMORY — Kid-Robot-Face

## Status

This is the initialized project memory for the Kid-Robot-Face project.
It is the compact, resumable state used by the orchestrator. New sessions
read this file instead of replaying full conversation history.

## Goal

Build a voice-reactive robot face that listens, understands, and responds
with expressive character animations. The system must be engaging, fun, and
safe for a young child.

## Project Details

**Project Name:** Kid-Robot-Face
**Project ID:** PROJECT-KID-ROBOT-001
**Version:** 0.1.0
**Current Phase:** REQUIREMENTS
**Owner:** Alireza Goudarzinemati
**Target User:** 5-year-old son

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
| REQ-011 | Response Latency < 1 sec | APPROVED |
| REQ-012 | Low Power (Battery) | APPROVED |
| REQ-013 | Child-Safe Code | APPROVED |
| REQ-014 | Offline Capable | APPROVED |
| REQ-015 | Easy Config via Web UI | APPROVED |

## Important Decisions

| DEC-ID | Decision | Status | Reason |
|--------|----------|--------|--------|
| DEC-001 | Use ESP32-C3 over ESP32 Classic | APPROVED | Smaller, less power, WiFi sufficient |
| DEC-002 | OLED 0.96 inch (128x64) not 1.3 inch | APPROVED | Size and cost fit for a small desk robot |
| DEC-003 | Ollama backend on home server | APPROVED | No internet required; privacy; offline capable |
| DEC-004 | Whisper + Piper instead of commercial APIs | APPROVED | Open-source; offline; no recurring costs |
| DEC-005 | Cute 8x8 face sprites instead of realistic | APPROVED | Safe, playful, easy to animate for a young child |
| DEC-006 | MicroPython instead of C/Arduino | APPROVED | Faster iteration; easier debugging |
| DEC-007 | FastAPI bridge instead of direct ESP32 APIs | APPROVED | Separation of concerns; easier testing |

## Current Blockers

None. Requirements capture complete; architecture approval pending TASK-002.

## Recently Completed

- Requirements capture (REQ-001 to REQ-015 approved)
- Research: ESP32 variants, OLED selection, audio processors, LLM options
- Architecture design: hardware block diagram, data flow, API interfaces
- Decision log: 7 major decisions, all approved

## Immediate Next Tasks

1. TASK-002: Finalize acceptance criteria and requirement traceability
2. TASK-003: Lock architecture (block diagram + pin mapping)
3. TASK-004: Draft hardware bill of materials

## Open Human Decisions

None at this phase. Approval will be needed before purchasing hardware.

## Technical Notes for Resume

- Ollama runs on the home server at 192.168.0.x:11434
- The FastAPI bridge runs on localhost:8000 and routes to Ollama, Whisper, and Piper
- ESP32 WiFi credentials live in a config file, never in source control
- Face animations use an 8x8 sprite library (32 base expressions x 4 emotion variants)

---

**Last Updated:** 2026-09-30
**Next Review:** After TASK-002 and TASK-003 complete
