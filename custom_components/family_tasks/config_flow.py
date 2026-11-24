"""Config, options, reauth and reconfigure flows for family_tasks."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any
from urllib.parse import urlsplit

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .client import CalDavAuthError, CalDavClient, CalDavConnectionError, CalDavError, Collection
from .const import (
    ATTR_COLOR,
    ATTR_PERSON,
    ATTR_TAG,
    CONF_ASSIGNEES,
    CONF_COMPLETED_DAYS,
    CONF_LISTS,
    CONF_SCAN_INTERVAL,
    CONF_VERIFY_SSL,
    DEFAULT_COLORS,
    DEFAULT_COMPLETED_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_URL = "https://cloud.tevelow.com/remote.php/dav"
DEFAULT_LIST_NAME = "family to-do"


class InvalidUrl(Exception):
    """URL is not an https URL without credentials."""


def validate_url(url: str) -> str:
    """Require https, a host, and no embedded credentials."""
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise InvalidUrl
    if parts.username or parts.password or "@" in parts.netloc:
        raise InvalidUrl
    if parts.query or parts.fragment:
        raise InvalidUrl
    return url.rstrip("/")


async def _discover(hass: HomeAssistant, data: Mapping[str, Any]) -> list[Collection]:
    client = CalDavClient(
        async_get_clientsession(hass, verify_ssl=data.get(CONF_VERIFY_SSL, True)),
        data[CONF_URL],
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        data.get(CONF_VERIFY_SSL, True),
    )
    return await client.discover()


async def _validate(hass: HomeAssistant, data: dict[str, Any]) -> tuple[dict[str, str], list[Collection]]:
    """Return (errors, collections). Never logs the URL or credentials."""
    errors: dict[str, str] = {}
    try:
        data[CONF_URL] = validate_url(data[CONF_URL])
    except InvalidUrl:
        return {CONF_URL: "invalid_url"}, []
    try:
        collections = await _discover(hass, data)
    except CalDavAuthError:
        errors["base"] = "invalid_auth"
    except CalDavConnectionError:
        errors["base"] = "cannot_connect"
    except CalDavError as err:
        _LOGGER.debug("discovery failed: %s", err)
        errors["base"] = "not_caldav"
    else:
        if not collections:
            errors["base"] = "no_task_lists"
        return errors, collections
    return errors, []


def default_assignees(hass: HomeAssistant) -> list[dict[str, str]]:
    """Person entity -> first word of its friendly name (Hannah, Rachel, Owen, Micah)."""
    result = []
    for idx, state in enumerate(sorted(hass.states.async_all("person"), key=lambda s: s.entity_id)):
        name = (state.name or state.entity_id.split(".", 1)[1]).split()[0]
        result.append(
            {ATTR_PERSON: state.entity_id, ATTR_TAG: name, ATTR_COLOR: DEFAULT_COLORS[idx % len(DEFAULT_COLORS)]}
        )
    return result


def _list_selector(collections: list[Collection]) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[selector.SelectOptionDict(value=c.path, label=c.name) for c in collections],
            multiple=True,
            mode=selector.SelectSelectorMode.LIST,
        )
    )


def _credentials_schema(defaults: Mapping[str, Any], *, password_required: bool = True) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_URL, default=defaults.get(CONF_URL, DEFAULT_URL)): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
            ),
            vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "household")): str,
            (vol.Required(CONF_PASSWORD) if password_required else vol.Optional(CONF_PASSWORD)): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Required(CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, True)): bool,
        }
    )


class FamilyTasksConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._collections: list[Collection] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = dict(user_input)
            errors, collections = await _validate(self.hass, data)
            if not errors:
                host = urlsplit(data[CONF_URL]).hostname
                await self.async_set_unique_id(f"{data[CONF_USERNAME].lower()}@{host}")
                self._abort_if_unique_id_configured()
                self._data = data
                self._collections = collections
                return await self.async_step_lists()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _credentials_schema({}), {k: v for k, v in (user_input or {}).items() if k != CONF_PASSWORD}
            ),
            errors=errors,
            description_placeholders={"example_url": "https://cloud.example.com/remote.php/dav"},
        )

    async def async_step_lists(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            chosen = user_input[CONF_LISTS]
            if not chosen:
                errors[CONF_LISTS] = "no_lists_selected"
            else:
                names = {c.path: c.name for c in self._collections}
                return self.async_create_entry(
                    title=f"{self._data[CONF_USERNAME]} @ {urlsplit(self._data[CONF_URL]).hostname}",
                    data=self._data,
                    options={
                        CONF_LISTS: {p: names[p] for p in chosen},
                        CONF_ASSIGNEES: default_assignees(self.hass),
                        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                        CONF_COMPLETED_DAYS: DEFAULT_COMPLETED_DAYS,
                    },
                )
        default = [c.path for c in self._collections if c.name.strip().lower() == DEFAULT_LIST_NAME]
        return self.async_show_form(
            step_id="lists",
            data_schema=vol.Schema({vol.Required(CONF_LISTS, default=default): _list_selector(self._collections)}),
            errors=errors,
        )

    # -- reauth / reconfigure (entry_id, and so entity IDs, stay the same) --
    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            errors, _ = await _validate(self.hass, data)
            if not errors:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    )
                }
            ),
            description_placeholders={CONF_USERNAME: entry.data[CONF_USERNAME]},
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, **user_input}
            if not user_input.get(CONF_PASSWORD):
                data[CONF_PASSWORD] = entry.data[CONF_PASSWORD]
            errors, _ = await _validate(self.hass, data)
            if not errors:
                host = urlsplit(data[CONF_URL]).hostname
                await self.async_set_unique_id(f"{data[CONF_USERNAME].lower()}@{host}")
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_credentials_schema(entry.data, password_required=False),
            errors=errors,
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
        try:
            collections = await _discover(self.hass, self.config_entry.data)
        except CalDavError:
            collections = [Collection(p, n, None, None, None) for p, n in current_lists.items()]
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
            if len(set(tags)) != len(tags) or len(set(persons)) != len(persons):
                errors[CONF_ASSIGNEES] = "duplicate_assignee"
            elif any("," in a[ATTR_TAG] for a in assignees):
                errors[CONF_ASSIGNEES] = "invalid_tag"
            if not user_input.get(CONF_LISTS):
                errors[CONF_LISTS] = "no_lists_selected"
            # "cannot_connect_lists" is only a warning shown on the form
            errors.pop("base", None)
            if not errors:
                names = {c.path: c.name for c in collections} | current_lists
                return self.async_create_entry(
                    data={
                        CONF_LISTS: {p: names.get(p, p) for p in user_input[CONF_LISTS]},
                        CONF_ASSIGNEES: assignees,
                        CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                        CONF_COMPLETED_DAYS: int(user_input[CONF_COMPLETED_DAYS]),
                    }
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_LISTS, default=list(current_lists)): _list_selector(collections),
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
                        min=0,
                        max=365,
                        step=1,
                        unit_of_measurement="d",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
