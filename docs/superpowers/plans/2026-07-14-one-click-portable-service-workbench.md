# Local Portable Service Workbench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let TTS More discover, relocate and independently control the three local TTS packages through validated paths and their own `Start.cmd` entries.

**Architecture:** A dedicated locator store resolves relative path, last absolute path and one-level sibling manifests. A portable package controller delegates lifecycle operations to exact validated root launchers; the existing generic supervisor remains for non-portable services. Loopback-only APIs expose per-service cards and operation polling to a focused React component.

**Tech Stack:** FastAPI, Pydantic 2, Python subprocess, Windows PowerShell folder picker, React 19, TypeScript 5.9, Vitest 4, pytest 8.

## Global Constraints

- Three local TTS repositories remain independently started; there is no implicit “start all”.
- TTS More stores machine paths only in `data/local/services.json`.
- Discovery scans only explicit roots and TTS More siblings, never whole drives.
- Local control validates component identity and exact package launchers before execution.
- LAN services remain `managed:false` and cannot browse folders or execute lifecycle actions.
- Unknown port owners are never terminated.
- Local control requests require loopback, same-origin and a per-process control token.
- Path handling covers spaces, Chinese names, different drives and moved removable disks.

---

### Task 1: Persist and resolve portable service locators

**Files:**
- Create: `backend/app/portable_services.py`
- Create: `backend/tests/test_portable_services.py`
- Modify: `backend/app/portable_discovery.py`
- Modify: `backend/app/models.py`
- Modify: `backend/tests/test_portable_discovery.py`

**Interfaces:**
- Consumes: completed schema v2 descriptor from Phase A.
- Produces: `PortableServiceLocator`, `PortableServiceStore.load/save/upsert`, and `resolve_locator(controller_root, locator, search_roots) -> PortablePackageDescriptor | None`.
- Test helper: `_write_package(root: Path, *, component: str, package_id: str, port: int = 9880) -> Path` writes all root launchers plus a completed schema v2 manifest and returns the package root.

- [ ] **Step 1: Write failing relocation-order tests**

```python
def test_locator_prefers_relative_then_absolute_then_sibling_identity(tmp_path: Path) -> None:
    controller = tmp_path / "suite" / "TTS More"
    moved = tmp_path / "suite" / "自定义 GPT 文件夹"
    _write_package(moved, component="gpt-sovits", package_id="gpt-main")
    locator = PortableServiceLocator(
        component="gpt-sovits",
        package_id="gpt-main",
        relative_to_tts_more="../自定义 GPT 文件夹",
        absolute_path_last_seen="X:/missing/GPT",
        build_id_last_seen="old-build",
    )
    descriptor = resolve_locator(controller, locator, [])
    assert descriptor is not None
    assert Path(descriptor.package_root) == moved.resolve()
```

- [ ] **Step 2: Run the new tests and confirm import failure**

Run: `py -3.11 -m pytest backend/tests/test_portable_services.py backend/tests/test_portable_discovery.py -q`

Expected: FAIL because locator models and resolver do not exist.

- [ ] **Step 3: Implement identity-based locator resolution and atomic persistence**

```python
class PortableServiceLocator(BaseModel):
    component: Literal["gpt-sovits", "indextts", "cosyvoice"]
    package_id: str
    relative_to_tts_more: str | None = None
    absolute_path_last_seen: str | None = None
    build_id_last_seen: str | None = None
    port_override: int | None = Field(default=None, ge=1, le=65535)

def resolve_locator(controller_root: Path, locator: PortableServiceLocator, search_roots: Sequence[Path]) -> PortablePackageDescriptor | None:
    candidates = _ordered_candidates(controller_root, locator, search_roots)
    for candidate in candidates:
        try:
            descriptor = read_portable_package(candidate)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if descriptor.valid and descriptor.component == locator.component and descriptor.package_id == locator.package_id and is_controller_compatible(descriptor.controller_range, CONTROLLER_VERSION):
            return descriptor
    return None
```

Use temp-file plus `os.replace()` for `data/local/services.json`. A relative locator may point only to one sibling below the TTS More parent; reject additional traversal. Keep the existing endpoint list compatible by embedding the locator under a new `portable_locator` field and adding `control_kind: "portable-package"` to `TTSServiceEndpoint`. Mark incompatible protocol ranges as visible but not manageable.

