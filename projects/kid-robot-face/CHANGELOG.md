# CHANGELOG — Kid-Robot-Face

All notable changes to this project will be documented in this file.

Format: [DATE] [VERSION] — [CHANGE TYPE] — [SUMMARY]

---

## [2026-09-29] 0.1.0-alpha — PROJECT INITIALIZATION

### ADDED
- Project charter and initial requirements (REQ-001 to REQ-015)
- Architecture design document (hardware + software stack)
- 7 core design decisions (microcontroller, display, LLM backend, etc.)
- Risk register with 7 identified risks and mitigations
- Task dependency graph (20 tasks, 8 parallel work groups)
- PROJECT.yaml, PROJECT_MEMORY.md, CURRENT_STATE.md, TASKS.yaml
- Decision log with full rationale and alternatives
- Checkpoint CP-KID-ROBOT-001-INIT (requirements + architecture locked)

### DECISION
- DEC-001: ESP32-C3 microcontroller (approved)
- DEC-002: 0.96" SSD1306 OLED (approved)
- DEC-003: Ollama home server LLM (approved)
- DEC-004: Whisper + Piper for STT/TTS (approved)
- DEC-005: Cute 8×8 sprites (approved)
- DEC-006: MicroPython firmware (approved)
- DEC-007: FastAPI bridge architecture (approved)

### AFFECTED REQUIREMENTS
- All 15 requirements in REQ-001..REQ-015 initialized and approved

### AFFECTED TASKS
- TASK-001: Requirements capture (DONE)
- TASK-005: Finalize requirements (IN_PROGRESS)
- TASK-010: Finalize architecture (IN_PROGRESS)
- TASK-012: Research OLED drivers (IN_PROGRESS)
- TASK-015..TASK-080: Full project plan created

### AFFECTED BOM
- Preliminary hardware list: ESP32-C3, SSD1306, INMP441, MAX98357A, 18650 battery, boost converter

### AFFECTED TESTS
- Test plan structure created; specific test cases to follow in TASK-050+

### STATUS
- Phase: REQUIREMENTS → ARCHITECTURE (in transition)
- Health: HEALTHY
- Blocked tasks: 0
- Context utilization: 35%

### MIGRATION / COMPATIBILITY NOTES
- This is the first release; no prior versions to migrate from
- Future releases will document backward compatibility concerns

### RELATED DECISIONS
- All 7 major architectural decisions made and documented

### NOTES
- Project assumes home server with Ollama, Whisper, Piper already running
- WiFi network required; no cellular fallback
- Target completion date: 2026-10-19 (20-day critical path)
- Budget estimate: ¥8,000–¥12,000 hardware + free software

---

## [UPCOMING] 0.2.0-beta — ARCHITECTURE LOCK & HARDWARE PROCUREMENT

### PLANNED
- TASK-010: Architecture finalized (pin mapping, block diagram, schematics)
- TASK-020: Hardware BOM approved and procurement initiated
- Checkpoint CP-KID-ROBOT-002-ARCH-LOCK
- Risk review: latency + power benchmarks

### EXPECTED DATE
- 2026-10-02

---

## [UPCOMING] 0.3.0-beta — FIRMWARE SKELETON & API BRIDGE

### PLANNED
- TASK-025: Firmware skeleton (I2C, I2S drivers, WiFi)
- TASK-030: FastAPI bridge (Whisper, Ollama, Piper endpoints)
- TASK-035: Face animation library (128 8×8 sprites)
- Integration testing begins

### EXPECTED DATE
- 2026-10-09

---

## [UPCOMING] 0.4.0-release-candidate — INTEGRATION & TESTING

### PLANNED
- TASK-040: End-to-end integration (mic → transcribe → chat → animate → speaker)
- TASK-050: Unit tests (firmware + server)
- TASK-060: System tests and acceptance criteria validation
- TASK-070: Documentation (user guide + implementation notes)
- Latency optimization and power profiling

### EXPECTED DATE
- 2026-10-16

---

## [UPCOMING] 1.0.0 — RELEASE

### PLANNED
- TASK-075: Independent review
- TASK-080: Final release, checkpoint, and delivery
- All requirements satisfied or accepted limitations documented
- All tests passing
- User guide and technical documentation complete

### EXPECTED DATE
- 2026-10-19

### RELEASE CRITERIA
- ✓ All mandatory requirements met or documented limitation
- ✓ All acceptance tests pass
- ✓ All reviews complete
- ✓ Code in GitHub with README
- ✓ Hardware build instructions included
- ✓ User receives robot + documentation

---

## Version History Summary

| Version | Date | Phase | Status | Checkpoint |
|---------|------|-------|--------|------------|
| 0.1.0-alpha | 2026-09-29 | REQUIREMENTS + ARCHITECTURE | IN_PROGRESS | CP-001-INIT |
| 0.2.0-beta | 2026-10-02 | PLANNING | PLANNED | CP-002-ARCH-LOCK |
| 0.3.0-beta | 2026-10-09 | IMPLEMENTATION | PLANNED | CP-003-FIRMWARE-READY |
| 0.4.0-rc | 2026-10-16 | TESTING | PLANNED | CP-004-INTEGRATION-COMPLETE |
| 1.0.0 | 2026-10-19 | RELEASE | PLANNED | CP-005-RELEASE |

---

## Risk & Decision Tracking

### New Decisions This Version
- DEC-001 to DEC-007 (all architectural decisions)

### Closed Risks
- None (all 7 risks remain OPEN pending implementation testing)

### Open Risks
- RISK-001: Whisper latency
- RISK-002: OLED contrast
- RISK-003: Audio echo
- RISK-004: Physical damage
- RISK-005: LLM content
- RISK-006: Battery safety
- RISK-007: WiFi dropouts

---

**Last Updated:** 2026-09-29  
**Next Changelog Entry:** After TASK-010 (Architecture Lock)
