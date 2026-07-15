from __future__ import annotations

import json
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Callable, ContextManager, Protocol, Sequence, TypeVar

from app.models import PortableComponent, PortableServiceLocator, TTSServiceEndpoint


PORTABLE_COMPONENT_LOCK_ORDER: tuple[PortableComponent, ...] = (
    "cosyvoice",
    "gpt-sovits",
    "indextts",
)
_PORTABLE_COMPONENTS = frozenset(PORTABLE_COMPONENT_LOCK_ORDER)
_Result = TypeVar("_Result")


class ManagedPortableLocatorMutationError(ValueError):
    """A generic registry write attempted to change protected locator identity."""


@dataclass(frozen=True)
class _PortableLocatorMutationPermit:
    component: PortableComponent


class _LifecycleSupervisor(Protocol):
    def portable_lifecycle_guard(
        self, component: PortableComponent
    ) -> ContextManager[None]: ...


class _ImportPlanInvalidator(Protocol):
    def invalidate_component(self, component: PortableComponent) -> None: ...


def require_managed_portable_locators_unchanged(
    current: Sequence[TTSServiceEndpoint],
    desired: Sequence[TTSServiceEndpoint],
) -> None:
    if _managed_portable_locator_fingerprints(current) != _managed_portable_locator_fingerprints(
        desired
    ):
        raise ManagedPortableLocatorMutationError(
            "managed portable locators must use a portable registration route"
        )


def require_managed_portable_locator_mutation_permitted(
    current: Sequence[TTSServiceEndpoint],
    desired: Sequence[TTSServiceEndpoint],
    permit: object | None,
) -> None:
    current_by_component = _managed_portable_locator_fingerprints_by_component(current)
    desired_by_component = _managed_portable_locator_fingerprints_by_component(desired)
    changed = {
        component
        for component in _PORTABLE_COMPONENTS
        if current_by_component[component] != desired_by_component[component]
    }
    if not changed:
        return
    if not isinstance(permit, _PortableLocatorMutationPermit) or changed != {permit.component}:
        raise ManagedPortableLocatorMutationError(
            "managed portable locators must use a portable registration route"
        )


@dataclass(frozen=True)
class PortableLocatorMutationCoordinator:
    supervisor: _LifecycleSupervisor
    import_plans: _ImportPlanInvalidator

    def mutate_component(
        self,
        component: PortableComponent,
        mutation: Callable[[object], _Result],
    ) -> _Result:
        with self.supervisor.portable_lifecycle_guard(component):
            result = mutation(_PortableLocatorMutationPermit(component))
            self.import_plans.invalidate_component(component)
            return result

    def publish_without_locator_changes(
        self,
        *,
        current_services: Callable[[], Sequence[TTSServiceEndpoint]],
        load_candidate: Callable[[], _Result],
        candidate_services: Callable[[_Result], Sequence[TTSServiceEndpoint]],
        publish: Callable[[_Result], None],
    ) -> _Result:
        with ExitStack() as stack:
            for component in PORTABLE_COMPONENT_LOCK_ORDER:
                stack.enter_context(self.supervisor.portable_lifecycle_guard(component))
            candidate = load_candidate()
            require_managed_portable_locators_unchanged(
                current_services(),
                candidate_services(candidate),
            )
            publish(candidate)
            return candidate


def _managed_portable_locator_fingerprints(
    services: Sequence[TTSServiceEndpoint],
) -> tuple[tuple[str, str, str], ...]:
    fingerprints: list[tuple[str, str, str]] = []
    for endpoint in services:
        locator = _managed_portable_locator(endpoint)
        if locator is None:
            continue
        fingerprints.append(
            (
                locator.component,
                endpoint.service_id,
                json.dumps(locator.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
            )
        )
    return tuple(sorted(fingerprints))


def _managed_portable_locator_fingerprints_by_component(
    services: Sequence[TTSServiceEndpoint],
) -> dict[str, tuple[tuple[str, str, str], ...]]:
    fingerprints = _managed_portable_locator_fingerprints(services)
    return {
        component: tuple(item for item in fingerprints if item[0] == component)
        for component in _PORTABLE_COMPONENTS
    }


def _managed_portable_locator(
    endpoint: TTSServiceEndpoint,
) -> PortableServiceLocator | None:
    locator = endpoint.portable_locator
    if (
        endpoint.control_kind != "portable-package"
        or locator is None
        or locator.component not in _PORTABLE_COMPONENTS
        or endpoint.mode != "local"
        or endpoint.network_scope != "localhost"
        or endpoint.api_contract != "tts-more-v1"
    ):
        return None
    return locator