- [ ] **Step 4: Run locator, model and discovery tests**

Run: `py -3.11 -m pytest backend/tests/test_portable_services.py backend/tests/test_portable_discovery.py backend/tests/test_services.py -q`

Expected: PASS.

- [ ] **Step 5: Commit locator persistence**

```powershell
git add backend/app/portable_services.py backend/app/portable_discovery.py backend/app/models.py backend/tests/test_portable_services.py backend/tests/test_portable_discovery.py
git commit -m "feat: persist relocatable portable service paths"
```

### Task 2: Delegate portable lifecycle actions to validated root commands

**Files:**
- Create: `backend/app/portable_control.py`
- Create: `backend/tests/test_portable_control.py`
- Modify: `backend/app/supervisor.py`
- Modify: `backend/tests/test_service_supervisor.py`
- Modify: `backend/app/portable_discovery.py`

**Interfaces:**
- Consumes: Task 1 descriptor and `control_kind="portable-package"`.
- Produces: `PortablePackageController.start/stop/repair/logs/status/open_folder`; exact `Start.cmd -OperationId UUID -ManagedBy tts-more -NoUi [-PortOverride N]` invocation.
- Test helper: `FakeProcess(pid: int)` exposes `pid`, `poll() -> None` and `wait(timeout=None) -> int` without spawning a process.

- [ ] **Step 1: Write failing command-allowlist tests**

```python
def test_controller_executes_only_manifest_root_launcher(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "GPT 包", component="gpt-sovits", package_id="gpt-main")
    calls: list[list[str]] = []
    controller = PortablePackageController(spawn=lambda command, **_kwargs: calls.append(command) or FakeProcess(42))
    result = controller.start(read_portable_package(package), operation_id="11111111-1111-4111-8111-111111111111", port_override=9980)
    assert calls == [["cmd.exe", "/d", "/c", str(package / "Start.cmd"), "-OperationId", "11111111-1111-4111-8111-111111111111", "-ManagedBy", "tts-more", "-NoUi", "-PortOverride", "9980"]]
    assert result["status"] == "starting"
```

- [ ] **Step 2: Run controller and supervisor tests and confirm failure**

Run: `py -3.11 -m pytest backend/tests/test_portable_control.py backend/tests/test_service_supervisor.py -q`

Expected: FAIL because the dedicated controller does not exist.

- [ ] **Step 3: Implement the validated controller and supervisor delegation**

```python
class PortablePackageController:
    def start(self, descriptor: PortablePackageDescriptor, *, operation_id: str, port_override: int | None = None) -> dict[str, object]:
        root = Path(descriptor.package_root).resolve(strict=True)
        fresh = read_portable_package(root)
        if not fresh.valid or fresh.package_id != descriptor.package_id:
            raise ValueError("portable package identity changed before start")
        launcher = (root / fresh.launcher).resolve(strict=True)
        launcher.relative_to(root)
        command = ["cmd.exe", "/d", "/c", str(launcher), "-OperationId", operation_id, "-ManagedBy", "tts-more", "-NoUi"]
        if port_override is not None:
            command.extend(["-PortOverride", str(port_override)])
        process = self._spawn(
            command,
            cwd=root,
            env=os.environ.copy(),
            creationflags=windows_creation_flags(),
        )
        return {"status": "starting", "operation_id": operation_id, "controller_pid": process.pid}
```

Implement stop and repair using the manifest’s exact launchers. `open_folder` may launch only `explorer.exe` with the freshly validated package root; the browser opens service URLs itself. Do not widen `_DEFAULT_ALLOWED_EXECUTABLES`. Make `ServiceSupervisor` delegate only endpoints with `control_kind="portable-package"` and a freshly validated descriptor.

- [ ] **Step 4: Run controller, supervisor and security tests**

Run: `py -3.11 -m pytest backend/tests/test_portable_control.py backend/tests/test_service_supervisor.py backend/tests/test_storage_security.py -q`

Expected: PASS; arbitrary paths and LAN actions are rejected.

- [ ] **Step 5: Commit controlled lifecycle delegation**

```powershell
git add backend/app/portable_control.py backend/app/supervisor.py backend/app/portable_discovery.py backend/tests/test_portable_control.py backend/tests/test_service_supervisor.py
git commit -m "feat: control portable packages through root launchers"
```

### Task 3: Add loopback-only portable management APIs and folder selection

