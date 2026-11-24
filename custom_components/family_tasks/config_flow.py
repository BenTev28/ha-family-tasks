"""Config, options, reauth and reconfigure flows.

Shape (same for every service):
  user (pick service) -> connect (backend-specific fields) -> lists -> entry
Options (assignees, lists, polling, completed visibility) are service-neutral.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
import voluptuous as vol

from .backends import BACKENDS, CONF_BACKEND, get_spec
from .backends.base import BackendError, BackendSpec, ListInfo
from .const import (
    ATTR_COLOR,
    ATTR_PERSON,
    ATTR_TAG,
    CONF_ASSIGNEES,
    CONF_COMPLETED_DAYS,
    CONF_LISTS,
    CONF_SCAN_INTERVAL,
    DEFAULT_COLORS,
    DEFAULT_COMPLETED_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)


def default_assignees(hass: HomeAssistant) -> list[dict[str, str]]:
    """Every person entity, tagged with the first word of its name."""
    result = []
    for idx, state in enumerate(sorted(hass.states.async_all("person"), key=lambda s: s.entity_id)):
        name = (state.name or state.entity_id.split(".", 1)[1]).split()[0]
        result.append(
            {ATTR_PERSON: state.entity_id, ATTR_TAG: name, ATTR_COLOR: DEFAULT_COLORS[idx % len(DEFAULT_COLORS)]}
        )
    return result


def default_lists(lists: list[ListInfo]) -> list[str]:
    """Preselect the only list, or lists whose name mentions 'family'."""
    if len(lists) == 1:
        return [lists[0].id]
    return [item.id for item in lists if "family" in item.name.lower()]


def _list_selector(lists: list[ListInfo]) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[selector.SelectOptionDict(value=item.id, label=item.name) for item in lists],
            multiple=True,
            mode=selector.SelectSelectorMode.LIST,
        )
    )


def _without_secrets(data: Mapping[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (data or {}).items() if "password" not in k and "secret" not in k and "token" not in k}


class FamilyTasksConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._spec: BackendSpec | None = None
        self._data: dict[str, Any] = {}
        self._lists: list[ListInfo] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if len(BACKENDS) == 1:
            self._spec = next(iter(BACKENDS.values()))
            return await self._async_step_connect(None)
        if user_input is not None:
            self._spec = get_spec(user_input[CONF_BACKEND])
            return await self._async_step_connect(None)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_BACKEND): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[selector.SelectOptionDict(value=k, label=s.name) for k, s in BACKENDS.items()],
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    # one entry point per backend so each has its own form texts (strings.json)
    async def async_step_connect_caldav(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_step_connect(user_input)

    async def _async_step_connect(self, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        assert self._spec is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**user_input, CONF_BACKEND: self._spec.key}
            errors, lists = await self._spec.async_validate(self.hass, data)
            if not errors:
                await self.async_set_unique_id(self._spec.unique_id(data))
                self._abort_if_unique_id_configured()
                self._data, self._lists = data, lists
                return await self.async_step_lists()
        return self.async_show_form(
            step_id=f"connect_{self._spec.key}",
            data_schema=self.add_suggested_values_to_schema(
                self._spec.credentials_schema({}), _without_secrets(user_input)
            ),
            errors=errors,
            description_placeholders={"service": self._spec.name, **self._spec.placeholders},
        )

    async def async_step_lists(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._spec is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            chosen = user_input[CONF_LISTS]
            if not chosen:
                errors[CONF_LISTS] = "no_lists_selected"
            else:
                names = {item.id: item.name for item in self._lists}
                return self.async_create_entry(
                    title=self._spec.title(self._data),
                    data=self._data,
                    options={
                        CONF_LISTS: {list_id: names[list_id] for list_id in chosen},
                        CONF_ASSIGNEES: default_assignees(self.hass),
                        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                        CONF_COMPLETED_DAYS: DEFAULT_COMPLETED_DAYS,
                    },
                )
        return self.async_show_form(
            step_id="lists",
            data_schema=vol.Schema(
                {vol.Required(CONF_LISTS, default=default_lists(self._lists)): _list_selector(self._lists)}
            ),
            errors=errors,
        )

    # -- reauth / reconfigure (entry_id, and so entity IDs, stay the same) --
    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reconfigure()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_reconfigure(user_input)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry() if self.source == "reauth" else self._get_reconfigure_entry()
        spec = get_spec(entry.data[CONF_BACKEND])
        errors: dict[str, str] = {}
        if user_input is not None:
            # empty secret fields keep the stored value (rotate only what was typed)
            data = {**entry.data, **{k: v for k, v in user_input.items() if v not in ("", None)}}
            errors, _ = await spec.async_validate(self.hass, data)
            if not errors:
                await self.async_set_unique_id(spec.unique_id(data))
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                spec.credentials_schema({}, reconfigure=True), _without_secrets(entry.data)
            ),
            errors=errors,
            description_placeholders={"service": spec.name, **spec.placeholders},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> FamilyTasksOptionsFlow:
        return FamilyTasksOptionsFlow()


class FamilyTasksOptionsFlow(OptionsFlow):
    """Assignee mapping, lists, polling, completed-task visibility."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        options = self.config_entry.options
        current_lists: dict[str, str] = options.get(CONF_LISTS, {})
        spec = get_spec(self.config_entry.data[CONF_BACKEND])
        try:
            lists = await spec.create(self.hass, self.config_entry.data).async_list_lists()
        except BackendError:
            lists = [ListInfo(list_id, name) for list_id, name in current_lists.items()]
            errors["base"] = "cannot_connect_lists"

        if user_input is not None:
            assignees = [
                {
                    ATTR_PERSON: a[ATTR_PERSON],
                    ATTR_TAG: str(a[ATTR_TAG]).strip(),
                    **({ATTR_COLOR: str(a[ATTR_COLOR]).strip()} if a.get(ATTR_COLOR) else {}),
                }
                for a in user_input.get(CONF_ASSIGNEES, [])
                if a.get(ATTR_PERSON) and str(a.get(ATTR_TAG, "")).strip()
            ]
            tags = [a[ATTR_TAG].lower() for a in assignees]
            persons = [a[ATTR_PERSON] for a in assignees]
            errors.pop("base", None)  # "cannot_connect_lists" is only a warning
            if len(set(tags)) != len(tags) or len(set(persons)) != len(persons):
                errors[CONF_ASSIGNEES] = "duplicate_assignee"
            elif any("," in a[ATTR_TAG] for a in assignees):
                errors[CONF_ASSIGNEES] = "invalid_tag"
            if not user_input.get(CONF_LISTS):
                errors[CONF_LISTS] = "no_lists_selected"
            if not errors:
                names = {item.id: item.name for item in lists} | current_lists
                return self.async_create_entry(
                    data={
                        CONF_LISTS: {list_id: names.get(list_id, list_id) for list_id in user_input[CONF_LISTS]},
                        CONF_ASSIGNEES: assignees,
                        CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                        CONF_COMPLETED_DAYS: int(user_input[CONF_COMPLETED_DAYS]),
                    }
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_LISTS, default=list(current_lists)): _list_selector(lists),
                vol.Optional(
                    CONF_ASSIGNEES, default=options.get(CONF_ASSIGNEES) or default_assignees(self.hass)
                ): selector.ObjectSelector(
                    selector.ObjectSelectorConfig(
                        multiple=True,
                        label_field=ATTR_TAG,
                        description_field=ATTR_PERSON,
                        translation_key="assignee",
                        fields={
                            ATTR_PERSON: selector.ObjectSelectorField(
                                selector={"entity": {"domain": "person"}}, required=True
                            ),
                            ATTR_TAG: selector.ObjectSelectorField(selector={"text": {}}, required=True),
                            ATTR_COLOR: selector.ObjectSelectorField(selector={"text": {}}, required=False),
                        },
                    )
                ),
                vol.Required(
                    CONF_SCAN_INTERVAL, default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=3600,
                        step=1,
                        unit_of_measurement="s",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_COMPLETED_DAYS, default=options.get(CONF_COMPLETED_DAYS, DEFAULT_COMPLETED_DAYS)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=365, step=1, unit_of_measurement="d", mode=selector.NumberSelectorMode.BOX
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
