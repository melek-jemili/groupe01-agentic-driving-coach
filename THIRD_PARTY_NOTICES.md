# Third-party notices

This repository is a self-contained teaching implementation. Parts of it are
adapted from prior work by the same research group, as detailed below.

## 1. agentic-driving-coach (original Lingua Franca implementation)

- Source: https://github.com/asu-kim/agentic-driving-coach
- Authors: Deeksha Prahlad, Daniel Fan, Hokeun Kim (ASU KIM Lab)
- License: BSD 2-Clause (reproduced below)
- Adapted material:
  - Driver behavior traces in `data/driver/` (copied data files)
  - The stop-sign policy boundaries, car acceleration map, planner state
    machine, warning throttle, and LLM prompt text (reimplemented in Python
    from `src/Approach/StopSign.lf`)
  - Speed-change scenario constants referenced in `ASSIGNMENT.md`
    (from `src/Approach/SpeedChanging.lf`)

```
BSD 2-Clause License

Copyright (c) 2025, ASU KIM Lab

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## 2. adc-xronos (preliminary Xronos translation)

- Source: sibling repository `adc-xronos` (ASU KIM Lab course material;
  no license file present at the time of adaptation - used with permission
  as internal material of the same group)
- Adapted material: enum naming (`Accelerate`/`Brake` action values,
  `PlannerMode`), the modal-reactor-to-enum translation strategy, and the
  `data/` file layout idea. No code from `adc-xronos` is imported at runtime;
  all reactors here have been validated against Xronos 0.13.

## 3. Xronos Python SDK

- https://docs.xronos.com - BSD 3-Clause SDK by Xronos Inc., installed from
  PyPI (`pip install xronos`). Not redistributed in this repository.

## 4. Paper

The system modeled here is described in
[FMSys'26](https://ieeexplore.ieee.org/document/11643660).