**Files:**
- Create: `backend/app/local_control.py`
- Create: `scripts/select-portable-folder.ps1`
- Create: `backend/tests/test_local_control.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/auth.py`
- Modify: `backend/tests/test_api.py`
- Modify: `Build-Package.ps1`

**Interfaces:**
- Consumes: Tasks 1-2 store and controller.
- Produces: `GET /api/local-control/token`; `GET /api/local-portable-services`; POST discover/select-folder/register/action; GET operation/logs.

- [ ] **Step 1: Write failing loopback and LAN rejection tests**

```python
def test_local_control_requires_loopback_and_control_header(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path / "data"))
    token = client.get("/api/local-control/token").json()["token"]
    assert client.post("/api/local-portable-services/discover", json={}).status_code == 403
    response = client.post(
        "/api/local-portable-services/discover",
        headers={"X-TTS-More-Control": token},
        json={},
    )
    assert response.status_code == 200
```

- [ ] **Step 2: Run API tests and confirm missing-route failure**

Run: `py -3.11 -m pytest backend/tests/test_local_control.py backend/tests/test_api.py -q`

Expected: FAIL with 404 for the new routes.

- [ ] **Step 3: Implement the control guard and explicit APIs**

```python
from urllib.parse import urlparse

def require_local_control(request: Request, token: str) -> None:
    host = request.client.host if request.client else ""
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() == "localhost"
    provided = request.headers.get("X-TTS-More-Control", "")
    origin = request.headers.get("origin")
    origin_host = urlparse(origin).hostname if origin else None
    origin_ok = origin_host is None or origin_host in {"127.0.0.1", "localhost", "::1"}
    if not loopback or not origin_ok or not hmac.compare_digest(provided, token):
        raise HTTPException(status_code=403, detail="local portable control requires loopback and a control token")
```

Generate one token with `secrets.token_urlsafe(32)` at app creation. Folder selection invokes only `scripts/select-portable-folder.ps1`, validates the returned manifest and never accepts a command path. Add the script to TTS More package staging.

- [ ] **Step 4: Run API, auth and discovery tests**

Run: `py -3.11 -m pytest backend/tests/test_local_control.py backend/tests/test_api.py backend/tests/test_portable_discovery.py -q`

Expected: PASS; simulated LAN clients receive 403; LAN service entries remain unmanageable.

- [ ] **Step 5: Commit the local management API**

```powershell
git add backend/app/local_control.py backend/app/main.py backend/app/auth.py scripts/select-portable-folder.ps1 Build-Package.ps1 backend/tests/test_local_control.py backend/tests/test_api.py
git commit -m "feat: expose loopback portable service controls"
```

### Task 4: Add focused React service cards and operation progress

**Files:**
- Create: `frontend/src/components/LocalPortableServicesPanel.tsx`
- Create: `frontend/src/lib/portableServices.ts`
- Create: `frontend/src/lib/portableServices.test.ts`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/api.test.ts`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/App.css`
- Modify: `frontend/src/i18n.ts`
- Modify: `frontend/src/i18n.test.ts`

**Interfaces:**
- Consumes: Task 3 APIs and operation events.
- Produces: three independent cards with browse, start, stop, repair, open, logs and progress actions.

- [ ] **Step 1: Write failing card-state and API tests**

```typescript
it("keeps the three components independent and disables LAN actions", () => {
  const cards = portableServiceCards([
    { component: "gpt-sovits", managed: true, status: "ready" },
    { component: "indextts", managed: false, status: "stopped" },
  ]);
  expect(cards.map((card) => card.component)).toEqual(["gpt-sovits", "indextts", "cosyvoice"]);
  expect(cards[1].actions.start).toBe(false);
  expect(cards[2].status).toBe("not_configured");
});
```

- [ ] **Step 2: Run focused frontend tests and confirm failure**

Run: `pnpm --dir frontend test -- portableServices.test.ts api.test.ts i18n.test.ts`

Expected: FAIL because portable service types and helpers do not exist.

- [ ] **Step 3: Implement typed API calls, pure state helpers and the panel**

