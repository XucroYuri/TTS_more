# macOS LAN CUDA Validation Design

Date: 2026-07-10
Status: Approved design

## Goal

Add a second CUDA validation architecture in which the current macOS machine runs the complete TTS More application while Windows CUDA workers run the three formal TTS services over a trusted LAN.

The detailed test plan and operational constraints are defined in [macOS 应用控制面与 LAN Windows CUDA 验证](../../cuda-e2e-macos-lan.md).

## Decisions

- Cover both one Windows GPU host running all three services and three Windows GPU hosts running one service each.
- Treat the three-host topology as the eventual formal distributed gate.
- Use macOS OpenSSH with key-only authentication, pinned host keys and SCP evidence collection.
- Keep all remote services `managed:false`; the macOS supervisor never owns remote processes.
- Use the existing `artifact-transfer` HTTP contract instead of a shared filesystem.
- Introduce the validation in two phases: auditable supplemental testing first, formal release gating only after cross-platform orchestration is implemented.
- Keep the existing Windows single-node and four-node gates authoritative until promotion criteria are met.

## Current Capability Boundary

The repository already provides topology rendering, remote service configuration, artifact transfer, worker contracts, Playwright workstation E2E and Windows SSH/SCP orchestration.

The current `scripts/run-cuda-validation.ps1` cannot certify a macOS controller because it assumes a Windows controller virtual environment, Windows registry identity and local `nvidia-smi`. The Python CUDA runner also treats repeated GPU UUIDs as invalid in `distributed` mode, so it cannot certify the one-host shared-GPU topology.

Phase one therefore produces a supplemental record and must not claim `distributed_orchestration_verified:true` or stable-release approval.

## Target Architecture

The implementation phase will extract a cross-platform Python orchestrator with thin POSIX and PowerShell wrappers. It will provide two topology-derived policies:

- `lan-shared`: one worker owns all three services on one GPU, capacity is one, loaded models cannot overlap and unload recovery is mandatory;
- `lan-distributed`: three workers own one service each, host identity and GPU UUID are unique, concurrent loading and single-node recovery are mandatory.

Remote Windows execution remains a bounded adapter that invokes repository PowerShell scripts through OpenSSH and retrieves only declared evidence paths.

## Security Boundary

- Trusted LAN only; no public exposure, TLS or reverse proxy work is included.
- SSH passwords, usernames, private keys, real hosts and paths stay outside committed topology files.
- Host key verification cannot be disabled.
- Reference audio and output artifacts use bounded HTTP transfer with size and hash checks.
- Raw platform UUID, Windows `MachineGuid` and internal addresses are not persisted in public evidence.

## Evidence And Acceptance

Both profiles require contract responses, commit maps, Playwright JUnit, 30 real synthesis tasks, playable local history, remote worker logs, per-worker `nvidia-smi`, fault recovery and human listening records.

The shared profile proves sequential resource switching and application survival when the remote host disappears. The three-host profile additionally proves overlapping GPU work and isolation when one node stops.

The three-host profile becomes a release gate only after the cross-platform entrypoint enforces clean deployment, one-time preflight, identity uniqueness, failure injection and complete evidence without bypass flags.

## Out Of Scope

This design does not cover public networks, TLS, reverse proxies, Linux GPU workers, commercial TTS providers, shared network filesystems or replacing the existing Windows CUDA certification before the promotion criteria pass.
