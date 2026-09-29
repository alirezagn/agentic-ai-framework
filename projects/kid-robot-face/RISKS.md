# RISKS — Kid-Robot-Face

## Risk Register

### RISK-001: Whisper Latency Exceeds 1-Second Budget

**Probability:** MEDIUM  
**Impact:** MEDIUM  
**Exposure:** MEDIUM  

**Description:**  
OpenAI Whisper (even the tiny model) may take 1–3 seconds on CPU alone, pushing total latency beyond 1 second when combined with network + Ollama + Piper.

**Trigger:**  
- Benchmark during TASK-012 shows Whisper > 0.5s on typical home server CPU
- Integration test (TASK-040) measures end-to-end > 1.5s

**Mitigation:**  
1. Use Whisper "tiny" model (~39M parameters) instead of "base" or "small"
2. Pre-load Whisper into memory at startup (avoid model loading overhead)
3. Use GPU acceleration if home server has NVIDIA GPU (10–50× faster)
4. Optimize: batch audio frames to reduce API calls
5. Fallback: accept 1.5s latency as acceptable (child won't notice < 2s difference)

**Contingency:**  
1. If tiny model insufficient, evaluate FastWhisper or faster-whisper (optimized port)
2. Consider Wav2Vec2 (smaller, faster) as fallback STT
3. Extend latency budget to 2 seconds (document as limitation)

**Owner:** Alireza Goudarzinemati (Software Agent)  
**Status:** OPEN  
**Related Tasks:** TASK-012, TASK-030, TASK-040  
**Related Decisions:** DEC-004 (Whisper selection)

---

### RISK-002: OLED Display Contrast/Visibility in Sunlight

**Probability:** LOW  
**Impact:** LOW  
**Exposure:** LOW  

**Description:**  
SSD1306 OLEDs have limited viewing angles and reduced contrast in bright ambient light. Child may not see face clearly if placed in direct sunlight.

**Trigger:**  
- Robot positioned near window
- Subjective observation during testing: display hard to read

**Mitigation:**  
1. Design: position robot away from direct sunlight (desk indoors, not windowsill)
2. Document: user guide warns against direct sunlight
3. Hardware option: consider anti-glare screen protector (optional purchase)
4. Software: no dynamic brightness adjustment possible (SSD1306 limitation)

**Contingency:**  
1. Accept as limitation (documented in release notes)
2. If critical: upgrade to 1.3" display with higher brightness (DEC-002 revisit)

**Owner:** Alireza Goudarzinemati (Hardware Agent)  
**Status:** OPEN  
**Related Tasks:** TASK-070 (user guide warning)  
**Related Decisions:** DEC-002 (OLED size)

---

### RISK-003: Audio Echo/Feedback During Playback

**Probability:** MEDIUM  
**Impact:** MEDIUM  
**Exposure:** MEDIUM  

**Description:**  
Microphone may pick up speaker output while robot is speaking, causing echo in the next transcription cycle. This reduces Whisper accuracy.

**Trigger:**  
- During integration test (TASK-040), Whisper transcribes speaker echo instead of user voice
- Subsequent LLM response is nonsensical (robot responds to itself)

**Mitigation:**  
1. **Hardware separation:** Place microphone far from speaker (opposite corners of enclosure)
2. **Audio muting:** Mute microphone while speaker is playing (firmware firmware logic)
3. **Acoustic dampening:** Use foam padding to isolate speaker from mic
4. **Software filter:** Apply simple high-pass filter to remove speaker frequency bands (if detectable)
5. **Human override:** Child can press button to interrupt/skip if robot acts confused

**Contingency:**  
1. If echo persists: use directional mic (condenser mic with polar pattern)
2. Accept as limitation: document in user guide (avoid noisy environments)
3. Post-processing: use audio denoising library (librosa) in FastAPI bridge

**Owner:** Alireza Goudarzinemati (Hardware + Software Agent)  
**Status:** OPEN  
**Related Tasks:** TASK-025 (firmware muting logic), TASK-040 (echo testing)  
**Related Decisions:** DEC-003, DEC-004 (audio capture/playback)

---

### RISK-004: Child Drops/Physically Damages Robot

**Probability:** MEDIUM  
**Impact:** MEDIUM  
**Exposure:** MEDIUM  

**Description:**  
A 5-year-old is unpredictable. Robot may be dropped, thrown, or sat on. Physical damage could create sharp edges, battery exposure, or electrical hazards.

**Trigger:**  
- During user testing with child
- Robot stops working due to impact
- Potential safety concern: cracked enclosure, exposed wires, battery damage

**Mitigation:**  
1. **Enclosure design:** Use soft/rounded plastic casing (3D-printed or injection-molded TPU)
2. **Impact padding:** Surround electronics with shock-absorbing foam
3. **Battery protection:** Encapsulate battery in protective case; use BMS with short-circuit protection
4. **No sharp corners:** Round all edges; sand any burrs from 3D prints
5. **Durability testing:** Drop test from 1m height before release (TASK-060)
6. **User guide:** Include warnings about drops; storage recommendations

**Contingency:**  
1. Provide replacement battery (cheap, easy to swap)
2. Design for modular assembly: if enclosure cracks, replace just the shell
3. Accept as wear-and-tear item (recommend protective case for travel)

**Owner:** Alireza Goudarzinemati (Hardware Agent)  
**Status:** OPEN  
**Related Tasks:** TASK-020 (BOM includes protective case), TASK-070 (user guide warnings)

---

### RISK-005: Inappropriate LLM Output (Scary, Vulgar, or Upsetting Content)

**Probability:** LOW  
**Impact:** MEDIUM  
**Exposure:** LOW  

**Description:**  
Ollama Gemma model may generate scary, inappropriate, or upsetting responses if the child asks questions about violence, death, or other sensitive topics. This could distress a 5-year-old.

**Trigger:**  
- During acceptance testing (TASK-060), tester asks robot about death/violence
- LLM responds with disturbing content
- Child hears response and becomes upset

**Mitigation:**  
1. **System prompt:** Craft Ollama system message to set friendly, child-safe persona
   - Example: "You are a friendly robot named [Name]. You are kind and silly. You refuse to talk about scary things. If someone asks about death/violence, you redirect to fun topics."
2. **Blocked words:** Simple keyword filter in FastAPI bridge (block: death, blood, violence, etc.)
3. **Review process:** Test 100+ child-appropriate questions before release
4. **Manual override:** Parent can edit system prompt if needed

**Contingency:**  
1. If LLM still produces bad output: use more restrictive prompt + larger keyword blacklist
2. Accept as limitation: document in release notes (parental supervision recommended)
3. Add feedback loop: log concerning responses for review

**Owner:** Alireza Goudarzinemati (Software Agent, with parental input)  
**Status:** OPEN  
**Related Tasks:** TASK-030 (implement content filter), TASK-060 (test appropriateness)  
**Related Decisions:** DEC-003 (Ollama selection for content control)

---

### RISK-006: Battery Overcharge or Fire Risk

**Probability:** LOW  
**Impact:** HIGH  
**Exposure:** LOW  

**Description:**  
If battery management is not carefully implemented, a cheap 18650 lithium battery could overcharge, swell, or potentially catch fire. This is a serious safety hazard for a child's toy.

**Trigger:**  
- Battery left on charger overnight
- Charging circuit lacks overvoltage protection
- Battery swells or becomes hot during use

**Mitigation:**  
1. **Battery Management IC (BMS):** Use dedicated BMS chip (e.g., DW01 + protection MOSFET pair)
2. **Firmware limits:** Implement coulomb counting + voltage cutoff in MicroPython
3. **User guide:** Explicit charging instructions (recommended charger, max charging time)
4. **Component selection:** Choose premium 18650 batteries with internal protection (e.g., Panasonic NCR18650B)
5. **Testing:** Charge/discharge cycle testing (TASK-060) to verify no swelling after 50 cycles

**Contingency:**  
1. Use USB-C PD charger instead of DIY charging circuit (simple, safe, standardized)
2. Accept USB-only power (remove battery option if too risky)
3. Use smaller capacity battery (e.g., 3000mAh instead of 5000mAh) for shorter runtime but lower thermal risk

**Owner:** Alireza Goudarzinemati (Hardware Agent)  
**Status:** OPEN  
**Related Tasks:** TASK-020 (BOM includes BMS), TASK-060 (safety testing)  
**Related Decisions:** DEC-001 (power budget)

---

### RISK-007: WiFi Dropouts or Network Unreachability

**Probability:** LOW  
**Impact:** LOW  
**Exposure:** LOW  

**Description:**  
If home WiFi is unstable or the FastAPI bridge server becomes unreachable, the robot will fail to process requests. This could frustrate the child.

**Trigger:**  
- WiFi router reboots
- FastAPI bridge process crashes (unhandled exception)
- Network congestion drops packets
- Robot moves out of WiFi range

**Mitigation:**  
1. **Firmware resilience:** Implement auto-reconnect logic in ESP32 firmware (attempt 5 reconnects before giving up)
2. **Timeout handling:** If FastAPI bridge doesn't respond in 2s, robot says "Let me think..." and tries again
3. **Fallback mode:** If network fails 3 times, robot responds with pre-recorded cheerful message ("I'm thinking hard!")
4. **Server monitoring:** Home server runs health check; FastAPI logs all errors
5. **Documentation:** User guide includes troubleshooting (check WiFi, restart router, check logs)

**Contingency:**  
1. Implement local fallback: pre-load a few simple joke responses on ESP32 as offline mode
2. Accept as limitation: document in release notes (WiFi connectivity required)
3. Add diagnostic endpoint: robot can report network status via web interface

**Owner:** Alireza Goudarzinemati (Software Agent)  
**Status:** OPEN  
**Related Tasks:** TASK-025 (firmware resilience), TASK-030 (error handling), TASK-040 (integration testing)  
**Related Decisions:** DEC-003 (home server architecture)

---

## Risk Summary

| RISK-ID | Description | Prob | Impact | Status | Owner |
|---------|-------------|------|--------|--------|-------|
| RISK-001 | Whisper latency > 1s | MED | MED | OPEN | Software Agent |
| RISK-002 | OLED contrast in sunlight | LOW | LOW | OPEN | Hardware Agent |
| RISK-003 | Audio echo/feedback | MED | MED | OPEN | Hardware + Software |
| RISK-004 | Physical damage from drops | MED | MED | OPEN | Hardware Agent |
| RISK-005 | Inappropriate LLM output | LOW | MED | OPEN | Software Agent |
| RISK-006 | Battery fire/overcharge | LOW | HIGH | OPEN | Hardware Agent |
| RISK-007 | WiFi dropouts | LOW | LOW | OPEN | Software Agent |

---

## Monitoring & Review

- **Review frequency:** After each major milestone (architecture lock, integration complete, pre-release)
- **Escalation:** Any risk with "MEDIUM or HIGH" impact is escalated to HUMAN_DECISION_REQUIRED if mitigation fails
- **Closure criteria:** A risk is CLOSED only when mitigation is verified + tested + documented

---

**Last Updated:** 2026-09-21  
**Total Risks:** 7 (all OPEN)  
**Critical Risks (High Impact):** 1 (RISK-006: battery safety)  
**Next Review:** After TASK-012 + TASK-025 complete (latency + power testing)