```typescript
export type PortableOperationPhase = "not_initialized" | "checking" | "downloading" | "installing" | "validating" | "starting" | "ready" | "stopped" | "repairable" | "blocked";

export interface PortableActionResponse {
  component: CatalogProvider;
  status: PortableOperationPhase;
  operation_id?: string;
  error_code?: string;
}

export async function portableServiceAction(component: CatalogProvider, action: "start" | "stop" | "repair", token: string): Promise<PortableActionResponse> {
  return request(`/api/local-portable-services/${encodeURIComponent(component)}/${action}`, {
    method: "POST",
    headers: { ...jsonHeaders, "X-TTS-More-Control": token },
  });
}
```

Keep rendering in `LocalPortableServicesPanel.tsx`; pass data and callbacks from `App.tsx` instead of expanding the existing service-directory block. Poll the active operation at 500 ms while non-terminal, then refresh `/api/services/status`. Show ordinary Chinese phase labels by default, put CUDA/Conda/uv/ONNX details under an expander, and show the manual proxy field only after all locked automatic sources fail.

- [ ] **Step 4: Run frontend tests and production build**

Run: `pnpm --dir frontend test`

Run: `pnpm --dir frontend build`

Expected: all Vitest tests PASS and TypeScript/Vite production build succeeds.

- [ ] **Step 5: Commit the workbench panel**

```powershell
git add frontend/src/components/LocalPortableServicesPanel.tsx frontend/src/lib/portableServices.ts frontend/src/lib/portableServices.test.ts frontend/src/api.ts frontend/src/api.test.ts frontend/src/types.ts frontend/src/App.tsx frontend/src/App.css frontend/src/i18n.ts frontend/src/i18n.test.ts
git commit -m "feat: manage local portable TTS services"
```

### Task 5: Verify Phase B end to end in TTS More

**Files:**
- Modify: `backend/tests/test_portable_discovery.py`
- Modify: `backend/tests/test_local_control.py`
- Modify: `frontend/src/lib/portableServices.test.ts`
- Modify: `docs/workers.md`
- Modify: `docs/deployment.md`

**Interfaces:**
- Consumes: all Phase B interfaces.
- Produces: documented, tested local-path workflow and LAN control boundary.
- Test helpers: `_write_suite(root: Path) -> Path` creates sibling `TTS More`, `GPT-SoVITS`, `IndexTTS` and `CosyVoice` fixtures; `_local_client(tts_more_root: Path) -> tuple[TestClient, str]` injects a fake portable controller and returns its loopback control token; `_control(token: str) -> dict[str, str]` returns the `X-TTS-More-Control` header.

- [ ] **Step 1: Add an end-to-end API test for moved sibling packages**

```python
def test_moved_four_folder_suite_is_rediscovered_and_started_independently(tmp_path: Path) -> None:
    suite = _write_suite(tmp_path / "移动盘")
    client, token = _local_client(suite / "TTS More")
    discovered = client.post("/api/local-portable-services/discover", headers=_control(token), json={}).json()
    assert {item["component"] for item in discovered["packages"]} == {"gpt-sovits", "indextts", "cosyvoice"}
    started = client.post("/api/local-portable-services/gpt-sovits/start", headers=_control(token)).json()
    assert started["component"] == "gpt-sovits"
    assert started["operation_id"]
```

- [ ] **Step 2: Run the backend and frontend Phase B slices**

Run: `py -3.11 -m pytest backend/tests/test_portable_discovery.py backend/tests/test_portable_services.py backend/tests/test_portable_control.py backend/tests/test_local_control.py -q`

Run: `pnpm --dir frontend test -- portableServices.test.ts api.test.ts i18n.test.ts`

Expected: PASS.

- [ ] **Step 3: Document the exact user flow and LAN boundary**

```markdown
1. 解压四个组件到任意可写目录。
2. 分别双击需要运行的组件 `Start.cmd`，或在 TTS More 的“本地 TTS 服务”卡片中选择目录并启动。
3. TTS More 不会自动批量启动三个服务。
4. LAN 服务只能注册地址和使用服务，不能远程启动、停止、修复或浏览目录。
```

- [ ] **Step 4: Run all backend tests and frontend build**

Run: `py -3.11 -m pytest backend -q`

Run: `pnpm --dir frontend test; pnpm --dir frontend build`

Expected: backend suite PASS; frontend tests and production build PASS.

- [ ] **Step 5: Commit the Phase B gate**

```powershell
git add backend/tests/test_portable_discovery.py backend/tests/test_local_control.py frontend/src/lib/portableServices.test.ts docs/workers.md docs/deployment.md
git commit -m "test: gate portable service workbench"
```
