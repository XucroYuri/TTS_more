# Windows Four-Repository One-Click Portable Execution Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver four Windows x64 ZIP packages whose only normal-user action is extracting the package and double-clicking `Start.cmd`, while keeping all four repositories independently runnable.

**Architecture:** TTS More remains the specification source. Phase A implements the shared manifest and operation protocol plus the one-click controller. Phase B adds safe local package discovery and per-service controls to the TTS More workbench. Phase C mirrors the approved integration into the three upstream forks. Phase D finishes staging, migration, CI and clean-machine acceptance.

**Tech Stack:** Python 3.11, Python 3.10 for CosyVoice, PowerShell 5.1, FastAPI, Pydantic 2, React 19, TypeScript 5.9, Vitest 4, pytest 8, uv, GitHub Actions.

## Global Constraints

- Supported systems are Windows 10 22H2 and Windows 11 x64.
- The normal-user flow is exactly “extract ZIP, double-click `Start.cmd`”.
- No system Python, Conda, Node, Git, CUDA Toolkit or administrator rights may be required at runtime.
- Bootstrap packages download locked runtime and models on first start; Full packages start offline and are never uploaded to GitHub.
- TTS More, GPT-SoVITS and IndexTTS use Python 3.11; CosyVoice uses Python 3.10.
- Default ports are TTS More 8000, GPT-SoVITS 9880, IndexTTS 9881 and CosyVoice 9882.
- Unknown port owners are reported and never terminated.
- Local packages are manageable only through validated package manifests; LAN services remain `managed:false`.
- GPT-SoVITS formal source baseline is `main`.
- TTS More can read schema v1 and v2 but only generates the completed schema v2 contract.
- Bootstrap device order is CU128, CU126, CPU; explicit CUDA failure never silently falls back.
- Four repositories use one release-train version and the `tts-more-v1` contract.

---

## Plan Order and Gates

1. Execute [Phase A: portable control core](2026-07-14-one-click-portable-control-core.md) in the TTS More isolated worktree.
2. Execute [Phase B: local service workbench](2026-07-14-one-click-portable-service-workbench.md) after Phase A interfaces pass.
3. Merge the TTS More specification-source PR, then execute [Phase C: three-fork rollout](2026-07-14-one-click-portable-fork-rollout.md).
4. Execute [Phase D: packaging and acceptance](2026-07-14-one-click-portable-packaging-acceptance.md) only after all three fork mirrors match the merged source revision.
5. Open the TTS More convergence PR that updates `repo.lock.json`, the compatibility matrix and the final acceptance evidence.

Each phase is a separate reviewer gate. A later phase must not compensate for a failing earlier phase. The release train is complete only when Phase D passes for all four packages.

## Specification Coverage

| Approved design area | Implementing phase |
|---|---|
| Design section 1: four deliverables and Bootstrap/Full definitions | Phases C and D |
| Design section 2: unified root commands and independent startup | Phases A and C |
| Design section 3: clean user-visible package layout | Phase D |
| Design section 4: completed schema v2 with v1 reader compatibility | Phase A |
| Design section 5: operations and shared Start protocol | Phase A |
| Design section 6: transactional Bootstrap initialization and repair | Phases A and D |
| Design section 7: Auto/CU128/CU126/CPU selection and fail-closed CUDA probes | Phases A, C and D |
| Design section 8: Full offline boundary and GitHub upload prohibition | Phase D |
| Design section 9: relocatable local service maintenance and independent shortcuts | Phase B |
| Design section 10: PID/port ownership and health checks | Phases A and B |
| Design section 11: progress, cancellation and ordinary-user errors | Phase A |
| Design section 12: compatible upgrades and copy-only migration | Phase D |
| Design section 13: immutable dependency/model locks and integration code | Phases C and D |
| Design section 14: controlled mirror and four-repository release train | Phases C and D |
| Design section 15: Bootstrap release audit, Full local-only rule, SBOM and licenses | Phase D |
| Design section 16: simulation, clean Windows, hardware, LAN and non-developer acceptance | Phases B and D |
